"""Användarens beslut: klassning, syfte, godkännande – och inlärningen av dem.

Varje ändring sparas som ``LearningFeedback`` med ursprungligt och nytt värde, plats,
veckodag, tid, kalenderkontext och modellens tidigare bedömning. Mönstren räknas om
efter varje beslut, och en korrigering kan ångras helt.

Omräkningen efter ett beslut (``after_decisions``) görs direkt i anropet, utom när anropet
svarar ägaren i webben eller på telefonen (``recompute.defer``): då görs bara resans egna
ärenden direkt och resten beställs till efter svaret (se ``learning.recompute``).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    CalendarEvent,
    CalendarMatch,
    KnownLocation,
    LearningFeedback,
    LogicalTrip,
    TripClassification,
    TripPurpose,
    TripSegment,
    Vehicle,
)
from app.models.enums import ApprovalSource, Category, DecisionSource, LogicalState, ReviewStatus
from app.services import audit
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.classify.purpose import NEEDS_CONFIRMATION_SUFFIX
from app.services.learning import patterns, recompute
from app.services.places.normalize import fold
from app.services.trips import segments as seg
from app.services.trips.segments import TripError

MAX_PURPOSE_LENGTH = 500
_SNAPSHOT_FIELDS = (
    "category",
    "subcategory",
    "category_source",
    "purpose",
    "purpose_source",
    "purpose_confirmed",
    "review_status",
    "confidence",
    "tax_note",
    "contacts",
    "notes",
    "approval_source",
    "approval_basis",
)


def _snapshot(trip: LogicalTrip) -> dict[str, Any]:
    snap = {f: getattr(trip, f) for f in _SNAPSHOT_FIELDS}
    snap["segments"] = [
        {"raw_trip_id": str(s.raw_trip_id), "seq": s.seq, "part": s.part, "distance_km": s.distance_km,
         "category_override": s.category_override, "note": s.note}
        for s in sorted(trip.segments, key=lambda s: (s.seq, s.part))
    ]
    return snap


def _calendar_context(db: Session, trip_id: uuid.UUID) -> dict[str, Any]:
    row = db.execute(
        select(CalendarMatch, CalendarEvent)
        .join(CalendarEvent, CalendarEvent.id == CalendarMatch.calendar_event_id)
        .where(CalendarMatch.logical_trip_id == trip_id, CalendarMatch.is_selected.is_(True))
        .limit(1)
    ).first()
    if row is None:
        return {}
    m, ev = row
    return {"event_id": str(ev.id), "subject": ev.subject, "score": m.score, "relation": m.relation}


def _model_assessment(db: Session, trip_id: uuid.UUID) -> dict[str, Any]:
    c = db.execute(
        select(TripClassification)
        .where(
            TripClassification.logical_trip_id == trip_id,
            TripClassification.source.in_((DecisionSource.RULE, DecisionSource.AI)),
        )
        .order_by(TripClassification.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if c is None:
        return {}
    return {
        "source": c.source,
        "category": c.category,
        "confidence": c.confidence,
        "model": c.model,
        "suggested_category": (c.signals or {}).get("suggested_category"),
    }


def apply_decision(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    actor: Actor,
    channel: str = "web",
    category: str | None = None,
    purpose: str | None = None,
    subcategory: str | None = None,
    contacts: str | None = None,
    notes: str | None = None,
    approve: bool = False,
    rebuild: bool = True,
) -> LogicalTrip:
    """Tillämpar ett användarbeslut på en resa och lär av det."""
    if trip.state != LogicalState.ACTIVE:
        raise TripError("Resan är inte aktiv och kan inte ändras.", "not_active")
    if category is not None and category not in (Category.WORK, Category.PRIVATE, Category.UNDECIDED):
        raise TripError("Ogiltig kategori.", "invalid_category")
    if purpose is not None and len(purpose) > MAX_PURPOSE_LENGTH:
        raise TripError(f"Syftet får vara högst {MAX_PURPOSE_LENGTH} tecken.", "purpose_too_long")

    if rebuild and recompute.is_deferred(db):
        recompute.request(db, trip.vehicle_id)
    before = _snapshot(trip)
    tz = settings_svc.user_timezone(db, user_id)
    decision_source = DecisionSource.HA if channel == "ha" else DecisionSource.USER
    category_changed = purpose_changed = False

    if category is not None:
        category_changed = category != trip.category or bool(any(s.category_override for s in trip.segments))
        # Ett uttryckligt beslut för hela resan gäller alla delsträckor.
        for s in trip.segments:
            s.category_override = None
        trip.category = category
        trip.category_source = decision_source
        trip.confidence = None
        trip.tax_note = None
        if subcategory is not None or category_changed:
            trip.subcategory = subcategory
        trip.review_status = ReviewStatus.APPROVED if category != Category.UNDECIDED else ReviewStatus.NEEDS_REVIEW
    elif subcategory is not None:
        trip.subcategory = subcategory

    if purpose is not None:
        text = purpose.strip()
        purpose_changed = (text or None) != (trip.purpose or None)
        trip.purpose = text or None
        trip.purpose_source = decision_source if text else None
        trip.purpose_confirmed = bool(text)
        db.execute(
            update(TripPurpose)
            .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
            .values(is_current=False)
        )
        if text:
            db.add(
                TripPurpose(
                    logical_trip_id=trip.id,
                    text=text,
                    source=decision_source,
                    rationale="Angivet av användaren.",
                    needs_confirmation=False,
                    created_by=actor.label,
                )
            )

    if approve:
        # Godkännandet gäller fördelningen per delsträcka. En resa vars alla delsträckor är
        # fördelade kan godkännas även om resan som helhet saknar kategori; oavgjorda km
        # får däremot aldrig godkännas i tysthet.
        undecided = [s for s in trip.segments if seg.segment_category(trip, s) == Category.UNDECIDED]
        if undecided and len(undecided) == len(trip.segments):
            raise TripError("Välj Arbete eller Privat innan resan godkänns.", "undecided")
        if sum(s.distance_km for s in undecided) > seg.KM_EPS:
            raise TripError("Fördela alla delsträckor innan resan godkänns.", "unallocated")
        trip.review_status = ReviewStatus.APPROVED
        trip.category_source = decision_source
        trip.tax_note = None
        # Godkännandet omfattar även ett föreslaget syfte – men inte platshållaren.
        if trip.purpose and not trip.purpose_confirmed and NEEDS_CONFIRMATION_SUFFIX not in fold(trip.purpose):
            trip.purpose_confirmed = True

    if contacts is not None:
        trip.contacts = contacts.strip() or None
    if notes is not None:
        trip.notes = notes.strip() or None
    if trip.category == Category.PRIVATE and purpose is None and not trip.purpose_confirmed:
        trip.purpose, trip.purpose_source = None, None  # privata resor behöver inget syfte

    if category is not None or purpose is not None or approve:
        # Ägarens beslut: källan blir ägaren (eller massgodkännandet), aldrig automatiken.
        if trip.review_status == ReviewStatus.APPROVED:
            trip.approval_source = ApprovalSource.BULK if channel == "bulk" else ApprovalSource.USER
        else:
            trip.approval_source = None
        trip.approval_basis = None
        trip.auto_approved_at = None

    trip.updated_by = actor.label
    seg.refresh_logical(db, trip)

    if category is not None or approve:
        db.execute(
            update(TripClassification)
            .where(TripClassification.logical_trip_id == trip.id, TripClassification.is_current.is_(True))
            .values(is_current=False)
        )
        db.add(
            TripClassification(
                logical_trip_id=trip.id,
                source=decision_source,
                category=trip.category,
                subcategory=trip.subcategory,
                confidence=None,
                requires_review=False,
                source_quality="user",
                signals={"approved_proposal": approve and category is None, "channel": channel},
                created_by=actor.label,
            )
        )

    origin = db.get(KnownLocation, trip.start_location_id) if trip.start_location_id else None
    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    when = trip.started_at or trip.sort_time
    local = when.astimezone(tz) if when else None
    was_proposal = before["category"] != Category.UNDECIDED and before["review_status"] not in (
        ReviewStatus.APPROVED,
    )
    is_correction = (category_changed and before["category"] != Category.UNDECIDED) or (
        purpose_changed and bool(before["purpose"])
    )
    if category is not None or purpose is not None or approve:
        db.add(
            LearningFeedback(
                logical_trip_id=trip.id,
                user_id=user_id,
                channel=channel,
                kind="correction" if is_correction else "confirmation",
                original_category=before["category"],
                new_category=trip.category,
                original_purpose=before["purpose"],
                new_purpose=trip.purpose,
                original_state=before,
                origin_location_id=trip.start_location_id,
                destination_location_id=trip.end_location_id,
                origin_type=origin.type if origin else None,
                destination_type=dest.type if dest else None,
                weekday=local.weekday() if local else None,
                local_hour=local.hour if local else None,
                calendar_context=_calendar_context(db, trip.id),
                model_assessment={**_model_assessment(db, trip.id), "was_proposal": was_proposal},
            )
        )
    audit.record(
        db,
        actor,
        "trip.update",
        "logical_trip",
        trip.id,
        before={k: v for k, v in before.items() if k != "segments"},
        after={f: getattr(trip, f) for f in _SNAPSHOT_FIELDS},
        user_id=user_id,
    )
    db.flush()
    # Rättelser av automatiska resor (A.2): t.ex. blir en rättad regel "fråga alltid".
    # Texten till ägaren läses av svaren från telefonen och webben.
    from app.services.classify import autopilot

    trip._autopilot_notice = autopilot.on_owner_decision(db, trip, before=before, actor=actor)  # type: ignore[attr-defined]
    if rebuild:
        rebuild_after(db, user_id=user_id, vehicle_id=trip.vehicle_id, actor=actor, trips=[trip])
    return trip


def rebuild_after(
    db: Session, *, user_id: uuid.UUID, vehicle_id: uuid.UUID, actor: Actor, trips: list[LogicalTrip]
) -> None:
    """Omräkningen efter beslut om ``trips``: direkt (after_decisions), eller – när anropet
    svarar ägaren (recompute.defer) – bara resornas egna ärenden nu och resten efter svaret."""
    if not recompute.is_deferred(db):
        after_decisions(db, user_id=user_id, vehicle_id=vehicle_id, actor=actor)
        return
    from app.services.trips import pipeline

    recompute.request(db, vehicle_id)
    for trip in trips:
        pipeline.sync_trip_review_items(db, trip, by=actor.label)
    db.flush()


def after_decisions(db: Session, *, user_id: uuid.UUID, vehicle_id: uuid.UUID, actor: Actor) -> None:
    """Räknar om mönster och uppdaterar förslag för övriga oavgjorda resor (utan LLM)."""
    from app.services.trips import pipeline

    patterns.rebuild(db, user_id)
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return
    reclassify(db, user_id=user_id, vehicle_id=vehicle_id, trips=seg.active_trips(db, vehicle_id), cache={})
    # Självgående läge: T och nya regler slår igenom på väntande resor direkt.
    from app.services.classify import autopilot

    autopilot.run_vehicle(db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM)
    # Kategoriregler för sammanslagning: en resa som just blivit avgjord kan slås ihop med
    # grannen av samma kategori (byggkontraktet 2.A.3).
    merge_questions = merge_after_decisions(db, user_id=user_id, vehicle=vehicle)
    for trip in seg.active_trips(db, vehicle_id):
        pipeline.sync_trip_review_items(db, trip, by=actor.label)
    notify_merge_questions(db, user_id=user_id, vehicle=vehicle, questions=merge_questions)
    db.flush()


def merge_after_decisions(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle) -> list[Any]:
    """Sammanslagning med kategoriregler efter beslut (gör inget med det tidigare regelverket)."""
    from app.services.trips import merge

    return merge.evaluate_after_decisions(db, vehicle, audit.SYSTEM)


def notify_merge_questions(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, questions: list[Any]) -> None:
    """Frågor om sammanslagning efter beslut går till telefonen som i pipelinen (notifier
    gör urvalet: notistypen, resor från de senaste 48 timmarna, tysta timmar)."""
    if not questions:
        return
    from app.services.ha import notifier

    notifier.enqueue_for_changes(db, user_id=user_id, vehicle=vehicle, merge_items=questions, trip_items=[])


def reclassify(
    db: Session, *, user_id: uuid.UUID, vehicle_id: uuid.UUID, trips: list[LogicalTrip], cache: dict[str, Any]
) -> None:
    """Regelbedömer ``trips`` (i given ordning) på nytt, utan LLM."""
    from app.services.classify import engine

    # Resor vars aktuella bedömning kommer från AI lämnas orörda: en regelbedömning utan AI
    # skulle annars ersätta AI-förslaget (annan indatahash) och förslaget gå förlorat tills
    # nästa AI-körning.
    ai_trips = set(
        db.execute(
            select(TripClassification.logical_trip_id)
            .join(LogicalTrip, LogicalTrip.id == TripClassification.logical_trip_id)
            .where(
                LogicalTrip.vehicle_id == vehicle_id,
                LogicalTrip.state == LogicalState.ACTIVE,
                TripClassification.is_current.is_(True),
                TripClassification.source == DecisionSource.AI,
            )
        ).scalars()
    )
    for trip in trips:
        if trip.id in ai_trips:
            continue
        engine.classify_trip(
            db, trip, user_id=user_id, actor=audit.SYSTEM, provider=None, use_llm=False, settings_cache=cache
        )
    db.flush()


def bulk_apply(
    db: Session,
    trips: list[LogicalTrip],
    *,
    user_id: uuid.UUID,
    actor: Actor,
    category: str | None = None,
    purpose: str | None = None,
    approve: bool = False,
) -> tuple[int, list[dict[str, str]]]:
    """Massredigering. Returnerar (antal ändrade, fel per resa)."""
    done = 0
    errors: list[dict[str, str]] = []
    vehicles: set[uuid.UUID] = set()
    if recompute.is_deferred(db):
        for vid in sorted({t.vehicle_id for t in trips}):
            recompute.request(db, vid)  # före resorna (samma låsordning som insamlingen)
    if trips:
        # PostgreSQL: lås resorna i id-ordning innan någon ändras – samma ordning som
        # omräkningens steg – så att de två aldrig låser varandra i kors. (SQLite: inget.)
        db.execute(
            select(LogicalTrip.id)
            .where(LogicalTrip.id.in_([t.id for t in trips]))
            .order_by(LogicalTrip.id)
            .with_for_update()
        ).all()
    for trip in trips:
        try:
            apply_decision(
                db, trip, user_id=user_id, actor=actor, channel="bulk", category=category, purpose=purpose,
                approve=approve, rebuild=False,
            )
            vehicles.add(trip.vehicle_id)
            done += 1
        except TripError as exc:
            errors.append({"trip_id": str(trip.id), "message": exc.message})
    for vid in vehicles:
        rebuild_after(db, user_id=user_id, vehicle_id=vid, actor=actor,
                      trips=[t for t in trips if t.vehicle_id == vid])
    return done, errors


def undo(db: Session, feedback: LearningFeedback, *, actor: Actor) -> LogicalTrip:
    """Ångrar en korrigering: resan återställs och inlärningen räknas om utan den."""
    if feedback.undone_at is not None:
        raise TripError("Ändringen är redan ångrad.", "already_undone")
    trip = seg.load_trip(db, feedback.logical_trip_id)
    if trip is None or trip.state != LogicalState.ACTIVE:
        raise TripError("Resan finns inte längre i aktivt skick.", "not_active")
    later = db.execute(
        select(LearningFeedback.id).where(
            LearningFeedback.logical_trip_id == trip.id,
            LearningFeedback.undone_at.is_(None),
            LearningFeedback.created_at > feedback.created_at,
        ).limit(1)
    ).first()
    if later is not None:
        raise TripError("Det finns senare ändringar av resan. Ångra dem först.", "later_changes")
    if recompute.is_deferred(db):
        recompute.request(db, trip.vehicle_id)
    state = feedback.original_state or {}
    before = {f: getattr(trip, f) for f in _SNAPSHOT_FIELDS}
    for f in _SNAPSHOT_FIELDS:
        if f in state:
            setattr(trip, f, state[f])
    wanted = {(s["raw_trip_id"], s["part"]): s for s in state.get("segments", [])}
    if wanted:
        current = {(str(s.raw_trip_id), s.part): s for s in trip.segments}
        if set(current) == set(wanted):
            for key, s in current.items():
                s.category_override = wanted[key]["category_override"]
        else:
            raws = {str(s.raw_trip_id): s.raw_trip for s in trip.segments}
            for s in list(trip.segments):
                trip.segments.remove(s)
            db.flush()
            for w in wanted.values():
                if w["raw_trip_id"] in raws:
                    trip.segments.append(
                        TripSegment(
                            raw_trip=raws[w["raw_trip_id"]], seq=w["seq"], part=w["part"],
                            distance_km=w["distance_km"], category_override=w["category_override"], note=w.get("note"),
                        )
                    )
    feedback.undone_at = utcnow()
    trip.updated_by = actor.label
    db.execute(
        update(TripClassification)
        .where(
            TripClassification.logical_trip_id == trip.id,
            TripClassification.is_current.is_(True),
            TripClassification.source.in_((DecisionSource.USER, DecisionSource.HA)),
        )
        .values(is_current=False)
    )
    _restore_proposal_rows(db, trip, feedback)
    seg.refresh_logical(db, trip)
    audit.record(
        db, actor, "trip.undo_change", "logical_trip", trip.id,
        before=before, after={f: getattr(trip, f) for f in _SNAPSHOT_FIELDS},
    )
    db.flush()
    rebuild_after(db, user_id=feedback.user_id, vehicle_id=trip.vehicle_id, actor=actor, trips=[trip])
    return trip


def _restore_proposal_rows(db: Session, trip: LogicalTrip, feedback: LearningFeedback) -> None:
    """Gör bedömningen och syftet från före beslutet aktuella igen vid ångra.

    Utan detta saknar resan aktuell bedömning efter ångra och regelmotorn skriver
    direkt över den återställda ögonblicksbilden (t.ex. ett AI-förslag), och den
    ångrade användartexten låg kvar som aktuellt syfte.
    """
    has_current = db.execute(
        select(TripClassification.id).where(
            TripClassification.logical_trip_id == trip.id, TripClassification.is_current.is_(True)
        ).limit(1)
    ).first()
    if has_current is None:
        prior = db.execute(
            select(TripClassification)
            .where(
                TripClassification.logical_trip_id == trip.id,
                TripClassification.created_at <= feedback.created_at,
                TripClassification.source.not_in((DecisionSource.USER, DecisionSource.HA)),
            )
            .order_by(TripClassification.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if prior is not None and prior.category == trip.category:
            prior.is_current = True
    if (feedback.original_purpose or None) != (feedback.new_purpose or None):
        db.execute(
            update(TripPurpose)
            .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
            .values(is_current=False)
        )
        if trip.purpose:
            row = db.execute(
                select(TripPurpose)
                .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.text == trip.purpose)
                .order_by(TripPurpose.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                row.is_current = True


def history(db: Session, trip_id: uuid.UUID) -> list[LearningFeedback]:
    return list(
        db.execute(
            select(LearningFeedback)
            .where(LearningFeedback.logical_trip_id == trip_id)
            .order_by(LearningFeedback.created_at.desc())
        ).scalars()
    )
