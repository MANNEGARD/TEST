"""Adresser för bilens stopp (omvänd geokodning mot OpenStreetMap/Nominatim), och
positioner för platser som bara har en adress (adressuppslag, 1.3.0).

Flödet (byggkontraktet C.1 och E.1):

* ``place_for_position`` körs i detekteringen. Ligger stoppet vid en känd plats används
  den, annars skapas en ny obekräftad plats med status "väntar på adress". Inget nätverk.
* ``run_pending`` körs i ``tick``. Den läser väntande platser i en kort session, gör
  uppslagen utan öppen transaktion och skriver svaren i en ny kort session.
* ``reverse`` är den rena nätverksdelen. Platsens position skickas (6 decimaler), aldrig
  resa, tid eller id. Cachen nycklas fortfarande per kartcell (4 decimaler).
* ``run_forward`` (från ``run_pending``, efter de omvända uppslagen och inom samma
  dygnsgräns) slår upp positionen för platser som har adress men saknar position, t.ex.
  platser från en importerad körjournal – högst ``FORWARD_PER_TICK`` per användare och
  körning. Positionen märks ``coordinate_source="geocoded"`` (ungefärlig, ur adressen).
  Ägarens egna och bilens positioner skrivs aldrig över. Tvetydiga och ohittade adresser
  lämnas utan position och prövas inte om, förrän ägaren ändrar adressen.
* Bostaden får sin adress uppslagen automatiskt som övriga platser (bara adress och ort).

Skydd: adressen prövas vid varje anrop (bara https på port 443, DNS-svaret och motpartens
adress måste vara globala), inga omdirigeringar, total deadline 4 s, högst 64 kB svar och
bara JSON. Högst ett anrop per 1,1 s i processen och 500 per dygn. Samma cell slås aldrig
upp två gånger (``geocode_cache``). Inget skickas förrän hemmet har en koordinat.

Svaret är opålitlig indata: bara fälten i ``GeoAddress`` sparas, och all text tvättas.
Ingen adress hittas på – går uppslaget inte att göra står fältet tomt.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.db import session_scope, utcnow
from app.models import GeocodeCache, KnownLocation, LocationAlias, RawTrip, TripSegment
from app.models.enums import GeocodeStatus, LocationType, PlaceKind, TripSource
from app.services import audit
from app.services import settings as settings_svc
from app.services.places import registry as places
from app.services.places.normalize import address_key, haversine_m, valid_coordinates
from app.services.text_safety import sanitize_text

logger = logging.getLogger(__name__)

USER_AGENT = f"SMGD-DriveLog/{__version__} (self-hosted logbook; reverse geocoding)"

# Adress → position (1.3.0).
FORWARD_PER_TICK = 3  # högst så många adressuppslag per användare och körning
FORWARD_SAME_PLACE_M = 300  # två träffar närmare än så räknas som samma plats
FORWARD_MERGE_MIN_M = 150  # en uppslagen plats inom så här långt från bostaden/kontoret är samma plats
FORWARD_QUERY_LIMIT = 200
FORWARD_OK = "ok"
FORWARD_NOT_FOUND = "not_found"
FORWARD_AMBIGUOUS = "ambiguous"
FORWARD_FAILED = "failed"
COORDINATE_GEOCODED = "geocoded"

# Namnet på en plats som ännu saknar adress. Ingenting hittas på.
UNRESOLVED_NAME = "Okänd plats"
NEW_PLACE_RADIUS_M = 75

MIN_INTERVAL_S = 1.1  # högst ett anrop per 1,1 s i processen
DAILY_LIMIT = 500  # högst 500 anrop per dygn
DEADLINE_S = 4.0  # total tid för ett anrop
MAX_BODY_BYTES = 64 * 1024
MAX_ATTEMPTS = 5
RETRY_DELAYS = (timedelta(minutes=5), timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
# Därefter, om karttjänsten är nere länge: ett försök per dygn i 30 dygn.
SLOW_RETRIES = 30
SLOW_DELAY = timedelta(days=1)
# Misslyckade platser prövas igen så länge omförsöken pågår (5 min + 1 h + 6 h + 24 h +
# 30 dygn). Ett nytt besök på platsen ger ett nytt försök (place_for_position).
FAILED_RETRY_WINDOW = timedelta(days=35)

# Kartobjekt vars namn är en gata, ort eller ett område, inte ett företag eller ställe.
_NO_POI_CATEGORIES = {"highway", "place", "boundary", "landuse", "natural", "waterway", "railway", "route"}
_STREET_KEYS = ("road", "pedestrian", "footway", "cycleway", "path", "square", "place")
_CITY_KEYS = ("city", "town", "village", "hamlet", "municipality")
_OSM_TOKEN = re.compile(r"^[a-z0-9_:\-]{1,29}$")

# Platsens art ur kartans typ (byggkontraktet A.3).
_WORK_CATEGORIES = {"office", "craft", "industrial"}
_NEVER_CATEGORIES = {"shop", "amenity", "leisure", "tourism", "healthcare"}
_WORK_TYPES = {("man_made", "works"), ("landuse", "industrial")} | {
    ("building", t) for t in ("industrial", "warehouse", "office")
}
# Kontor och hantverk där man oftare uträttar privata ärenden (myndighet, advokat, mäklare,
# försäkring, samfund, vingård …): aldrig ett antagande på typen ensam, alltså "unclear".
_UNCLEAR_TYPES = {
    ("office", t) for t in (
        "government", "lawyer", "estate_agent", "insurance", "religion", "employment_agency",
        "diplomatic", "association", "educational_institution",
    )
} | {("craft", t) for t in ("winery", "brewery", "distillery")}
_NEVER_TYPES = {("landuse", "residential"), ("place", "house")} | {
    ("building", t) for t in ("residential", "house", "apartments", "detached", "terrace")
}
_SUGGESTED_TYPES = {
    ("amenity", "fuel"): LocationType.FUEL_STATION,
    ("amenity", "charging_station"): LocationType.CHARGING_STATION,
    ("tourism", "hotel"): LocationType.HOTEL,
    ("tourism", "motel"): LocationType.HOTEL,
}
_SUGGESTION_REASONS = {
    LocationType.FUEL_STATION: "Kartan (OpenStreetMap) visar en bensinstation här.",
    LocationType.CHARGING_STATION: "Kartan (OpenStreetMap) visar en laddstation här.",
    LocationType.HOTEL: "Kartan (OpenStreetMap) visar ett hotell här.",
}

# Transport som används när anroparen inte anger någon. Testerna sätter en "offline"-
# transport här (tests/conftest.py), så att ingen test kan nå nätet.
_default_transport: httpx.BaseTransport | None = None

# Takten: lås och monoton klocka. Utbytbara i tester.
_rate_lock = threading.Lock()
_last_call: float | None = None
_monotonic = time.monotonic
_sleep = time.sleep
_getaddrinfo = socket.getaddrinfo


class GeocodeError(Exception):
    """Uppslaget gick inte att göra (nätverk, skydd eller ogiltigt svar)."""


class DailyLimitReached(GeocodeError):
    """Dygnets gräns för anrop till karttjänsten är nådd."""


def set_default_transport(transport: httpx.BaseTransport | None) -> None:
    global _default_transport
    _default_transport = transport


def default_transport() -> httpx.BaseTransport | None:
    return _default_transport


@dataclass(frozen=True)
class GeoAddress:
    label: str  # "Storgatan 5, 553 20 Jönköping", alltid utan POI-namn
    street: str | None
    house_number: str | None
    postcode: str | None
    city: str | None
    poi_name: str | None
    osm_class: str | None
    place_kind: Literal["work", "never", "unclear"]


# --------------------------------------------------------------------------- detekteringen


def place_for_position(
    db: Session, *, user_id: uuid.UUID, lat: float | None, lon: float | None, demo: bool
) -> KnownLocation | None:
    """Känd plats för positionen, annars en ny obekräftad plats. Inget nätverk.

    Med läget ``manual`` och geokodningen av skapas ingen plats (som före 1.1.0).
    Demofordon får platser med status ``off`` och slås aldrig upp.
    """
    loc, _ = places.resolve_position(db, user_id, lat, lon, demo=demo)
    if loc is not None and loc.geocode_status == GeocodeStatus.FAILED and not demo and not loc.is_demo:
        _retry_on_visit(db, loc)
    if loc is not None or not valid_coordinates(lat, lon):
        return loc
    enabled = geocoding_enabled(db, user_id)
    if not enabled and settings_svc.automation_mode(db, user_id) == settings_svc.MODE_MANUAL:
        return None
    loc = KnownLocation(
        owner_user_id=user_id,
        name=UNRESOLVED_NAME,
        type=LocationType.UNKNOWN,
        confirmed=False,
        created_from="api",
        coordinate_source="api",
        latitude=lat,
        longitude=lon,
        radius_m=NEW_PLACE_RADIUS_M,
        is_demo=demo,
        geocode_status=GeocodeStatus.PENDING if (enabled and not demo) else GeocodeStatus.OFF,
    )
    db.add(loc)
    db.flush()
    return loc


def _retry_on_visit(db: Session, loc: KnownLocation) -> None:
    """Ett nytt besök på en plats vars adress inte gick att slå upp: platsen prövas igen
    vid nästa körning, även om de vanliga omförsöken tagit slut."""
    loc.geocode_status = GeocodeStatus.PENDING
    if loc.latitude is None or loc.longitude is None:
        return
    for row in db.execute(
        select(GeocodeCache).where(
            GeocodeCache.owner_user_id == loc.owner_user_id,
            GeocodeCache.lat_key == cell_key(loc.latitude),
            GeocodeCache.lon_key == cell_key(loc.longitude),
            GeocodeCache.status == GeocodeStatus.FAILED,
        )
    ).scalars():
        row.attempts = min(int(row.attempts or 0), MAX_ATTEMPTS)
        row.next_attempt_at = None
    db.flush()


def address_for(loc: KnownLocation | None) -> str | None:
    """Adresstext för en resa. En plats som väntar på (eller saknar) adress ger None."""
    if loc is None:
        return None
    if loc.geocode_status is not None and not loc.address:
        return None
    return loc.address or loc.name


def geocoding_enabled(db: Session, user_id: uuid.UUID) -> bool:
    return bool(settings_svc.get_section(db, user_id, "privacy").get("geocoding_enabled"))


def cell_key(value: float) -> int:
    return round(value * 1e4)


def classify_place(osm_category: str | None, osm_type: str | None) -> str:
    """Platsens art ur kartans typ (tabellen i A.3): ``work``, ``never`` eller ``unclear``."""
    cat = (osm_category or "").strip().lower()
    typ = (osm_type or "").strip().lower()
    if (cat, typ) in _UNCLEAR_TYPES:
        return PlaceKind.UNCLEAR.value
    if cat in _WORK_CATEGORIES or (cat, typ) in _WORK_TYPES:
        return PlaceKind.WORK.value
    if cat in _NEVER_CATEGORIES or (cat, typ) in _NEVER_TYPES:
        return PlaceKind.NEVER.value
    return PlaceKind.UNCLEAR.value


# --------------------------------------------------------------------------- bakgrundsjobbet


@dataclass
class _Job:
    loc_id: uuid.UUID
    user_id: uuid.UUID
    provider_url: str
    host: str
    lat_key: int
    lon_key: int
    cached: tuple[str, dict[str, Any] | None] | None = None  # (status, result) från cachen
    # Platsens egen position (skickas med 6 decimaler; cachen nycklas per cell).
    lat: float | None = None
    lon: float | None = None
    home: bool = False  # bostaden: bara adress och ort sätts

    @property
    def cell(self) -> tuple[uuid.UUID, str, int, int]:
        return (self.user_id, self.host, self.lat_key, self.lon_key)


@dataclass
class _Plan:
    jobs: list[_Job] = field(default_factory=list)
    fetch: dict[tuple[uuid.UUID, str, int, int], _Job] = field(default_factory=dict)


def run_pending(now: datetime | None = None, limit: int = 5) -> dict[str, Any]:
    """Slår upp väntande platser (högst ``limit`` nätverksanrop).

    Egen sessionshantering: läser i en kort session, gör HTTP utan öppen transaktion och
    skriver i en ny kort session. Sist prövas berörda resor igen (``autopilot.reconsider``).
    """
    now = now or utcnow()
    out: dict[str, Any] = {
        "looked_up": 0, "from_cache": 0, "ok": 0, "not_found": 0, "failed": 0,
        "merged": 0, "waiting_for_home": 0, "off": 0,
    }
    with session_scope() as db:
        plan = _collect(db, now=now, limit=limit, out=out)
    if not plan.jobs:
        _forward_into(out, now)
        return _nonzero(out)

    # Nätverket: ingen databastransaktion är öppen här.
    results: dict[tuple[uuid.UUID, str, int, int], tuple[str, GeoAddress | None]] = {}
    for cell, job in plan.fetch.items():
        out["looked_up"] += 1
        try:
            addr = reverse(provider_url=job.provider_url, lat_key=job.lat_key, lon_key=job.lon_key,
                           lat=job.lat, lon=job.lon)
        except (GeocodeError, ValueError) as exc:
            logger.warning("Adressuppslaget misslyckades: %s", exc)
            results[cell] = (GeocodeStatus.FAILED, None)
            continue
        except Exception as exc:  # ett oväntat svar får inte fälla hela körningen
            logger.warning("Adressuppslaget misslyckades oväntat (%s).", type(exc).__name__)
            results[cell] = (GeocodeStatus.FAILED, None)
            continue
        results[cell] = (GeocodeStatus.OK, addr) if addr is not None else (GeocodeStatus.NOT_FOUND, None)

    with session_scope() as db:
        touched = _apply(db, plan, results, now=now, out=out)

    if touched:
        from app.services.classify import autopilot

        for user_id, trip_ids in touched.items():
            try:
                with session_scope() as db:
                    autopilot.reconsider(db, user_id=user_id, trip_ids=sorted(trip_ids), actor=audit.SYSTEM)
            except Exception:
                logger.exception("Omprövningen efter adressuppslaget misslyckades.")
    _forward_into(out, now)
    return _nonzero(out)


def _forward_into(out: dict[str, Any], now: datetime) -> None:
    """Adressuppslagen (adress → position) efter de omvända, inom samma dygnsgräns."""
    try:
        for key, value in run_forward(now=now).items():
            out[f"forward_{key}"] = out.get(f"forward_{key}", 0) + value
    except Exception:  # adressuppslagen får aldrig stoppa resten av körningen
        logger.exception("Adressuppslagen (adress → position) misslyckades.")


def _nonzero(out: dict[str, Any]) -> dict[str, Any]:
    """Bara räknare som inte är noll; inget att göra ger ``{}``."""
    return {k: v for k, v in out.items() if v}


def _collect(db: Session, *, now: datetime, limit: int, out: dict[str, Any]) -> _Plan:
    plan = _Plan()
    from_vehicle = (KnownLocation.created_from == "api") & (
        (KnownLocation.geocode_status == GeocodeStatus.PENDING)
        | (
            (KnownLocation.geocode_status == GeocodeStatus.FAILED)
            & (KnownLocation.created_at >= now - FAILED_RETRY_WINDOW)
        )
    )
    # Bostaden slås upp automatiskt som övriga platser (precision före integritet, 1.3.0):
    # en bekräftad bostad med position men utan adress, oavsett hur den skapades.
    home_without_address = (
        (KnownLocation.type == LocationType.HOME)
        & KnownLocation.confirmed.is_(True)
        & (KnownLocation.address.is_(None) | (KnownLocation.address == ""))
        & (
            KnownLocation.geocode_status.is_(None)
            | KnownLocation.geocode_status.in_((GeocodeStatus.PENDING, GeocodeStatus.FAILED))
        )
    )
    rows = list(
        db.execute(
            select(KnownLocation)
            .where(
                KnownLocation.is_demo.is_(False),
                KnownLocation.merged_into_id.is_(None),
                KnownLocation.latitude.is_not(None),
                KnownLocation.longitude.is_not(None),
                from_vehicle | home_without_address,
            )
            .order_by(KnownLocation.created_at)
            .limit(max(limit, 1) * 20)
        ).scalars()
    )
    if not rows:
        return plan
    budget = min(limit, DAILY_LIMIT - _calls_last_day(db, now))
    users: dict[uuid.UUID, tuple[str, str] | None] = {}
    for loc in rows:
        if loc.owner_user_id not in users:
            users[loc.owner_user_id] = _user_provider(db, loc.owner_user_id)
        provider = users[loc.owner_user_id]
        home = loc.type == LocationType.HOME and bool(loc.confirmed) and not (loc.address or "").strip()
        if provider is None:
            if home:
                continue  # bostaden väntar tills uppslag är tillåtna (dess läge ändras inte)
            if not geocoding_enabled(db, loc.owner_user_id):
                loc.geocode_status = GeocodeStatus.OFF
                out["off"] += 1
            else:
                out["waiting_for_home"] += 1
            continue
        url, host = provider
        job = _Job(loc.id, loc.owner_user_id, url, host, cell_key(loc.latitude), cell_key(loc.longitude),  # type: ignore[arg-type]
                   lat=loc.latitude, lon=loc.longitude, home=home)
        cache = _cache_row(db, job.user_id, host, job.lat_key, job.lon_key)
        if cache is not None and cache.status in (GeocodeStatus.OK, GeocodeStatus.NOT_FOUND):
            job.cached = (cache.status, cache.result if isinstance(cache.result, dict) else None)
            plan.jobs.append(job)
            continue
        if cache is not None and cache.status == GeocodeStatus.FAILED:
            due = cache.next_attempt_at is None or cache.next_attempt_at <= now
            gave_up = cache.attempts >= MAX_ATTEMPTS + SLOW_RETRIES and cache.next_attempt_at is None
            if gave_up or not due:
                loc.geocode_status = GeocodeStatus.FAILED
                continue
        if job.cell in plan.fetch:
            plan.jobs.append(job)
            continue
        if budget <= 0:
            continue
        budget -= 1
        plan.fetch[job.cell] = job
        plan.jobs.append(job)
    return plan


def _user_provider(db: Session, user_id: uuid.UUID) -> tuple[str, str] | None:
    """(adress, värdnamn) för användarens geokodare, eller None om inget får skickas."""
    if not geocoding_enabled(db, user_id):
        return None
    if not _home_has_coordinate(db, user_id):
        return None  # inget skickas förrän hemmet har koordinat
    raw = settings_svc.get_section(db, user_id, "privacy").get("geocoder_url") or ""
    try:
        url = settings_svc.validate_geocoder_url(raw)
    except ValueError as exc:
        logger.warning("Geokodarens adress godtas inte: %s", exc)
        return None
    return url, (urlsplit(url).hostname or "").lower()


def _home_has_coordinate(db: Session, user_id: uuid.UUID) -> bool:
    """Finns ägarens riktiga, bekräftade hem med koordinat? Demobilens påhittade hem räknas inte."""
    return (
        db.execute(
            select(KnownLocation.id).where(
                KnownLocation.owner_user_id == user_id,
                KnownLocation.type == LocationType.HOME,
                KnownLocation.is_demo.is_(False),
                KnownLocation.confirmed.is_(True),
                KnownLocation.merged_into_id.is_(None),
                KnownLocation.latitude.is_not(None),
                KnownLocation.longitude.is_not(None),
            ).limit(1)
        ).first()
        is not None
    )


def lookup_for_place(
    db: Session, user_id: uuid.UUID, provider_url: str, lat: float, lon: float, *, now: datetime | None = None
) -> GeoAddress | None:
    """Ägarens tryck på "Hämta adressen från kartan": samma skydd som uppslagen i bakgrunden.

    Svaret sparas per ruta (ett nytt tryck besvaras ur cachen) och anropet räknas mot
    ``DAILY_LIMIT``; är gränsen nådd kastas ``DailyLimitReached`` utan att något skickas.
    """
    now = now or utcnow()
    job = _Job(uuid.uuid4(), user_id, provider_url, (urlsplit(provider_url).hostname or "").lower(),
               cell_key(lat), cell_key(lon), lat=lat, lon=lon)
    cache = _cache_row(db, user_id, job.host, job.lat_key, job.lon_key)
    if cache is not None and cache.status == GeocodeStatus.NOT_FOUND:
        return None
    if cache is not None and cache.status == GeocodeStatus.OK:
        found = _from_cache(cache.result if isinstance(cache.result, dict) else None)
        if found is not None:
            return found
    if _calls_last_day(db, now) >= DAILY_LIMIT:
        raise DailyLimitReached("Dygnets gräns för adressuppslag är nådd.")
    try:
        found = reverse(provider_url=provider_url, lat_key=job.lat_key, lon_key=job.lon_key, lat=lat, lon=lon)
    except (GeocodeError, ValueError):
        _store_result(db, job, GeocodeStatus.FAILED, None, now)
        raise
    _store_result(db, job, GeocodeStatus.OK if found is not None else GeocodeStatus.NOT_FOUND, found, now)
    return found


def _calls_last_day(db: Session, now: datetime) -> int:
    """Anrop till karttjänsten det senaste dygnet: omvända uppslag (en cachepost per ruta) och
    adressuppslag (platsens senaste försök, ``forward_at``). Ett adressuppslag som prövas om
    flera gånger samma dygn räknas en gång – gränsen är en artighetsgräns, inget exakt tak."""
    since = now - timedelta(days=1)
    reverse_calls = db.execute(
        select(func.count()).select_from(GeocodeCache).where(GeocodeCache.fetched_at >= since)
    ).scalar_one()
    forward_calls = db.execute(
        select(func.count()).select_from(KnownLocation).where(KnownLocation.forward_at >= since)
    ).scalar_one()
    return int(reverse_calls or 0) + int(forward_calls or 0)


def _cache_row(db: Session, user_id: uuid.UUID, host: str, lat_key: int, lon_key: int) -> GeocodeCache | None:
    return db.execute(
        select(GeocodeCache).where(
            GeocodeCache.owner_user_id == user_id,
            GeocodeCache.provider_host == host,
            GeocodeCache.lat_key == lat_key,
            GeocodeCache.lon_key == lon_key,
        )
    ).scalar_one_or_none()


def _store_result(
    db: Session, job: _Job, status: str, addr: GeoAddress | None, now: datetime
) -> None:
    row = _cache_row(db, job.user_id, job.host, job.lat_key, job.lon_key)
    if row is None:
        row = GeocodeCache(
            owner_user_id=job.user_id, provider_host=job.host, lat_key=job.lat_key, lon_key=job.lon_key, attempts=0
        )
        db.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.status = status
    row.result = asdict(addr) if addr is not None else None
    row.fetched_at = now
    if status == GeocodeStatus.FAILED and row.attempts < MAX_ATTEMPTS:
        row.next_attempt_at = now + RETRY_DELAYS[min(row.attempts, len(RETRY_DELAYS)) - 1]
    elif status == GeocodeStatus.FAILED and row.attempts < MAX_ATTEMPTS + SLOW_RETRIES:
        row.next_attempt_at = now + SLOW_DELAY  # karttjänsten nere länge: ett försök per dygn
    else:
        row.next_attempt_at = None
    db.flush()


def _apply(
    db: Session,
    plan: _Plan,
    results: dict[tuple[uuid.UUID, str, int, int], tuple[str, GeoAddress | None]],
    *,
    now: datetime,
    out: dict[str, Any],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    for cell, job in plan.fetch.items():
        status, addr = results.get(cell, (GeocodeStatus.FAILED, None))
        _store_result(db, job, status, addr, now)

    touched: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for job in plan.jobs:
        loc = db.get(KnownLocation, job.loc_id)
        if loc is None or loc.merged_into_id is not None:
            continue
        if job.home:
            _apply_home(db, loc, job, results, now=now, out=out)
            continue
        if loc.geocode_status not in (GeocodeStatus.PENDING, GeocodeStatus.FAILED):
            continue
        if job.cached is not None:
            out["from_cache"] += 1
            status, result = job.cached
            addr = _from_cache(result) if status == GeocodeStatus.OK else None
            if status == GeocodeStatus.OK and addr is None:
                status = GeocodeStatus.NOT_FOUND
        else:
            status, addr = results.get(job.cell, (GeocodeStatus.FAILED, None))
        before = loc.geocode_status
        if status == GeocodeStatus.OK and addr is not None:
            out["ok"] += 1
            target = _apply_address(db, loc, addr, now, out)
            touched[loc.owner_user_id].update(_fill_trips(db, target))
            continue
        if status == GeocodeStatus.NOT_FOUND:
            out["not_found"] += 1
            loc.geocode_status = GeocodeStatus.NOT_FOUND
            loc.geocoded_at = now
        else:
            out["failed"] += 1
            loc.geocode_status = GeocodeStatus.FAILED
        db.flush()
        if before != loc.geocode_status:
            touched[loc.owner_user_id].update(_trip_ids_for_location(db, loc.id))
    return {uid: ids for uid, ids in touched.items() if ids}


def _apply_home(
    db: Session,
    loc: KnownLocation,
    job: _Job,
    results: dict[tuple[uuid.UUID, str, int, int], tuple[str, GeoAddress | None]],
    *,
    now: datetime,
    out: dict[str, Any],
) -> None:
    """Bostaden: bara adress, ort och uppslagets läge sätts – aldrig namn, kartans typ,
    förslag eller sammanslagning med en annan plats. Adressalias läggs till som vanligt."""
    if loc.type != LocationType.HOME or (loc.address or "").strip():
        return  # ägaren hann skriva adressen (eller ändra platsen) under tiden
    if job.cached is not None:
        out["from_cache"] += 1
        status, result = job.cached
        addr = _from_cache(result) if status == GeocodeStatus.OK else None
        if status == GeocodeStatus.OK and addr is None:
            status = GeocodeStatus.NOT_FOUND
    else:
        status, addr = results.get(job.cell, (GeocodeStatus.FAILED, None))
    if status == GeocodeStatus.OK and addr is not None:
        out["ok"] += 1
        loc.address, loc.city = addr.label, addr.city
        loc.geocode_status, loc.geocoded_at = GeocodeStatus.OK, now
        if addr.house_number:
            _add_aliases(db, loc, addr)
    elif status == GeocodeStatus.NOT_FOUND:
        out["not_found"] += 1
        loc.geocode_status, loc.geocoded_at = GeocodeStatus.NOT_FOUND, now
    else:
        out["failed"] += 1
        loc.geocode_status = GeocodeStatus.FAILED
    db.flush()


def _add_aliases(db: Session, loc: KnownLocation, addr: GeoAddress) -> None:
    for text in _address_variants(addr):
        key = address_key(text)
        exists = db.execute(
            select(LocationAlias.id).where(LocationAlias.owner_user_id == loc.owner_user_id, LocationAlias.address_key == key)
        ).first()
        if key and exists is None:
            db.add(LocationAlias(owner_user_id=loc.owner_user_id, location_id=loc.id, address_key=key, raw_address=text))


def _from_cache(result: dict[str, Any] | None) -> GeoAddress | None:
    if not isinstance(result, dict):
        return None
    try:
        addr = GeoAddress(**{k: result.get(k) for k in GeoAddress.__dataclass_fields__})
    except TypeError:
        return None
    if not addr.label or addr.place_kind not in {k.value for k in PlaceKind}:
        return None
    return addr


def _apply_address(db: Session, loc: KnownLocation, addr: GeoAddress, now: datetime, out: dict[str, Any]) -> KnownLocation:
    """Sparar adressen på platsen, eller slår ihop med en befintlig plats med samma adress.

    Rör aldrig ``type`` eller ``confirmed``: kartans svar är bara ett förslag.
    """
    loc.geocode_status = GeocodeStatus.OK
    loc.geocoded_at = now
    loc.osm_class = addr.osm_class
    loc.place_kind = addr.place_kind
    target = _existing_for(db, loc, addr)
    if target is not None:
        if target.place_kind is None:
            target.osm_class, target.place_kind = addr.osm_class, addr.place_kind
        places.learn_coordinates(db, target, loc.latitude, loc.longitude, "api")
        places.merge_locations(db, target, loc)
        db.flush()
        db.expire_all()
        out["merged"] += 1
        return db.get(KnownLocation, target.id)  # type: ignore[return-value]
    loc.name = addr.poi_name or addr.label
    loc.address = addr.label
    loc.city = addr.city
    if addr.place_kind == PlaceKind.WORK and addr.poi_name and not loc.company:
        loc.company = addr.poi_name
    suggested = _SUGGESTED_TYPES.get(_split_class(addr.osm_class))
    if suggested is not None and loc.suggested_type is None:
        loc.suggested_type = suggested
        loc.suggestion_reason = _SUGGESTION_REASONS[suggested]
    if addr.house_number:
        # Bara exakta gatuadresser blir alias; en gata utan nummer kan vara lång.
        _add_aliases(db, loc, addr)
    db.flush()
    return loc


def _existing_for(db: Session, loc: KnownLocation, addr: GeoAddress) -> KnownLocation | None:
    """Befintlig plats (t.ex. från importen) med samma gatuadress."""
    if not addr.house_number:
        return None
    for text in _address_variants(addr):
        found = places.find_by_address(db, loc.owner_user_id, text)
        if found is not None and found.id != loc.id and bool(found.is_demo) == bool(loc.is_demo):
            return found
    return None


def _address_variants(addr: GeoAddress) -> list[str]:
    """Etiketten, och samma adress utan postnummer (så skriver importen ofta)."""
    out = [addr.label]
    if addr.street and addr.house_number and addr.city:
        short = f"{addr.street} {addr.house_number}, {addr.city}"
        if address_key(short) != address_key(addr.label):
            out.append(short)
    return out


def _split_class(osm_class: str | None) -> tuple[str, str]:
    if not osm_class or "=" not in osm_class:
        return ("", "")
    cat, typ = osm_class.split("=", 1)
    return (cat, typ)


_LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)


def _fill_trips(db: Session, loc: KnownLocation) -> set[uuid.UUID]:
    """Fyller tomma adresser på live-resor till platsen och räknar om journalresorna."""
    text = address_for(loc)
    raws = list(
        db.execute(
            select(RawTrip).where(
                RawTrip.source.in_(_LIVE_SOURCES),
                (RawTrip.start_location_id == loc.id) | (RawTrip.end_location_id == loc.id),
            )
        ).scalars()
    )
    for raw in raws:
        if raw.start_location_id == loc.id and not raw.start_address and text:
            raw.start_address = text
        if raw.end_location_id == loc.id and not raw.end_address and text:
            raw.end_address = text
    db.flush()
    return _refresh_for_raws(db, [r.id for r in raws])


def _trip_ids_for_location(db: Session, loc_id: uuid.UUID) -> set[uuid.UUID]:
    raw_ids = list(
        db.execute(
            select(RawTrip.id).where(
                RawTrip.source.in_(_LIVE_SOURCES),
                (RawTrip.start_location_id == loc_id) | (RawTrip.end_location_id == loc_id),
            )
        ).scalars()
    )
    if not raw_ids:
        return set()
    return set(db.execute(select(TripSegment.logical_trip_id).where(TripSegment.raw_trip_id.in_(raw_ids))).scalars())


def _refresh_for_raws(db: Session, raw_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    from app.services.trips import segments as seg

    if not raw_ids:
        return set()
    trip_ids = set(db.execute(select(TripSegment.logical_trip_id).where(TripSegment.raw_trip_id.in_(raw_ids))).scalars())
    for trip_id in trip_ids:
        trip = seg.load_trip(db, trip_id)
        if trip is not None:
            seg.refresh_logical(db, trip)
    db.flush()
    return trip_ids


# --------------------------------------------------------------------------- nätverket


def validate_runtime_url(url: str, *, resolve: bool | None = None) -> str:
    """Kontrollerar geokodarens adress före ett anrop och returnerar den; kastar ValueError.

    Först den statiska kontrollen (https, port 443, ingen användarinfo, frågesträng,
    fragment eller lokal IP). Med ``resolve`` prövas även varje DNS-svar: alla adresser
    måste vara globala och får inte vara multicast. Standard (None): DNS prövas när den
    riktiga transporten används, alltså inte när testerna har bytt ut den.
    """
    base = settings_svc.validate_geocoder_url(url)
    if resolve is None:
        resolve = default_transport() is None
    if not resolve:
        return base
    host = urlsplit(base).hostname or ""
    try:
        infos = _getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Geokodarens namn gick inte att slå upp: {host}") from exc
    if not infos:
        raise ValueError(f"Geokodarens namn gav inga adresser: {host}")
    for info in infos:
        _check_ip(str(info[4][0]))
    return base


def _check_ip(value: str) -> None:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError("Geokodarens adress är ogiltig.") from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise ValueError("Geokodaren pekar på en lokal eller reserverad adress.")


def _check_peer(response: httpx.Response, *, required: bool) -> None:
    """Motpartens adress efter anslutningen (stänger DNS-rebinding)."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        if required:
            raise GeocodeError("Motpartens adress gick inte att kontrollera.")
        return
    addr = stream.get_extra_info("server_addr")
    if not addr:
        raise GeocodeError("Motpartens adress gick inte att kontrollera.")
    try:
        _check_ip(str(addr[0]))
    except ValueError as exc:
        raise GeocodeError(str(exc)) from exc


def _throttle() -> None:
    global _last_call
    with _rate_lock:
        now = _monotonic()
        if _last_call is not None:
            wait = _last_call + MIN_INTERVAL_S - now
            if wait > 0:
                _sleep(wait)
                now = _monotonic()
        _last_call = now


def _real_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport(verify=True, trust_env=False, retries=0)


def reverse(
    *,
    provider_url: str,
    lat_key: int,
    lon_key: int,
    lat: float | None = None,
    lon: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> GeoAddress | None:
    """Själva nätverksanropet. Ger None när kartan inte har någon adress, och kastar
    GeocodeError (eller ValueError för en otillåten adress) när uppslaget inte gick att göra.

    Frågan skickar platsens egen position (``lat``/``lon``, 6 decimaler) så att rätt hus
    hittas; utan den kartcellens mittpunkt (``lat_key``/``lon_key``, 4 decimaler). Cachen
    nycklas alltid per cell.

    En utbytt transport (tester) hoppar över DNS-kontrollen, eftersom inget riktigt namn
    slås upp, men motpartens adress kontrolleras om transporten anger den.
    """
    exact = lat is not None and lon is not None and valid_coordinates(lat, lon)
    params = {
        "lat": f"{lat:.6f}" if exact else f"{lat_key / 1e4:.4f}",
        "lon": f"{lon:.6f}" if exact else f"{lon_key / 1e4:.4f}",
        "format": "jsonv2",
        "zoom": "18",
        "addressdetails": "1",
        "accept-language": "sv",
    }
    data = _get_json(provider_url, "reverse", params, transport)
    if not isinstance(data, dict):
        raise GeocodeError("Geokodarens svar hade fel form.")
    return parse_response(data)


@dataclass(frozen=True)
class GeoHit:
    """En träff från adressuppslaget (adress → position)."""

    lat: float
    lon: float
    osm_class: str | None
    place_kind: Literal["work", "never", "unclear"]


def search(*, provider_url: str, query: str, transport: httpx.BaseTransport | None = None) -> list[GeoHit]:
    """Adressuppslaget: adress → högst två träffar. Samma skydd som ``reverse``.

    Bara adresstexten skickas (tvättad, högst 200 tecken), aldrig resa, tid eller id.
    Kastar GeocodeError när uppslaget inte gick att göra.
    """
    q = sanitize_text(query, FORWARD_QUERY_LIMIT)
    if not q:
        return []
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": "2",
        "addressdetails": "1",
        "countrycodes": "se",
        "accept-language": "sv",
    }
    data = _get_json(provider_url, "search", params, transport)
    if not isinstance(data, list):
        raise GeocodeError("Geokodarens svar hade fel form.")
    return parse_search(data)


def parse_search(data: list[Any]) -> list[GeoHit]:
    """Tolkar Nominatims jsonv2-svar på en sökning. Bara position och kartans typ sparas."""
    hits: list[GeoHit] = []
    for item in data[:2]:
        if not isinstance(item, dict):
            continue
        try:
            lat, lon = float(item.get("lat")), float(item.get("lon"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not valid_coordinates(lat, lon):
            continue
        category, typ = _token(item.get("category")), _token(item.get("type"))
        hits.append(GeoHit(
            lat=round(lat, 6), lon=round(lon, 6),
            osm_class=f"{category}={typ}" if category and typ else None,
            place_kind=classify_place(category, typ),  # type: ignore[arg-type]
        ))
    return hits


def _get_json(
    provider_url: str, path: str, params: dict[str, str], transport: httpx.BaseTransport | None
) -> Any:
    """GET mot geokodaren med alla skydd: adressen prövas, inga omdirigeringar, deadline,
    storleksgräns, bara okomprimerad JSON och strypning (högst ett anrop per 1,1 s)."""
    injected = transport or default_transport()
    base = validate_runtime_url(provider_url, resolve=injected is None)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"}
    _throttle()
    deadline = _monotonic() + DEADLINE_S
    body = bytearray()
    try:
        with httpx.Client(
            transport=injected or _real_transport(),
            trust_env=False,
            verify=True,
            follow_redirects=False,
            timeout=httpx.Timeout(DEADLINE_S),
            headers=headers,
        ) as client:
            with client.stream("GET", f"{base}/{path}", params=params) as resp:
                _check_peer(resp, required=injected is None)
                if _monotonic() > deadline:
                    raise GeocodeError("Geokodaren svarade inte i tid.")
                if resp.status_code != 200:
                    raise GeocodeError(f"Geokodaren svarade {resp.status_code}.")
                encoding = resp.headers.get("content-encoding", "").strip().lower()
                if encoding not in ("", "identity"):
                    raise GeocodeError("Geokodaren skickade ett packat svar.")
                if "json" not in resp.headers.get("content-type", "").lower():
                    raise GeocodeError("Geokodaren svarade inte med JSON.")
                for chunk in resp.iter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise GeocodeError("Geokodarens svar var för stort.")
                    if _monotonic() > deadline:
                        raise GeocodeError("Geokodaren svarade inte i tid.")
    except httpx.HTTPError as exc:
        raise GeocodeError(f"Geokodaren gick inte att nå ({type(exc).__name__}).") from exc
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise GeocodeError("Geokodarens svar var inte giltig JSON.") from exc


def _text(value: Any, limit: int) -> str | None:
    return sanitize_text(value, limit) if isinstance(value, str) else None


def _token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if _OSM_TOKEN.match(v) else None


def parse_response(data: dict[str, Any]) -> GeoAddress | None:
    """Tolkar Nominatims jsonv2-svar. Bara fälten i GeoAddress sparas, all text tvättas."""
    if "error" in data:
        return None
    address = data.get("address")
    if not isinstance(address, dict):
        address = {}
    street = next((s for s in (_text(address.get(k), 120) for k in _STREET_KEYS) if s), None)
    house_number = _text(address.get("house_number"), 20) if street else None
    postcode = _text(address.get("postcode"), 20)
    city = next((c for c in (_text(address.get(k), 120) for k in _CITY_KEYS) if c), None)
    category, typ = _token(data.get("category")), _token(data.get("type"))
    osm_class = f"{category}={typ}" if category and typ else None
    poi_name = _text(data.get("name"), 200)
    if not category or category in _NO_POI_CATEGORIES or (poi_name and street and poi_name.casefold() == street.casefold()):
        poi_name = None
    street_part = f"{street} {house_number}" if street and house_number else street
    city_part = f"{postcode} {city}" if postcode and city else city
    label = sanitize_text(", ".join(p for p in (street_part, city_part) if p), 300)
    if not label:
        return None
    return GeoAddress(
        label=label,
        street=street,
        house_number=house_number,
        postcode=postcode if city else None,
        city=city,
        poi_name=poi_name,
        osm_class=osm_class,
        place_kind=classify_place(category, typ),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- adress → position (1.3.0)


@dataclass
class _ForwardJob:
    loc_id: uuid.UUID
    user_id: uuid.UUID
    provider_url: str
    query: str
    address: str


def forward_query(loc: KnownLocation) -> str:
    """Frågan till adressuppslaget: platsens adress, och orten om den inte redan står där."""
    address = (loc.address or "").strip()
    city = (loc.city or "").strip()
    if city and fold_text(city) not in fold_text(address):
        address = f"{address}, {city}" if address else city
    return sanitize_text(address, FORWARD_QUERY_LIMIT)


def fold_text(value: str) -> str:
    return " ".join(value.casefold().split())


def position_lookup_state(loc: KnownLocation, *, enabled: Any) -> str | None:
    """Läget för adress → position som webben visar: ``pending`` (adressen slås upp i
    bakgrunden), ``not_found``, ``ambiguous`` eller None (positionen finns, platsen saknar
    adress, demoplats). ``enabled`` är en funktion som säger om geokodningen är på; den
    anropas bara när svaret beror på den."""
    if loc.latitude is not None or loc.is_demo or not (loc.address or "").strip() or loc.merged_into_id is not None:
        return None
    if loc.forward_status == FORWARD_NOT_FOUND:
        return FORWARD_NOT_FOUND
    if loc.forward_status == FORWARD_AMBIGUOUS:
        return FORWARD_AMBIGUOUS
    if loc.forward_status in (None, FORWARD_FAILED) and enabled():
        return "pending"
    return None


def reset_forward(loc: KnownLocation) -> None:
    """Ägaren har ändrat adressen: den nya adressen prövas, och en position som bara var
    uppslagen ur den gamla adressen tas bort."""
    loc.forward_status = None
    loc.forward_at = None
    loc.forward_attempts = 0
    loc.forward_next_at = None
    if loc.coordinate_source == COORDINATE_GEOCODED:
        loc.latitude = loc.longitude = None
        loc.coordinate_source = None


def run_forward(db_factory: Any = None, *, now: datetime | None = None,
                limit_per_user: int = FORWARD_PER_TICK) -> dict[str, int]:
    """Slår upp positionen för platser som har adress men saknar position.

    Högst ``limit_per_user`` per användare och körning, inom dygnets gräns (``DAILY_LIMIT``,
    delad med de omvända uppslagen) och med samma skydd och strypning. Läser i en kort
    session, gör uppslagen utan öppen transaktion och skriver i en ny kort session.
    """
    factory = db_factory or session_scope
    now = now or utcnow()
    out: dict[str, int] = defaultdict(int)
    with factory() as db:
        jobs = _collect_forward(db, now=now, limit_per_user=limit_per_user)
    if not jobs:
        return {}
    results: dict[uuid.UUID, tuple[str, GeoHit | None]] = {}
    for job in jobs:
        try:
            hits = search(provider_url=job.provider_url, query=job.query)
        except (GeocodeError, ValueError) as exc:
            logger.warning("Adressuppslaget (adress → position) misslyckades: %s", exc)
            results[job.loc_id] = (FORWARD_FAILED, None)
            continue
        except Exception as exc:  # ett oväntat svar får inte fälla hela körningen
            logger.warning("Adressuppslaget misslyckades oväntat (%s).", type(exc).__name__)
            results[job.loc_id] = (FORWARD_FAILED, None)
            continue
        out["looked_up"] += 1
        if not hits:
            results[job.loc_id] = (FORWARD_NOT_FOUND, None)
        elif len(hits) > 1 and haversine_m(hits[0].lat, hits[0].lon, hits[1].lat, hits[1].lon) > FORWARD_SAME_PLACE_M:
            results[job.loc_id] = (FORWARD_AMBIGUOUS, None)
        else:
            results[job.loc_id] = (FORWARD_OK, hits[0])
    with factory() as db:
        _apply_forward(db, jobs, results, now=now, out=out)
    return {k: v for k, v in out.items() if v}


def _collect_forward(db: Session, *, now: datetime, limit_per_user: int) -> list[_ForwardJob]:
    rows = list(
        db.execute(
            select(KnownLocation)
            .where(
                KnownLocation.address.is_not(None),
                KnownLocation.address != "",
                KnownLocation.latitude.is_(None),
                KnownLocation.is_demo.is_(False),
                KnownLocation.merged_into_id.is_(None),
                KnownLocation.forward_status.is_(None)
                | (
                    (KnownLocation.forward_status == FORWARD_FAILED)
                    & (KnownLocation.forward_next_at.is_(None) | (KnownLocation.forward_next_at <= now))
                ),
            )
            .order_by(KnownLocation.created_at)
            .limit(500)
        ).scalars()
    )
    if not rows:
        return []
    budget = DAILY_LIMIT - _calls_last_day(db, now)
    providers: dict[uuid.UUID, tuple[str, str] | None] = {}
    per_user: dict[uuid.UUID, int] = defaultdict(int)
    jobs: list[_ForwardJob] = []
    for loc in rows:
        if budget <= 0:
            break
        if loc.owner_user_id not in providers:
            providers[loc.owner_user_id] = _user_provider(db, loc.owner_user_id)
        provider = providers[loc.owner_user_id]
        if provider is None or per_user[loc.owner_user_id] >= limit_per_user:
            continue
        query = forward_query(loc)
        if not query:
            continue
        per_user[loc.owner_user_id] += 1
        budget -= 1
        jobs.append(_ForwardJob(loc.id, loc.owner_user_id, provider[0], query, loc.address or ""))
    return jobs


def _apply_forward(
    db: Session, jobs: list[_ForwardJob], results: dict[uuid.UUID, tuple[str, GeoHit | None]], *,
    now: datetime, out: dict[str, int],
) -> None:
    merged_users: set[uuid.UUID] = set()
    for job in jobs:
        loc = db.get(KnownLocation, job.loc_id)
        # Ägaren kan ha satt ut positionen, ändrat adressen eller slagit ihop platsen under tiden.
        if loc is None or loc.merged_into_id is not None or loc.latitude is not None or (loc.address or "") != job.address:
            continue
        status, hit = results.get(job.loc_id, (FORWARD_FAILED, None))
        loc.forward_at = now
        if status == FORWARD_OK and hit is not None:
            out["ok"] += 1
            loc.latitude, loc.longitude, loc.coordinate_source = hit.lat, hit.lon, COORDINATE_GEOCODED
            loc.forward_status, loc.forward_next_at = FORWARD_OK, None
            if not loc.osm_class and hit.osm_class:
                loc.osm_class = hit.osm_class
            if not loc.place_kind:
                loc.place_kind = hit.place_kind
            db.flush()
            if _merge_with_known(db, loc, now=now):
                out["merged"] += 1
                merged_users.add(loc.owner_user_id)
        elif status in (FORWARD_NOT_FOUND, FORWARD_AMBIGUOUS):
            out[status] += 1
            loc.forward_status, loc.forward_next_at = status, None
        else:
            out["failed"] += 1
            loc.forward_attempts = int(loc.forward_attempts or 0) + 1
            if loc.forward_attempts >= MAX_ATTEMPTS:
                loc.forward_status, loc.forward_next_at = FORWARD_NOT_FOUND, None  # ger upp
            else:
                loc.forward_status = FORWARD_FAILED
                loc.forward_next_at = now + RETRY_DELAYS[min(loc.forward_attempts, len(RETRY_DELAYS)) - 1]
        db.flush()
    if merged_users:
        _after_merge(db, merged_users)


def _merge_candidate(db: Session, loc: KnownLocation) -> KnownLocation | None:
    """Bekräftad bostad eller bekräftat kontor, med position från bilen eller ägaren, inom
    ``max(radie, 150 m)`` från den uppslagna platsen. Närmast vinner."""
    best: tuple[float, KnownLocation] | None = None
    for known in db.execute(
        select(KnownLocation).where(
            KnownLocation.owner_user_id == loc.owner_user_id,
            KnownLocation.id != loc.id,
            KnownLocation.merged_into_id.is_(None),
            KnownLocation.is_demo.is_(loc.is_demo),
            KnownLocation.confirmed.is_(True),
            KnownLocation.type.in_((LocationType.HOME, LocationType.REGULAR_WORKPLACE, LocationType.OFFICE)),
            KnownLocation.coordinate_source.in_(("api", "user")),
            KnownLocation.latitude.is_not(None),
            KnownLocation.longitude.is_not(None),
        )
    ).scalars():
        d = haversine_m(loc.latitude, loc.longitude, known.latitude, known.longitude)  # type: ignore[arg-type]
        if d <= max(float(known.radius_m or 0), FORWARD_MERGE_MIN_M) and (best is None or d < best[0]):
            best = (d, known)
    return best[1] if best is not None else None


def _merge_with_known(db: Session, loc: KnownLocation, *, now: datetime) -> bool:
    """En uppslagen plats vid bostaden eller ett kontor utan adress (eller med samma adress)
    slås ihop med den – så får bostaden sin adress ur den importerade platsen. Har båda adress
    och adresserna skiljer sig blir det bara ett förslag (``registry.merge_suggestions``).
    Andra platser (kunder, obekräftade) rörs inte."""
    known = _merge_candidate(db, loc)
    if known is None:
        return False
    if (known.address or "").strip() and address_key(known.address) != address_key(loc.address):
        return False  # olika adresser: bara ett förslag (address_merge_suggestions)
    if not _may_merge_into(db, loc, known):
        return False  # platsen har en egen betydelse eller det är oklart vilken adress: bara ett förslag
    address, city = loc.address, loc.city
    places.merge_locations(db, known, loc)
    if not (known.address or "").strip():
        known.address = address
    if not (known.city or "").strip():
        known.city = city
    db.flush()
    audit.record(
        db, audit.SYSTEM, "location.merged_by_address", "known_location", known.id,
        after={"merged": str(loc.id), "address": known.address}, user_id=known.owner_user_id,
    )
    return True


def _has_own_meaning(db: Session, loc: KnownLocation, known: KnownLocation) -> bool:
    """Den uppslagna platsen betyder något eget för ägaren: bekräftad som annan typ (t.ex. en
    kund), har ett företag eller ingår i en egen regel. Den slås aldrig ihop av sig själv."""
    from app.models import LocationPattern

    if loc.confirmed and loc.type not in (None, LocationType.UNKNOWN, known.type):
        return True
    if (loc.company or "").strip():
        return True
    rule = db.execute(
        select(LocationPattern.id).where(
            LocationPattern.owner_user_id == loc.owner_user_id,
            LocationPattern.is_manual_rule.is_(True),
            (LocationPattern.origin_location_id == loc.id) | (LocationPattern.destination_location_id == loc.id),
        ).limit(1)
    ).first()
    return rule is not None


def _other_addresses_near(db: Session, loc: KnownLocation, known: KnownLocation) -> bool:
    """Ligger fler uppslagna platser med andra adresser vid samma bostad/kontor går det inte
    att veta vilken adress som är bostadens – då blir det bara förslag."""
    radius = max(float(known.radius_m or 0), FORWARD_MERGE_MIN_M)
    key = address_key(loc.address)
    for other in db.execute(
        select(KnownLocation).where(
            KnownLocation.owner_user_id == loc.owner_user_id,
            KnownLocation.id.not_in((loc.id, known.id)),
            KnownLocation.merged_into_id.is_(None),
            KnownLocation.coordinate_source == COORDINATE_GEOCODED,
            KnownLocation.latitude.is_not(None),
            KnownLocation.longitude.is_not(None),
        )
    ).scalars():
        if address_key(other.address) == key:
            continue
        if haversine_m(known.latitude, known.longitude, other.latitude, other.longitude) <= radius:  # type: ignore[arg-type]
            return True
    return False


def _may_merge_into(db: Session, loc: KnownLocation, known: KnownLocation) -> bool:
    if _has_own_meaning(db, loc, known):
        return False
    if not (known.address or "").strip() and _other_addresses_near(db, loc, known):
        return False
    return True


def address_merge_suggestions(db: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """Förslag: en plats vars position är uppslagen ur adressen ligger vid bostaden eller ett
    kontor som redan har en annan adress – kan vara samma plats (visas under Platser)."""
    out: list[dict[str, Any]] = []
    for loc in db.execute(
        select(KnownLocation).where(
            KnownLocation.owner_user_id == user_id,
            KnownLocation.merged_into_id.is_(None),
            KnownLocation.coordinate_source == COORDINATE_GEOCODED,
            KnownLocation.latitude.is_not(None),
        )
    ).scalars():
        known = _merge_candidate(db, loc)
        if known is None or not (loc.address or "").strip():
            continue
        if not (known.address or "").strip() or address_key(known.address) == address_key(loc.address):
            # Samma adress eller bostaden saknar adress: slås ihop av sig själv – förslag bara
            # när platsen inte fick slås ihop (egen betydelse, eller flera adresser vid bostaden).
            if _may_merge_into(db, loc, known):
                continue
        out.append({
            "street_key": f"address:{loc.id}",
            "locations": [
                {"id": str(m.id), "name": m.name, "address": m.address, "visit_count": m.visit_count}
                for m in (known, loc)
            ],
            "reason": f"Adressen {loc.address} ligger vid {known.name} – kan vara samma plats.",
        })
    return out


def _after_merge(db: Session, user_ids: set[uuid.UUID]) -> None:
    """Efter en sammanslagning: mönstren och förslagen räknas om i bakgrunden."""
    from app.models import Vehicle
    from app.services.learning import patterns, recompute

    for user_id in user_ids:
        patterns.rebuild(db, user_id)
        for vehicle_id in db.execute(select(Vehicle.id).where(Vehicle.owner_user_id == user_id)).scalars():
            recompute.request(db, vehicle_id)
