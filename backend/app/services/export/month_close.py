"""Månadsskiftet: när månaden är komplett skapas underlaget av sig självt (byggkontraktet C.6).

STUBBE (G0): signaturerna följer kontraktet. ``evaluate`` ger läget ``open`` utan
blockerare och ``run_due`` gör inget förrän S1e fyller modulen.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import Vehicle

MONTH_GRACE = timedelta(hours=6)


@dataclass
class Blocker:
    code: str
    text: str
    count: int = 0
    trip_ids: list[str] = field(default_factory=list)
    web_only: bool = False


@dataclass
class MonthStatus:
    vehicle_id: uuid.UUID
    month: str  # "2026-10"
    state: Literal["open", "waiting", "exported", "stale", "empty"]
    blockers: list[Blocker]
    notes: list[Blocker]
    export_id: uuid.UUID | None


def month_key(month: date) -> str:
    return f"{month.year:04d}-{month.month:02d}"


def evaluate(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, month: date, now: datetime) -> MonthStatus:
    """Månadens läge, blockerare och upplysningar; skriver inget. (Stubbe: ``open``.)"""
    return MonthStatus(vehicle_id=vehicle.id, month=month_key(month), state="open", blockers=[], notes=[],
                       export_id=None)


def run_due(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Kontrollerar månadsskiftet för alla fordon (högst 1 gång/h). (Stubbe: gör inget.)"""
    return {}


def run_for(
    db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, month: date, now: datetime, force: bool = False
) -> MonthStatus:
    """Kontrollerar en månad och skapar underlaget när den är komplett. (Stubbe: bara läget.)"""
    return evaluate(db, user_id=user_id, vehicle=vehicle, month=month, now=now)


def rows_fingerprint(journal: Any) -> str:
    """sha256 över underlagets rader och summor. (Stubbe: tom text.)"""
    return ""
