"""Celery-konfiguration: driftsäker kö med Redis som mäklare och schemalagda jobb."""

from __future__ import annotations

from celery import Celery, signals
from celery.schedules import crontab

from app.config import get_config
from app.logging_setup import setup_celery_logging

cfg = get_config()

celery = Celery("drivelog", broker=cfg.redis_url, backend=None, include=["app.worker.tasks"])
celery.conf.update(
    task_default_queue="drivelog",
    task_acks_late=True,  # jobbet kvitteras först när det är klart → överlever omstart
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=600,
    task_soft_time_limit=540,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 1800},
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    worker_hijack_root_logger=False,
    beat_schedule={
        "tick-every-minute": {"task": "drivelog.tick", "schedule": 60.0},
        "calendar-sync": {"task": "drivelog.calendar.sync_due", "schedule": 15 * 60.0},
        "nightly-maintenance": {"task": "drivelog.maintenance", "schedule": crontab(hour=2, minute=17)},
    },
)

# Loggmaskering även i worker och beat (tokens, nycklar, lösenord, VIN).
signals.after_setup_logger.connect(setup_celery_logging, weak=False)
signals.after_setup_task_logger.connect(setup_celery_logging, weak=False)
