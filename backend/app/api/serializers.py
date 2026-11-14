"""Bygger svarsmodeller ur databasobjekt (med batchade uppslag)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CalendarMatch, KnownLocation, LogicalTrip, ReviewItem, Vehicle
from app.models.enums import ReviewState
from app.schemas.common import LocationBrief, LocationOut, TripOut, VehicleOut
from app.services import vehicles as vehicles_svc


def vehicle_out(v: Vehicle) -> VehicleOut:
    out = VehicleOut.model_validate(v)
    out.vin_masked = vehicles_svc.mask_vin(v.vin)
    return out


def trips_out(db: Session, trips: Sequence[LogicalTrip]) -> list[TripOut]:
    if not trips:
        return []
    ids = [t.id for t in trips]
    loc_ids = {i for t in trips for i in (t.start_location_id, t.end_location_id) if i}
    locs: dict[uuid.UUID, KnownLocation] = {}
    if loc_ids:
        locs = {
            loc.id: loc
            for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(loc_ids))).scalars()
        }
    kinds: dict[uuid.UUID, list[str]] = defaultdict(list)
    for trip_id, kind in db.execute(
        select(ReviewItem.logical_trip_id, ReviewItem.kind).where(
            ReviewItem.logical_trip_id.in_(ids), ReviewItem.status == ReviewState.OPEN
        )
    ).all():
        kinds[trip_id].append(kind)
    matched = {
        tid
        for (tid,) in db.execute(
            select(CalendarMatch.logical_trip_id).where(
                CalendarMatch.logical_trip_id.in_(ids), CalendarMatch.is_selected.is_(True)
            )
        ).all()
    }
    out: list[TripOut] = []
    for t in trips:
        item = TripOut.model_validate(t)
        s, e = locs.get(t.start_location_id), locs.get(t.end_location_id)  # type: ignore[arg-type]
        item.start_location = LocationBrief.model_validate(s) if s else None
        item.end_location = LocationBrief.model_validate(e) if e else None
        item.open_review_kinds = sorted(kinds.get(t.id, []))
        item.has_calendar_match = t.id in matched
        out.append(item)
    return out


def trip_out(db: Session, trip: LogicalTrip) -> TripOut:
    return trips_out(db, [trip])[0]


def location_out(loc: KnownLocation, stats: dict | None = None) -> LocationOut:
    out = LocationOut.model_validate(
        {
            **{c.name: getattr(loc, c.name) for c in loc.__table__.columns},
            "aliases": [a.raw_address for a in loc.aliases],
            "stats": stats or {},
        }
    )
    return out
