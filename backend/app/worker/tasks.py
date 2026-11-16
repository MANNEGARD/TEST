"""Bakgrundsjobb. Varje jobb är en vanlig funktion (registrerad i ``app.jobs``) som
även exponeras som Celery-uppgift. Jobben är idempotenta och tål att köras om."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import jobs
from app.db import session_scope, utcnow
from app.models import OAuthState, User, Vehicle, VehicleObservation
from app.services import audit

logger = logging.getLogger(__name__)


def _safe(name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Kör ett delsteg; ett fel i en integration får aldrig stoppa de övriga.

    Vid fel rullas delstegets session tillbaka, så att ``session_scope`` kan avsluta
    (annars kastar commit ``PendingRollbackError`` och resten av ``tick()`` körs inte).
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("Bakgrundssteget %s misslyckades", name)
        for arg in (*args, *kwargs.values()):
            if isinstance(arg, Session):
                arg.rollback()
        return {"error": True}


@jobs.register("tick")
def tick() -> dict[str, Any]:
    """Körs varje minut: fordonsinsamling, adresser, frågor, notisleverans, larm och månadsskiftet.

    Ordningen är fast (byggkontraktet C.8). Varje steg har en egen session och ett fel i
    ett steg stoppar aldrig de övriga. Adressuppslaget sköter sina sessioner själv, så att
    inget nätverksanrop görs med en öppen transaktion.
    """
    from app.services.classify import autopilot
    from app.services.export import month_close
    from app.services.ha import alerts, notifier
    from app.services.learning import recompute
    from app.services.places import geocode
    from app.services.trips import review
    from app.services.volvo import collector
    from app.services.volvo import demo as volvo_demo

    out: dict[str, Any] = {}
    with session_scope() as db:
        out["active_since"] = _safe("autopilot.ensure_active_since", autopilot.ensure_active_since, db)
    with session_scope() as db:
        out["volvo"] = _safe("volvo.poll_due", collector.poll_due, db)
    with session_scope() as db:
        out["volvo_demo"] = _safe("volvo.demo.tick", volvo_demo.tick, db)
    with session_scope() as db:
        out["snoozed_woken"] = _safe("review.wake_snoozed", review.wake_snoozed, db)
    # Omräkningar efter ägarens beslut som blivit kvar (t.ex. efter en omstart).
    out["recompute"] = _safe("learning.recompute.run_due", recompute.run_due)
    out["geocode"] = _safe("places.geocode.run_pending", geocode.run_pending)
    with session_scope() as db:
        out["questions"] = _safe("ha.notifier.ensure_open_questions", notifier.ensure_open_questions, db)
    with session_scope() as db:
        out["notifications"] = _safe("ha.deliver_due", notifier.deliver_due, db)
    with session_scope() as db:
        out["scheduled"] = _safe("ha.run_scheduled", notifier.run_scheduled, db)
    with session_scope() as db:
        out["alerts"] = _safe("ha.alerts.run", alerts.run, db)
    with session_scope() as db:
        out["month_close"] = _safe("export.month_close.run_due", month_close.run_due, db)
    return out


@jobs.register("calendar.sync_due")
def calendar_sync_due() -> dict[str, Any]:
    """Kalendersynken: först Outlook (Graph), sedan kalenderlänken (ICS) i en egen session.
    Ett fel i den ena stoppar aldrig den andra."""
    from app.services.calendar import ics, sync

    with session_scope() as db:
        graph_result = _safe("calendar.sync_all_due", sync.sync_all_due, db)
    with session_scope() as db:
        ics_result = _safe("calendar.ics.sync_all_due", ics.sync_all_due, db)
    return {**graph_result, "ics": ics_result}


@jobs.register("classify.pending")
def classify_pending(user_id: str, vehicle_id: str) -> dict[str, Any]:
    """Klassificerar oavgjorda resor med AI-leverantören (om en är ansluten)."""
    from app.services.classify import engine
    from app.services.trips import pipeline
    from app.services.trips import segments as seg

    with session_scope() as db:
        vehicle = db.get(Vehicle, uuid.UUID(vehicle_id))
        if vehicle is None or str(vehicle.owner_user_id) != user_id:
            return {"skipped": True}
        if engine.get_provider(db, vehicle.owner_user_id) is None:
            return {"skipped": True, "reason": "no_provider"}
        changed = engine.classify_pending(db, user_id=vehicle.owner_user_id, vehicle=vehicle, actor=audit.Actor("ai", "llm"))
        for trip in seg.active_trips(db, vehicle.id):
            pipeline.sync_trip_review_items(db, trip)
        return {"changed": changed}


@jobs.register("learning.recompute")
def learning_recompute(vehicle_id: str) -> dict[str, Any]:
    """Omräkningen efter ägarens beslut (förslag, autopilot och ärenden), efter svaret."""
    from app.services.learning import recompute

    return recompute.run_vehicle(uuid.UUID(vehicle_id))


@jobs.register("pipeline.vehicle")
def pipeline_vehicle(user_id: str, vehicle_id: str, notify: bool = True) -> dict[str, Any]:
    from app.services.trips import pipeline

    with session_scope() as db:
        vehicle = db.get(Vehicle, uuid.UUID(vehicle_id))
        if vehicle is None or str(vehicle.owner_user_id) != user_id:
            return {"skipped": True}
        result = pipeline.after_trips_changed(
            db, user_id=vehicle.owner_user_id, vehicle=vehicle, actor=audit.SYSTEM, notify=notify
        )
        return {k: v for k, v in result.items() if isinstance(v, (int, float, str))}


@jobs.register("maintenance")
def maintenance() -> dict[str, Any]:
    """Nattlig städning: utgångna sessioner, avvikelsesökning och ev. gallring av rådata."""
    from app.security import sessions
    from app.services import settings as settings_svc
    from app.services.stats import anomalies

    out: dict[str, Any] = {"anomalies": 0, "observations_purged": 0}
    with session_scope() as db:
        out["sessions_purged"] = sessions.purge_expired(db)
        db.execute(delete(OAuthState).where(OAuthState.expires_at <= utcnow()))
    with session_scope() as db:
        for user in db.execute(select(User).where(User.is_active.is_(True))).scalars().all():
            retention = int(settings_svc.get_section(db, user.id, "privacy")["observation_retention_days"])
            for vehicle in db.execute(select(Vehicle).where(Vehicle.owner_user_id == user.id)).scalars().all():
                result = _safe("anomalies.scan_vehicle", anomalies.scan_vehicle, db, vehicle, user_id=user.id)
                if isinstance(result, dict):
                    out["anomalies"] += int(result.get("created_total", 0) or 0)
                if retention > 0:
                    # Bearbetade rådataobservationer gallras; resorna och mätarställningarna finns kvar.
                    res = db.execute(
                        delete(VehicleObservation).where(
                            VehicleObservation.vehicle_id == vehicle.id,
                            VehicleObservation.processed_at.is_not(None),
                            VehicleObservation.last_seen_at < utcnow() - timedelta(days=retention),
                        )
                    )
                    out["observations_purged"] += res.rowcount or 0
    return out


def _bind_celery() -> None:
    """Exponerar de registrerade jobben som Celery-uppgifter (drivelog.<namn>)."""
    from app.config import get_config

    if get_config().jobs_eager:  # enprocessläget: ingen Celery, ingen kö att ansluta till
        return
    try:
        from app.worker.celery_app import celery
    except Exception:  # Celery/Redis behövs inte i test eller eager-läge
        return
    for name, fn in jobs.registered().items():
        task_name = f"drivelog.{name}"
        if task_name not in celery.tasks:
            celery.task(name=task_name, autoretry_for=(ConnectionError,), retry_backoff=True, retry_jitter=True,
                        max_retries=3)(fn)


_bind_celery()
