"""Tunn abstraktion över bakgrundskön (Celery).

Tjänstekod anropar ``enqueue("namn", **kwargs)``. I test/utveckling utan Redis
(DRIVELOG_JOBS_EAGER=true) körs jobbet direkt i processen efter aktuell transaktion.
Jobbfunktionerna registreras i ``app.worker.tasks``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.config import get_config

logger = logging.getLogger(__name__)

_registry: dict[str, Callable[..., Any]] = {}
_test_queue: list[tuple[str, dict[str, Any]]] | None = None


def register(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        _registry[name] = fn
        return fn

    return deco


def registered() -> dict[str, Callable[..., Any]]:
    return dict(_registry)


def capture_for_tests(enable: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Samlar köade jobb i en lista i stället för att köra dem (används av tester)."""
    global _test_queue
    _test_queue = [] if enable else None
    return _test_queue if _test_queue is not None else []


def enqueue(name: str, *, countdown: int | None = None, **kwargs: Any) -> None:
    """Köar ett bakgrundsjobb. Fel vid köläggning får aldrig fälla anropet."""
    if _test_queue is not None:
        _test_queue.append((name, kwargs))
        return
    cfg = get_config()
    if cfg.jobs_eager or cfg.environment == "test":
        import app.worker.tasks  # noqa: F401 - registrerar jobben

        fn = _registry.get(name)
        if fn is None:
            logger.warning("Okänt jobb: %s", name)
            return
        try:
            fn(**kwargs)
        except Exception:
            logger.exception("Jobbet %s misslyckades", name)
        return
    try:
        from app.worker.celery_app import celery

        celery.send_task(f"drivelog.{name}", kwargs=kwargs, countdown=countdown)
    except Exception:
        logger.exception("Kunde inte köa jobbet %s", name)
