"""Dashboard, månadsjämförelse, avvikelsesökning samt vecko- och dagsrapporter.

Alla siffror räknas ur lagrad data. Perioder tolkas i användarens tidszon.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import DbDep, Principal, PrincipalDep, get_vehicle
from app.api.errors import bad_request
from app.models import Vehicle
from app.services import settings as settings_svc
from app.services.stats import anomalies, common, dashboard, reports
from app.services.stats.common import Period, PeriodError

router = APIRouter(tags=["Dashboard"])

PERIOD_PATTERN = "^(today|week|month|prev_month|year|all|custom)$"


@dataclass
class Scope:
    vehicle: Vehicle
    tz: ZoneInfo
    period: Period


def period_scope(
    db: Session,
    principal: Principal,
    *,
    vehicle_id: uuid.UUID | None,
    period: str | None,
    date_from: date | None,
    date_to: date | None,
    year: int | None = None,
) -> Scope:
    """Fordon, tidszon och period (UTC-gränser) för en förfrågan."""
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    try:
        resolved = common.resolve_period(period, tz=tz, date_from=date_from, date_to=date_to, year=year)
    except PeriodError as exc:
        raise bad_request(str(exc), "invalid_period") from exc
    return Scope(vehicle, tz, resolved)


def _scope_dep(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    period: str = Query(default="month", pattern=PERIOD_PATTERN),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    year: int | None = Query(default=None, ge=1990, le=2200),
) -> Scope:
    return period_scope(db, principal, vehicle_id=vehicle_id, period=period, date_from=date_from, date_to=date_to,
                        year=year)


ScopeDep = Annotated[Scope, Depends(_scope_dep)]


def _period_out(scope: Scope) -> dict[str, Any]:
    return {**scope.period.as_dict(), "timezone": str(scope.tz)}


@router.get("/dashboard/summary", summary="Nyckeltal för perioden")
def summary(scope: ScopeDep, db: DbDep) -> dict[str, Any]:
    out = dashboard.summary(db, vehicle=scope.vehicle, start=scope.period.start, end=scope.period.end, tz=scope.tz)
    out["period"] = _period_out(scope)
    db.commit()
    return out


@router.get("/dashboard/timeseries", summary="Körsträcka, bensin och el per dag, vecka eller månad")
def timeseries(
    scope: ScopeDep, db: DbDep, bucket: str = Query(default="day", pattern="^(day|week|month)$")
) -> dict[str, Any]:
    try:
        out = dashboard.timeseries(
            db, vehicle=scope.vehicle, start=scope.period.start, end=scope.period.end, tz=scope.tz, bucket=bucket
        )
    except PeriodError as exc:
        raise bad_request(str(exc), "invalid_period") from exc
    out["period"] = _period_out(scope)
    db.commit()
    return out


@router.get("/dashboard/destinations", summary="Vanligaste destinationerna")
def destinations(scope: ScopeDep, db: DbDep, limit: int = Query(default=15, ge=1, le=100)) -> dict[str, Any]:
    items = dashboard.destinations(
        db, vehicle=scope.vehicle, start=scope.period.start, end=scope.period.end, tz=scope.tz, limit=limit
    )
    db.commit()
    return {"period": _period_out(scope), "items": items}


@router.get("/dashboard/routes", summary="Vanligaste resvägarna")
def routes(scope: ScopeDep, db: DbDep, limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    items = dashboard.routes(
        db, vehicle=scope.vehicle, start=scope.period.start, end=scope.period.end, tz=scope.tz, limit=limit
    )
    db.commit()
    return {"period": _period_out(scope), "items": items}


@router.get("/dashboard/monthly", summary="Månad för månad jämfört med föregående år")
def monthly(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    year: int | None = Query(default=None, ge=1990, le=2200),
) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    out = dashboard.monthly_comparison(db, vehicle=vehicle, tz=tz, year=year)
    db.commit()
    return out


@router.post("/dashboard/anomalies/scan", summary="Sök efter avvikelser (skapar granskningsärenden)")
def scan_anomalies(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    result = anomalies.scan_vehicle(db, vehicle, user_id=principal.user.id)
    db.commit()
    return result


@router.get("/reports/weekly", summary="Veckorapport")
def weekly(
    principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None, week_start: date | None = None
) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    out = reports.weekly_report(db, vehicle=vehicle, tz=tz, week_start=week_start)
    db.commit()
    return out


@router.get("/reports/daily", summary="Daglig avstämning")
def daily(
    principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None, day: date | None = None
) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    out = reports.daily_reconciliation(db, vehicle=vehicle, tz=tz, day=day)
    db.commit()
    return out
