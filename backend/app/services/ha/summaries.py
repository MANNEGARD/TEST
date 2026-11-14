"""Sammanställningar ur faktisk lagrad data (för notiser och HA-sensorer)."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DataGap, KnownLocation, LogicalTrip, ReviewItem, Vehicle
from app.models.enums import GapStatus, LogicalState, ReviewState, ReviewStatus, TripSource

_NEEDS_REVIEW = (ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED)
_OPEN_ITEM = (ReviewState.OPEN, ReviewState.SNOOZED)


def real_vehicle_ids(db: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Användarens riktiga fordon – demodata räknas aldrig in i sammanställningar."""
    return [
        v
        for (v,) in db.execute(
            select(Vehicle.id).where(
                Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(False), Vehicle.is_active.is_(True)
            )
        ).all()
    ]


def open_review_count(db: Session, vehicle_ids: list[uuid.UUID]) -> int:
    if not vehicle_ids:
        return 0
    return (
        db.scalar(
            select(func.count())
            .select_from(ReviewItem)
            .where(ReviewItem.vehicle_id.in_(vehicle_ids), ReviewItem.status.in_(_OPEN_ITEM))
        )
        or 0
    )


def period_summary(
    db: Session, user_id: uuid.UUID, start: datetime, end: datetime, *, top_destinations: int = 3
) -> dict[str, Any]:
    """Summerar aktiva journalresor med start i [start, end). Kilometer räknas per
    segmentkategori (work_km/private_km/undecided_km) – inget dubbelräknas."""
    from app.services.ha.messages import safe_label

    vehicle_ids = real_vehicle_ids(db, user_id)
    out: dict[str, Any] = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "trips": 0,
        "work_km": 0.0,
        "private_km": 0.0,
        "undecided_km": 0.0,
        "total_km": 0.0,
        "review_count": 0,
        "open_review_total": 0,
        "fuel_liters": None,
        "energy_kwh": None,
        "top_destinations": [],
        "open_gaps": 0,
    }
    if not vehicle_ids:
        return out
    trips = list(
        db.execute(
            select(LogicalTrip).where(
                LogicalTrip.vehicle_id.in_(vehicle_ids),
                LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.sort_time >= start,
                LogicalTrip.sort_time < end,
            )
        ).scalars()
    )
    physical = [t for t in trips if t.source != TripSource.GAP_FILL]
    out["trips"] = len(physical)
    out["work_km"] = round(sum(t.work_km for t in trips), 1)
    out["private_km"] = round(sum(t.private_km for t in trips), 1)
    out["undecided_km"] = round(sum(t.undecided_km for t in trips), 1)
    out["total_km"] = round(out["work_km"] + out["private_km"] + out["undecided_km"], 1)
    fuel = [t.fuel_liters for t in physical if t.fuel_liters is not None]
    energy = [t.energy_kwh for t in physical if t.energy_kwh is not None]
    out["fuel_liters"] = round(sum(fuel), 1) if fuel else None
    out["energy_kwh"] = round(sum(energy), 1) if energy else None

    trip_ids = {t.id for t in physical}
    needing = {t.id for t in physical if t.review_status in _NEEDS_REVIEW}
    if trip_ids:
        needing |= {
            tid
            for (tid,) in db.execute(
                select(ReviewItem.logical_trip_id).where(
                    ReviewItem.logical_trip_id.in_(trip_ids), ReviewItem.status.in_(_OPEN_ITEM)
                )
            ).all()
            if tid is not None
        }
    out["review_count"] = len(needing)
    out["open_review_total"] = open_review_count(db, vehicle_ids)

    dest_counts = Counter(t.end_location_id for t in physical if t.end_location_id is not None)
    for loc_id, n in dest_counts.most_common(top_destinations):
        loc = db.get(KnownLocation, loc_id)
        out["top_destinations"].append({"label": safe_label(loc, loc.address if loc else None), "trips": n})
    out["open_gaps"] = (
        db.scalar(
            select(func.count())
            .select_from(DataGap)
            .where(
                DataGap.vehicle_id.in_(vehicle_ids),
                DataGap.is_current.is_(True),
                DataGap.status == GapStatus.OPEN,
            )
        )
        or 0
    )
    return out
