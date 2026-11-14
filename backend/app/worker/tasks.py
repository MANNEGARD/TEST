"""Bakgrundsjobb. Varje jobb är en vanlig funktion (registrerad i ``app.jobs``) som
även exponeras som Celery-uppgift. Jobben är idempotenta och tål att köras om."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from app import jobs
from app.db import session_scope, utcnow
from app.models import OAuthState, User, Vehicle, VehicleObservation
from app.services import audit

logger = logging.getLogger(__name__)


def _safe(name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Kör ett delsteg; ett fel i en integration får aldrig stoppa de övriga."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("Bakgrundssteget %s misslyckades", name)
        return {"error": True}


@jobs.register("tick")
def tick() -> dict[str, Any]:
    """Körs varje minut: fordonsinsamling, notisleverans, schemalagda rapporter."""
    from app.services.ha import notifier
    from app.services.trips import review
    from app.services.volvo import collector
    from app.services.volvo import demo as volvo_demo

    out: dict[str, Any] = {}
    with session_scope() as db:
        out["volvo"] = _safe("volvo.poll_due", collector.poll_due, db)
    with session_scope() as db:
        out["volvo_demo"] = _safe("volvo.demo.tick", volvo_demo.tick, db)
    with session_scope() as db:
        out["snoozed_woken"] = _safe("review.wake_snoozed", review.wake_snoozed, db)
    with session_scope() as db:
        out["notifications"] = _safe("ha.deliver_due", notifier.deliver_due, db)
    with session_scope() as db:
        out["scheduled"] = _safe("ha.run_scheduled", notifier.run_scheduled, db)
    return out


@jobs.register("calendar.sync_due")
def calendar_sync_due() -> dict[str, Any]:
    from app.services.calendar import sync

    with session_scope() as db:
        return _safe("calendar.sync_all_due", sync.sync_all_due, db)


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
