"""Notiser som inte är frågor: antagande, månadsskiftet och larm (byggkontraktet C.4).

STUBBE (G0): signaturerna följer kontraktet. Ingen notis skapas förrän S2a fyller modulen.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import ExportJob, LogicalTrip, NotificationRequest


def queue_assumed(
    db: Session, *, user_id: uuid.UUID, trip: LogicalTrip, now: datetime
) -> NotificationRequest | None:
    """"Antaget: Kundbesök …", bara första gången per plats. (Stubbe: ingen notis.)"""
    return None


def queue_month_waiting(db: Session, *, user_id: uuid.UUID, status: Any, now: datetime) -> NotificationRequest | None:
    """"Oktober väntar …". (Stubbe: ingen notis.)"""
    return None


def queue_month_ready(
    db: Session, *, user_id: uuid.UUID, status: Any, export: ExportJob | None, now: datetime
) -> NotificationRequest | None:
    """"Oktober klart …" med länk till underlaget. (Stubbe: ingen notis.)"""
    return None


def queue_alert(
    db: Session, *, user_id: uuid.UUID, code: str, episode: str, title: str, message: str, now: datetime
) -> NotificationRequest | None:
    """Larm med dedupe-nyckeln ``alert:{code}:{episode}``. (Stubbe: ingen notis.)"""
    return None


def resolve_alert(db: Session, *, user_id: uuid.UUID, code: str) -> int:
    """Rensar larmet på telefonen utan push. (Stubbe: inget att rensa.)"""
    return 0
