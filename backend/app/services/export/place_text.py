"""Platsernas text i underlaget: alltid gatuadress och ort – aldrig "Hem".

* Bostaden skrivs med sin adress, aldrig med sitt namn.
* En bekräftad plats med eget namn skrivs "Namn, adress" (t.ex. "Acme AB, Kundvägen 2, Ort").
* Övriga platser skrivs med adressen. Orten läggs till när den inte redan står i adressen.
* Saknar bostaden eller en annan bekräftad plats adress blir texten tom, och kontrollen
  ``place_address_missing`` säger var adressen fylls i (Platser).

Gränssnittet i övrigt visar platsernas namn som förut; bara underlaget följer reglerna här.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnownLocation, LogicalTrip
from app.models.enums import LocationType, TripSource
from app.services.places.normalize import clean, fold

LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)


def _address(loc: KnownLocation) -> str:
    """Platsens adress med ort, eller tom text."""
    address = clean(loc.address)
    city = clean(loc.city)
    if address and city and fold(city) not in fold(address):
        address = f"{address}, {city}"
    return address


def needs_address(loc: KnownLocation | None) -> bool:
    """Bostaden eller en bekräftad plats som saknar adress (då blir underlagets text tom)."""
    return loc is not None and (loc.type == LocationType.HOME or bool(loc.confirmed)) and not clean(loc.address)


def report_text(loc: KnownLocation | None, fallback: str | None, *, live: bool) -> str:
    """Platsens text i underlaget.

    ``fallback`` är resans egen adresstext. Den används bara när platsen själv inte har
    någon adress och inte är bostaden eller bekräftad – och aldrig för resor från bilen, där
    resans text bara är platsens namn (t.ex. "Hem").
    """
    if loc is None:
        return "" if live and fallback is None else clean(fallback)
    address = _address(loc)
    if loc.type == LocationType.HOME:
        return address
    if not address:
        return "" if live or needs_address(loc) else clean(fallback)
    name = clean(loc.name)
    if loc.confirmed and name and fold(name) not in fold(address):
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
    ids = trip_location_ids(trips)
    if db is None or not ids:
        return {}
    return {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(ids))).scalars()}


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
        here = [locs.get(i) for i in (t.start_location_id, t.end_location_id) if i is not None]
        for stop in t.via or []:
            loc = via_location(stop, locs)
            if loc is not None and str(loc.id) not in private_stop_ids:
                here.append(loc)
        missing = [loc for loc in here if needs_address(loc)]
        if missing:
            affected.append(t)
            for loc in missing:
                found[loc.id] = loc  # type: ignore[union-attr]
    ordered = sorted(found.values(), key=lambda loc: (loc.type != LocationType.HOME, fold(loc.name)))
    return ordered, affected
