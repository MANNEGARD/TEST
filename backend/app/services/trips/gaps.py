"""Dataluckor: kilometer i mätarställningen som saknar redovisad resa.

Luckor beräknas ur de fysiska resorna (RawTrip) och är oberoende av hur resorna
grupperats i journalen – en sammanslagning kan därför aldrig dölja en lucka.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import DataGap, LogicalTrip, RawTrip, TripSegment, Vehicle
from app.models.enums import (
    Category,
    DecisionSource,
    DistanceQuality,
    GapKind,
    GapStatus,
    LogicalState,
    PositionSource,
    ReviewKind,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.services import audit
from app.services.audit import Actor
from app.services.trips import review
from app.services.trips import segments as seg
from app.services.trips.segments import TripError

GAP_FILL_PURPOSE = "Privat körning (sammanfattad, ej detaljredovisad)"


def recompute(db: Session, vehicle_id: uuid.UUID, tolerance_km: float = 2.0) -> dict[str, Any]:
    """Räknar om odometerluckor för ett fordon. Idempotent; tidigare beslut bevaras."""
    raws = list(
        db.execute(
            select(RawTrip)
            .where(
                RawTrip.vehicle_id == vehicle_id,
                RawTrip.voided_at.is_(None),
                # Sammanfattade luckor är inga fysiska resor; luckan ska finnas kvar med sin status.
                RawTrip.source != TripSource.GAP_FILL,
                RawTrip.odometer_start.is_not(None),
                RawTrip.odometer_end.is_not(None),
            )
            .order_by(RawTrip.odometer_start.asc(), RawTrip.odometer_end.asc())
        ).scalars()
    )
    existing = {
        g.dedupe_key: g
        for g in db.execute(
            select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.kind == GapKind.ODOMETER)
        ).scalars()
    }
    seen: set[str] = set()
    overlaps = 0
    for a, b in zip(raws, raws[1:], strict=False):
        diff = b.odometer_start - a.odometer_end  # type: ignore[operator]
        if diff > tolerance_km:
            key = f"odo:{a.odometer_end:.0f}:{b.odometer_start:.0f}"
            seen.add(key)
            gap = existing.get(key)
            if gap is None:
                gap = DataGap(vehicle_id=vehicle_id, kind=GapKind.ODOMETER, dedupe_key=key)
                db.add(gap)
            gap.odometer_start, gap.odometer_end = a.odometer_end, b.odometer_start
            gap.km = round(diff, 1)
            gap.from_time = a.ended_at
            gap.to_time = b.started_at or b.start_earliest
            gap.before_raw_trip_id, gap.after_raw_trip_id = a.id, b.id
            gap.is_current = True
        elif diff < -tolerance_km:
            overlaps += 1
            review.upsert(
                db,
                vehicle_id=vehicle_id,
                kind=ReviewKind.ANOMALY,
                dedupe_key=f"odo-overlap:{a.id}:{b.id}",
                title="Motstridiga mätarställningar",
                detail=(
                    f"Två resor överlappar i mätarställning ({a.odometer_end:.0f} km respektive "
                    f"{b.odometer_start:.0f} km). Det kan vara en dubblett eller ett avläsningsfel."
                ),
                data={"type": "odometer_overlap", "first_raw_trip_id": str(a.id), "second_raw_trip_id": str(b.id)},
            )
    for key, gap in existing.items():
        if key not in seen:
            gap.is_current = False
    db.flush()
    current = [g for g in db.execute(
        select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.is_current.is_(True))
    ).scalars()]
    open_gaps = [g for g in current if g.status == GapStatus.OPEN]
    return {
        "gaps": len(current),
        "open_gaps": len(open_gaps),
        "open_gap_km": round(sum(g.km for g in open_gaps), 1),
        "odometer_overlaps": overlaps,
    }


def list_gaps(db: Session, vehicle_id: uuid.UUID, *, only_open: bool = False) -> list[DataGap]:
    q = select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.is_current.is_(True))
    if only_open:
        q = q.where(DataGap.status == GapStatus.OPEN)
    return list(db.execute(q.order_by(DataGap.odometer_start.asc().nulls_last())).scalars())


def resolve_as_private(db: Session, gap: DataGap, *, actor: Actor, note: str | None = None) -> LogicalTrip:
    """Användaren intygar att luckan var privat körning. En sammanfattande privat post
    skapas så att mätarställningarna går att stämma av. Starttid hittas inte på."""
    if gap.status != GapStatus.OPEN:
        raise TripError("Luckan är redan hanterad.", "gap_not_open")
    if gap.kind != GapKind.ODOMETER or gap.odometer_start is None or gap.odometer_end is None:
        raise TripError("Endast odometerluckor kan sammanfattas som privat körning.", "gap_kind")
    vehicle = db.get(Vehicle, gap.vehicle_id)
    assert vehicle is not None
    raw = RawTrip(
        vehicle_id=gap.vehicle_id,
        source=TripSource.GAP_FILL,
        source_key=f"gapfill:{gap.dedupe_key}",
        started_at=None,
        ended_at=None,
        start_time_quality=TimeQuality.BOUNDED,
        end_time_quality=TimeQuality.BOUNDED,
        start_earliest=gap.from_time,
        start_latest=gap.to_time,
        odometer_start=gap.odometer_start,
        odometer_end=gap.odometer_end,
        odometer_source="derived",
        distance_km=gap.km,
        distance_quality=DistanceQuality.ODOMETER,
        position_source=PositionSource.NONE,
        fuel_quality=ValueQuality.UNKNOWN,
        energy_quality=ValueQuality.UNKNOWN,
        is_complete=False,
        quality_flags=["summary_of_gap"],
        updated_by=actor.label,
    )
    raw.sort_time = gap.from_time or gap.to_time
    db.add(raw)
    db.flush()
    trip = seg.create_logical_for_raw(
        db,
        raw,
        category=Category.PRIVATE,
        category_source=DecisionSource.USER,
        purpose=GAP_FILL_PURPOSE,
        purpose_source=DecisionSource.USER,
        purpose_confirmed=True,
        review_status=ReviewStatus.APPROVED,
        actor=actor.label,
    )
    trip.notes = note
    gap.status = GapStatus.RESOLVED_PRIVATE
    gap.resolution_note = note
    gap.resolved_raw_trip_id = raw.id
    gap.resolved_by = actor.label
    gap.resolved_at = utcnow()
    audit.record(db, actor, "gap.resolve_private", "data_gap", gap.id, after={"km": gap.km, "trip_id": str(trip.id)})
    db.flush()
    return trip


def ignore(db: Session, gap: DataGap, *, actor: Actor, note: str | None = None) -> DataGap:
    """Markerar luckan som medvetet lämnad. Den redovisas fortsatt som oavstämd i exporten."""
    if gap.status != GapStatus.OPEN:
        raise TripError("Luckan är redan hanterad.", "gap_not_open")
    gap.status = GapStatus.IGNORED
    gap.resolution_note = note
    gap.resolved_by = actor.label
    gap.resolved_at = utcnow()
    audit.record(db, actor, "gap.ignore", "data_gap", gap.id, after={"km": gap.km})
    db.flush()
    return gap


def reopen(db: Session, gap: DataGap, *, actor: Actor) -> DataGap:
    """Ångrar hanteringen av en lucka; en ev. sammanfattande privatpost makuleras."""
    if gap.status == GapStatus.OPEN:
        return gap
    if gap.resolved_raw_trip_id is not None:
        raw = db.get(RawTrip, gap.resolved_raw_trip_id)
        if raw is not None and raw.voided_at is None:
            raw.voided_at = utcnow()
            raw.void_reason = "Lucka återöppnad"
            for trip in db.execute(
                select(LogicalTrip)
                .join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
                .where(LogicalTrip.state == LogicalState.ACTIVE, TripSegment.raw_trip_id == raw.id)
            ).scalars().unique():
                trip.state = LogicalState.VOIDED
                trip.updated_by = actor.label
    gap.status = GapStatus.OPEN
    gap.resolution_note = None
    gap.resolved_raw_trip_id = None
    gap.resolved_by = None
    gap.resolved_at = None
    audit.record(db, actor, "gap.reopen", "data_gap", gap.id)
    db.flush()
    return gap
