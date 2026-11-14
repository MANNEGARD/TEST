"""Veckorapport och daglig avstämning. Siffrorna hämtas alltid ur lagrad data."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, ReviewItem, Vehicle
from app.models.enums import CATEGORY_LABELS_SV, Category, ReviewState, ReviewStatus
from app.services import settings as settings_svc
from app.services.classify.purpose import NEEDS_CONFIRMATION_SUFFIX
from app.services.places.normalize import fold
from app.services.stats import common, dashboard
from app.services.trips import segments as seg

_UNREVIEWED = (ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED, ReviewStatus.AUTO)


def sv_number(value: float, decimals: int = 1) -> str:
    """Svensk decimalform: 48.6 → '48,6'."""
    return f"{value:.{decimals}f}".replace(".", ",")


def purpose_missing(trip: LogicalTrip) -> bool:
    """Tjänstekörning utan syfte (eller med platshållaren 'syfte behöver bekräftas')."""
    if trip.work_km <= common.KM_EPS:
        return False
    text = (trip.purpose or "").strip()
    return not text or NEEDS_CONFIRMATION_SUFFIX in fold(text)


def purpose_unconfirmed(trip: LogicalTrip) -> bool:
    return trip.work_km > common.KM_EPS and not purpose_missing(trip) and not trip.purpose_confirmed


def _trip_brief(trip: LogicalTrip, tz: ZoneInfo) -> dict[str, Any]:
    start = common.to_local(trip.started_at, tz)
    end = common.to_local(trip.ended_at, tz)
    return {
        "id": str(trip.id),
        "started_at": trip.started_at,
        "ended_at": trip.ended_at,
        "local_start": start.strftime("%Y-%m-%d %H:%M") if start else None,
        "local_end": end.strftime("%H:%M") if end else None,
        "start_address": trip.start_address,
        "end_address": trip.end_address,
        "distance_km": round(trip.distance_km, 1),
        "category": trip.category,
        "category_label": "Blandad" if trip.has_mixed_categories else CATEGORY_LABELS_SV[Category(trip.category)],
        "purpose": trip.purpose,
        "review_status": trip.review_status,
    }


def weekly_report(
    db: Session,
    *,
    vehicle: Vehicle,
    tz: ZoneInfo,
    week_start: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Veckans tjänstemil, privata mil, förbrukning, destinationer och öppna frågor.

    ``week_start`` normaliseras till veckans måndag; utelämnas den används innevarande vecka.
    När endast tjänsteresor redovisas (``business_only``) nämns inte luckor i mätarställningen
    i meddelandet – de är privat körning.
    """
    now = now or utcnow()
    scope = settings_svc.journal_scope(db, vehicle.owner_user_id)
    gaps_matter = scope != settings_svc.SCOPE_BUSINESS_ONLY
    day = week_start or now.astimezone(tz).date()
    monday = day - timedelta(days=day.weekday())
    start, end = common.local_midnight(monday, tz), common.local_midnight(monday + timedelta(days=7), tz)
    trips = common.load_trips(db, vehicle.id, start, end)
    totals = common.totals_for(trips)
    gaps = common.open_gaps(db, vehicle.id, start, end)
    unreviewed = [t for t in trips if t.review_status in _UNREVIEWED]
    missing_purpose = [t for t in trips if purpose_missing(t) or purpose_unconfirmed(t)]
    to_review = {t.id for t in unreviewed} | {t.id for t in missing_purpose}
    top = dashboard.destinations(db, vehicle=vehicle, start=start, end=end, tz=tz, limit=5)
    iso = monday.isocalendar()

    work_mil, private_mil = totals.work_km / 10, totals.private_km / 10
    parts = [f"{sv_number(work_mil)} mil i tjänsten", f"{sv_number(private_mil)} mil privat"]
    n = len(to_review)
    tail = f"{n} resa som behöver granskas" if n == 1 else f"{n} resor som behöver granskas"
    message = f"Veckans körjournal: {parts[0]}, {parts[1]} och {tail}."
    if totals.undecided_km > common.KM_EPS:
        message += f" {sv_number(totals.undecided_km / 10)} mil är inte klassade."
    if gaps and gaps_matter:
        message += f" {len(gaps)} dataluckor ({sv_number(sum(g.km for g in gaps), 0)} km) saknar journalpost."

    return {
        "vehicle_id": str(vehicle.id),
        "is_demo": vehicle.is_demo,
        "journal_scope": scope,
        "week": {"year": iso.year, "week": iso.week, "start": monday.isoformat(),
                 "end": (monday + timedelta(days=6)).isoformat()},
        "period": {"start": start, "end": end},
        "distance": totals.distance_dict(),
        "trips": {"total": totals.trips, "by_category": totals.by_kind},
        "fuel": totals.fuel.as_dict("l"),
        "electricity": {**totals.energy.as_dict("kWh"), "regenerated_kwh": round(totals.regen_kwh, 2)},
        "top_destinations": [
            {"label": d["label"], "trips": d["trips"], "total_km": d["total_km"]} for d in top
        ],
        "unreviewed_trips": len(unreviewed),
        "missing_purpose_trips": len(missing_purpose),
        "trips_to_review": n,
        "data_gaps": {"count": len(gaps), "km": round(sum(g.km for g in gaps), 1)},
        "message": message,
    }


def daily_reconciliation(
    db: Session,
    *,
    vehicle: Vehicle,
    tz: ZoneInfo,
    day: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dagens resor, saknade syften och klassificeringsproblem.

    När endast tjänsteresor redovisas (``business_only``) räknas luckor i mätarställningen
    inte som något att åtgärda, och helt privata resor flaggas inte för misstänkt datalucka.
    """
    now = now or utcnow()
    scope = settings_svc.journal_scope(db, vehicle.owner_user_id)
    gaps_matter = scope != settings_svc.SCOPE_BUSINESS_ONLY
    day = day or now.astimezone(tz).date()
    start, end = common.local_midnight(day, tz), common.local_midnight(day + timedelta(days=1), tz)
    trips = common.load_trips(db, vehicle.id, start, end)
    totals = common.totals_for(trips)

    problems: list[dict[str, Any]] = []
    for t in trips:
        issues: list[str] = []
        if t.undecided_km > common.KM_EPS:
            issues.append("Kategori är inte avgjord")
        elif t.review_status == ReviewStatus.NEEDS_REVIEW:
            issues.append("Klassningen behöver granskas")
        elif t.review_status in (ReviewStatus.PROPOSED, ReviewStatus.AUTO):
            issues.append("Preliminär klassning är inte godkänd")
        if t.has_mixed_categories and t.review_status != ReviewStatus.APPROVED:
            issues.append("Blandad privat/tjänst – kontrollera fördelningen")
        if purpose_missing(t):
            issues.append("Syfte saknas")
        elif purpose_unconfirmed(t):
            issues.append("Föreslaget syfte är inte bekräftat")
        if t.tax_note and t.review_status not in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED):
            issues.append("Skattemässig bedömning krävs")
        flags = set(t.quality_flags or [])
        if "missing_odometer" in flags:
            issues.append("Mätarställning saknas")
        if "possible_missed_trips" in flags and (gaps_matter or not seg.is_private_only(t)):
            issues.append("Misstänkt datalucka")
        if issues:
            problems.append({**_trip_brief(t, tz), "issues": issues})

    trip_ids = [t.id for t in trips]
    open_items = 0
    if trip_ids:
        open_items = len(
            db.execute(
                select(ReviewItem.id).where(
                    ReviewItem.logical_trip_id.in_(trip_ids), ReviewItem.status == ReviewState.OPEN
                )
            ).all()
        )
    gaps = common.open_gaps(db, vehicle.id, start, end)
    unresolved_gaps = bool(gaps) and gaps_matter
    missing = sum(1 for t in trips if purpose_missing(t))
    if not trips:
        message = "Inga resor registrerade idag."
    elif not problems and not unresolved_gaps:
        message = f"Dagens {totals.trips} resor ({sv_number(totals.total_km / 10)} mil) är klara."
    else:
        message = (
            f"Dagens körjournal: {totals.trips} resor, {sv_number(totals.total_km / 10)} mil. "
            f"{len(problems)} behöver åtgärdas"
            + (f", varav {missing} saknar syfte" if missing else "")
            + "."
        )
    return {
        "vehicle_id": str(vehicle.id),
        "is_demo": vehicle.is_demo,
        "journal_scope": scope,
        "day": day.isoformat(),
        "period": {"start": start, "end": end},
        "distance": totals.distance_dict(),
        "trips": [_trip_brief(t, tz) for t in trips],
        "trip_count": totals.trips,
        "problems": problems,
        "missing_purpose": missing,
        "open_review_items": open_items,
        "data_gaps": {"count": len(gaps), "km": round(sum(g.km for g in gaps), 1)},
        "all_clear": bool(trips) and not problems and not unresolved_gaps,
        "message": message,
    }
