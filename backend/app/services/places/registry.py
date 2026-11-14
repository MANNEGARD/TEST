"""Platsregistret: kända platser, alias och geofencing."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import KnownLocation, LocationAlias, LocationPattern, LogicalTrip, RawTrip
from app.models.enums import LocationType
from app.services.places.normalize import (
    address_key,
    clean,
    haversine_m,
    parse_address,
    valid_coordinates,
)

WORK_TYPES = {LocationType.REGULAR_WORKPLACE, LocationType.OFFICE, LocationType.CUSTOMER}


def find_by_address(db: Session, user_id: uuid.UUID, address: str | None) -> KnownLocation | None:
    key = address_key(address)
    if not key:
        return None
    alias = db.execute(
        select(LocationAlias).where(LocationAlias.owner_user_id == user_id, LocationAlias.address_key == key)
    ).scalar_one_or_none()
    if alias is None:
        return None
    return _canonical(db, db.get(KnownLocation, alias.location_id))


def _canonical(db: Session, loc: KnownLocation | None) -> KnownLocation | None:
    hops = 0
    while loc is not None and loc.merged_into_id is not None and hops < 10:
        loc = db.get(KnownLocation, loc.merged_into_id)
        hops += 1
    return loc


def resolve_address(
    db: Session,
    user_id: uuid.UUID,
    address: str | None,
    *,
    create: bool = True,
    created_from: str = "import",
    is_demo: bool = False,
) -> KnownLocation | None:
    """Hittar platsen för en adress eller skapar en obekräftad plats."""
    raw = clean(address)
    if not raw:
        return None
    loc = find_by_address(db, user_id, raw)
    if loc is not None or not create:
        return loc
    parsed = parse_address(raw)
    loc = KnownLocation(
        owner_user_id=user_id,
        name=raw,
        type=LocationType.UNKNOWN,
        address=raw,
        city=parsed.city,
        confirmed=False,
        created_from=created_from,
        is_demo=is_demo,
    )
    db.add(loc)
    db.flush()
    db.add(LocationAlias(owner_user_id=user_id, location_id=loc.id, address_key=address_key(raw), raw_address=raw))
    db.flush()
    return loc


def resolve_position(
    db: Session, user_id: uuid.UUID, lat: float | None, lon: float | None
) -> tuple[KnownLocation | None, float | None]:
    """Närmaste kända plats vars geofence innehåller positionen."""
    if not valid_coordinates(lat, lon):
        return None, None
    best: tuple[KnownLocation | None, float | None] = (None, None)
    rows = db.execute(
        select(KnownLocation).where(
            KnownLocation.owner_user_id == user_id,
            KnownLocation.latitude.is_not(None),
            KnownLocation.longitude.is_not(None),
            KnownLocation.merged_into_id.is_(None),
        )
    ).scalars()
    for loc in rows:
        d = haversine_m(lat, lon, loc.latitude, loc.longitude)  # type: ignore[arg-type]
        if d <= max(loc.radius_m, 30) and (best[1] is None or d < best[1]):
            best = (loc, d)
    return best


def add_alias(db: Session, loc: KnownLocation, address: str) -> None:
    key = address_key(address)
    if not key:
        return
    existing = db.execute(
        select(LocationAlias).where(
            LocationAlias.owner_user_id == loc.owner_user_id, LocationAlias.address_key == key
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            LocationAlias(
                owner_user_id=loc.owner_user_id, location_id=loc.id, address_key=key, raw_address=clean(address)
            )
        )
    elif existing.location_id != loc.id:
        existing.location_id = loc.id
    db.flush()


def merge_locations(db: Session, keep: KnownLocation, other: KnownLocation) -> KnownLocation:
    """Slår ihop två platser (t.ex. grannadresser som är samma hotell)."""
    if keep.id == other.id:
        return keep
    if keep.owner_user_id != other.owner_user_id:
        raise ValueError("Platserna tillhör olika användare.")
    db.execute(update(LocationAlias).where(LocationAlias.location_id == other.id).values(location_id=keep.id))
    for model, cols in (
        (RawTrip, ("start_location_id", "end_location_id")),
        (LogicalTrip, ("start_location_id", "end_location_id")),
    ):
        for col in cols:
            db.execute(update(model).where(getattr(model, col) == other.id).values({col: keep.id}))
    # Mönster räknas om av learning.patterns; ta bort de som pekar på den gamla platsen.
    for pat in db.execute(
        select(LocationPattern).where(
            (LocationPattern.destination_location_id == other.id) | (LocationPattern.origin_location_id == other.id)
        )
    ).scalars():
        if not pat.is_manual_rule:
            db.delete(pat)
        else:
            if pat.destination_location_id == other.id:
                pat.destination_location_id = keep.id
            if pat.origin_location_id == other.id:
                pat.origin_location_id = keep.id
    if keep.latitude is None and other.latitude is not None:
        keep.latitude, keep.longitude, keep.coordinate_source = other.latitude, other.longitude, other.coordinate_source
    other.merged_into_id = keep.id
    db.flush()
    recount_visits(db, keep.owner_user_id)
    return keep


def recount_visits(db: Session, user_id: uuid.UUID) -> None:
    """Räknar besök (ankomster) per plats ur aktiva journalresor."""
    counts: dict[uuid.UUID, int] = defaultdict(int)
    last: dict[uuid.UUID, datetime] = {}
    rows = db.execute(
        select(RawTrip.end_location_id, func.count(), func.max(RawTrip.ended_at))
        .join(KnownLocation, KnownLocation.id == RawTrip.end_location_id)
        .where(KnownLocation.owner_user_id == user_id, RawTrip.voided_at.is_(None))
        .group_by(RawTrip.end_location_id)
    ).all()
    for loc_id, n, latest in rows:
        counts[loc_id] = n
        if latest is not None:
            last[loc_id] = latest
    for loc in db.execute(select(KnownLocation).where(KnownLocation.owner_user_id == user_id)).scalars():
        loc.visit_count = counts.get(loc.id, 0)
        latest = last.get(loc.id)
        if latest is not None and getattr(latest, "tzinfo", None) is None:
            from datetime import UTC

            latest = latest.replace(tzinfo=UTC)
        loc.last_visited_at = latest
    db.flush()


def learn_coordinates(db: Session, loc: KnownLocation, lat: float | None, lon: float | None, source: str) -> bool:
    """Sparar koordinater på en plats som saknar dem (t.ex. när en API-position
    kan kopplas till en importerad adress). Skriver aldrig över användarens val."""
    if not valid_coordinates(lat, lon):
        return False
    if loc.latitude is not None and loc.coordinate_source == "user":
        return False
    if loc.latitude is None:
        loc.latitude, loc.longitude, loc.coordinate_source = lat, lon, source
        db.flush()
        return True
    return False


def display_name(loc: KnownLocation | None, fallback: str | None = None) -> str:
    if loc is None:
        return fallback or "Okänd plats"
    return loc.name or loc.address or fallback or "Okänd plats"


def public_label(loc: KnownLocation | None, address: str | None, *, hide_private: bool = True) -> str:
    """Etikett som är lämplig i notiser: privata platser visas aldrig med adress."""
    if loc is not None:
        if loc.type == LocationType.HOME:
            return "Hemmet"
        if loc.type == LocationType.PRIVATE and hide_private:
            return "Privat plats"
        if loc.confirmed and loc.name:
            return loc.name
        if loc.city:
            return loc.city
    parsed = parse_address(address)
    return parsed.city or "okänd plats"


def merge_suggestions(db: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """Föreslår sammanslagning av platser på samma gata med närliggande nummer
    (GPS-brus ger ofta grannadresser för samma parkering)."""
    groups: dict[str, list[tuple[KnownLocation, int | None]]] = defaultdict(list)
    for loc in db.execute(
        select(KnownLocation).where(KnownLocation.owner_user_id == user_id, KnownLocation.merged_into_id.is_(None))
    ).scalars():
        parsed = parse_address(loc.address)
        if parsed.street_key:
            groups[parsed.street_key].append((loc, parsed.number_int))
    out: list[dict[str, Any]] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        nums = [n for _, n in members if n is not None]
        if len(nums) >= 2 and max(nums) - min(nums) > 12:
            continue
        members.sort(key=lambda m: -m[0].visit_count)
        out.append(
            {
                "street_key": key,
                "locations": [
                    {"id": str(m[0].id), "name": m[0].name, "address": m[0].address, "visit_count": m[0].visit_count}
                    for m in members
                ],
                "reason": "Samma gata och ort med närliggande gatunummer – kan vara samma plats.",
            }
        )
    out.sort(key=lambda g: -sum(m["visit_count"] for m in g["locations"]))
    return out
