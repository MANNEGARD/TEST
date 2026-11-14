"""Sammanslagning av resor enligt regel A, B och C.

* Regel A – stillestånd ≤ ``auto_max_minutes`` (standard 15): automatisk sammanslagning.
* Regel B – stillestånd > A-gränsen och ≤ ``ask_max_minutes`` (standard 60): fråga.
* Regel C – längre stillestånd: endast manuell sammanslagning.

Originalresorna (RawTrip) ändras aldrig. En sammanslagning skapar en ny LogicalTrip
vars segment pekar på samma RawTrips; källresorna markeras ``merged`` och kan
återaktiveras. Privat körsträcka omklassas aldrig till tjänst av en sammanslagning,
och en odometerlucka mellan delresor döljs aldrig.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, MergeDecision, RawTrip, TripMerge, TripSegment
from app.models.enums import Category, LogicalState, MergeKind, ReviewKind, ReviewStatus, TimeQuality
from app.services import audit
from app.services.audit import Actor
from app.services.trips import review
from app.services.trips import segments as seg
from app.services.trips.segments import TripError

_REVIEW_ORDER = {
    ReviewStatus.NEEDS_REVIEW: 0,
    ReviewStatus.PROPOSED: 1,
    ReviewStatus.AUTO: 2,
    ReviewStatus.IMPORTED: 3,
    ReviewStatus.APPROVED: 4,
}
_EXACT = {TimeQuality.EXACT, TimeQuality.REPORTED}


@dataclass(frozen=True)
class StopGap:
    """Stillestånd mellan två på varandra följande fysiska resor."""

    minutes_low: float | None  # None = okänt
    minutes_high: float | None
    exact: bool
    odometer_gap_km: float | None
    overlap: bool = False

    @property
    def known(self) -> bool:
        return self.minutes_low is not None

    @property
    def label(self) -> str:
        if self.minutes_low is None:
            return "okänt stillestånd"
        if self.exact or self.minutes_high == self.minutes_low:
            return f"{round(self.minutes_low)} minuter"
        if self.minutes_high is None:
            return f"minst {round(self.minutes_low)} minuter"
        return f"mellan {round(self.minutes_low)} och {round(self.minutes_high)} minuter"


def stop_gap(prev: RawTrip, nxt: RawTrip) -> StopGap:
    odo_gap = None
    if prev.odometer_end is not None and nxt.odometer_start is not None:
        odo_gap = nxt.odometer_start - prev.odometer_end
    if prev.ended_at is None:
        return StopGap(None, None, False, odo_gap)
    if nxt.started_at is not None:
        minutes = (nxt.started_at - prev.ended_at).total_seconds() / 60
        exact = prev.end_time_quality in _EXACT and nxt.start_time_quality in _EXACT
        if minutes < -1:
            return StopGap(minutes, minutes, exact, odo_gap, overlap=True)
        minutes = max(0.0, minutes)
        return StopGap(minutes, minutes, exact, odo_gap)
    # Faktisk starttid saknas: använd endast kända gränser, hitta inte på något.
    lo, hi = nxt.start_earliest, nxt.start_latest
    if lo is None and hi is None:
        return StopGap(None, None, False, odo_gap)
    low = max(0.0, (lo - prev.ended_at).total_seconds() / 60) if lo else 0.0
    high = max(0.0, (hi - prev.ended_at).total_seconds() / 60) if hi else None
    return StopGap(low, high, False, odo_gap)


def decide(gap: StopGap, *, auto_max: float, ask_max: float, odometer_tolerance: float) -> str:
    """Returnerar 'auto', 'ask', 'none', 'overlap' eller 'odometer_gap'."""
    if gap.overlap:
        return "overlap"
    if gap.odometer_gap_km is not None:
        if gap.odometer_gap_km > odometer_tolerance:
            return "odometer_gap"  # körning saknas mellan resorna – ingen sammanhängande resa
        if gap.odometer_gap_km < -odometer_tolerance:
            return "overlap"
    if not gap.known:
        return "none"
    low, high = gap.minutes_low, gap.minutes_high
    assert low is not None
    if high is not None and high <= auto_max:
        return "auto"
    if low > ask_max:
        return "none"
    return "ask"


# ----------------------------------------------------------------------- hjälpfunktioner


def _edge_raws(trip: LogicalTrip) -> tuple[RawTrip, RawTrip]:
    raws = seg.ordered_raws(trip)
    return raws[0], raws[-1]


def _decision_for(db: Session, first_raw: uuid.UUID, second_raw: uuid.UUID) -> MergeDecision | None:
    return db.execute(
        select(MergeDecision).where(
            MergeDecision.first_raw_trip_id == first_raw, MergeDecision.second_raw_trip_id == second_raw
        )
    ).scalar_one_or_none()


def remember_decision(
    db: Session, vehicle_id: uuid.UUID, first_raw: uuid.UUID, second_raw: uuid.UUID, decision: str, by: str
) -> None:
    row = _decision_for(db, first_raw, second_raw)
    if row is None:
        db.add(
            MergeDecision(
                vehicle_id=vehicle_id,
                first_raw_trip_id=first_raw,
                second_raw_trip_id=second_raw,
                decision=decision,
                decided_by=by,
            )
        )
    else:
        row.decision, row.decided_by, row.decided_at = decision, by, utcnow()
    db.flush()


def _combined_text(values: list[str | None], sep: str = "; ") -> str | None:
    out: list[str] = []
    for v in values:
        if v and v.strip() and v not in out:
            out.append(v)
    return sep.join(out) if out else None


def _mergeable(trip: LogicalTrip) -> bool:
    return all(seg.is_mergeable_source(r) for r in seg.ordered_raws(trip))


# ----------------------------------------------------------------------- sammanslagning


def merge_trips(
    db: Session,
    trips: list[LogicalTrip],
    *,
    trigger: str,
    actor: Actor,
    acknowledge_gap: bool = False,
    odometer_tolerance: float = 2.0,
) -> LogicalTrip:
    """Slår samman två eller flera på varandra följande journalresor.

    ``trigger``: rule_a | rule_b | manual | ha
    """
    if len(trips) < 2:
        raise TripError("Välj minst två resor att slå samman.", "too_few_trips")
    vehicle_ids = {t.vehicle_id for t in trips}
    if len(vehicle_ids) != 1:
        raise TripError("Resorna tillhör olika fordon.", "different_vehicles")
    if any(t.state != LogicalState.ACTIVE for t in trips):
        raise TripError("En eller flera resor är inte längre aktiva.", "not_active")
    if len({t.id for t in trips}) != len(trips):
        raise TripError("Samma resa har valts flera gånger.", "duplicate_trip")
    if not all(_mergeable(t) for t in trips):
        raise TripError("Sammanfattade luckor kan inte slås samman med resor.", "not_mergeable")
    if any(t.sort_time is None for t in trips):
        raise TripError("Resor utan känd tidpunkt kan inte slås samman.", "missing_time")

    trips = sorted(trips, key=lambda t: (t.sort_time, t.odometer_start or 0))
    vehicle_id = trips[0].vehicle_id

    # Resorna måste ligga i följd – inga andra aktiva resor får finnas emellan.
    active = seg.active_trips(db, vehicle_id)
    order = {t.id: i for i, t in enumerate(active)}
    idx = [order.get(t.id) for t in trips]
    if any(i is None for i in idx) or any(b - a != 1 for a, b in zip(idx, idx[1:], strict=False)):  # type: ignore[operator]
        raise TripError(
            "Resorna ligger inte i direkt följd. Det finns andra resor emellan som också måste väljas.",
            "not_adjacent",
        )

    gaps: list[dict[str, Any]] = []
    gap_km_total = 0.0
    for a, b in zip(trips, trips[1:], strict=False):
        g = stop_gap(_edge_raws(a)[1], _edge_raws(b)[0])
        if g.overlap:
            raise TripError("Resorna överlappar varandra i tid eller mätarställning.", "overlap")
        if g.odometer_gap_km is not None and g.odometer_gap_km > odometer_tolerance:
            gap_km_total += g.odometer_gap_km
        gaps.append(
            {
                "minutes_low": g.minutes_low,
                "minutes_high": g.minutes_high,
                "exact": g.exact,
                "odometer_gap_km": g.odometer_gap_km,
            }
        )
    if gap_km_total > 0 and not acknowledge_gap:
        raise TripError(
            f"Det saknas {gap_km_total:.0f} km i mätarställningen mellan resorna. "
            "Sammanslagningen döljer inte luckan – bekräfta att du ändå vill slå samman.",
            "gap_requires_ack",
            {"gap_km": round(gap_km_total, 1)},
        )

    categories = {t.category for t in trips}
    any_override = any(s.category_override for t in trips for s in t.segments)
    uniform = len(categories) == 1 and not any_override

    merged = LogicalTrip(
        vehicle_id=vehicle_id,
        driver_id=trips[0].driver_id,
        state=LogicalState.ACTIVE,
        merge_kind={"rule_a": MergeKind.AUTO, "rule_b": MergeKind.PROMPTED, "ha": MergeKind.PROMPTED}.get(
            trigger, MergeKind.MANUAL
        ),
        updated_by=actor.label,
    )
    seq = 0
    for t in trips:
        by_raw: dict[uuid.UUID, list[TripSegment]] = {}
        for s in sorted(t.segments, key=lambda s: (s.seq, s.part)):
            by_raw.setdefault(s.raw_trip_id, []).append(s)
        for parts in by_raw.values():
            for s in parts:
                merged.segments.append(
                    TripSegment(
                        raw_trip=s.raw_trip,
                        seq=seq,
                        part=s.part,
                        distance_km=s.distance_km,
                        # Bevara varje delsträckas egen kategori när källorna skiljer sig åt.
                        category_override=None if uniform else (s.category_override or t.category),
                        note=s.note,
                    )
                )
            seq += 1

    if uniform:
        merged.category = trips[0].category
        merged.subcategory = _combined_single([t.subcategory for t in trips])
        merged.category_source = _combined_single([t.category_source for t in trips]) or "system"
        merged.review_status = min((t.review_status for t in trips), key=lambda s: _REVIEW_ORDER.get(s, 0))
        confidences = [t.confidence for t in trips if t.confidence is not None]
        merged.confidence = min(confidences) if confidences else None
    else:
        # Motstridiga kategorier: gruppera men kräv manuell fördelning. Ingenting omklassas.
        merged.category = Category.UNDECIDED
        merged.category_source = "system"
        merged.review_status = ReviewStatus.NEEDS_REVIEW
        merged.confidence = None

    purposes = [t.purpose for t in trips]
    merged.purpose = _combined_text(purposes)
    distinct_sources = {t.purpose_source for t in trips if t.purpose}
    merged.purpose_source = next(iter(distinct_sources)) if len(distinct_sources) == 1 else ("merged" if distinct_sources else None)
    merged.purpose_confirmed = bool(merged.purpose) and all(t.purpose_confirmed for t in trips if t.purpose)
    merged.contacts = _combined_text([t.contacts for t in trips])
    merged.notes = _combined_text([t.notes for t in trips], sep="\n")
    merged.tax_note = _combined_text([t.tax_note for t in trips], sep=" ")
    merged.is_favorite = any(t.is_favorite for t in trips)
    journeys = {t.journey_id for t in trips if t.journey_id}
    merged.journey_id = next(iter(journeys)) if len(journeys) == 1 else None

    db.add(merged)
    seg.refresh_logical(db, merged)
    merged.has_gap = merged.gap_km > odometer_tolerance
    db.flush()

    for t in trips:
        t.state = LogicalState.MERGED
        t.merged_into_id = merged.id
        t.updated_by = actor.label
        review.resolve_for_trip(
            db,
            t.id,
            (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION),
            by=actor.label,
            resolution={"superseded_by": str(merged.id)},
        )
    for a, b in zip(trips, trips[1:], strict=False):
        first_raw, second_raw = _edge_raws(a)[1], _edge_raws(b)[0]
        review.resolve_by_key(
            db,
            vehicle_id,
            review.merge_key(first_raw.id, second_raw.id),
            by=actor.label,
            resolution={"decision": "merged", "trip_id": str(merged.id)},
        )
        remember_decision(db, vehicle_id, first_raw.id, second_raw.id, "merged", actor.label)

    op = TripMerge(
        vehicle_id=vehicle_id,
        operation="merge",
        trigger=trigger,
        result_trip_ids=[str(merged.id)],
        source_trip_ids=[str(t.id) for t in trips],
        details={"gaps": gaps, "mixed_categories": not uniform, "gap_km": round(gap_km_total, 1)},
        created_by=actor.label,
    )
    db.add(op)
    audit.record(
        db,
        actor,
        "trip.merge",
        "logical_trip",
        merged.id,
        before={"source_trip_ids": [str(t.id) for t in trips]},
        after={"trigger": trigger, "distance_km": merged.distance_km, "mixed": not uniform},
    )
    db.flush()
    return merged


def _combined_single(values: list[str | None]) -> str | None:
    distinct = {v for v in values if v}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _sources_of(db: Session, trip: LogicalTrip) -> list[LogicalTrip]:
    return list(
        db.execute(
            select(LogicalTrip)
            .where(LogicalTrip.merged_into_id == trip.id, LogicalTrip.state == LogicalState.MERGED)
            .order_by(LogicalTrip.sort_time.asc())
        ).scalars()
    )


def dissolve(
    db: Session, trip: LogicalTrip, *, actor: Actor, reason: str = "user", remember: bool = True
) -> list[LogicalTrip]:
    """Ångrar en sammanslagning: källresorna återaktiveras med sina tidigare beslut."""
    if trip.state != LogicalState.ACTIVE:
        raise TripError("Resan är inte aktiv och kan inte delas upp.", "not_active")
    sources = _sources_of(db, trip)
    if not sources:
        raise TripError("Resan är inte en sammanslagning.", "not_merged")
    for s in sources:
        s.state = LogicalState.ACTIVE
        s.merged_into_id = None
        s.updated_by = actor.label
    trip.state = LogicalState.SPLIT
    trip.updated_by = actor.label
    review.resolve_for_trip(
        db,
        trip.id,
        (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION),
        by=actor.label,
        resolution={"dissolved": True},
    )
    for op in db.execute(
        select(TripMerge).where(
            TripMerge.vehicle_id == trip.vehicle_id, TripMerge.operation == "merge", TripMerge.undone_at.is_(None)
        )
    ).scalars():
        if str(trip.id) in (op.result_trip_ids or []):
            op.undone_at = utcnow()
            op.undone_by = actor.label
    if remember:
        for a, b in zip(sources, sources[1:], strict=False):
            remember_decision(
                db, trip.vehicle_id, _edge_raws(a)[1].id, _edge_raws(b)[0].id, "unmerged", actor.label
            )
    audit.record(
        db,
        actor,
        "trip.unmerge",
        "logical_trip",
        trip.id,
        after={"restored_trip_ids": [str(s.id) for s in sources], "reason": reason},
    )
    db.flush()
    return sources


def split_trip(db: Session, trip: LogicalTrip, *, after_index: int, actor: Actor) -> tuple[LogicalTrip, LogicalTrip]:
    """Delar en sammanslagen resa i två vid gränsen efter delresa ``after_index`` (0-baserat)."""
    if trip.state != LogicalState.ACTIVE:
        raise TripError("Resan är inte aktiv och kan inte delas.", "not_active")
    raws = seg.ordered_raws(trip)
    if len(raws) < 2:
        raise TripError(
            "Resan består av en enda körning. Använd fördelning av delsträckor för att dela upp privat och tjänst.",
            "single_raw",
        )
    if not (0 <= after_index < len(raws) - 1):
        raise TripError("Ogiltig delningspunkt.", "invalid_split_point")

    def build(part_raws: list[RawTrip]) -> LogicalTrip:
        ids = {r.id for r in part_raws}
        new = LogicalTrip(
            vehicle_id=trip.vehicle_id,
            driver_id=trip.driver_id,
            state=LogicalState.ACTIVE,
            category=trip.category,
            subcategory=trip.subcategory,
            category_source=trip.category_source,
            purpose=trip.purpose,
            purpose_source=trip.purpose_source,
            purpose_confirmed=trip.purpose_confirmed,
            review_status=trip.review_status,
            confidence=trip.confidence,
            tax_note=trip.tax_note,
            contacts=trip.contacts,
            notes=trip.notes,
            journey_id=trip.journey_id,
            merge_kind=MergeKind.MANUAL if len(part_raws) > 1 else MergeKind.NONE,
            updated_by=actor.label,
        )
        seq_map = {r.id: i for i, r in enumerate(part_raws)}
        for s in sorted(trip.segments, key=lambda s: (s.seq, s.part)):
            if s.raw_trip_id in ids:
                new.segments.append(
                    TripSegment(
                        raw_trip=s.raw_trip,
                        seq=seq_map[s.raw_trip_id],
                        part=s.part,
                        distance_km=s.distance_km,
                        category_override=s.category_override,
                        note=s.note,
                    )
                )
        db.add(new)
        seg.refresh_logical(db, new)
        return new

    left, right = build(raws[: after_index + 1]), build(raws[after_index + 1 :])
    trip.state = LogicalState.SPLIT
    trip.updated_by = actor.label
    db.flush()
    remember_decision(db, trip.vehicle_id, raws[after_index].id, raws[after_index + 1].id, "unmerged", actor.label)
    review.resolve_for_trip(
        db,
        trip.id,
        (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION),
        by=actor.label,
        resolution={"split_into": [str(left.id), str(right.id)]},
    )
    db.add(
        TripMerge(
            vehicle_id=trip.vehicle_id,
            operation="split",
            trigger="manual",
            source_trip_ids=[str(trip.id)],
            result_trip_ids=[str(left.id), str(right.id)],
            details={"after_index": after_index},
            created_by=actor.label,
        )
    )
    audit.record(
        db, actor, "trip.split", "logical_trip", trip.id, after={"result_trip_ids": [str(left.id), str(right.id)]}
    )
    db.flush()
    return left, right


def undo_operation(db: Session, op: TripMerge, *, actor: Actor) -> list[LogicalTrip]:
    """Ångrar en loggad sammanslagning eller uppdelning."""
    if op.undone_at is not None:
        raise TripError("Åtgärden är redan ångrad.", "already_undone")
    if op.operation == "merge":
        result = db.get(LogicalTrip, uuid.UUID(op.result_trip_ids[0]))
        if result is None or result.state != LogicalState.ACTIVE:
            raise TripError(
                "Den sammanslagna resan har ändrats av en senare åtgärd. Ångra den åtgärden först.", "stale_operation"
            )
        return dissolve(db, seg.load_trip(db, result.id) or result, actor=actor)
    # split
    results = [db.get(LogicalTrip, uuid.UUID(i)) for i in op.result_trip_ids]
    source = db.get(LogicalTrip, uuid.UUID(op.source_trip_ids[0]))
    if source is None or any(r is None or r.state != LogicalState.ACTIVE for r in results):
        raise TripError("De uppdelade resorna har ändrats av en senare åtgärd.", "stale_operation")
    for r in results:
        assert r is not None
        r.state = LogicalState.VOIDED
        r.updated_by = actor.label
        review.resolve_for_trip(
            db,
            r.id,
            (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION),
            by=actor.label,
            resolution={"split_undone": True},
        )
    source.state = LogicalState.ACTIVE
    source.updated_by = actor.label
    op.undone_at = utcnow()
    op.undone_by = actor.label
    audit.record(db, actor, "trip.split_undo", "logical_trip", source.id)
    db.flush()
    return [source]


# ----------------------------------------------------------------------- regelmotor


def evaluate_vehicle(
    db: Session,
    *,
    vehicle_id: uuid.UUID,
    merge_settings: dict[str, Any],
    actor: Actor,
    apply_auto: bool = True,
) -> dict[str, Any]:
    """Går igenom fordonets aktiva resor i tidsordning och tillämpar regel A och B.

    Körs retroaktivt varje gång nya resor tillkommer (Volvo rapporterar ofta nästa
    körning först när den är avslutad). Returnerar även de frågor som skapades så att
    anroparen kan avgöra om en notis ska skickas.
    """
    auto_max = float(merge_settings["auto_max_minutes"])
    ask_max = float(merge_settings["ask_max_minutes"])
    tol = float(merge_settings["odometer_tolerance_km"])
    result: dict[str, Any] = {"auto_merged": 0, "questions": [], "overlaps": 0, "merged_trip_ids": []}
    if not merge_settings.get("enabled", True):
        return result

    trips = seg.active_trips(db, vehicle_id)
    i = 0
    while i < len(trips) - 1:
        a, b = trips[i], trips[i + 1]
        if not (_mergeable(a) and _mergeable(b)) or a.sort_time is None or b.sort_time is None:
            i += 1
            continue
        prev_raw, next_raw = _edge_raws(a)[1], _edge_raws(b)[0]
        gap = stop_gap(prev_raw, next_raw)
        verdict = decide(gap, auto_max=auto_max, ask_max=ask_max, odometer_tolerance=tol)

        if verdict == "overlap":
            _, created = review.upsert(
                db,
                vehicle_id=vehicle_id,
                kind=ReviewKind.ANOMALY,
                dedupe_key=f"overlap:{prev_raw.id}:{next_raw.id}",
                title="Överlappande resor",
                detail="Två resor överlappar varandra i tid eller mätarställning. Kontrollera uppgifterna.",
                logical_trip_id=b.id,
                data={"first_trip_id": str(a.id), "second_trip_id": str(b.id), "type": "overlap"},
            )
            result["overlaps"] += 1 if created else 0
            i += 1
            continue

        decision = _decision_for(db, prev_raw.id, next_raw.id)
        if verdict == "auto" and apply_auto and (decision is None or decision.decision == "merged"):
            merged = merge_trips(db, [a, b], trigger="rule_a", actor=actor, odometer_tolerance=tol)
            merged = seg.load_trip(db, merged.id) or merged
            trips[i : i + 2] = [merged]
            result["auto_merged"] += 1
            result["merged_trip_ids"].append(str(merged.id))
            continue  # jämför den sammanslagna resan med nästa (kedjor av korta stopp)

        if verdict in ("ask", "auto") and decision is None:
            # 'auto' hamnar här endast när automatik är avstängd för körningen (t.ex. import).
            item, created = review.upsert(
                db,
                vehicle_id=vehicle_id,
                kind=ReviewKind.MERGE,
                dedupe_key=review.merge_key(prev_raw.id, next_raw.id),
                title=f"Slå samman två resor? Stillestånd: {gap.label}.",
                detail=_merge_detail(a, b),
                logical_trip_id=b.id,
                data={
                    "first_trip_id": str(a.id),
                    "second_trip_id": str(b.id),
                    "first_raw_trip_id": str(prev_raw.id),
                    "second_raw_trip_id": str(next_raw.id),
                    "minutes_low": gap.minutes_low,
                    "minutes_high": gap.minutes_high,
                    "exact": gap.exact,
                    "rule": "A" if verdict == "auto" else "B",
                },
            )
            if created:
                result["questions"].append(item)
        i += 1
    return result


def _merge_detail(a: LogicalTrip, b: LogicalTrip) -> str:
    return f"{a.start_address or 'Okänd start'} → {a.end_address or 'okänd plats'} → {b.end_address or 'okänd plats'}"


def answer_question(
    db: Session,
    *,
    vehicle_id: uuid.UUID,
    first_raw_trip_id: uuid.UUID,
    second_raw_trip_id: uuid.UUID,
    accept: bool,
    actor: Actor,
    trigger: str = "rule_b",
    odometer_tolerance: float = 2.0,
) -> LogicalTrip | None:
    """Tillämpar svaret på en sammanslagningsfråga (från webben eller Home Assistant)."""
    key = review.merge_key(first_raw_trip_id, second_raw_trip_id)
    if not accept:
        remember_decision(db, vehicle_id, first_raw_trip_id, second_raw_trip_id, "keep_separate", actor.label)
        review.resolve_by_key(db, vehicle_id, key, by=actor.label, resolution={"decision": "keep_separate"})
        audit.record(db, actor, "trip.merge_declined", "raw_trip", first_raw_trip_id, after={"next": str(second_raw_trip_id)})
        return None

    def active_trip_with(raw_id: uuid.UUID) -> LogicalTrip | None:
        return db.execute(
            select(LogicalTrip)
            .join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
            .where(TripSegment.raw_trip_id == raw_id, LogicalTrip.state == LogicalState.ACTIVE)
        ).scalars().first()

    a, b = active_trip_with(first_raw_trip_id), active_trip_with(second_raw_trip_id)
    if a is None or b is None:
        raise TripError("Resorna finns inte längre.", "not_found")
    if a.id == b.id:  # redan sammanslagna
        review.resolve_by_key(db, vehicle_id, key, by=actor.label, resolution={"decision": "merged", "trip_id": str(a.id)})
        return a
    a, b = seg.load_trip(db, a.id), seg.load_trip(db, b.id)
    assert a is not None and b is not None
    return merge_trips(db, [a, b], trigger=trigger, actor=actor, odometer_tolerance=odometer_tolerance)
