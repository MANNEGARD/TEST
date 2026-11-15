"""Notiser som inte är frågor: antagande, månadsskiftet och larm (byggkontraktet C.4).

Ingen av dem räknas som en fråga i leveranspolicyn (högst en fråga åt gången), men tysta
timmar gäller. Texterna innehåller aldrig gatuadresser. Larm och månadsnotiser innehåller
aldrig platser, VIN eller belopp per resa.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExportJob, LogicalTrip, NotificationRequest
from app.models.enums import NotificationKind
from app.services import settings as settings_svc
from app.services.ha import connection, messages, notifier

ALERT_VALID = timedelta(days=7)
MONTHLY_VALID = timedelta(days=7)  # samma som länkens giltighet (links.LINK_VALID)


def _channel_ok(db: Session, user_id: uuid.UUID, vehicle_id: uuid.UUID | None = None) -> bool:
    """Notiser skapas bara när notiserna är på och en kanal finns (som för frågorna)."""
    cfg = settings_svc.get_section(db, user_id, "notifications")
    if not cfg["enabled"]:
        return False
    state = connection.load(db, user_id)
    if state.channel is None:
        return False
    if vehicle_id is not None and state.channel != connection.CHANNEL_DEMO:
        from app.models import Vehicle

        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is not None and vehicle.is_demo:
            return False  # demofordon går aldrig via riktig HA
    return True


def _create(
    db: Session, *, user_id: uuid.UUID, kind: str, built: dict[str, Any], now: datetime, valid: timedelta,
    dedupe_key: str, vehicle_id: uuid.UUID | None = None, trip_id: uuid.UUID | None = None,
    extra_context: dict[str, Any] | None = None,
) -> NotificationRequest:
    cfg = settings_svc.get_section(db, user_id, "notifications")
    req = notifier._create(
        db, user_id=user_id, kind=kind, built=built, public_id=notifier.new_public_id(), now=now,
        cfg={**cfg, "expire_after_hours": valid.total_seconds() / 3600}, vehicle_id=vehicle_id, trip_id=trip_id,
        dedupe_key=dedupe_key, send_after=now, extra_context=extra_context,
    )
    return req


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def queue_assumed(
    db: Session, *, user_id: uuid.UUID, trip: LogicalTrip, now: datetime
) -> NotificationRequest | None:
    """"Antaget: Kundbesök …", bara första gången per plats (A.3).

    Ingen notis när vetotiden är 0 ("Fråga mig först"), då blir antagandet en vanlig fråga.
    """
    if trip.approval_source != "assumed" or trip.end_location_id is None:
        return None
    veto_hours = int(settings_svc.get_section(db, user_id, "automation").get("assumption_veto_hours") or 0)
    if veto_hours <= 0:
        return None
    if not _channel_ok(db, user_id, trip.vehicle_id):
        return None
    key = f"assumed:{trip.end_location_id}"
    if notifier._exists(db, user_id, key):
        return None
    basis = trip.approval_basis if isinstance(trip.approval_basis, dict) else {}
    veto_until = _parse_dt(basis.get("veto_until"))
    if veto_until is None and trip.auto_approved_at is not None:
        veto_until = trip.auto_approved_at + timedelta(hours=veto_hours)
    tz = settings_svc.user_timezone(db, user_id)
    hide = bool(settings_svc.get_section(db, user_id, "notifications")["hide_locations_on_lock_screen"])
    built = messages.assumed(trip, tz=tz, now=now, veto_until=veto_until, hide_locations=hide)
    if hide:
        built["context"]["detail_message"] = f"{trip.purpose or 'Kundbesök'} · {built['message']}"
    valid = (veto_until - now) if veto_until is not None and veto_until > now else timedelta(hours=veto_hours)
    return _create(
        db, user_id=user_id, kind=NotificationKind.ASSUMED, built=built, now=now, valid=valid, dedupe_key=key,
        vehicle_id=trip.vehicle_id, trip_id=trip.id,
    )


def _month_of(status: Any) -> str:
    return str(getattr(status, "month", "") or "")


def _supersede_open_monthly(db: Session, user_id: uuid.UUID, vehicle_id: Any, month: str) -> None:
    """En ny månadsnotis för samma månad ersätter den förra på telefonen."""
    prefix = f"monthly:{vehicle_id}:{month}:"
    for req in db.execute(
        select(NotificationRequest).where(
            NotificationRequest.user_id == user_id,
            NotificationRequest.kind == NotificationKind.MONTHLY,
            NotificationRequest.status.in_(notifier.OPEN_STATUSES),
            NotificationRequest.dedupe_key.like(f"{prefix}%"),
        )
    ).scalars():
        notifier._cancel(db, req, "replaced")


def queue_month_waiting(db: Session, *, user_id: uuid.UUID, status: Any, now: datetime) -> NotificationRequest | None:
    """"Oktober väntar": namnger det som väntar. Högst en per dygn och månad.

    Hur ofta den skickas (första gången och sedan var 3:e dygn, högst 3 gånger) styrs av
    ``month_close.run_due``.
    """
    month = _month_of(status)
    if not month or not _channel_ok(db, user_id, status.vehicle_id):
        return None
    tz = settings_svc.user_timezone(db, user_id)
    key = f"monthly:{status.vehicle_id}:{month}:waiting:{now.astimezone(tz).date().isoformat()}"
    if notifier._exists(db, user_id, key):
        return None
    _supersede_open_monthly(db, user_id, status.vehicle_id, month)
    built = messages.month_waiting(month, list(status.blockers or []), list(status.notes or []))
    return _create(
        db, user_id=user_id, kind=NotificationKind.MONTHLY, built=built, now=now, valid=MONTHLY_VALID,
        dedupe_key=key, vehicle_id=status.vehicle_id,
    )


def queue_month_ready(
    db: Session, *, user_id: uuid.UUID, status: Any, export: ExportJob | None, now: datetime
) -> NotificationRequest | None:
    """"Oktober klart" med länk till underlaget, eller "Oktober: inga tjänsteresor".

    Länkens ``exp`` sparas i notisens ``context`` (``link_exp``), så att samma token byggs
    varje gång utkorgen läses. Ersätter den här filen en äldre fil blir rubriken
    "Oktober uppdaterad" med skillnaden i resor och km.
    """
    month = _month_of(status)
    if not month or not _channel_ok(db, user_id, status.vehicle_id):
        return None
    key = f"monthly:{status.vehicle_id}:{month}:ready:{export.id if export is not None else 'empty'}"
    if notifier._exists(db, user_id, key):
        return None
    previous = None
    if export is not None:
        older = db.execute(
            select(ExportJob)
            .where(ExportJob.superseded_by_id == export.id, ExportJob.user_id == user_id)
            .order_by(ExportJob.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if older is not None:
            previous = dict(older.summary or {})
    _supersede_open_monthly(db, user_id, status.vehicle_id, month)
    built = messages.month_ready(
        month, summary=dict(export.summary or {}) if export is not None else None, notes=list(status.notes or []),
        export_id=export.id if export is not None else None, previous=previous,
    )
    extra: dict[str, Any] = {}
    if export is not None:
        extra["link_exp"] = int((now + MONTHLY_VALID).timestamp())
        extra["export_id"] = str(export.id)
    return _create(
        db, user_id=user_id, kind=NotificationKind.MONTHLY, built=built, now=now, valid=MONTHLY_VALID,
        dedupe_key=key, vehicle_id=status.vehicle_id, extra_context=extra,
    )


def queue_alert(
    db: Session, *, user_id: uuid.UUID, code: str, episode: str, title: str, message: str, now: datetime
) -> NotificationRequest | None:
    """Larm med dedupe-nyckeln ``alert:{code}:{episode}``: en push per episod.

    Texten ska vara kort och sakna adresser. Gränsen på 2 larm per dygn och hur ofta
    villkoren prövas sköts av ``ha.alerts``.
    """
    if not _channel_ok(db, user_id):
        return None
    key = f"alert:{code}:{episode}"[:160]
    if notifier._exists(db, user_id, key):
        return None
    built = messages.alert(title, message)
    return _create(
        db, user_id=user_id, kind=NotificationKind.ALERT, built=built, now=now, valid=ALERT_VALID,
        dedupe_key=key, extra_context={"alert_code": code, "episode": episode},
    )


def resolve_alert(db: Session, *, user_id: uuid.UUID, code: str) -> int:
    """Felet är borta: larmet tas bort från telefonen utan ny push. Returnerar antalet."""
    db.flush()
    count = 0
    for req in db.execute(
        select(NotificationRequest).where(
            NotificationRequest.user_id == user_id,
            NotificationRequest.kind == NotificationKind.ALERT,
            NotificationRequest.status.in_(notifier.OPEN_STATUSES),
            NotificationRequest.dedupe_key.like(f"alert:{code}:%"),
        )
    ).scalars():
        notifier._cancel(db, req, "resolved")
        count += 1
    db.flush()
    return count
