"""Platsernas text i underlaget: alltid gatuadress och ort – aldrig "Hem".

* Bostaden skrivs med sin adress, aldrig med sitt namn. En resa vars plats bara heter "Hem"
  (manuell eller importerad, inte kopplad till bostaden) skrivs också med bostadens adress.
* En bekräftad plats med eget namn skrivs "Namn, adress" (t.ex. "Acme AB, Kundvägen 2, Ort").
  En privat plats skrivs bara med adressen: dess namn går inte till arbetsgivaren.
* Övriga platser skrivs med adressen. Orten läggs till när den inte redan står i adressen.
* Saknar bostaden eller en annan bekräftad plats adress eller ort blir texten tom eller utan
  ort, och kontrollen ``place_address_missing`` säger var adressen fylls i (Platser).

Gränssnittet i övrigt visar platsernas namn som förut; bara underlaget följer reglerna här.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import KnownLocation, LogicalTrip, Vehicle
from app.models.enums import LocationType, TripSource
from app.services.places.normalize import clean, fold, parse_address

LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
HOME_WORD = "Hem"
_POSTCODE = re.compile(r"^\d{3}\s?\d{2}\s+")


def _part(text: str) -> str:
    return fold(_POSTCODE.sub("", text.strip()))


def _in_address(text: str, address: str) -> bool:
    """Står ``text`` som en egen del av adressen ("Kundvägen 2, 123 45 Ort" har delen "Ort")?"""
    parts = [p for p in address.split(",") if p.strip()]
    return _part(text) in {_part(p) for p in parts} | {fold(p.strip()) for p in parts}


def _repeats_address(name: str, address: str) -> bool:
    """Säger namnet bara det som redan står i adressen? Ett namn utan komma jämförs som en egen
    del ("Lund" finns i "Lundavägen 1, Lund" men "Berg" inte i "Bergsgatan 2, Ort"). Ett namn
    som självt är en adress ("Kundvägen 2, Ort") upprepar adressen när varje del av namnet är
    en del av adressen – postnumret räknas inte."""
    if _in_address(name, address):
        return True
    own = {_part(p) for p in name.split(",") if p.strip()}
    theirs = {_part(p) for p in address.split(",") if p.strip()}
    return len(own) > 1 and own <= theirs


def _address(loc: KnownLocation) -> str:
    """Platsens adress med ort, eller tom text."""
    address = clean(loc.address)
    city = clean(loc.city)
    if address and city and not _in_address(city, address):
        address = f"{address}, {city}"
    return address


def _has_city(loc: KnownLocation) -> bool:
    return bool(clean(loc.city)) or parse_address(loc.address).city is not None


def needs_address(loc: KnownLocation | None) -> bool:
    """Bostaden eller en bekräftad plats som saknar adress eller ort (då blir underlagets text
    tom eller utan ort)."""
    if loc is None or not (loc.type == LocationType.HOME or bool(loc.confirmed)):
        return False
    return not clean(loc.address) or not _has_city(loc)


def _home_word(loc: KnownLocation | None, fallback: str | None, home: KnownLocation | None) -> bool:
    """Är platsens enda text ordet Hem (eller bostadens namn)? T.ex. en manuell eller importerad
    resa där ägaren skrev "Hem" – den platsen är inte kopplad till bostaden."""
    if loc is not None and loc.type == LocationType.HOME:
        return False
    text = (clean(loc.address) or clean(loc.name)) if loc is not None else clean(fallback)
    words = {fold(HOME_WORD)} | ({fold(clean(home.name))} if home is not None and clean(home.name) else set())
    return bool(text) and fold(text) in words


def home_of(locs: dict[uuid.UUID, KnownLocation], like: KnownLocation | None = None) -> KnownLocation | None:
    """Bostaden bland platserna (``load_locations`` tar med den), helst med samma ägare och
    demoläge som ``like``."""
    homes = [loc for loc in locs.values() if loc.type == LocationType.HOME and loc.merged_into_id is None]
    if like is not None:
        homes.sort(key=lambda h: (h.owner_user_id != like.owner_user_id, bool(h.is_demo) != bool(like.is_demo)))
    return homes[0] if homes else None


def place_for(
    loc: KnownLocation | None, fallback: str | None, locs: dict[uuid.UUID, KnownLocation] | None
) -> KnownLocation | None:
    """Platsen som underlaget skriver: bostaden när platsen bara heter "Hem", annars platsen."""
    home = home_of(locs or {}, loc)
    if home is not None and _home_word(loc, fallback, home):
        return home
    return loc


def report_text(
    loc: KnownLocation | None, fallback: str | None, *, live: bool,
    locs: dict[uuid.UUID, KnownLocation] | None = None,
) -> str:
    """Platsens text i underlaget.

    ``fallback`` är resans egen adresstext. Den används bara när platsen själv inte har
    någon adress och inte är bostaden eller bekräftad – och aldrig för resor från bilen, där
    resans text bara är platsens namn (t.ex. "Hem"). Ordet Hem skrivs aldrig: bostadens
    adress står i stället, eller ingenting (och kontrollen säger till).
    """
    loc = place_for(loc, fallback, locs)
    if _home_word(loc, fallback, None):
        return ""  # ingen bostad att hämta adressen från
    if loc is None:
        return clean(fallback)
    address = _address(loc)
    if loc.type == LocationType.HOME:
        return address
    if not address:
        return "" if live or needs_address(loc) else clean(fallback)
    name = clean(loc.name)
    if loc.confirmed and loc.type != LocationType.PRIVATE and name and not _repeats_address(name, address):
        return f"{name}, {address}"
    return address


def trip_location_ids(trips: Iterable[LogicalTrip]) -> set[uuid.UUID]:
    """Start, mål och mellanstopp för resorna."""
    ids: set[uuid.UUID] = set()
    for t in trips:
        ids.update(i for i in (t.start_location_id, t.end_location_id) if i is not None)
        for stop in t.via or []:
            if isinstance(stop, dict) and stop.get("location_id"):
                try:
                    ids.add(uuid.UUID(str(stop["location_id"])))
                except ValueError:
                    continue
    return ids


def load_locations(db: Session | None, trips: list[LogicalTrip]) -> dict[uuid.UUID, KnownLocation]:
    """Resornas platser, och fordonsägarens bostad (för resor där platsen bara heter "Hem")."""
    if db is None or not trips:
        return {}
    ids = trip_location_ids(trips)
    owners = select(Vehicle.owner_user_id).where(Vehicle.id.in_({t.vehicle_id for t in trips}))
    homes = and_(KnownLocation.type == LocationType.HOME, KnownLocation.owner_user_id.in_(owners),
                 KnownLocation.merged_into_id.is_(None))
    where = or_(KnownLocation.id.in_(ids), homes) if ids else homes
    return {loc.id: loc for loc in db.execute(select(KnownLocation).where(where)).scalars()}


def via_location(stop: Any, locs: dict[uuid.UUID, KnownLocation]) -> KnownLocation | None:
    if not isinstance(stop, dict) or not stop.get("location_id"):
        return None
    try:
        return locs.get(uuid.UUID(str(stop["location_id"])))
    except ValueError:
        return None


def missing_address_places(
    trips: list[LogicalTrip], locs: dict[uuid.UUID, KnownLocation], private_stop_ids: frozenset[str] = frozenset()
) -> tuple[list[KnownLocation], list[LogicalTrip]]:
    """Platser i underlaget som saknar adress (bostaden först), och resorna där de står."""
    found: dict[uuid.UUID, KnownLocation] = {}
    affected: list[LogicalTrip] = []
    for t in trips:
        here = [
            place_for(locs.get(i) if i is not None else None, text, locs)
            for i, text in ((t.start_location_id, t.start_address), (t.end_location_id, t.end_address))
        ]
        for stop in t.via or []:
            loc = via_location(stop, locs)
            if loc is not None and str(loc.id) not in private_stop_ids:
                here.append(place_for(loc, stop.get("address"), locs))
        # En plats som bara heter "Hem" när ingen bostad finns: fyll i adressen på den platsen.
        missing = [loc for loc in here if needs_address(loc) or (loc is not None and _home_word(loc, None, None))]
        if missing:
            affected.append(t)
            for loc in missing:
                found[loc.id] = loc  # type: ignore[union-attr]
    ordered = sorted(found.values(), key=lambda loc: (loc.type != LocationType.HOME, fold(loc.name)))
    return ordered, affected


def missing_city_only(loc: KnownLocation) -> bool:
    """Har platsen gatuadress men ingen ort?"""
    return bool(clean(loc.address)) and not _has_city(loc)
