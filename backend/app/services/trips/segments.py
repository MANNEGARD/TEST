"""Uppbyggnad av journalresor (LogicalTrip) ur fysiska resor (RawTrip).

All kilometerredovisning sker per segment, så att en körsträcka aldrig kan
dubbelräknas och blandad privat/tjänst kan fördelas korrekt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import LogicalTrip, RawTrip, TripSegment
from app.models.enums import Category, LogicalState, ReviewStatus, TimeQuality, TripSource

KM_EPS = 0.05


class TripError(Exception):
    """Affärsregelfel med svenskt meddelande som kan visas för användaren."""

    def __init__(self, message: str, code: str = "trip_error", data: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}


def raw_sort_time(raw: RawTrip) -> datetime | None:
    return raw.started_at or raw.start_latest or raw.start_earliest or raw.ended_at


def set_raw_sort_time(raw: RawTrip) -> None:
    raw.sort_time = raw_sort_time(raw)


def load_trip(db: Session, trip_id: uuid.UUID) -> LogicalTrip | None:
    return db.execute(
        select(LogicalTrip)
        .where(LogicalTrip.id == trip_id)
        .options(selectinload(LogicalTrip.segments).selectinload(TripSegment.raw_trip))
    ).scalar_one_or_none()


def ordered_raws(trip: LogicalTrip) -> list[RawTrip]:
    """Ingående RawTrips i ordning, utan dubbletter (en RawTrip kan ha flera delar)."""
    seen: set[int] = set()
    out: list[RawTrip] = []
    for seg in sorted(trip.segments, key=lambda s: (s.seq, s.part)):
        # Jämför objektidentitet: raw_trip_id är inte satt förrän segmentet har sparats.
        if id(seg.raw_trip) not in seen:
            seen.add(id(seg.raw_trip))
            out.append(seg.raw_trip)
    return out


def segment_category(trip: LogicalTrip, seg: TripSegment) -> str:
    return seg.category_override or trip.category


def _sum_quality(values: Sequence[tuple[float | None, str]]) -> tuple[float | None, str]:
    """Summerar mätvärden och anger kvalitet: verified, estimated, partial eller unknown."""
    known = [(v, q) for v, q in values if v is not None and q != "unknown"]
    if not known:
        return None, "unknown"
    total = round(sum(v for v, _ in known), 3)
    if len(known) < len(values):
        return total, "partial"
    if any(q == "estimated" for _, q in known):
        return total, "estimated"
    return total, "verified"


def refresh_logical(db: Session, trip: LogicalTrip) -> LogicalTrip:
    """Räknar om alla cachefält på journalresan ur dess segment."""
    raws = ordered_raws(trip)
    if not raws:
        trip.distance_km = 0.0
        trip.segment_count = 0
        return trip
    first, last = raws[0], raws[-1]
    trip.started_at = first.started_at
    trip.ended_at = last.ended_at
    trip.start_time_quality = first.start_time_quality
    trip.end_time_quality = last.end_time_quality
    trip.sort_time = raw_sort_time(first)
    trip.odometer_start = first.odometer_start
    trip.odometer_end = last.odometer_end
    trip.start_address = first.start_address
    trip.end_address = last.end_address
    trip.start_location_id = first.start_location_id
    trip.end_location_id = last.end_location_id
    trip.segment_count = len(raws)

    work = private = undecided = 0.0
    for seg in trip.segments:
        cat = segment_category(trip, seg)
        if cat == Category.WORK:
            work += seg.distance_km
        elif cat == Category.PRIVATE:
            private += seg.distance_km
        else:
            undecided += seg.distance_km
    trip.work_km, trip.private_km, trip.undecided_km = round(work, 3), round(private, 3), round(undecided, 3)
    trip.distance_km = round(work + private + undecided, 3)
    trip.has_mixed_categories = work > KM_EPS and private > KM_EPS

    durations = [r.duration_seconds for r in raws]
    trip.driving_seconds = sum(d for d in durations if d is not None) if all(d is not None for d in durations) else None
    trip.fuel_liters, trip.fuel_quality = _sum_quality([(r.fuel_liters, r.fuel_quality) for r in raws])
    trip.energy_kwh, trip.energy_quality = _sum_quality([(r.energy_kwh, r.energy_quality) for r in raws])
    regen = [r.regen_kwh for r in raws if r.regen_kwh is not None]
    trip.regen_kwh = round(sum(regen), 3) if regen else None

    via: list[dict[str, Any]] = []
    gap_km = 0.0
    for prev, nxt in zip(raws, raws[1:], strict=False):
        stop_minutes = None
        if prev.ended_at and nxt.started_at:
            stop_minutes = round((nxt.started_at - prev.ended_at).total_seconds() / 60, 1)
        via.append(
            {
                "address": prev.end_address,
                "location_id": str(prev.end_location_id) if prev.end_location_id else None,
                "arrived_at": prev.ended_at.isoformat() if prev.ended_at else None,
                "departed_at": nxt.started_at.isoformat() if nxt.started_at else None,
                "stop_minutes": stop_minutes,
                "odometer": prev.odometer_end,
            }
        )
        if prev.odometer_end is not None and nxt.odometer_start is not None:
            diff = nxt.odometer_start - prev.odometer_end
            if diff > 0:
                gap_km += diff
    trip.via = via
    trip.gap_km = round(gap_km, 1)

    sources = {r.source for r in raws}
    trip.source = next(iter(sources)) if len(sources) == 1 else "mixed"
    flags: set[str] = set()
    uncertain = (TimeQuality.BOUNDED, TimeQuality.UNKNOWN)
    for i, r in enumerate(raws):
        flags.update(r.quality_flags or [])
        if r.possible_missed_trips:
            flags.add("possible_missed_trips")
        # Resans start/slut är osäker bara om första/sista delresan är det;
        # osäkra tider vid mellanstopp flaggas separat.
        if r.start_time_quality in uncertain:
            flags.add("start_time_uncertain" if i == 0 else "stop_times_uncertain")
        if r.end_time_quality in uncertain:
            flags.add("end_time_uncertain" if i == len(raws) - 1 else "stop_times_uncertain")
        if r.odometer_start is None or r.odometer_end is None:
            flags.add("missing_odometer")
    trip.quality_flags = sorted(flags)
    trip.is_complete = all(r.is_complete for r in raws) and "missing_odometer" not in flags
    return trip


def create_logical_for_raw(
    db: Session,
    raw: RawTrip,
    *,
    category: str = Category.UNDECIDED,
    category_source: str = "system",
    purpose: str | None = None,
    purpose_source: str | None = None,
    purpose_confirmed: bool = False,
    review_status: str = ReviewStatus.NEEDS_REVIEW,
    driver_id: uuid.UUID | None = None,
    actor: str = "system",
) -> LogicalTrip:
    trip = LogicalTrip(
        vehicle_id=raw.vehicle_id,
        driver_id=driver_id,
        state=LogicalState.ACTIVE,
        category=category,
        category_source=category_source,
        purpose=purpose,
        purpose_source=purpose_source,
        purpose_confirmed=purpose_confirmed,
        review_status=review_status,
        updated_by=actor,
    )
    trip.segments.append(TripSegment(raw_trip=raw, seq=0, part=0, distance_km=raw.distance_km))
    db.add(trip)
    refresh_logical(db, trip)
    db.flush()
    return trip


def active_trips_query(vehicle_id: uuid.UUID):
    return (
        select(LogicalTrip)
        .where(LogicalTrip.vehicle_id == vehicle_id, LogicalTrip.state == LogicalState.ACTIVE)
        .options(selectinload(LogicalTrip.segments).selectinload(TripSegment.raw_trip))
        .order_by(LogicalTrip.sort_time.asc().nulls_last(), LogicalTrip.odometer_start.asc().nulls_last())
    )


def active_trips(db: Session, vehicle_id: uuid.UUID) -> list[LogicalTrip]:
    return list(db.execute(active_trips_query(vehicle_id)).scalars())


def allocate_raw_trip(
    db: Session,
    trip: LogicalTrip,
    raw_trip_id: uuid.UUID,
    parts: Iterable[dict[str, Any]],
    actor: str,
) -> LogicalTrip:
    """Fördelar en fysisk resa på delsträckor med egna kategorier (privat/tjänst).

    Summan av delarna måste motsvara den uppmätta sträckan – kilometer kan varken
    försvinna eller uppstå.
    """
    parts = list(parts)
    existing = [s for s in trip.segments if s.raw_trip_id == raw_trip_id]
    if not existing:
        raise TripError("Delresan ingår inte i den här resan.", "segment_not_found")
    raw = existing[0].raw_trip
    if not parts:
        raise TripError("Minst en delsträcka krävs.", "invalid_allocation")
    total = 0.0
    for p in parts:
        km = float(p.get("distance_km", 0))
        if km <= 0:
            raise TripError("Varje delsträcka måste vara större än 0 km.", "invalid_allocation")
        if p.get("category") not in (Category.WORK, Category.PRIVATE, Category.UNDECIDED):
            raise TripError("Ogiltig kategori för delsträcka.", "invalid_allocation")
        total += km
    if abs(total - raw.distance_km) > KM_EPS:
        raise TripError(
            f"Delsträckorna summerar till {total:.1f} km men resan är {raw.distance_km:.1f} km.",
            "allocation_sum_mismatch",
            {"expected_km": raw.distance_km, "got_km": round(total, 3)},
        )
    seq = existing[0].seq
    for seg in existing:
        trip.segments.remove(seg)
    db.flush()
    for i, p in enumerate(parts):
        trip.segments.append(
            TripSegment(
                raw_trip=raw,
                seq=seq,
                part=i,
                distance_km=round(float(p["distance_km"]), 3),
                category_override=p["category"],
                note=p.get("note"),
            )
        )
    trip.updated_by = actor
    refresh_logical(db, trip)
    db.flush()
    return trip


def clear_allocation(db: Session, trip: LogicalTrip, raw_trip_id: uuid.UUID, actor: str) -> LogicalTrip:
    existing = [s for s in trip.segments if s.raw_trip_id == raw_trip_id]
    if not existing:
        raise TripError("Delresan ingår inte i den här resan.", "segment_not_found")
    raw, seq = existing[0].raw_trip, existing[0].seq
    for seg in existing:
        trip.segments.remove(seg)
    db.flush()
    trip.segments.append(TripSegment(raw_trip=raw, seq=seq, part=0, distance_km=raw.distance_km))
    trip.updated_by = actor
    refresh_logical(db, trip)
    db.flush()
    return trip


def is_mergeable_source(raw: RawTrip) -> bool:
    """Sammanfattade luckor (gap_fill) är inga fysiska resor och slås aldrig ihop."""
    return raw.source != TripSource.GAP_FILL and raw.voided_at is None
