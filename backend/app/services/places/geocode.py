"""Adresser för bilens stopp (omvänd geokodning mot OpenStreetMap/Nominatim).

Flödet (byggkontraktet C.1 och E.1):

* ``place_for_position`` körs i detekteringen. Ligger stoppet vid en känd plats används
  den, annars skapas en ny obekräftad plats med status "väntar på adress". Inget nätverk.
* ``run_pending`` körs i ``tick``. Den läser väntande platser i en kort session, gör
  uppslagen utan öppen transaktion och skriver svaren i en ny kort session.
* ``reverse`` är den rena nätverksdelen. Bara kartcellens mittpunkt skickas (4 decimaler),
  aldrig resa, tid eller id.

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
from app.services.places.normalize import address_key, valid_coordinates
from app.services.text_safety import sanitize_text

logger = logging.getLogger(__name__)

USER_AGENT = f"SMGD-DriveLog/{__version__} (self-hosted logbook; reverse geocoding)"

# Namnet på en plats som ännu saknar adress. Ingenting hittas på.
UNRESOLVED_NAME = "Okänd plats"
NEW_PLACE_RADIUS_M = 75

MIN_INTERVAL_S = 1.1  # högst ett anrop per 1,1 s i processen
DAILY_LIMIT = 500  # högst 500 anrop per dygn
DEADLINE_S = 4.0  # total tid för ett anrop
MAX_BODY_BYTES = 64 * 1024
MAX_ATTEMPTS = 5
RETRY_DELAYS = (timedelta(minutes=5), timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
# Misslyckade platser prövas igen bara så länge omförsöken pågår (5 min + 1 h + 6 h + 24 h).
FAILED_RETRY_WINDOW = timedelta(days=3)

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
        return _nonzero(out)

    # Nätverket: ingen databastransaktion är öppen här.
    results: dict[tuple[uuid.UUID, str, int, int], tuple[str, GeoAddress | None]] = {}
    for cell, job in plan.fetch.items():
        out["looked_up"] += 1
        try:
            addr = reverse(provider_url=job.provider_url, lat_key=job.lat_key, lon_key=job.lon_key)
        except (GeocodeError, ValueError) as exc:
            logger.warning("Adressuppslaget misslyckades: %s", exc)
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
    return _nonzero(out)


def _nonzero(out: dict[str, Any]) -> dict[str, Any]:
    """Bara räknare som inte är noll; inget att göra ger ``{}``."""
    return {k: v for k, v in out.items() if v}


def _collect(db: Session, *, now: datetime, limit: int, out: dict[str, Any]) -> _Plan:
    plan = _Plan()
    rows = list(
        db.execute(
            select(KnownLocation)
            .where(
                KnownLocation.created_from == "api",
                KnownLocation.is_demo.is_(False),
                KnownLocation.merged_into_id.is_(None),
                KnownLocation.latitude.is_not(None),
                KnownLocation.longitude.is_not(None),
                (KnownLocation.geocode_status == GeocodeStatus.PENDING)
                | (
                    (KnownLocation.geocode_status == GeocodeStatus.FAILED)
                    & (KnownLocation.created_at >= now - FAILED_RETRY_WINDOW)
                ),
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
        if provider is None:
            if not geocoding_enabled(db, loc.owner_user_id):
                loc.geocode_status = GeocodeStatus.OFF
                out["off"] += 1
            else:
                out["waiting_for_home"] += 1
            continue
        url, host = provider
        job = _Job(loc.id, loc.owner_user_id, url, host, cell_key(loc.latitude), cell_key(loc.longitude))  # type: ignore[arg-type]
        cache = _cache_row(db, job.user_id, host, job.lat_key, job.lon_key)
        if cache is not None and cache.status in (GeocodeStatus.OK, GeocodeStatus.NOT_FOUND):
            job.cached = (cache.status, cache.result if isinstance(cache.result, dict) else None)
            plan.jobs.append(job)
            continue
        if cache is not None and cache.status == GeocodeStatus.FAILED:
            due = cache.next_attempt_at is None or cache.next_attempt_at <= now
            if cache.attempts >= MAX_ATTEMPTS or not due:
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
    return (
        db.execute(
            select(KnownLocation.id).where(
                KnownLocation.owner_user_id == user_id,
                KnownLocation.type == LocationType.HOME,
                KnownLocation.merged_into_id.is_(None),
                KnownLocation.latitude.is_not(None),
                KnownLocation.longitude.is_not(None),
            ).limit(1)
        ).first()
        is not None
    )


def _calls_last_day(db: Session, now: datetime) -> int:
    return int(
        db.execute(select(func.count()).select_from(GeocodeCache).where(GeocodeCache.fetched_at >= now - timedelta(days=1))).scalar_one()
        or 0
    )


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
        for text in _address_variants(addr):
            key = address_key(text)
            exists = db.execute(
                select(LocationAlias.id).where(LocationAlias.owner_user_id == loc.owner_user_id, LocationAlias.address_key == key)
            ).first()
            if key and exists is None:
                db.add(LocationAlias(owner_user_id=loc.owner_user_id, location_id=loc.id, address_key=key, raw_address=text))
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
    *, provider_url: str, lat_key: int, lon_key: int, transport: httpx.BaseTransport | None = None
) -> GeoAddress | None:
    """Själva nätverksanropet. Ger None när kartan inte har någon adress, och kastar
    GeocodeError (eller ValueError för en otillåten adress) när uppslaget inte gick att göra.

    En utbytt transport (tester) hoppar över DNS-kontrollen, eftersom inget riktigt namn
    slås upp, men motpartens adress kontrolleras om transporten anger den.
    """
    injected = transport or default_transport()
    base = validate_runtime_url(provider_url, resolve=injected is None)
    params = {
        "lat": f"{lat_key / 1e4:.4f}",
        "lon": f"{lon_key / 1e4:.4f}",
        "format": "jsonv2",
        "zoom": "18",
        "addressdetails": "1",
        "accept-language": "sv",
    }
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
            with client.stream("GET", f"{base}/reverse", params=params) as resp:
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
        data = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GeocodeError("Geokodarens svar var inte giltig JSON.") from exc
    if not isinstance(data, dict):
        raise GeocodeError("Geokodarens svar hade fel form.")
    return parse_response(data)


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
