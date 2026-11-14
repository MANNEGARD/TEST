"""Demonstrationsläge för Home Assistant-integrationen.

I demoläget "levereras" notiser till en simulerad telefon i webbappen. Statusen är
``demo`` – integrationen visas aldrig som ansluten – och inget anrop görs mot någon
riktig Home Assistant.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, NotificationRequest, ReviewItem, Vehicle
from app.models.enums import (
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
from app.services.ha import connection, notifier
from app.services.ha.connection import CHANNEL_DEMO

MAX_DEMO_QUESTIONS = 3


def enable(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    conn = connection.get_or_create(db, user_id)
    if conn.status != IntegrationStatus.DEMO:
        integrations.update_config(conn, {"demo_previous_status": conn.status})
        conn.status = IntegrationStatus.DEMO
    db.flush()
    seeded = seed(db, user_id, now)
    notifier.deliver_user(db, user_id, now)
    return {"status": conn.status, "seeded": seeded}


def disable(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    state = connection.load(db, user_id)
    conn = state.conn
    if conn is None or conn.status != IntegrationStatus.DEMO:
        return {"status": conn.status if conn else IntegrationStatus.NOT_CONFIGURED}
    for req in db.execute(
        select(NotificationRequest).where(
            NotificationRequest.user_id == user_id, NotificationRequest.status.in_(notifier.OPEN_STATUSES)
        )
    ).scalars():
        if req.channel in (None, CHANNEL_DEMO):
            req.status = NotificationStatus.CANCELLED
            notifier.set_context(req, cancel_reason="demo_disabled")
    # Återgå till ett sanningsenligt läge: aldrig "ansluten" utan ny verifiering.
    has_config = bool(state.base_url and state.token) or state.heartbeat_at() is not None
    conn.status = IntegrationStatus.CONFIGURED if has_config else IntegrationStatus.NOT_CONFIGURED
    integrations.update_config(conn, {"demo_previous_status": None})
    db.flush()
    return {"status": conn.status}


def seed(db: Session, user_id: uuid.UUID, now: datetime) -> int:
    """Skapar en testnotis och – om demobilen har resor att granska – några riktiga frågor."""
    cfg = settings_svc.get_section(db, user_id, "notifications")
    count = 0
    has_test = db.execute(
        select(NotificationRequest.id).where(
            NotificationRequest.user_id == user_id,
            NotificationRequest.kind == NotificationKind.TEST,
            NotificationRequest.status.in_(notifier.OPEN_STATUSES),
        ).limit(1)
    ).first()
    if has_test is None:
        notifier.create_test(db, user_id, now)
        count += 1
    demo_vehicle = db.execute(
        select(Vehicle).where(Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(True)).limit(1)
    ).scalar_one_or_none()
    if demo_vehicle is None:
        return count
    trips = db.execute(
        select(LogicalTrip)
        .where(
            LogicalTrip.vehicle_id == demo_vehicle.id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.review_status.in_((ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED)),
        )
        .order_by(LogicalTrip.sort_time.desc())
        .limit(MAX_DEMO_QUESTIONS)
    ).scalars().all()
    for trip in trips:
        if notifier.create_classify_question(db, user_id=user_id, trip=trip, cfg=cfg, now=now) is not None:
            count += 1
    item = db.execute(
        select(ReviewItem)
        .where(
            ReviewItem.vehicle_id == demo_vehicle.id,
            ReviewItem.kind == ReviewKind.MERGE,
            ReviewItem.status == ReviewState.OPEN,
        )
        .order_by(ReviewItem.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if item is not None and notifier.create_merge_question(db, user_id=user_id, item=item, cfg=cfg, now=now) is not None:
        count += 1
    db.flush()
    return count


def inbox(db: Session, user_id: uuid.UUID, limit: int = 30) -> list[NotificationRequest]:
    """Notiser som visas på den simulerade telefonen (nyast först)."""
    return list(
        db.execute(
            select(NotificationRequest)
            .where(
                NotificationRequest.user_id == user_id,
                NotificationRequest.channel == CHANNEL_DEMO,
                NotificationRequest.status == NotificationStatus.SENT,
            )
            .order_by(NotificationRequest.sent_at.desc())
            .limit(limit)
        ).scalars()
    )
