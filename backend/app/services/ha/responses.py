"""Tillämpar användarens svar på en notis – från Home Assistant, webben eller demoläget.

Garantier:

* Idempotens: samma ``idempotency_key`` ger samma resultat utan ny ändring, och det
  första giltiga svaret på en fråga gäller (dubbla knapptryckningar är ofarliga).
* En expirerad fråga ändrar ingenting när svaret kommer från telefonen, men kan
  fortfarande besvaras i den inloggade webbappen.
* Okända frågor och åtgärder avvisas utan sidoeffekter.

Funktionen gör ``db.flush()``; anroparen ansvarar för commit och bör anropa den i en
egen transaktion.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, NotificationRequest, NotificationResponse, Vehicle
from app.models.enums import Category, LogicalState, NotificationKind, NotificationStatus
from app.services import audit, integrations, text_safety
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.ha import actions as A
from app.services.ha import connection, notifier
from app.services.learning import feedback
from app.services.learning.feedback import MAX_PURPOSE_LENGTH
from app.services.trips import merge
from app.services.trips import segments as seg
from app.services.trips.segments import TripError

logger = logging.getLogger(__name__)

OUTCOME_APPLIED = "applied"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_EXPIRED = "expired"
OUTCOME_INVALID = "invalid"
OUTCOME_OPENED = "opened"
OUTCOME_SNOOZED = "snoozed"

_PHONE_SOURCES = ("ha", "demo")
MAX_REPLY_TEXT = 200  # E.4: textsvar 1–200 tecken efter tvätt

# Svar som gäller resans kategori och ärende i självgående läge (C.4).
_WORK_KEYS = (f"{A.WORK_PICK}0", f"{A.WORK_ALWAYS}0", A.WORK_TEXT)
_TRIP_KINDS = (NotificationKind.CLASSIFY, NotificationKind.PURPOSE, NotificationKind.COMPANY, NotificationKind.ASSUMED)


def _hash_key(source: str, user_id: uuid.UUID, key: str) -> str:
    return hashlib.sha256(f"{source}|{user_id}|{key}".encode()).hexdigest()


def _result(req: NotificationRequest | None, outcome: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "message": message,
        "public_id": req.public_id if req else None,
        "kind": req.kind if req else None,
        "status": req.status if req else None,
        "trip_id": str(req.logical_trip_id) if req and req.logical_trip_id else None,
        "follow_up": None,
        # Sant när telefonanvändaren behöver få veta att svaret inte gick igenom.
        "notify_user": outcome in (OUTCOME_EXPIRED, OUTCOME_INVALID),
        **extra,
    }


def _actor(user_id: uuid.UUID, source: str) -> Actor:
    if source in _PHONE_SOURCES:
        return Actor("ha", str(user_id), user_id)
    return audit.user_actor(user_id)


def _find_action(req: NotificationRequest, key: str) -> dict[str, Any] | None:
    return next((a for a in (req.actions or []) if a.get("key") == key), None)


def _load_active_trip(db: Session, req: NotificationRequest) -> LogicalTrip:
    trip = seg.load_trip(db, req.logical_trip_id) if req.logical_trip_id else None
    if trip is None or trip.state != LogicalState.ACTIVE:
        raise TripError("Resan har ändrats sedan frågan ställdes. Öppna körjournalen för att granska den.", "not_active")
    return trip


def _snooze(db: Session, req: NotificationRequest, user_id: uuid.UUID, now: datetime, minutes: float | None) -> datetime:
    cfg = settings_svc.get_section(db, user_id, "notifications")
    delay = timedelta(minutes=float(minutes if minutes else cfg["snooze_minutes"]))
    until = now + delay
    req.channel = None  # levereras på nytt när tiden är inne
    req.status = NotificationStatus.PENDING
    req.send_after = until
    if req.expires_at is not None and req.expires_at < until + timedelta(hours=1):
        req.expires_at = until + timedelta(hours=float(cfg["expire_after_hours"]))
    notifier.set_context(req, snoozed_until=until.isoformat(), snooze_count=int((req.context or {}).get("snooze_count", 0)) + 1)
    return until


def clean_reply(reply_text: str | None) -> str:
    """Textsvaret tvättat (sanitize_text). Tomt eller över 200 tecken ger TripError."""
    text = text_safety.sanitize_text(reply_text, limit=10_000)
    if not text:
        raise TripError("Ingen text angavs.", "empty_text")
    if len(text) > MAX_REPLY_TEXT:
        raise TripError(f"Texten får vara högst {MAX_REPLY_TEXT} tecken.", "text_too_long")
    return text


def _action_purpose(action: dict[str, Any]) -> str:
    text = text_safety.sanitize_text(action.get("purpose"))
    if not text:
        raise TripError("Det valda ärendet finns inte längre.", "empty_purpose")
    return text


def _owner_notice(trip: LogicalTrip) -> str | None:
    """Text från autopilot.on_owner_decision (t.ex. "Regeln för X frågar dig nu i stället")."""
    notice = getattr(trip, "_autopilot_notice", None)
    return notice if isinstance(notice, str) and notice else None


def _remember(
    db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, category: str, purpose: str | None, actor: Actor
) -> str | None:
    """Sparar ägarens egen regel ("Alltid …"). Returnerar en text om det inte gick."""
    from app.services.classify import autopilot

    trip = seg.load_trip(db, trip.id) or trip
    if not autopilot.can_remember(db, trip, purpose if category == Category.WORK else None):
        return "Ingen regel sparades för den här platsen."
    try:
        autopilot.remember_always(db, user_id=user_id, trip=trip, category=category, purpose=purpose, actor=actor)
    except ValueError:
        return "Ingen regel sparades för den här platsen."
    return None


def _decide_trip(
    db: Session, req: NotificationRequest, *, user_id: uuid.UUID, actor: Actor, channel: str, category: str,
    purpose: str | None, always: bool,
) -> dict[str, Any]:
    trip = _load_active_trip(db, req)
    if category == Category.WORK and purpose is not None and trip.category == Category.WORK and trip.has_mixed_categories:
        # Blandad resa: bara ärendet, delsträckornas fördelning rörs inte.
        trip = feedback.apply_decision(db, trip, user_id=user_id, actor=actor, channel=channel, purpose=purpose)
    else:
        trip = feedback.apply_decision(
            db, trip, user_id=user_id, actor=actor, channel=channel, category=category, purpose=purpose
        )
    label = "Tjänst" if category == Category.WORK else "Privat"
    message = f"Resan sparades som {label}" + (f": {purpose}." if purpose else ".")
    if always:
        failed = _remember(db, trip, user_id=user_id, category=category, purpose=purpose, actor=actor)
        message += f" {failed}" if failed else " Regeln sparades och gäller nästa gång."
    notice = _owner_notice(trip)
    if notice:
        message += f" {notice}"
    result = _result(req, OUTCOME_APPLIED, message, category=category)
    if purpose:
        result["purpose"] = purpose
    return result


def _apply_self_driving(
    db: Session, req: NotificationRequest, key: str, action: dict[str, Any], reply_text: str | None, *,
    user_id: uuid.UUID, actor: Actor, channel: str, always: bool,
) -> dict[str, Any] | None:
    """Svaren på frågorna i självgående läge (C.4). None när nyckeln inte hör hit."""
    from app.services.classify import autopilot

    kind = req.kind
    if kind == NotificationKind.CLASSIFY and key in _WORK_KEYS:
        purpose = clean_reply(reply_text) if key == A.WORK_TEXT else _action_purpose(action)
        return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.WORK,
                            purpose=purpose, always=always or key.startswith(A.WORK_ALWAYS))
    if kind == NotificationKind.CLASSIFY and key == A.PRIVATE_ALWAYS:
        return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.PRIVATE,
                            purpose=None, always=True)
    if kind == NotificationKind.PURPOSE and key == f"{A.PURPOSE_ALWAYS}0":
        return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.WORK,
                            purpose=_action_purpose(action), always=True)
    if kind in (NotificationKind.PURPOSE, NotificationKind.COMPANY, NotificationKind.ASSUMED) and key == A.PRIVATE:
        # På ett antagande är det en rättelse (A.3 b), som autopilot.on_owner_decision tar hand om.
        return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.PRIVATE,
                            purpose=None, always=always)
    if kind == NotificationKind.ASSUMED and key == A.WORK_TEXT:
        return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.WORK,
                            purpose=clean_reply(reply_text), always=always)
    if kind == NotificationKind.ASSUMED and key == A.OK:
        trip = _load_active_trip(db, req)
        autopilot.confirm_assumption(db, trip, actor=actor)
        return _result(req, OUTCOME_APPLIED, "Tack. Platsen räknas som kund, och nästa besök godkänns som tjänst.",
                       category=Category.WORK)
    if kind == NotificationKind.COMPANY and key == A.COMPANY_TEXT:
        text = clean_reply(reply_text)
        trip = _load_active_trip(db, req)
        autopilot.set_company(db, trip, text, actor=actor)
        return _result(req, OUTCOME_APPLIED, f"Sparat: Kundbesök {text}.", category=Category.WORK, company=text)
    return None


def _apply(
    db: Session,
    req: NotificationRequest,
    key: str,
    action: dict[str, Any],
    reply_text: str | None,
    *,
    user_id: uuid.UUID,
    source: str,
    now: datetime,
    always: bool = False,
) -> dict[str, Any]:
    """Utför åtgärden. Kastar TripError om den inte kan genomföras."""
    actor = _actor(user_id, source)
    channel = "ha" if source in _PHONE_SOURCES else "web"
    # "Gör alltid så här" finns bara i webben (RespondIn.always) och prövas med can_remember.
    always = always and source == "web"

    handled = _apply_self_driving(
        db, req, key, action, reply_text, user_id=user_id, actor=actor, channel=channel, always=always
    )
    if handled is not None:
        return handled

    if always and req.kind in (NotificationKind.CLASSIFY, NotificationKind.PURPOSE) and (
        key == A.PRIVATE or key == A.PURPOSE_TEXT or key.startswith(A.PURPOSE_PICK)
    ):
        if key == A.PRIVATE:
            return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.PRIVATE,
                                purpose=None, always=True)
        purpose = clean_reply(reply_text) if key == A.PURPOSE_TEXT else _action_purpose(action)
        return _decide_trip(db, req, user_id=user_id, actor=actor, channel=channel, category=Category.WORK,
                            purpose=purpose, always=True)

    if key in (A.WORK, A.PRIVATE) and req.kind == NotificationKind.CLASSIFY:
        trip = _load_active_trip(db, req)
        category = Category.WORK if key == A.WORK else Category.PRIVATE
        feedback.apply_decision(db, trip, user_id=user_id, actor=actor, channel=channel, category=category)
        label = "Arbete" if category == Category.WORK else "Privat"
        result = _result(req, OUTCOME_APPLIED, f"Resan klassades som {label}.", category=category)
        if category == Category.WORK and source in _PHONE_SOURCES:
            # Följdfråga om syftet direkt på telefonen (i webben frågar appen själv).
            follow = _follow_up_purpose(db, user_id, req.logical_trip_id, now)
            if follow is not None:
                result["follow_up"] = follow.public_id
        return result

    if req.kind == NotificationKind.PURPOSE and (key == A.PURPOSE_TEXT or key.startswith(A.PURPOSE_PICK)):
        if key == A.PURPOSE_TEXT:
            if not text_safety.sanitize_text(reply_text):
                raise TripError("Inget syfte angavs.", "empty_purpose")
            text = clean_reply(reply_text)  # E.4: tvättad text, 1–200 tecken
        else:
            text = (action.get("purpose") or "").strip()
            if not text:
                raise TripError("Det valda syftet finns inte längre.", "empty_purpose")
        if len(text) > MAX_PURPOSE_LENGTH:
            raise TripError(f"Syftet får vara högst {MAX_PURPOSE_LENGTH} tecken.", "purpose_too_long")
        trip = _load_active_trip(db, req)
        feedback.apply_decision(db, trip, user_id=user_id, actor=actor, channel=channel, purpose=text)
        return _result(req, OUTCOME_APPLIED, "Syftet sparades.", purpose=text)

    if key in (A.MERGE_YES, A.MERGE_NO) and req.kind == NotificationKind.MERGE:
        ctx = req.context or {}
        try:
            first, second = uuid.UUID(ctx["first_raw_trip_id"]), uuid.UUID(ctx["second_raw_trip_id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise TripError("Frågan saknar uppgifter om resorna.", "invalid_request") from exc
        if req.vehicle_id is None:
            raise TripError("Frågan saknar fordon.", "invalid_request")
        tolerance = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
        merged = merge.answer_question(
            db,
            vehicle_id=req.vehicle_id,
            first_raw_trip_id=first,
            second_raw_trip_id=second,
            accept=key == A.MERGE_YES,
            actor=actor,
            trigger="ha" if source in _PHONE_SOURCES else "rule_b",
            odometer_tolerance=tolerance,
        )
        if merged is not None:
            # Mönster, förslag och granskningsärenden uppdateras för den sammanslagna resan.
            feedback.after_decisions(db, user_id=user_id, vehicle_id=req.vehicle_id, actor=actor)
            return _result(req, OUTCOME_APPLIED, "Resorna slogs samman.", merged_trip_id=str(merged.id))
        return _result(req, OUTCOME_APPLIED, "Resorna behålls separata.")

    if key == A.TEST_OK and req.kind == NotificationKind.TEST:
        conn = connection.load(db, user_id).conn
        if conn is not None:
            integrations.update_config(conn, {"last_test_confirmed_at": now.isoformat()})
        return _result(req, OUTCOME_APPLIED, "Testnotisen bekräftades – svaren når fram.")

    raise TripError("Åtgärden passar inte den här frågan.", "invalid_action")


def _follow_up_purpose(
    db: Session, user_id: uuid.UUID, trip_id: uuid.UUID | None, now: datetime
) -> NotificationRequest | None:
    """Efter svaret "Arbete": fråga direkt efter syftet om det saknas."""
    if trip_id is None:
        return None
    cfg = settings_svc.get_section(db, user_id, "notifications")
    if not cfg["enabled"] or not cfg["kinds"].get("purpose", True):
        return None
    trip = seg.load_trip(db, trip_id)
    if trip is None or trip.state != LogicalState.ACTIVE or trip.work_km <= 0:
        return None
    if trip.purpose and trip.purpose.strip() and trip.purpose_confirmed:
        return None
    state = connection.load(db, user_id)
    if state.channel is None:
        return None
    vehicle = db.get(Vehicle, trip.vehicle_id)
    if vehicle is not None and vehicle.is_demo and not state.is_demo:
        return None
    follow = notifier.create_purpose_question(
        db, user_id=user_id, trip=trip, cfg=cfg, now=now, send_after=now, extra_context={"follow_up": True}
    )
    if follow is not None:
        try:
            notifier.deliver_now(db, follow, now)
        except Exception:  # följdfrågan ligger kvar och levereras av bakgrundsjobbet
            logger.exception("Följdfrågan kunde inte levereras direkt")
    return follow


def handle(
    db: Session,
    *,
    user_id: uuid.UUID,
    public_id: str,
    action: str,
    reply_text: str | None = None,
    idempotency_key: str,
    source: str = "ha",
    now: datetime | None = None,
    snooze_minutes: float | None = None,
    always: bool = False,
) -> dict[str, Any]:
    """Tar emot ett svar. Returnerar ett resultat med ``outcome``:
    applied | duplicate | expired | invalid | opened | snoozed."""
    now = now or utcnow()
    if source not in ("ha", "web", "demo"):
        raise ValueError("Okänd källa för svaret.")
    key_hash = _hash_key(source, user_id, idempotency_key)
    key = A.normalize_action(action or "")
    parsed = A.parse_action_id(action or "")
    if parsed is not None and not public_id:
        public_id = parsed[1]

    req = db.execute(
        select(NotificationRequest)
        .where(NotificationRequest.user_id == user_id, NotificationRequest.public_id == (public_id or ""))
        .with_for_update()
    ).scalar_one_or_none()

    previous = db.execute(
        select(NotificationResponse).where(NotificationResponse.idempotency_key == key_hash)
    ).scalar_one_or_none()
    if previous is not None:
        stored = dict(previous.result or {})
        stored.update({"outcome": OUTCOME_DUPLICATE, "first_outcome": previous.outcome, "notify_user": False})
        if req is not None:
            stored["status"] = req.status
        return stored

    if req is None:
        return _result(None, OUTCOME_INVALID, "Frågan finns inte.")

    resp = NotificationResponse(
        request_id=req.id,
        action=key[:40],
        reply_text=(reply_text or None) and reply_text[:2000],
        source=source,
        idempotency_key=key_hash,
        received_at=now,
        applied=False,
        outcome="processing",
    )
    db.add(resp)
    try:
        db.flush()
    except IntegrityError:
        # Samma svar behandlades samtidigt av en annan förfrågan.
        db.rollback()
        return _result(None, OUTCOME_DUPLICATE, "Svaret är redan mottaget.", notify_user=False)

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        result["status"] = req.status
        resp.outcome = result["outcome"]
        resp.applied = result["outcome"] == OUTCOME_APPLIED
        resp.result = result
        db.flush()
        return result

    action_def = _find_action(req, key)
    if action_def is None and key == A.LATER and source == "web":
        action_def = {"key": A.LATER, "title": "Senare"}  # allt kan skjutas upp från webben
    if action_def is None:
        return finish(_result(req, OUTCOME_INVALID, "Okänd åtgärd för den här frågan."))

    # Navigering (Granska/Öppna) ändrar aldrig några uppgifter.
    if key in A.NAVIGATION_ACTIONS:
        return finish(_result(req, OUTCOME_OPENED, "Öppna körjournalen för att granska.", notify_user=False))

    if req.status == NotificationStatus.ANSWERED:
        first = db.execute(
            select(NotificationResponse)
            .where(NotificationResponse.request_id == req.id, NotificationResponse.applied.is_(True))
            .order_by(NotificationResponse.received_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        base = dict(first.result) if first is not None and first.result else _result(req, OUTCOME_APPLIED, "")
        base.update({"outcome": OUTCOME_DUPLICATE, "message": "Frågan är redan besvarad.", "notify_user": False})
        return finish(base)
    if req.status == NotificationStatus.CANCELLED:
        return finish(_result(req, OUTCOME_DUPLICATE, "Frågan är redan hanterad i körjournalen.", notify_user=False))
    if req.status == NotificationStatus.FAILED and source in _PHONE_SOURCES:
        return finish(_result(req, OUTCOME_INVALID, "Frågan kunde inte levereras och är inte längre aktiv."))

    expired = req.status == NotificationStatus.EXPIRED or (req.expires_at is not None and req.expires_at <= now)
    if expired and source in _PHONE_SOURCES:
        if req.status in notifier.OPEN_STATUSES:
            req.status = NotificationStatus.EXPIRED
        return finish(
            _result(req, OUTCOME_EXPIRED, "Frågan har gått ut. Öppna SMGD DriveLog för att svara i appen.")
        )

    if key == A.LATER:
        until = _snooze(db, req, user_id, now, snooze_minutes)
        return finish(
            _result(req, OUTCOME_SNOOZED, "Frågan skjuts upp och ställs igen senare.", snoozed_until=until.isoformat(), notify_user=False)
        )

    try:
        result = _apply(db, req, key, action_def, reply_text, user_id=user_id, source=source, now=now, always=always)
    except TripError as exc:
        return finish(_result(req, OUTCOME_INVALID, exc.message, code=exc.code))

    was_delivered_elsewhere = source == "web"
    req.status = NotificationStatus.ANSWERED
    req.answered_at = now
    if was_delivered_elsewhere:
        notifier.mark_clear(req)  # besvarad i webben → ta bort notisen från telefonen
    elif req.channel and ":" in req.channel:
        req.channel = req.channel.split(":")[0]
    if req.digest_id is not None:
        notifier.refresh_digest(db, req.digest_id)
    if req.logical_trip_id is not None:
        notifier.cancel_for_trip(db, req.logical_trip_id)  # syskonfrågor som blivit inaktuella
    return finish(result)
