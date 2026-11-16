"""Sammanslagning av resor enligt regel A, B och C.

* Regel A – stillestånd ≤ ``auto_max_minutes`` (standard 15): automatisk sammanslagning.
* Regel B – stillestånd > A-gränsen och ≤ ``ask_max_minutes`` (standard 60): fråga.
* Regel C – längre stillestånd: endast manuell sammanslagning.

Två regelverk (``merge.by_category``):

* Kategoriregler (1.3.0, standard): bara två avgjorda resor av samma kategori slås ihop,
  tjänsteresor med tjänstegränserna och privata med privatgränserna. Stopp hemma eller vid
  en bekräftad privat plats slås aldrig ihop. Samma i manuellt och självgående läge.
* Det tidigare regelverket (befintliga installationer tills ägaren sparar sektionen):
  ett stopp hemma eller vid en bekräftad privat plats slås aldrig ihop när någon av resorna
  är en live-resa. I självgående läge slås live-resor bara ihop vid ett kort stopp vid en
  bekräftad tank- eller laddstation, och inga frågor ställs.

Originalresorna (RawTrip) ändras aldrig. En sammanslagning skapar en ny LogicalTrip
vars segment pekar på samma RawTrips; källresorna markeras ``merged`` och kan
återaktiveras. Privat körsträcka omklassas aldrig till tjänst av en sammanslagning,
och en odometerlucka mellan delresor döljs aldrig.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    KnownLocation,
    LogicalTrip,
    MergeDecision,
    RawTrip,
    ReviewItem,
    TripMerge,
    TripSegment,
    Vehicle,
)
from app.models.enums import (
    AUTO_APPROVAL_SOURCES,
    ApprovalSource,
    Category,
    LocationType,
    LogicalState,
    MergeKind,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TimeQuality,
    TripSource,
)
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
_LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
# Starttid som bara är en gräns (t.ex. endast datum i en import): räkna med gränserna.
_UNCERTAIN_START = {TimeQuality.BOUNDED, TimeQuality.UNKNOWN}
# Ett startintervall så brett som (ungefär) ett dygn säger ingenting om stilleståndet.
_DAY_LEVEL_SPAN = timedelta(hours=12)


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
    if prev.ended_at is None or prev.end_time_quality in _UNCERTAIN_START:
        # Sluttiden är bara en gräns (t.ex. endast datum i en import): underlaget säger
        # ingenting om stilleståndet – varken automatisk sammanslagning eller fråga.
        return StopGap(None, None, False, odo_gap)
    # En uträknad starttid (live-resa) räknas inte som känd här: stilleståndet bedöms
    # med samma gränser som när starttiden är okänd (byggkontraktet C.2).
    estimated = nxt.start_time_quality == TimeQuality.ESTIMATED and (
        nxt.start_earliest is not None or nxt.start_latest is not None
    )
    if nxt.started_at is not None and nxt.start_time_quality not in _UNCERTAIN_START and not estimated:
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
    if lo is not None and hi is not None and hi - lo >= _DAY_LEVEL_SPAN:
        # Starttiden är bara känd till dygnet: stilleståndet är i praktiken okänt.
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
                # Bevara varje delsträckas egen kategori när källorna skiljer sig åt. En oavgjord
                # delsträcka får ingen egen kategori utan följer resans, så att den avgörs när
                # resan avgörs (annars kan resan se avgjord ut medan sträckan förblir oavgjord).
                own = None if uniform else (s.category_override or t.category)
                merged.segments.append(
                    TripSegment(
                        raw_trip=s.raw_trip,
                        seq=seq,
                        part=s.part,
                        distance_km=s.distance_km,
                        category_override=None if own == Category.UNDECIDED else own,
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
        _set_approval(merged, trips)
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


# Vid hopslagning vinner den svagaste godkännandekällan: antaget, övriga automatiska,
# massgodkännande, ägaren (NULL räknas som ägaren).
def _approval_rank(source: str | None) -> int:
    if source == ApprovalSource.ASSUMED:
        return 0
    if source in AUTO_APPROVAL_SOURCES:
        return 1
    if source == ApprovalSource.BULK:
        return 2
    return 3


def _set_approval(target: LogicalTrip, sources: list[LogicalTrip]) -> None:
    """``approval_source``, ``approval_basis`` och ``auto_approved_at`` för en resa som
    byggts av andra. Bara en godkänd resa har en godkännandekälla."""
    if target.review_status != ReviewStatus.APPROVED or not sources:
        target.approval_source = target.approval_basis = target.auto_approved_at = None
        return
    weakest = min(sources, key=lambda t: _approval_rank(t.approval_source))
    target.approval_source = weakest.approval_source
    target.approval_basis = dict(weakest.approval_basis) if weakest.approval_basis else None
    target.auto_approved_at = weakest.auto_approved_at


def stop_kind(loc: KnownLocation | None) -> str:
    """Stoppets art för sammanslagningen: home | private | refuel | other.

    Läser bara ``type`` och ``confirmed`` – aldrig kartans förslag (``suggested_type``,
    ``osm_class``, ``place_kind``).
    """
    if loc is None or not loc.confirmed:
        return "other"
    if loc.type == LocationType.HOME:
        return "home"
    if loc.type == LocationType.PRIVATE:
        return "private"
    if loc.type in (LocationType.FUEL_STATION, LocationType.CHARGING_STATION):
        return "refuel"
    return "other"


def _has_live(trip: LogicalTrip) -> bool:
    return any(r.source in _LIVE_SOURCES for r in seg.ordered_raws(trip))


def _combined_single(values: list[str | None]) -> str | None:
    distinct = {v for v in values if v}
    return next(iter(distinct)) if len(distinct) == 1 else None


_SUPERSEDED_KINDS = (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION)


def _sources_of(db: Session, trip: LogicalTrip, *, any_state: bool = False) -> list[LogicalTrip]:
    q = select(LogicalTrip).where(LogicalTrip.merged_into_id == trip.id)
    if not any_state:
        q = q.where(LogicalTrip.state == LogicalState.MERGED)
    return list(db.execute(q.order_by(LogicalTrip.sort_time.asc())).scalars())


def _raw_ids(trip: LogicalTrip) -> set[uuid.UUID]:
    return {s.raw_trip_id for s in trip.segments}


def _segment_signature(trip: LogicalTrip, segments: list[TripSegment]) -> list[tuple[str, int, float, str]]:
    return sorted(
        (str(s.raw_trip_id), s.part, round(s.distance_km, 3), seg.segment_category(trip, s)) for s in segments
    )


def _carry_decisions(db: Session, from_trip: LogicalTrip, to_trip: LogicalTrip) -> bool:
    """För över beslut från en resa som upphör till en resa som återställs.

    Används när en sammanslagning eller delning ångras. Har kategorin eller fördelningen
    för de gemensamma fysiska resorna ändrats efter åtgärden får den återställda resan
    samma kategorier för just de delsträckorna (km per RawTrip oförändrade). Annars
    återställs resan som den var; ett godkännande av samma kategorier följer med.
    En privat sträcka blir därmed aldrig tjänst för att en åtgärd ångras.
    """
    # Sessionen flushar inte automatiskt. Delsträckor som byggts om av ett tidigare anrop
    # (när flera delar förs över till samma resa) saknar annars raw_trip_id, och då skulle
    # den här delens beslut tappas bort.
    db.flush()
    ids = _raw_ids(to_trip) & _raw_ids(from_trip)
    if not ids:
        return False
    full = ids == _raw_ids(to_trip)
    from_segs = [s for s in from_trip.segments if s.raw_trip_id in ids]
    to_segs = [s for s in to_trip.segments if s.raw_trip_id in ids]
    if _segment_signature(from_trip, from_segs) == _segment_signature(to_trip, to_segs):
        if full and from_trip.review_status == ReviewStatus.APPROVED and to_trip.review_status != ReviewStatus.APPROVED:
            to_trip.review_status = ReviewStatus.APPROVED
            to_trip.category_source = from_trip.category_source
            to_trip.tax_note = None
            _set_approval(to_trip, [from_trip])
        return False
    seq_map = {r.id: i for i, r in enumerate(seg.ordered_raws(to_trip))}
    wanted = [
        (s.raw_trip, s.seq, s.part, s.distance_km, seg.segment_category(to_trip, s), s.note)
        for s in to_trip.segments
        if s.raw_trip_id not in ids
    ] + [
        (s.raw_trip, seq_map[s.raw_trip_id], s.part, s.distance_km, seg.segment_category(from_trip, s), s.note)
        for s in from_segs
    ]
    cats = {w[4] for w in wanted}
    single = next(iter(cats)) if len(cats) == 1 else None
    for s in list(to_trip.segments):
        to_trip.segments.remove(s)
    db.flush()
    for raw, seq, part, km, cat, note in sorted(wanted, key=lambda w: (w[1], w[2])):
        to_trip.segments.append(
            TripSegment(
                raw_trip=raw,
                seq=seq,
                part=part,
                distance_km=km,
                # Oavgjorda delsträckor följer resans kategori (som då är oavgjord).
                category_override=None if single or cat == Category.UNDECIDED else cat,
                note=note,
            )
        )
    if single:
        to_trip.category = single
    elif Category.UNDECIDED in cats:
        to_trip.category = Category.UNDECIDED
    else:
        to_trip.category = from_trip.category if from_trip.category != Category.UNDECIDED else to_trip.category
    to_trip.category_source = from_trip.category_source
    if full:
        to_trip.subcategory = from_trip.subcategory
        to_trip.review_status = from_trip.review_status
        to_trip.confidence = from_trip.confidence
        to_trip.tax_note = from_trip.tax_note
        _set_approval(to_trip, [from_trip])
    else:
        to_trip.review_status = min(
            (to_trip.review_status, from_trip.review_status), key=lambda st: _REVIEW_ORDER.get(st, 0)
        )
        _set_approval(to_trip, [to_trip, from_trip])
    if to_trip.category == Category.UNDECIDED and to_trip.review_status not in (ReviewStatus.NEEDS_REVIEW,):
        if any(seg.segment_category(to_trip, s) == Category.UNDECIDED for s in to_trip.segments):
            to_trip.review_status = ReviewStatus.NEEDS_REVIEW
    seg.refresh_logical(db, to_trip)
    return True


def _part_texts(trip: LogicalTrip, sources: list[LogicalTrip], part_ids: set[uuid.UUID]) -> dict[str, Any]:
    """Syfte, kontakter och anteckningar för en del av en resa som delas.

    Om syftet inte ändrats sedan sammanslagningen och delen består av hela källresor
    används källresornas egna uppgifter. Annars kopieras resans syfte men måste
    bekräftas på nytt – ett hopslaget syfte påstås aldrig gälla en enskild del.
    """
    if sources and trip.purpose == _combined_text([s.purpose for s in sources]):
        inside = [s for s in sources if _raw_ids(s) <= part_ids]
        covered: set[uuid.UUID] = set().union(*(_raw_ids(s) for s in inside)) if inside else set()
        if inside and covered == part_ids:
            purpose = _combined_text([s.purpose for s in inside])
            sources_with_purpose = {s.purpose_source for s in inside if s.purpose}
            return {
                "purpose": purpose,
                "purpose_source": next(iter(sources_with_purpose)) if len(sources_with_purpose) == 1
                else ("merged" if sources_with_purpose else None),
                "purpose_confirmed": bool(purpose) and all(s.purpose_confirmed for s in inside if s.purpose),
                "contacts": _combined_text([s.contacts for s in inside]),
                "notes": _combined_text([s.notes for s in inside], sep="\n"),
            }
    return {
        "purpose": trip.purpose,
        "purpose_source": trip.purpose_source,
        "purpose_confirmed": False,
        "contacts": trip.contacts,
        "notes": trip.notes,
    }


def _build_part(
    db: Session, trip: LogicalTrip, part_raws: list[RawTrip], sources: list[LogicalTrip], actor: Actor
) -> LogicalTrip:
    """Ny journalresa av en del av ``trip`` med delsträckornas kategorier bevarade."""
    ids = {r.id for r in part_raws}
    new = LogicalTrip(
        vehicle_id=trip.vehicle_id,
        driver_id=trip.driver_id,
        state=LogicalState.ACTIVE,
        category=trip.category,
        subcategory=trip.subcategory,
        category_source=trip.category_source,
        review_status=trip.review_status,
        confidence=trip.confidence,
        tax_note=trip.tax_note,
        journey_id=trip.journey_id,
        is_favorite=trip.is_favorite,
        approval_source=trip.approval_source,
        approval_basis=dict(trip.approval_basis) if trip.approval_basis else None,
        auto_approved_at=trip.auto_approved_at,
        merge_kind=MergeKind.MANUAL if len(part_raws) > 1 else MergeKind.NONE,
        updated_by=actor.label,
        **_part_texts(trip, sources, ids),
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


def dissolve(
    db: Session, trip: LogicalTrip, *, actor: Actor, reason: str = "user", remember: bool = True
) -> list[LogicalTrip]:
    """Ångrar en sammanslagning: källresorna återaktiveras med sina tidigare beslut.

    Beslut som fattats på den sammanslagna resan (kategori, fördelning) följer med till
    källresorna. En flerdelsresa utan källresor (resultatet av en delning) bryts ned till
    en journalresa per fysisk resa med bevarade delsträckor.
    """
    if trip.state != LogicalState.ACTIVE:
        raise TripError("Resan är inte aktiv och kan inte delas upp.", "not_active")
    sources = _sources_of(db, trip)
    if not sources:
        return _break_down(db, trip, actor=actor, reason=reason, remember=remember)
    for s in sources:
        s.state = LogicalState.ACTIVE
        s.merged_into_id = None
        s.updated_by = actor.label
        _carry_decisions(db, trip, s)
    trip.state = LogicalState.SPLIT
    trip.updated_by = actor.label
    review.resolve_for_trip(db, trip.id, _SUPERSEDED_KINDS, by=actor.label, resolution={"dissolved": True})
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


def _break_down(db: Session, trip: LogicalTrip, *, actor: Actor, reason: str, remember: bool) -> list[LogicalTrip]:
    raws = seg.ordered_raws(trip)
    if len(raws) < 2:
        raise TripError("Resan är inte en sammanslagning.", "not_merged")
    parts = [_build_part(db, trip, [r], [], actor) for r in raws]
    trip.state = LogicalState.SPLIT
    trip.updated_by = actor.label
    db.flush()
    if remember:
        for a, b in zip(raws, raws[1:], strict=False):
            remember_decision(db, trip.vehicle_id, a.id, b.id, "unmerged", actor.label)
    review.resolve_for_trip(
        db, trip.id, _SUPERSEDED_KINDS, by=actor.label, resolution={"split_into": [str(p.id) for p in parts]}
    )
    db.add(
        TripMerge(
            vehicle_id=trip.vehicle_id,
            operation="split",
            trigger="unmerge",
            source_trip_ids=[str(trip.id)],
            result_trip_ids=[str(p.id) for p in parts],
            details={"reason": reason},
            created_by=actor.label,
        )
    )
    audit.record(
        db,
        actor,
        "trip.unmerge",
        "logical_trip",
        trip.id,
        after={"result_trip_ids": [str(p.id) for p in parts], "reason": reason},
    )
    db.flush()
    return parts


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

    sources = _sources_of(db, trip)
    left = _build_part(db, trip, raws[: after_index + 1], sources, actor)
    right = _build_part(db, trip, raws[after_index + 1 :], sources, actor)
    trip.state = LogicalState.SPLIT
    trip.updated_by = actor.label
    db.flush()
    remember_decision(db, trip.vehicle_id, raws[after_index].id, raws[after_index + 1].id, "unmerged", actor.label)
    review.resolve_for_trip(
        db, trip.id, _SUPERSEDED_KINDS, by=actor.label, resolution={"split_into": [str(left.id), str(right.id)]}
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
        # Beslut som fattats på delarna efter delningen följer med tillbaka.
        _carry_decisions(db, seg.load_trip(db, r.id) or r, seg.load_trip(db, source.id) or source)
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
    if uses_category_rules(merge_settings):
        return _evaluate_by_category(
            db, vehicle_id=vehicle_id, merge_settings=merge_settings, actor=actor, apply_auto=apply_auto, result=result
        )

    from app.services.classify import autopilot

    vehicle = db.get(Vehicle, vehicle_id)
    self_driving = vehicle is not None and autopilot.is_active(db, vehicle.owner_user_id)

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

        if verdict in ("auto", "ask") and (_has_live(a) or _has_live(b)):
            kind = stop_kind(db.get(KnownLocation, prev_raw.end_location_id) if prev_raw.end_location_id else None)
            if kind in ("home", "private"):
                # Vakten: ett stopp hemma eller vid en privat plats slås aldrig ihop (A.1).
                i += 1
                continue
            if self_driving and not (verdict == "auto" and kind == "refuel"):
                # Självgående läge: bara korta stopp vid bekräftad tank- eller laddstation slås
                # ihop. Allt annat blir separata resor, och ingen fråga ställs.
                i += 1
                continue

        decision = _decision_for(db, prev_raw.id, next_raw.id)
        # En obesvarad fråga om paret (t.ex. från en import utan automatisk sammanslagning)
        # väntar på användarens svar och avgörs inte i tysthet av en senare körning.
        pending = decision is None and _open_question(db, vehicle_id, prev_raw.id, next_raw.id)
        if verdict == "auto" and apply_auto and not pending and (decision is None or decision.decision == "merged"):
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


# ----------------------------------------------------------------------- kategoriregler (1.3.0)

_DECIDED_STATUSES = (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
_CATEGORY_TITLES = {
    Category.WORK: "Slå samman två tjänsteresor?",
    Category.PRIVATE: "Slå samman två privata resor?",
}


def uses_category_rules(merge_settings: dict[str, Any]) -> bool:
    """Gäller kategorireglerna (``merge.by_category``)? Annars det tidigare regelverket."""
    return bool(merge_settings.get("by_category", False))


def category_limits(merge_settings: dict[str, Any], category: str) -> tuple[float, float]:
    """(automatiskt, fråga) i minuter för två avgjorda resor av samma kategori."""
    if category == Category.PRIVATE:
        return float(merge_settings["private_auto_max_minutes"]), float(merge_settings["private_ask_max_minutes"])
    return float(merge_settings["auto_max_minutes"]), float(merge_settings["ask_max_minutes"])


def decided_category(trip: LogicalTrip) -> str | None:
    """Resans kategori när den är avgjord av ägaren, en regel eller importen – annars None.

    Ett antagande med vetotid (``assumed``) är inget beslut: slogs det ihop med ägarens egen
    resa skulle hela resan bli antagen (``_set_approval``), och ett "Fel" öppna båda.
    """
    if trip.review_status not in _DECIDED_STATUSES:
        return None
    if trip.category not in (Category.WORK, Category.PRIVATE):
        return None
    if trip.has_mixed_categories or trip.undecided_km > seg.KM_EPS:
        return None
    if trip.approval_source == ApprovalSource.ASSUMED:
        return None
    return trip.category


def _rules_since(merge_settings: dict[str, Any]) -> Any:
    from datetime import datetime

    value = merge_settings.get("category_rules_since")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _created_since(trip: LogicalTrip, since: Any) -> bool:
    return any(r.created_at is not None and r.created_at >= since for r in seg.ordered_raws(trip))


def _evaluate_by_category(
    db: Session,
    *,
    vehicle_id: uuid.UUID,
    merge_settings: dict[str, Any],
    actor: Actor,
    apply_auto: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Kategoriregler: två avgjorda tjänsteresor i rad följer tjänstegränserna, två privata
    privatgränserna. Allt annat (tjänst + privat, något oavgjort eller antaget) slås inte ihop
    automatiskt och ger ingen fråga. Stopp hemma eller vid en bekräftad privat plats slås
    aldrig ihop, oavsett källa. Gäller lika i manuellt och självgående läge.
    """
    tol = float(merge_settings["odometer_tolerance_km"])
    since = _rules_since(merge_settings)
    trips = seg.active_trips(db, vehicle_id)
    i = 0
    while i < len(trips) - 1:
        a, b = trips[i], trips[i + 1]
        if not (_mergeable(a) and _mergeable(b)) or a.sort_time is None or b.sort_time is None:
            i += 1
            continue
        prev_raw, next_raw = _edge_raws(a)[1], _edge_raws(b)[0]
        gap = stop_gap(prev_raw, next_raw)
        if decide(gap, auto_max=0, ask_max=0, odometer_tolerance=tol) == "overlap":
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

        kind = stop_kind(db.get(KnownLocation, prev_raw.end_location_id) if prev_raw.end_location_id else None)
        if kind in ("home", "private"):
            i += 1  # stopp hemma eller vid en privat plats slås aldrig ihop
            continue
        cat_a, cat_b = decided_category(a), decided_category(b)
        if cat_a is None or cat_a != cat_b:
            i += 1  # inte avgjorda, eller tjänst och privat: ingen automatik och ingen fråga
            continue
        if since is not None and not (_created_since(a, since) or _created_since(b, since)):
            i += 1  # resor från före kategorireglerna slås inte ihop i efterhand
            continue

        auto_max, ask_max = category_limits(merge_settings, cat_a)
        verdict = decide(gap, auto_max=auto_max, ask_max=ask_max, odometer_tolerance=tol)
        decision = _decision_for(db, prev_raw.id, next_raw.id)
        pending = decision is None and _open_question(db, vehicle_id, prev_raw.id, next_raw.id)
        if verdict == "auto" and apply_auto and not pending and (decision is None or decision.decision == "merged"):
            merged = merge_trips(db, [a, b], trigger="rule_a", actor=actor, odometer_tolerance=tol)
            merged = seg.load_trip(db, merged.id) or merged
            trips[i : i + 2] = [merged]
            result["auto_merged"] += 1
            result["merged_trip_ids"].append(str(merged.id))
            continue  # jämför den sammanslagna resan med nästa (kedjor av korta stopp)

        if verdict in ("ask", "auto") and decision is None:
            item, created = review.upsert(
                db,
                vehicle_id=vehicle_id,
                kind=ReviewKind.MERGE,
                dedupe_key=review.merge_key(prev_raw.id, next_raw.id),
                title=f"{_CATEGORY_TITLES[cat_a]} Stillestånd: {gap.label}.",
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
                    "category": cat_a,
                },
            )
            if created:
                result["questions"].append(item)
        i += 1
    return result


def _open_question(db: Session, vehicle_id: uuid.UUID, first_raw: uuid.UUID, second_raw: uuid.UUID) -> bool:
    return (
        db.execute(
            select(ReviewItem.id).where(
                ReviewItem.vehicle_id == vehicle_id,
                ReviewItem.dedupe_key == review.merge_key(first_raw, second_raw),
                ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)),
            )
        ).first()
        is not None
    )


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
