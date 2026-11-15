"""Notismotorn: skapar frågor, levererar dem enligt policy och håller ordning på dem.

Livscykel för en fråga (``NotificationRequest``):

    pending ──leverans──▶ sent ──svar──▶ answered
       │                   │
       │                   ├─ påminnelse (högst max_reminders)
       └──────┬────────────┘
              ├─ expired   (efter expire_after_hours; kan fortfarande besvaras i webben)
              └─ cancelled (saken avgjordes på annat sätt)

Kolumnen ``channel`` visar leveransläget:

    None             ännu inte levererad
    demo             visad i den simulerade telefonen (demoläge)
    ha_push          skickad via Home Assistants REST-API
    ha_pull:out      frisläppt till HA-integrationens utkorg, väntar på kvittens
    ha_pull          levererad av HA-integrationen
    ha_pull:remind   påminnelse väntar i utkorgen
    <kanal>:clear    notisen ska tas bort från telefonen
    digest           ingår i en samlingsnotis

Ingen fråga går förlorad om Home Assistant är nere: i push-läget ligger frågan kvar
som ``pending`` och försöks igen med växande intervall, i pull-läget ligger den kvar
i utkorgen tills integrationen kvitterat leveransen.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import (
    IntegrationConnection,
    LogicalTrip,
    NotificationRequest,
    ReviewItem,
    Vehicle,
)
from app.models.enums import (
    Category,
    IntegrationProvider,
    IntegrationStatus,
    LogicalState,
    NotificationKind,
    NotificationStatus,
    ReviewKind,
    ReviewState,
    ReviewStatus,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.ha import actions as A
from app.services.ha import connection, messages, policy, summaries
from app.services.ha.client import HAClient, HAError
from app.services.ha.connection import CHANNEL_DEMO, CHANNEL_DIGEST, CHANNEL_PULL, CHANNEL_PUSH, HAState

logger = logging.getLogger(__name__)

QUESTION_KINDS = (NotificationKind.CLASSIFY, NotificationKind.PURPOSE, NotificationKind.MERGE)
SUMMARY_KINDS = (NotificationKind.WEEKLY, NotificationKind.DAILY)
OPEN_STATUSES = (NotificationStatus.PENDING, NotificationStatus.SENT)
RECENT_WINDOW = timedelta(hours=48)
DAILY_WINDOW = timedelta(minutes=90)
MAX_CLEAR_ATTEMPTS = 3
MAX_PULL_FAILURES = 20

PULL_OUT = f"{CHANNEL_PULL}:out"
PULL_REMIND = f"{CHANNEL_PULL}:remind"
_OUTBOX_CHANNELS = (PULL_OUT, PULL_REMIND, f"{CHANNEL_PULL}:clear")
_DELIVERED_CHANNELS = (CHANNEL_DEMO, CHANNEL_PUSH, CHANNEL_PULL)
_STAT_KEYS = ("sent", "digests", "reminders", "expired", "deferred_quiet", "deferred_cap", "failed", "cancelled", "cleared")


# ----------------------------------------------------------------------------- hjälpare


def new_public_id() -> str:
    return secrets.token_hex(8)


def set_context(req: NotificationRequest, **changes: Any) -> None:
    ctx = dict(req.context or {})
    for key, value in changes.items():
        if value is None:
            ctx.pop(key, None)
        else:
            ctx[key] = value
    req.context = ctx


def _base_channel(req: NotificationRequest) -> str | None:
    return req.channel.split(":")[0] if req.channel else None


def open_path(req: NotificationRequest) -> str:
    if req.kind in (NotificationKind.DIGEST, *SUMMARY_KINDS):
        return "/review"
    return f"/m/q/{req.public_id}"


def service_data(req: NotificationRequest, *, reminder: bool = False) -> dict[str, Any]:
    """Färdig tjänstedata för notify.mobile_app_<enhet>."""
    return A.build_notify_data(
        public_id=req.public_id,
        kind=req.kind,
        title=req.title,
        message=req.message,
        actions=list(req.actions or []),
        base_url=get_config().base_url,
        open_path=open_path(req),
        reminder=reminder,
    )


def _exists(db: Session, user_id: uuid.UUID, dedupe_key: str) -> bool:
    return (
        db.execute(
            select(NotificationRequest.id)
            .where(NotificationRequest.user_id == user_id, NotificationRequest.dedupe_key == dedupe_key)
            .limit(1)
        ).first()
        is not None
    )


def _create(
    db: Session,
    *,
    user_id: uuid.UUID,
    kind: str,
    built: dict[str, Any],
    public_id: str,
    now: datetime,
    cfg: dict[str, Any],
    vehicle_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
    review_item_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    send_after: datetime | None = None,
    extra_context: dict[str, Any] | None = None,
) -> NotificationRequest:
    req = NotificationRequest(
        public_id=public_id,
        user_id=user_id,
        vehicle_id=vehicle_id,
        kind=kind,
        logical_trip_id=trip_id,
        review_item_id=review_item_id,
        dedupe_key=dedupe_key,
        title=built["title"],
        message=built["message"],
        actions=built["actions"],
        context={**built.get("context", {}), **(extra_context or {})},
        status=NotificationStatus.PENDING,
        created_at=now,
        send_after=send_after if send_after is not None else now,
        expires_at=now + timedelta(hours=float(cfg["expire_after_hours"])),
    )
    db.add(req)
    db.flush()
    return req


def _recent(trip: LogicalTrip | None, now: datetime) -> bool:
    """Endast nyligen avslutade resor ger notiser – aldrig historisk import."""
    if trip is None:
        return False
    when = trip.ended_at or trip.sort_time
    # Demosimulatorn kan lägga resor en bit fram i tiden när flera simuleras i följd.
    return when is not None and -timedelta(hours=24) <= (now - when) <= RECENT_WINDOW


def previous_purposes(db: Session, user_id: uuid.UUID, trip: LogicalTrip, limit: int = 3) -> list[str]:
    """Tidigare bekräftade syften för samma rutt/destination (till knapparna)."""
    from app.services.classify import purpose as purpose_mod
    from app.services.classify import signals as sig
    from app.services.classify.purpose import NEEDS_CONFIRMATION_SUFFIX

    try:
        ctx = sig.build_context(
            db,
            trip,
            user_id=user_id,
            tz=settings_svc.user_timezone(db, user_id),
            tax=settings_svc.get_section(db, user_id, "tax"),
            ai=settings_svc.get_section(db, user_id, "ai"),
            abbreviations=settings_svc.get_section(db, user_id, "abbreviations")["map"],
        )
        cands = purpose_mod.candidate_purposes(ctx, limit=limit + 2)
    except Exception:  # syftesknapparna är en bekvämlighet – får aldrig stoppa notisen
        logger.exception("Kunde inte ta fram tidigare syften")
        return []
    out: list[str] = []
    for p in cands:
        text = (p.get("text") or "").strip()
        if text and NEEDS_CONFIRMATION_SUFFIX not in text.lower() and text not in out:
            out.append(text)
    return out[:limit]


# ----------------------------------------------------------------------------- skapa frågor


def create_classify_question(
    db: Session, *, user_id: uuid.UUID, trip: LogicalTrip, cfg: dict[str, Any], now: datetime,
    send_after: datetime | None = None, extra_context: dict[str, Any] | None = None,
) -> NotificationRequest | None:
    key = f"classify:{trip.id}"
    if _exists(db, user_id, key):
        return None
    pid = new_public_id()
    built = messages.classify(db, trip, pid, hide_locations=bool(cfg["hide_locations_on_lock_screen"]))
    return _create(
        db, user_id=user_id, kind=NotificationKind.CLASSIFY, built=built, public_id=pid, now=now, cfg=cfg,
        vehicle_id=trip.vehicle_id, trip_id=trip.id, dedupe_key=key, send_after=send_after, extra_context=extra_context,
    )


def create_purpose_question(
    db: Session, *, user_id: uuid.UUID, trip: LogicalTrip, cfg: dict[str, Any], now: datetime,
    send_after: datetime | None = None, extra_context: dict[str, Any] | None = None,
) -> NotificationRequest | None:
    key = f"purpose:{trip.id}"
    if _exists(db, user_id, key):
        return None
    pid = new_public_id()
    built = messages.purpose(
        db, trip, pid, hide_locations=bool(cfg["hide_locations_on_lock_screen"]),
        previous=previous_purposes(db, user_id, trip),
    )
    return _create(
        db, user_id=user_id, kind=NotificationKind.PURPOSE, built=built, public_id=pid, now=now, cfg=cfg,
        vehicle_id=trip.vehicle_id, trip_id=trip.id, dedupe_key=key, send_after=send_after, extra_context=extra_context,
    )


def create_merge_question(
    db: Session, *, user_id: uuid.UUID, item: ReviewItem, cfg: dict[str, Any], now: datetime,
    send_after: datetime | None = None,
) -> NotificationRequest | None:
    key = f"merge:{item.id}"
    if _exists(db, user_id, key):
        return None
    data = item.data or {}
    pid = new_public_id()
    built = messages.merge(pid, data.get("minutes_low"), data.get("minutes_high"), bool(data.get("exact", True)))
    return _create(
        db, user_id=user_id, kind=NotificationKind.MERGE, built=built, public_id=pid, now=now, cfg=cfg,
        vehicle_id=item.vehicle_id, review_item_id=item.id, dedupe_key=key, send_after=send_after,
        extra_context={
            "first_raw_trip_id": data.get("first_raw_trip_id"),
            "second_raw_trip_id": data.get("second_raw_trip_id"),
            "first_trip_id": data.get("first_trip_id"),
            "second_trip_id": data.get("second_trip_id"),
        },
    )


def enqueue_for_changes(
    db: Session,
    *,
    user_id: uuid.UUID,
    vehicle: Vehicle,
    merge_items: list[ReviewItem],
    trip_items: list[tuple[LogicalTrip, list[str]]],
    now: datetime | None = None,
) -> list[NotificationRequest]:
    """Skapar frågor för nya granskningsärenden. Anropas av trips.pipeline.

    Inga frågor skapas om notiser är avstängda, om ingen leveranskanal finns, för
    historiska resor (äldre än 48 timmar) eller för demofordonet via riktig HA.
    """
    now = now or utcnow()
    cfg = settings_svc.get_section(db, user_id, "notifications")
    if not cfg["enabled"]:
        return []
    state = connection.load(db, user_id)
    channel = state.channel
    if channel is None:
        return []
    if vehicle.is_demo and channel != CHANNEL_DEMO:
        return []
    kinds = cfg["kinds"]
    # I demoläget visas frågorna direkt i den simulerade telefonen (ingen väntan på sammanläggning).
    demo = channel == CHANNEL_DEMO
    send_after = now if demo else now + timedelta(minutes=float(cfg["batch_window_minutes"]))
    created: list[NotificationRequest] = []

    if kinds.get("merge", True):
        for item in merge_items:
            if item.kind != ReviewKind.MERGE or item.status != ReviewState.OPEN:
                continue
            trip = db.get(LogicalTrip, item.logical_trip_id) if item.logical_trip_id else None
            if not _recent(trip, now):
                continue
            req = create_merge_question(db, user_id=user_id, item=item, cfg=cfg, now=now, send_after=send_after)
            if req is not None:
                created.append(req)

    for trip, new_kinds in trip_items:
        if trip.state != LogicalState.ACTIVE or not _recent(trip, now):
            continue
        asked = False
        if kinds.get("classify", True) and (ReviewKind.CATEGORY in new_kinds or ReviewKind.TAX_UNCERTAIN in new_kinds):
            req = create_classify_question(db, user_id=user_id, trip=trip, cfg=cfg, now=now, send_after=send_after)
            if req is not None:
                created.append(req)
                asked = True
        if (
            not asked
            and kinds.get("purpose", True)
            and ReviewKind.PURPOSE in new_kinds
            and trip.category == Category.WORK
            and ReviewKind.CATEGORY not in new_kinds
        ):
            req = create_purpose_question(db, user_id=user_id, trip=trip, cfg=cfg, now=now, send_after=send_after)
            if req is not None:
                created.append(req)
    db.flush()
    if demo and created:
        deliver_user(db, user_id, now)
    return created


def create_test(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> NotificationRequest:
    now = now or utcnow()
    cfg = settings_svc.get_section(db, user_id, "notifications")
    pid = new_public_id()
    return _create(
        db, user_id=user_id, kind=NotificationKind.TEST, built=messages.test(pid), public_id=pid, now=now,
        cfg={**cfg, "expire_after_hours": 1},
    )


# ----------------------------------------------------------------------------- avsluta frågor


def mark_clear(req: NotificationRequest) -> None:
    """Begär att notisen tas bort från telefonen om den har levererats dit."""
    base = _base_channel(req)
    if req.channel == PULL_OUT:
        req.channel = None  # hann aldrig levereras
    elif base in (CHANNEL_PUSH, CHANNEL_PULL):
        req.channel = f"{base}:clear"
        set_context(req, clear_attempts=None)


def _cancel(db: Session, req: NotificationRequest, reason: str) -> None:
    if req.status not in OPEN_STATUSES:
        return
    req.status = NotificationStatus.CANCELLED
    set_context(req, cancel_reason=reason)
    mark_clear(req)
    if req.digest_id is not None:
        refresh_digest(db, req.digest_id)


def refresh_digest(db: Session, digest_id: uuid.UUID) -> None:
    """Stänger samlingsnotisen när inga av dess frågor längre är öppna."""
    digest = db.get(NotificationRequest, digest_id)
    if digest is None or digest.status not in OPEN_STATUSES:
        return
    db.flush()
    still_open = db.execute(
        select(NotificationRequest.id)
        .where(NotificationRequest.digest_id == digest_id, NotificationRequest.status.in_(OPEN_STATUSES))
        .limit(1)
    ).first()
    if still_open is None:
        digest.status = NotificationStatus.CANCELLED
        set_context(digest, cancel_reason="all_members_handled")
        mark_clear(digest)


def _trip_question_moot(trip: LogicalTrip | None, kind: str) -> bool:
    if trip is None or trip.state != LogicalState.ACTIVE:
        return True
    if kind == NotificationKind.CLASSIFY:
        return trip.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED, ReviewStatus.AUTO)
    if kind == NotificationKind.PURPOSE:
        return trip.work_km <= 0 or bool(trip.purpose and trip.purpose.strip() and trip.purpose_confirmed)
    return False


def still_relevant(db: Session, req: NotificationRequest) -> bool:
    """Är frågan fortfarande obesvarad i körjournalen?"""
    if req.kind in (NotificationKind.CLASSIFY, NotificationKind.PURPOSE):
        trip = db.get(LogicalTrip, req.logical_trip_id) if req.logical_trip_id else None
        return not _trip_question_moot(trip, req.kind)
    if req.kind == NotificationKind.MERGE:
        item = db.get(ReviewItem, req.review_item_id) if req.review_item_id else None
        return item is not None and item.status in (ReviewState.OPEN, ReviewState.SNOOZED)
    return True


def cancel_for_trip(
    db: Session, trip_id: uuid.UUID, *, kinds: tuple[str, ...] | None = None, force: bool = False
) -> int:
    """Avbryter frågor om en resa som användaren redan avgjort (t.ex. i webben).

    Utan ``force`` avbryts bara de frågor som faktiskt blivit inaktuella: en
    klassningsfråga när resan är godkänd, en syftesfråga när syftet är bekräftat
    eller resan inte längre är en tjänsteresa.
    """
    db.flush()
    trip = db.get(LogicalTrip, trip_id)
    reqs = db.execute(
        select(NotificationRequest).where(
            NotificationRequest.logical_trip_id == trip_id,
            NotificationRequest.status.in_(OPEN_STATUSES),
            NotificationRequest.kind.in_((NotificationKind.CLASSIFY, NotificationKind.PURPOSE)),
        )
    ).scalars().all()
    count = 0
    for req in reqs:
        if kinds is not None and req.kind not in kinds:
            continue
        if force or _trip_question_moot(trip, req.kind):
            _cancel(db, req, "handled_elsewhere")
            count += 1
    db.flush()
    return count


def cancel_for_review_item(db: Session, item_id: uuid.UUID) -> int:
    """Avbryter frågor kopplade till ett granskningsärende (t.ex. sammanslagning)."""
    db.flush()
    reqs = db.execute(
        select(NotificationRequest).where(
            NotificationRequest.review_item_id == item_id, NotificationRequest.status.in_(OPEN_STATUSES)
        )
    ).scalars().all()
    for req in reqs:
        _cancel(db, req, "handled_elsewhere")
    db.flush()
    return len(reqs)


def cancel_all_open(db: Session, user_id: uuid.UUID, *, only_channel: str | None = None) -> int:
    db.flush()
    reqs = db.execute(
        select(NotificationRequest).where(
            NotificationRequest.user_id == user_id, NotificationRequest.status.in_(OPEN_STATUSES)
        )
    ).scalars().all()
    count = 0
    for req in reqs:
        if only_channel is not None and _base_channel(req) != only_channel:
            continue
        req.status = NotificationStatus.CANCELLED
        set_context(req, cancel_reason="integration_changed")
        if req.channel and ":" in req.channel:
            req.channel = _base_channel(req)
        count += 1
    db.flush()
    return count


# ----------------------------------------------------------------------------- leverans


class _Delivery:
    """Leveranstillstånd för en användare under en körning."""

    def __init__(self, db: Session, state: HAState, now: datetime):
        self.db, self.state, self.now = db, state, now
        self.channel = state.channel
        self._client: HAClient | None = None
        self.offline = False
        self.stats: Counter[str] = Counter()

    def client(self) -> HAClient:
        if self._client is None:
            self._client = self.state.client()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _push(self, data: dict[str, Any]) -> None:
        assert self.state.notify_service
        self.client().notify(self.state.notify_service, data)

    def send(self, req: NotificationRequest, *, reminder: bool = False) -> bool:
        """Levererar (eller släpper till utkorgen). False om HA inte kunde nås."""
        now = self.now
        if self.channel == CHANNEL_DEMO:
            req.channel = CHANNEL_DEMO
        elif self.channel == CHANNEL_PULL:
            # Frisläppt till utkorgen; räknas som levererad först när integrationen kvitterat.
            if reminder:
                req.channel = PULL_REMIND
            else:
                req.channel = PULL_OUT
                req.sent_at = now
            return True
        elif self.channel == CHANNEL_PUSH:
            conn = self.state.conn
            assert conn is not None
            if self.offline:
                if not reminder:
                    req.send_after = now + policy.retry_delay(max(1, req.delivery_attempts or 0))
                return False
            try:
                self._push(service_data(req, reminder=reminder))
            except HAError as exc:
                self.offline = True
                self.stats["failed"] += 1
                req.delivery_attempts = (req.delivery_attempts or 0) + 1
                req.last_error = exc.message
                integrations.mark_error(conn, exc.message, needs_reauth=exc.kind == "auth")
                if not reminder:
                    req.send_after = now + policy.retry_delay(req.delivery_attempts)
                logger.warning("Notis kunde inte levereras till Home Assistant: %s", exc.message)
                return False
            integrations.mark_success(conn, now)
            req.channel = CHANNEL_PUSH
            req.last_error = None
        else:
            return False
        if reminder:
            req.reminder_count = (req.reminder_count or 0) + 1
            req.last_reminder_at = now
        else:
            req.status = NotificationStatus.SENT
            req.sent_at = now
        return True

    def clear(self, req: NotificationRequest) -> None:
        """Tar bort en levererad notis från telefonen (endast push; pull går via utkorgen)."""
        if _base_channel(req) != CHANNEL_PUSH or self.channel != CHANNEL_PUSH or self.offline:
            if self.channel != CHANNEL_PUSH and _base_channel(req) == CHANNEL_PUSH:
                req.channel = CHANNEL_PUSH  # kan inte längre rensas via push
            return
        try:
            self._push(A.build_clear_data(req.public_id))
        except HAError as exc:
            self.offline = self.offline or exc.kind == "unreachable"
            attempts = int((req.context or {}).get("clear_attempts", 0)) + 1
            set_context(req, clear_attempts=attempts)
            if attempts >= MAX_CLEAR_ATTEMPTS:
                req.channel = CHANNEL_PUSH
            return
        req.channel = CHANNEL_PUSH
        set_context(req, clear_attempts=None)
        self.stats["cleared"] += 1


def _units_today(db: Session, user_id: uuid.UUID, day_start: datetime) -> int:
    """Antal levererade notiser (inklusive påminnelser) under det lokala dygnet."""
    rows = db.execute(
        select(NotificationRequest).where(
            NotificationRequest.user_id == user_id,
            or_(NotificationRequest.sent_at >= day_start, NotificationRequest.last_reminder_at >= day_start),
        )
    ).scalars().all()
    units = 0
    for r in rows:
        if r.kind not in (*QUESTION_KINDS, NotificationKind.DIGEST) or r.digest_id is not None:
            continue
        if (r.context or {}).get("follow_up"):
            continue
        if r.sent_at is not None and r.sent_at >= day_start:
            units += 1
        if r.last_reminder_at is not None and r.last_reminder_at >= day_start:
            units += 1
    return units


def _bypasses_policy(req: NotificationRequest) -> bool:
    """Testnotiser, schemalagda sammanställningar och direkta följdfrågor går alltid fram."""
    return req.kind in (NotificationKind.TEST, *SUMMARY_KINDS) or bool((req.context or {}).get("follow_up"))


def deliver_user(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, int]:
    """Kör hela leveranspolicyn för en användare."""
    now = now or utcnow()
    cfg = settings_svc.get_section(db, user_id, "notifications")
    tz: ZoneInfo = settings_svc.user_timezone(db, user_id)
    state = connection.load(db, user_id)
    delivery = _Delivery(db, state, now)
    stats = delivery.stats
    try:
        reqs = list(
            db.execute(
                select(NotificationRequest)
                .where(NotificationRequest.user_id == user_id, NotificationRequest.status.in_(OPEN_STATUSES))
                .order_by(NotificationRequest.created_at.asc())
            ).scalars()
        )

        # 1. Expirering – frågan kan därefter bara besvaras i webbappen.
        for r in reqs:
            if r.expires_at is not None and r.expires_at <= now:
                r.status = NotificationStatus.EXPIRED
                mark_clear(r)
                stats["expired"] += 1
                if r.digest_id is not None:
                    refresh_digest(db, r.digest_id)
        reqs = [r for r in reqs if r.status in OPEN_STATUSES]

        # 2. Rensa notiser på telefonen för avgjorda frågor (push).
        db.flush()  # sessionen har autoflush avstängt
        for r in db.execute(
            select(NotificationRequest).where(
                NotificationRequest.user_id == user_id, NotificationRequest.channel == f"{CHANNEL_PUSH}:clear"
            )
        ).scalars():
            delivery.clear(r)

        channel = delivery.channel
        # Bytt leveransläge: det som låg i pull-utkorgen levereras om via den nya kanalen.
        if channel != CHANNEL_PULL:
            for r in reqs:
                if r.channel == PULL_OUT:
                    r.channel = None
                elif r.channel == PULL_REMIND:
                    r.channel = CHANNEL_PULL
        if channel is None:
            return {k: stats.get(k, 0) for k in _STAT_KEYS}

        # 3. Frågor som blivit inaktuella avbryts tyst; demofordon går aldrig via riktig HA.
        demo_vehicles: set[uuid.UUID] = set()
        if channel != CHANNEL_DEMO:
            demo_vehicles = {
                v
                for (v,) in db.execute(
                    select(Vehicle.id).where(Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(True))
                ).all()
            }
        for r in reqs:
            if r.vehicle_id in demo_vehicles or not still_relevant(db, r):
                _cancel(db, r, "no_longer_relevant")
                stats["cancelled"] += 1
        reqs = [r for r in reqs if r.status in OPEN_STATUSES]

        due = [
            r
            for r in reqs
            if r.status == NotificationStatus.PENDING
            and r.channel is None
            and (r.send_after is None or r.send_after <= now)
        ]
        bypass = [r for r in due if _bypasses_policy(r)]
        regular = [r for r in due if not _bypasses_policy(r)]

        for r in bypass:
            if delivery.send(r):
                stats["sent"] += 1

        enabled = bool(cfg["enabled"])
        # Den simulerade telefonen i demoläget har inga tysta timmar.
        quiet = channel != CHANNEL_DEMO and policy.in_quiet_hours(
            now.astimezone(tz), cfg["quiet_start"], cfg["quiet_end"]
        )
        remaining = policy.remaining_today(
            int(cfg["max_per_day"]), _units_today(db, user_id, policy.local_day_start(now, tz))
        )

        def room() -> bool:
            return remaining is None or remaining > 0

        def used() -> None:
            nonlocal remaining
            if remaining is not None:
                remaining -= 1

        if regular and enabled:
            if quiet:
                stats["deferred_quiet"] += len(regular)
            elif not room():
                stats["deferred_cap"] += len(regular)
            else:
                questions = [r for r in regular if r.kind in QUESTION_KINDS]
                others = [r for r in regular if r.kind not in QUESTION_KINDS]
                threshold = int(cfg["digest_threshold"])
                if questions and (len(questions) > threshold or (remaining is not None and len(questions) > remaining)):
                    # Sammanläggning: en samlingsnotis i stället för många enskilda.
                    built = messages.digest(len(questions))
                    digest = NotificationRequest(
                        public_id=new_public_id(),
                        user_id=user_id,
                        kind=NotificationKind.DIGEST,
                        title=built["title"],
                        message=built["message"],
                        actions=built["actions"],
                        context=built["context"],
                        status=NotificationStatus.PENDING,
                        created_at=now,
                        send_after=now,
                        expires_at=now + timedelta(hours=float(cfg["expire_after_hours"])),
                    )
                    if delivery.send(digest):
                        db.add(digest)
                        db.flush()
                        for r in questions:
                            r.digest_id = digest.id
                            r.status = NotificationStatus.SENT
                            r.sent_at = now
                            r.channel = CHANNEL_DIGEST
                        stats["digests"] += 1
                        stats["sent"] += 1
                        used()
                    else:
                        for r in questions:
                            r.send_after = now + policy.retry_delay(max(1, digest.delivery_attempts or 0, r.delivery_attempts or 0))
                            r.delivery_attempts = (r.delivery_attempts or 0) + 1
                            r.last_error = digest.last_error
                else:
                    for r in questions:
                        if not room():
                            stats["deferred_cap"] += 1
                            continue
                        if delivery.send(r):
                            stats["sent"] += 1
                            used()
                for r in others:  # t.ex. en uppskjuten samlingsnotis
                    if not room():
                        stats["deferred_cap"] += 1
                        continue
                    if delivery.send(r):
                        stats["sent"] += 1
                        used()

        # 4. Påminnelser för obesvarade frågor.
        max_reminders = int(cfg["max_reminders"])
        if enabled and max_reminders > 0 and not quiet:
            wait = timedelta(hours=float(cfg["reminder_after_hours"]))
            for r in reqs:
                if (
                    r.status != NotificationStatus.SENT
                    or r.digest_id is not None
                    or r.kind not in (*QUESTION_KINDS, NotificationKind.DIGEST)
                    or r.channel not in _DELIVERED_CHANNELS
                    or (r.reminder_count or 0) >= max_reminders
                ):
                    continue
                base = r.last_reminder_at or r.sent_at
                if base is None or base + wait > now or not room():
                    continue
                if delivery.send(r, reminder=True):
                    stats["reminders"] += 1
                    used()
        db.flush()
    finally:
        delivery.close()
    return {k: stats.get(k, 0) for k in _STAT_KEYS}


def deliver_due(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Levererar allt som är moget. Körs av bakgrundsjobbet varje minut."""
    now = now or utcnow()
    total: Counter[str] = Counter()
    user_ids = {
        u
        for (u,) in db.execute(
            select(NotificationRequest.user_id)
            .where(
                or_(
                    NotificationRequest.status.in_(OPEN_STATUSES),
                    NotificationRequest.channel == f"{CHANNEL_PUSH}:clear",
                )
            )
            .distinct()
        ).all()
    }
    for user_id in user_ids:
        # En sparpunkt per användare: ett databasfel för en användare rullar bara tillbaka
        # dennes ändringar och lämnar sessionen användbar för övriga.
        try:
            with db.begin_nested():
                total.update(deliver_user(db, user_id, now))
        except Exception:
            logger.exception("Notisleverans misslyckades för en användare")
            total["failed"] += 1
    db.flush()
    return {k: total.get(k, 0) for k in _STAT_KEYS}


def deliver_now(db: Session, req: NotificationRequest, now: datetime | None = None) -> bool:
    """Levererar en enskild notis direkt, förbi tysta timmar och dygnsgräns
    (testnotiser och följdfrågor direkt efter ett svar)."""
    now = now or utcnow()
    state = connection.load(db, req.user_id)
    delivery = _Delivery(db, state, now)
    try:
        ok = delivery.send(req) if delivery.channel is not None else False
    finally:
        delivery.close()
    db.flush()
    return ok


# ----------------------------------------------------------------------------- pull-läge


def outbox(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> list[dict[str, Any]]:
    """Notiser som HA-integrationen ska leverera (pull-läget)."""
    now = now or utcnow()
    state = connection.load(db, user_id)
    if state.channel != CHANNEL_PULL:
        return []
    deliver_user(db, user_id, now)
    rows = db.execute(
        select(NotificationRequest)
        .where(NotificationRequest.user_id == user_id, NotificationRequest.channel.in_(_OUTBOX_CHANNELS))
        .order_by(NotificationRequest.created_at.asc())
        .limit(50)
    ).scalars().all()
    items: list[dict[str, Any]] = []
    for r in rows:
        if r.channel == f"{CHANNEL_PULL}:clear":
            items.append(
                {"id": r.public_id, "type": "clear", "service": state.notify_service, "data": A.build_clear_data(r.public_id)}
            )
        elif r.status in OPEN_STATUSES:
            items.append(
                {
                    "id": r.public_id,
                    "type": "notify",
                    "service": state.notify_service,
                    "data": service_data(r, reminder=r.channel == PULL_REMIND),
                }
            )
        else:
            r.channel = CHANNEL_PULL  # avgjord innan den hann hämtas
    db.flush()
    return items


def outbox_ack(
    db: Session,
    user_id: uuid.UUID,
    *,
    delivered: list[str],
    failed: list[dict[str, str]] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Kvittens från HA-integrationen på levererade (eller misslyckade) notiser."""
    now = now or utcnow()
    ok = bad = 0

    def find(pid: str) -> NotificationRequest | None:
        return db.execute(
            select(NotificationRequest).where(
                NotificationRequest.user_id == user_id, NotificationRequest.public_id == pid
            )
        ).scalar_one_or_none()

    for pid in delivered:
        r = find(pid)
        if r is None or r.channel not in _OUTBOX_CHANNELS:
            continue
        if r.channel == PULL_OUT:
            if r.status == NotificationStatus.PENDING:
                r.status = NotificationStatus.SENT
            r.sent_at = now
        elif r.channel == PULL_REMIND:
            r.reminder_count = (r.reminder_count or 0) + 1
            r.last_reminder_at = now
        r.channel = CHANNEL_PULL
        r.last_error = None
        ok += 1
    for entry in failed or []:
        r = find(entry.get("id", ""))
        if r is None or r.channel not in _OUTBOX_CHANNELS:
            continue
        r.delivery_attempts = (r.delivery_attempts or 0) + 1
        r.last_error = (entry.get("error") or "Leveransen misslyckades i Home Assistant.")[:300]
        if r.delivery_attempts >= MAX_PULL_FAILURES:
            if r.status in OPEN_STATUSES and r.channel == PULL_OUT:
                r.status = NotificationStatus.FAILED
            r.channel = CHANNEL_PULL
        bad += 1
    conn = connection.load(db, user_id).conn
    if conn is not None and ok:
        integrations.mark_success(conn, now)
    db.flush()
    return {"acknowledged": ok, "failed": bad}


# ----------------------------------------------------------------------------- schemalagt


def _has_channel_users(db: Session) -> list[uuid.UUID]:
    return [
        u
        for (u,) in db.execute(
            select(IntegrationConnection.user_id).where(
                IntegrationConnection.provider == IntegrationProvider.HOME_ASSISTANT,
                IntegrationConnection.status != IntegrationStatus.NOT_CONFIGURED,
            )
        ).all()
    ]


def run_scheduled(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Veckosammanställning och daglig avstämning. Idempotent per period och användare.

    Siffrorna räknas ur lagrade resor: kilometer per segmentkategori och resor med
    öppna granskningsärenden.
    """
    now = now or utcnow()
    stats = {"weekly": 0, "daily": 0, "daily_nothing_to_report": 0}
    for user_id in _has_channel_users(db):
        cfg = settings_svc.get_section(db, user_id, "notifications")
        if not cfg["enabled"]:
            continue
        state = connection.load(db, user_id)
        if state.channel is None:
            continue
        tz = settings_svc.user_timezone(db, user_id)
        local = now.astimezone(tz)
        kinds = cfg["kinds"]
        created = False

        if kinds.get("weekly", True) and local.weekday() == int(cfg["weekly_day"]):
            if local.time() >= policy.parse_hhmm(cfg["weekly_time"]):
                iso = local.isocalendar()
                key = f"weekly:{iso.year}-W{iso.week:02d}"
                if not _exists(db, user_id, key):
                    summary = summaries.period_summary(db, user_id, policy.local_week_start(now, tz), now)
                    pid = new_public_id()
                    _create(
                        db, user_id=user_id, kind=NotificationKind.WEEKLY, built=messages.weekly(summary),
                        public_id=pid, now=now, cfg=cfg, dedupe_key=key,
                    )
                    stats["weekly"] += 1
                    created = True

        if kinds.get("daily", False):
            at = policy.parse_hhmm(cfg["daily_time"])
            scheduled = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
            today = local.date().isoformat()
            conn = state.conn
            assert conn is not None
            if timedelta(0) <= (local - scheduled) <= DAILY_WINDOW and (conn.config or {}).get("last_daily") != today:
                integrations.update_config(conn, {"last_daily": today})
                summary = summaries.period_summary(db, user_id, policy.local_day_start(now, tz), now)
                key = f"daily:{today}"
                if summary["review_count"] > 0 and not _exists(db, user_id, key):
                    pid = new_public_id()
                    _create(
                        db, user_id=user_id, kind=NotificationKind.DAILY, built=messages.daily(summary),
                        public_id=pid, now=now, cfg=cfg, dedupe_key=key,
                    )
                    stats["daily"] += 1
                    created = True
                else:
                    stats["daily_nothing_to_report"] += 1  # inget att fråga om → ingen onödig notis
        if created:
            deliver_user(db, user_id, now)
    db.flush()
    return stats


def ensure_open_questions(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Skapar frågor för öppna ärenden på live-resor i självgående läge (byggkontraktet C.4).

    STUBBE (G0): gör inget förrän S2a fyller funktionen.
    """
    return {}
