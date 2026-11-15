"""Notiser i webbappen: frågelista, mobil snabbvy, svar och demotelefon."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, WebPrincipalDep
from app.api.errors import not_found
from app.config import get_config
from app.db import utcnow
from app.models import LogicalTrip, NotificationRequest, RawTrip
from app.models.enums import NotificationKind, NotificationStatus
from app.services.ha import actions as A
from app.services.ha import demo, messages, notifier, responses
from app.services.ha.connection import CHANNEL_DEMO

router = APIRouter(prefix="/notifications", tags=["Notifications"])

_STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "open": (NotificationStatus.PENDING, NotificationStatus.SENT),
    "answered": (NotificationStatus.ANSWERED,),
    "expired": (NotificationStatus.EXPIRED,),
    "closed": (NotificationStatus.ANSWERED, NotificationStatus.EXPIRED, NotificationStatus.CANCELLED, NotificationStatus.FAILED),
}
_ANSWERABLE = (NotificationStatus.PENDING, NotificationStatus.SENT, NotificationStatus.EXPIRED)


class ActionOut(BaseModel):
    key: str
    title: str
    text_input: bool = False
    uri: str | None = None


class TripSummary(BaseModel):
    id: uuid.UUID
    started_at: datetime | None
    ended_at: datetime | None
    start_address: str | None
    end_address: str | None
    distance_km: float
    category: str
    review_status: str
    purpose: str | None
    purpose_confirmed: bool
    state: str
    # Samma platsetiketter som notistexterna (aldrig hemadress/privat plats i klartext).
    start_label: str | None = None
    end_label: str | None = None
    start_time_quality: str | None = None
    end_time_quality: str | None = None


class NotificationOut(BaseModel):
    public_id: str
    kind: str
    title: str
    message: str
    detail_message: str | None
    status: str
    channel: str | None
    created_at: datetime
    sent_at: datetime | None
    answered_at: datetime | None
    expires_at: datetime | None
    send_after: datetime | None
    reminder_count: int
    expired: bool
    answerable: bool
    in_digest: bool
    actions: list[ActionOut]
    trip_id: uuid.UUID | None
    review_item_id: uuid.UUID | None
    last_error: str | None


class NotificationDetail(NotificationOut):
    trip: TripSummary | None = None
    merge_trips: list[TripSummary] = Field(default_factory=list)
    summary: dict[str, Any] | None = None
    answer: dict[str, Any] | None = None


class RespondIn(BaseModel):
    action: str = Field(min_length=1, max_length=80)
    reply_text: str | None = Field(default=None, max_length=2000)
    as_phone: bool = Field(default=False, description="Simulera knapptryck på demotelefonen")
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


class SnoozeIn(BaseModel):
    minutes: int | None = Field(default=None, ge=5, le=7 * 24 * 60)


class DemoPhoneNotification(BaseModel):
    public_id: str
    kind: str
    title: str
    message: str
    sent_at: datetime | None
    actions: list[ActionOut]
    ha_payload: dict[str, Any]  # exakt den tjänstedata som skulle ha skickats till Home Assistant


def _actions(req: NotificationRequest) -> list[ActionOut]:
    base = get_config().base_url
    return [
        ActionOut(
            key=a["key"],
            title=a["title"],
            text_input=bool(a.get("text_input")),
            uri=A.absolute_url(base, a["uri"]) if a.get("uri") else None,
        )
        for a in (req.actions or [])
    ]


def _out(req: NotificationRequest, now: datetime) -> dict[str, Any]:
    expired = req.status == NotificationStatus.EXPIRED or (
        req.status in _STATUS_FILTERS["open"] and req.expires_at is not None and req.expires_at <= now
    )
    return {
        "public_id": req.public_id,
        "kind": req.kind,
        "title": req.title,
        "message": req.message,
        "detail_message": (req.context or {}).get("detail_message"),
        "status": req.status,
        "channel": req.channel.split(":")[0] if req.channel else None,
        "created_at": req.created_at,
        "sent_at": req.sent_at,
        "answered_at": req.answered_at,
        "expires_at": req.expires_at,
        "send_after": req.send_after,
        "reminder_count": req.reminder_count or 0,
        "expired": expired,
        "answerable": req.status in _ANSWERABLE,
        "in_digest": req.digest_id is not None,
        "actions": _actions(req),
        "trip_id": req.logical_trip_id,
        "review_item_id": req.review_item_id,
        "last_error": req.last_error,
    }


def _trip_summary(db, trip: LogicalTrip | None) -> TripSummary | None:
    if trip is None:
        return None
    start_label, end_label = messages.trip_labels(db, trip)
    return TripSummary(
        id=trip.id,
        started_at=trip.started_at,
        ended_at=trip.ended_at,
        start_address=trip.start_address,
        end_address=trip.end_address,
        distance_km=round(trip.distance_km, 1),
        category=trip.category,
        review_status=trip.review_status,
        purpose=trip.purpose,
        purpose_confirmed=trip.purpose_confirmed,
        state=trip.state,
        start_label=start_label,
        end_label=end_label,
        start_time_quality=trip.start_time_quality,
        end_time_quality=trip.end_time_quality,
    )


def _get(db, user_id: uuid.UUID, public_id: str) -> NotificationRequest:
    req = db.execute(
        select(NotificationRequest).where(
            NotificationRequest.user_id == user_id, NotificationRequest.public_id == public_id
        )
    ).scalar_one_or_none()
    if req is None:
        raise not_found("Frågan")
    return req


@router.get("", response_model=list[NotificationOut], summary="Lista frågor och notiser")
def list_notifications(
    principal: WebPrincipalDep,
    db: DbDep,
    status: str = Query("open", pattern="^(open|answered|expired|closed|all)$"),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    q = select(NotificationRequest).where(NotificationRequest.user_id == principal.user.id)
    if status != "all":
        q = q.where(NotificationRequest.status.in_(_STATUS_FILTERS[status]))
    rows = db.execute(q.order_by(NotificationRequest.created_at.desc()).limit(limit)).scalars().all()
    now = utcnow()
    return [_out(r, now) for r in rows]


@router.get("/demo-inbox", response_model=list[DemoPhoneNotification], summary="Demotelefonens notiser (demoläge)")
def demo_inbox(principal: WebPrincipalDep, db: DbDep) -> list[DemoPhoneNotification]:
    return [
        DemoPhoneNotification(
            public_id=r.public_id,
            kind=r.kind,
            title=r.title,
            message=r.message,
            sent_at=r.sent_at,
            actions=_actions(r),
            ha_payload=notifier.service_data(r),
        )
        for r in demo.inbox(db, principal.user.id)
    ]


@router.get("/{public_id}", response_model=NotificationDetail, summary="En fråga med underlag (mobil snabbvy)")
def get_notification(public_id: str, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    req = _get(db, principal.user.id, public_id)
    out = _out(req, utcnow())
    ctx = req.context or {}
    out["trip"] = _trip_summary(db, db.get(LogicalTrip, req.logical_trip_id) if req.logical_trip_id else None)
    merge_trips: list[TripSummary] = []
    if req.kind == NotificationKind.MERGE:
        from app.models import TripSegment
        from app.models.enums import LogicalState

        for raw_key in ("first_raw_trip_id", "second_raw_trip_id"):
            try:
                raw_id = uuid.UUID(ctx.get(raw_key, ""))
            except (ValueError, TypeError):
                continue
            if db.get(RawTrip, raw_id) is None:
                continue
            trip = db.execute(
                select(LogicalTrip)
                .join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
                .where(TripSegment.raw_trip_id == raw_id, LogicalTrip.state == LogicalState.ACTIVE)
                .limit(1)
            ).scalar_one_or_none()
            summary = _trip_summary(db, trip)
            if summary is not None and all(t.id != summary.id for t in merge_trips):
                merge_trips.append(summary)
    out["merge_trips"] = merge_trips
    out["summary"] = ctx.get("summary")
    applied = next((r for r in req.responses if r.applied), None)
    out["answer"] = (
        {"action": applied.action, "source": applied.source, "received_at": applied.received_at,
         "message": (applied.result or {}).get("message")}
        if applied is not None else None
    )
    return out


@router.post("/{public_id}/respond", summary="Besvara en fråga i webbappen")
def respond(public_id: str, body: RespondIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    req = _get(db, principal.user.id, public_id)
    source = "demo" if (body.as_phone and req.channel == CHANNEL_DEMO) else "web"
    result = responses.handle(
        db,
        user_id=principal.user.id,
        public_id=public_id,
        action=body.action,
        reply_text=body.reply_text,
        idempotency_key=body.idempotency_key or uuid.uuid4().hex,
        source=source,
    )
    db.commit()
    return result


@router.post("/{public_id}/snooze", summary="Skjut upp en fråga")
def snooze(public_id: str, body: SnoozeIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    _get(db, principal.user.id, public_id)
    result = responses.handle(
        db,
        user_id=principal.user.id,
        public_id=public_id,
        action=A.LATER,
        idempotency_key=uuid.uuid4().hex,
        source="web",
        snooze_minutes=body.minutes,
    )
    db.commit()
    return result
