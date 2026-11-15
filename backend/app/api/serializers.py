"""Bygger svarsmodeller ur databasobjekt (med batchade uppslag)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CalendarMatch, KnownLocation, LogicalTrip, RawTrip, ReviewItem, TripSegment, Vehicle
from app.models.enums import ReviewState, TimeQuality, TripSource
from app.schemas.common import LocationBrief, LocationOut, TripOut, VehicleOut
from app.services import settings as settings_svc
from app.services import vehicles as vehicles_svc
from app.services.trips import start_time

_UNCERTAIN = (TimeQuality.BOUNDED, TimeQuality.UNKNOWN)
_USER_SOURCES = (TripSource.MANUAL, TripSource.GAP_FILL)


def start_time_source(trip: LogicalTrip) -> str:
    """car | estimated | user | unknown (TripOut.start_time_source)."""
    if trip.start_time_override is not None:
        return "user"
    if trip.started_at is None or trip.start_time_quality in _UNCERTAIN:
        return "unknown"
    if trip.start_time_quality == TimeQuality.ESTIMATED:
        return "estimated"
    if trip.source in _USER_SOURCES:
        return "user"
    return "car"


def approval_reason(trip: LogicalTrip) -> str | None:
    basis = trip.approval_basis if isinstance(trip.approval_basis, dict) else {}
    reason = basis.get("reason")
    return reason if isinstance(reason, str) and reason else None


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
    # Möjligt startintervall från första delresan när starttiden är okänd/begränsad, och
    # grunden för en uträknad starttid.
    uncertain = {t.id for t in trips if t.started_at is None or t.start_time_quality in _UNCERTAIN}
    estimated = {t.id for t in trips if t.start_time_quality == TimeQuality.ESTIMATED}
    bounds: dict[uuid.UUID, tuple] = {}
    bases: dict[uuid.UUID, dict | None] = {}
    if uncertain or estimated:
        rows = db.execute(
            select(TripSegment.logical_trip_id, RawTrip.start_earliest, RawTrip.start_latest,
                   RawTrip.start_estimate_basis)
            .join(RawTrip, RawTrip.id == TripSegment.raw_trip_id)
            .where(TripSegment.logical_trip_id.in_(uncertain | estimated))
            .order_by(TripSegment.logical_trip_id, TripSegment.seq, TripSegment.part)
        ).all()
        for tid, lo, hi, basis in rows:
            if tid in uncertain:
                bounds.setdefault(tid, (lo, hi))
            if tid in estimated:
                bases.setdefault(tid, basis if isinstance(basis, dict) else None)
    # Tidszon per fordonsägare för den färdiga starttidstexten.
    zones: dict[uuid.UUID, object] = {}
    owners = dict(
        db.execute(select(Vehicle.id, Vehicle.owner_user_id).where(Vehicle.id.in_({t.vehicle_id for t in trips}))).all()
    )
    out: list[TripOut] = []
    for t in trips:
        item = TripOut.model_validate(t)
        s, e = locs.get(t.start_location_id), locs.get(t.end_location_id)  # type: ignore[arg-type]
        item.start_location = LocationBrief.model_validate(s) if s else None
        item.end_location = LocationBrief.model_validate(e) if e else None
        item.open_review_kinds = sorted(kinds.get(t.id, []))
        item.has_calendar_match = t.id in matched
        item.start_earliest, item.start_latest = bounds.get(t.id, (None, None))
        owner = owners.get(t.vehicle_id)
        if owner is not None and owner not in zones:
            zones[owner] = settings_svc.user_timezone(db, owner)
        tz = zones.get(owner) if owner is not None else None
        item.start_time_source = start_time_source(t)  # type: ignore[assignment]
        item.start_time_text = start_time.display(t, tz) if tz is not None else ""  # type: ignore[arg-type]
        item.start_estimate_basis = bases.get(t.id)
        item.approval_reason = approval_reason(t)
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
