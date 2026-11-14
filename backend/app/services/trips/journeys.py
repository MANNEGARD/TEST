"""Överordnade tjänsteresor: flera körningar, hotellövernattning och hemresa.

En kedja börjar när en tjänsteresa lämnar hemmet och slutar när bilen är hemma igen.
Kedjor med hotell, flera dagar eller minst tre körningar föreslås som en "Tjänsteresa".
"""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BusinessJourney, KnownLocation, LogicalTrip, Vehicle
from app.models.enums import Category, LocationType, TripSource
from app.services import settings as settings_svc
from app.services.trips import segments as seg


def _locations(db: Session, user_id: uuid.UUID) -> dict[uuid.UUID, KnownLocation]:
    return {
        loc.id: loc
        for loc in db.execute(select(KnownLocation).where(KnownLocation.owner_user_id == user_id)).scalars()
    }


def detect(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle) -> int:
    """Föreslår tjänsteresor för kedjor som saknar en. Returnerar antal nya förslag."""
    locs = _locations(db, user_id)
    homes = {i for i, loc in locs.items() if loc.confirmed and loc.type == LocationType.HOME}
    if not homes:
        return 0  # utan bekräftad hemadress går kedjor inte att avgränsa
    tz: ZoneInfo = settings_svc.user_timezone(db, user_id)
    trips = [t for t in seg.active_trips(db, vehicle.id) if t.source != TripSource.GAP_FILL and t.sort_time is not None]
    created = 0
    chain: list[LogicalTrip] = []

    def close(chain: list[LogicalTrip]) -> int:
        if len(chain) < 2 or any(t.journey_id for t in chain):
            return 0
        hotel = any(
            (t.end_location_id in locs and locs[t.end_location_id].confirmed
             and locs[t.end_location_id].type == LocationType.HOTEL)
            for t in chain
        )
        first_day = chain[0].sort_time.astimezone(tz).date()  # type: ignore[union-attr]
        last = chain[-1].ended_at or chain[-1].sort_time
        last_day = last.astimezone(tz).date()  # type: ignore[union-attr]
        multi_day = last_day > first_day
        if not (hotel or multi_day or len(chain) >= 3):
            return 0
        cities: list[str] = []
        for t in chain:
            loc = locs.get(t.end_location_id) if t.end_location_id else None
            city = loc.city if loc is not None else None
            if city and loc.id not in homes and city not in cities:  # type: ignore[union-attr]
                cities.append(city)
        span = first_day.isoformat() if not multi_day else f"{first_day.isoformat()} – {last_day.isoformat()}"
        journey = BusinessJourney(
            vehicle_id=vehicle.id,
            name=f"Tjänsteresa {', '.join(cities[:3]) or 'utan känd ort'} ({span})",
            started_at=chain[0].started_at or chain[0].sort_time,
            ended_at=chain[-1].ended_at,
            status="suggested",
        )
        db.add(journey)
        db.flush()
        for t in chain:
            t.journey_id = journey.id
        return 1

    for t in trips:
        work = t.category == Category.WORK and not t.has_mixed_categories
        starts_home = t.start_location_id in homes
        ends_home = t.end_location_id in homes
        if not chain:
            if work and starts_home and not ends_home:
                chain = [t]
            continue
        if not work:
            chain = []  # en privat eller oavgjord resa bryter kedjan – inget antas
            continue
        chain.append(t)
        if ends_home:
            created += close(chain)
            chain = []
    db.flush()
    return created


def confirm(db: Session, journey: BusinessJourney, *, name: str | None = None, purpose: str | None = None) -> BusinessJourney:
    journey.status = "confirmed"
    if name:
        journey.name = name[:200]
    if purpose is not None:
        journey.purpose = purpose
    db.flush()
    return journey


def dissolve(db: Session, journey: BusinessJourney) -> None:
    for t in db.execute(select(LogicalTrip).where(LogicalTrip.journey_id == journey.id)).scalars():
        t.journey_id = None
    db.delete(journey)
    db.flush()
