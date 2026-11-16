"""Månadsskiftet: månadens läge, att köra kontrollen nu och att stänga nedladdningslänkar."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, get_vehicle
from app.api.errors import bad_request
from app.db import utcnow
from app.models import KnownLocation, LogicalTrip, Vehicle
from app.models.enums import AUTO_APPROVAL_SOURCES, LogicalState
from app.services import audit
from app.services import settings as settings_svc
from app.services.export import links, month_close
from app.services.places import registry as places
from app.services.stats import common

router = APIRouter(prefix="/month-close", tags=["MonthClose"])

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class BlockerOut(BaseModel):
    code: str
    text: str
    count: int = 0
    trip_ids: list[str] = []
    web_only: bool = False


class ReviewRowOut(BaseModel):
    trip_id: uuid.UUID
    date: date | None
    route: str
    distance_km: float
    purpose: str | None
    category: str
    approval_source: str
    reason: str | None


class MonthStatusOut(BaseModel):
    vehicle_id: uuid.UUID
    month: str
    state: str
    blockers: list[BlockerOut]
    notes: list[BlockerOut]
    export_id: uuid.UUID | None
    # Underlaget skapades innan månaden var slut ("Skapa underlaget nu"), och när.
    closed_early: bool = False
    export_created_at: datetime | None = None
    # Antagna och automatiskt godkända rader i månaden, för ägarens genomgång.
    review_rows: list[ReviewRowOut] = []


class MonthCloseOut(BaseModel):
    # Innevarande månad först, sedan föregående.
    months: list[MonthStatusOut]


def _first_of(d: date) -> date:
    return d.replace(day=1)


def _previous(month: date) -> date:
    return date(month.year - 1, 12, 1) if month.month == 1 else date(month.year, month.month - 1, 1)


def _next(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def review_rows(db, *, vehicle: Vehicle, month: date, tz) -> list[ReviewRowOut]:
    """Månadens rader som automatiken godkände eller antog, med skälet."""
    lo, hi = common.local_midnight(month, tz), common.local_midnight(_next(month), tz)
    trips = list(db.execute(
        select(LogicalTrip).where(
            LogicalTrip.vehicle_id == vehicle.id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.approval_source.in_(sorted(AUTO_APPROVAL_SOURCES)),
            LogicalTrip.sort_time >= lo,
            LogicalTrip.sort_time < hi,
        ).order_by(LogicalTrip.sort_time)
    ).scalars())
    loc_ids = {i for t in trips for i in (t.start_location_id, t.end_location_id) if i}
    locs = {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(loc_ids))).scalars()} \
        if loc_ids else {}
    rows: list[ReviewRowOut] = []
    for t in trips:
        when = t.started_at or t.ended_at or t.sort_time
        basis = t.approval_basis if isinstance(t.approval_basis, dict) else {}
        start = places.display_name(locs.get(t.start_location_id), t.start_address)  # type: ignore[arg-type]
        end = places.display_name(locs.get(t.end_location_id), t.end_address)  # type: ignore[arg-type]
        rows.append(ReviewRowOut(
            trip_id=t.id, date=when.astimezone(tz).date() if when else None, route=f"{start} → {end}",
            distance_km=round(float(t.distance_km or 0), 1), purpose=t.purpose, category=t.category,
            approval_source=t.approval_source or "", reason=basis.get("reason") if isinstance(basis.get("reason"), str) else None,
        ))
    return rows


def status_out(status: month_close.MonthStatus, rows: list[ReviewRowOut]) -> MonthStatusOut:
    data: dict[str, Any] = dataclasses.asdict(status)
    return MonthStatusOut(**data, review_rows=rows)


@router.get("", response_model=MonthCloseOut, summary="Månadsskiftet: läget för innevarande och föregående månad")
def get_month_close(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None) -> MonthCloseOut:
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    now = utcnow()
    current = _first_of(now.astimezone(tz).date())
    months = []
    for month in (current, _previous(current)):
        status = month_close.evaluate(db, user_id=principal.user.id, vehicle=vehicle, month=month, now=now)
        months.append(status_out(status, review_rows(db, vehicle=vehicle, month=month, tz=tz)))
    return MonthCloseOut(months=months)


@router.post("/links/revoke", status_code=204, summary="Stäng alla nedladdningslänkar")
def revoke_links(principal: WebPrincipalDep, db: DbDep) -> None:
    links.revoke_all(db, principal.user)
    audit.record(db, principal.actor, "export.links_revoked", "user", principal.user.id)
    db.commit()


@router.post("/{month}/run", response_model=MonthStatusOut, summary="Kontrollera månaden nu (och skapa underlaget om den är klar)")
def run_month(
    month: str, principal: WebPrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None,
    force: bool = Query(default=False, description="Hoppa över väntan på månadsslutet"),
) -> MonthStatusOut:
    if not MONTH_RE.match(month):
        raise bad_request("Månaden ska anges som ÅÅÅÅ-MM.", "invalid_month")
    try:
        first = date(int(month[:4]), int(month[5:7]), 1)
    except ValueError as exc:
        raise bad_request("Månaden ska anges som ÅÅÅÅ-MM.", "invalid_month") from exc
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    now: datetime = utcnow()
    if first > _first_of(now.astimezone(tz).date()):
        raise bad_request("Månaden har inte börjat än.", "future_month")
    status = month_close.run_for(db, user_id=principal.user.id, vehicle=vehicle, month=first, now=now, force=force)
    audit.record(db, principal.actor, "month_close.run", "vehicle", vehicle.id, after={"month": month, "force": force})
    db.commit()
    return status_out(status, review_rows(db, vehicle=vehicle, month=first, tz=tz))
