"""Självgående läge: regeltabellen (A.2), platsantagandet (A.3) och ägarens rättelser.

STUBBE (G0): signaturerna följer byggkontraktet (C.3). Alla beslut är neutrala
(``AutoDecision("skip")``, inget skrivs), så att appen beter sig som i dag tills S1c och
S1d fyller modulen. Färdiga redan nu: ``ensure_active_since`` och ``is_active``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LocationPattern, LogicalTrip, User, Vehicle
from app.services import audit
from app.services import settings as settings_svc

HISTORY_MIN = 3


@dataclass(frozen=True)
class AutoDecision:
    outcome: Literal["approve", "ask", "ask_purpose", "ask_company", "wait", "skip"]
    category: str | None = None
    purpose: str | None = None
    source: str | None = None  # enums.ApprovalSource
    pattern_id: uuid.UUID | None = None
    related_trip_id: uuid.UUID | None = None
    reason: str = ""  # "Din regel för Acme AB", "Som de 3 senaste gångerna" …
    candidates: tuple[str, ...] = ()  # ärendeknappar


SKIP = AutoDecision("skip")


def is_active(db: Session, user_id: uuid.UUID) -> bool:
    """Sant när självgående läge är valt och har en starttidpunkt (``active_since``)."""
    return (
        settings_svc.automation_mode(db, user_id) == settings_svc.MODE_SELF_DRIVING
        and settings_svc.active_since(db, user_id) is not None
    )


def ensure_active_since(db: Session, user_id: uuid.UUID | None = None, now: datetime | None = None) -> None:
    """Sätter ``automation.active_since`` för användare i självgående läge som saknar den.

    Utan ``user_id`` gäller det alla aktiva användare (första steget i ``tick``). En redan
    satt tidpunkt ändras aldrig, så resor från före uppgraderingen rörs aldrig och ingen
    omräkning görs. I läget ``manual`` sätts ingenting.
    """
    now = now or utcnow()
    if user_id is not None:
        user_ids = [user_id]
    else:
        user_ids = list(db.execute(select(User.id).where(User.is_active.is_(True))).scalars())
    for uid in user_ids:
        if settings_svc.automation_mode(db, uid) != settings_svc.MODE_SELF_DRIVING:
            continue
        if settings_svc.get_section(db, uid, "automation").get("active_since"):
            continue
        value = now.isoformat(timespec="seconds")
        settings_svc.update_section(db, uid, "automation", {"active_since": value}, allow_read_only=True)
        audit.record(db, audit.SYSTEM, "settings.automation_active_since", "settings", "automation",
                     after={"active_since": value}, user_id=uid)
    db.flush()


def eligible(trip: LogicalTrip, *, active_since: datetime | None) -> bool:
    """Live-resa, aktiv, ej godkänd och skapad efter ``active_since``. (Stubbe: aldrig.)"""
    return False


def decide(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, ctx: Any, rules: Any, cfg: Any) -> AutoDecision:
    """Beslut enligt regeltabellen; skriver inget. (Stubbe: ``skip``.)"""
    return SKIP


def apply(db: Session, trip: LogicalTrip, d: AutoDecision, *, actor: audit.Actor) -> bool:
    """Genomför ett beslut. (Stubbe: ändrar inget.)"""
    return False


def run_vehicle(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, actor: audit.Actor) -> dict[str, int]:
    """decide + apply för alla resor som omfattas. (Stubbe: inga.)"""
    return {}


def reconsider(db: Session, *, user_id: uuid.UUID, trip_ids: list[uuid.UUID], actor: audit.Actor) -> None:
    """Prövar resorna igen när adressen blivit klar. (Stubbe: gör inget.)"""
    return None


def purpose_candidates(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, limit: int = 2) -> list[str]:
    """Ärendeknappar för frågan. (Stubbe: inga.)"""
    return []


def can_remember(db: Session, trip: LogicalTrip, text: str | None) -> bool:
    """Om "Alltid" får erbjudas för resan. (Stubbe: aldrig.)"""
    return False


def remember_always(
    db: Session, *, user_id: uuid.UUID, trip: LogicalTrip, category: str, purpose: str | None, actor: audit.Actor
) -> LocationPattern:
    """Skapar ägarens egen regel; kräver ``can_remember``. (Stubbe: tillåts aldrig.)"""
    raise ValueError("Det går inte att spara en regel för den här resan.")


def on_owner_decision(db: Session, trip: LogicalTrip, *, before: dict[str, Any], actor: audit.Actor) -> str | None:
    """Följder av ägarens beslut på en automatisk resa. (Stubbe: inga.)"""
    return None


def confirm_assumption(db: Session, trip: LogicalTrip, *, actor: audit.Actor) -> None:
    """"Stämmer" på en antagen resa. (Stubbe: gör inget.)"""
    return None


def set_company(db: Session, trip: LogicalTrip, text: str, *, actor: audit.Actor) -> None:
    """Företagsnamnet för platsen. (Stubbe: gör inget.)"""
    return None


def classify_place(osm_category: str | None, osm_type: str | None) -> str:
    """Platsens art ur kartans typ (tabellen i A.3). (Stubbe: alltid ``unclear``.)"""
    return "unclear"
