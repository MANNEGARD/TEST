"""Tunn abstraktion över bakgrundskön (Celery).

Tjänstekod anropar ``enqueue("namn", **kwargs)``. Jobbfunktionerna registreras i
``app.worker.tasks``.

* Compose-driften: jobbet skickas till Celery via Redis.
* Testsviten: jobbet körs direkt i anropet.
* Enprocessläget (DRIVELOG_JOBS_EAGER=true, t.ex. Home Assistant-tillägget): jobbet körs i
  den tråd som begär det. Har tråden en databassession med en öppen skrivtransaktion skjuts
  jobbet upp tills den transaktionen är avslutad – annars skulle jobbets egen session vänta
  på ett skrivlås som den anropande tråden själv håller ("database is locked" med SQLite).
  Ett jobb med ``countdown`` körs i stället i en bakgrundstråd efter fördröjningen, så att
  anropet inte väntar på det. Begärs samma jobb (samma namn och argument) igen innan det
  har startat slås det ihop med det väntande – dock aldrig längre än ``MAX_DEBOUNCE_SECONDS``
  från den första begäran. Det som inte hann köras före en omstart måste jobbet självt
  kunna ta igen (se t.ex. ``learning.recompute``).
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction

from app.config import get_config

logger = logging.getLogger(__name__)

_registry: dict[str, Callable[..., Any]] = {}
_test_queue: list[tuple[str, dict[str, Any]]] | None = None

_DEFERRED_KEY = "drivelog_deferred_jobs"
# Enprocessläget: ett fördröjt jobb skjuts upp av nya begäranden högst så här länge.
MAX_DEBOUNCE_SECONDS = 10.0


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


# --------------------------------------------------------------------------- sessionsbevakning
# Varje tråd håller reda på sina sessioner som har en databasanslutning i en pågående
# transaktion, så att enqueue() kan avgöra om tråden själv håller ett skrivlås.

_local = threading.local()


def _thread_sessions() -> weakref.WeakKeyDictionary[Session, Connection]:
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = weakref.WeakKeyDictionary()
        _local.conns = conns
    return conns


@event.listens_for(Session, "after_begin")
def _track_begin(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    _thread_sessions()[session] = connection


@event.listens_for(Session, "after_transaction_end")
def _track_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is not None:  # SAVEPOINT eller inre transaktion
        return
    _thread_sessions().pop(session, None)
    pending = session.info.pop(_DEFERRED_KEY, None)
    for name, kwargs in pending or ():
        _run_now(name, kwargs)


def _holds_write_lock(connection: Connection) -> bool:
    """Har anslutningen en öppen skrivtransaktion i databasen?"""
    try:
        if connection.closed or connection.invalidated:
            return False
        dbapi = connection.connection.dbapi_connection
    except Exception:
        return False
    # sqlite3 inleder transaktionen först vid första ändringen: in_transaction är då sant.
    return bool(getattr(dbapi, "in_transaction", False))


def _session_with_write_lock() -> Session | None:
    for session, connection in list(_thread_sessions().items()):
        if connection.dialect.name == "sqlite" and _holds_write_lock(connection):
            return session
    return None


# --------------------------------------------------------------------------- körning


def _run_now(name: str, kwargs: dict[str, Any]) -> None:
    import app.worker.tasks  # noqa: F401 - registrerar jobben

    fn = _registry.get(name)
    if fn is None:
        logger.warning("Okänt jobb: %s", name)
        return
    try:
        fn(**kwargs)
    except Exception:
        logger.exception("Jobbet %s misslyckades", name)


# Enprocessläget: väntande fördröjda jobb, per (namn, argument) → (timer, första begäran).
_later: dict[tuple[str, tuple[tuple[str, Any], ...]], tuple[threading.Timer, float]] = {}
_later_lock = threading.Lock()


def _run_later(name: str, kwargs: dict[str, Any], delay: float) -> None:
    key = (name, tuple(sorted(kwargs.items())))
    now = time.monotonic()
    with _later_lock:
        waiting = _later.get(key)
        first = now
        if waiting is not None:
            timer, first = waiting
            if now - first >= MAX_DEBOUNCE_SECONDS:
                return  # startar snart ändå och ser då även den här begäran
            timer.cancel()
        delay = min(delay, max(0.0, first + MAX_DEBOUNCE_SECONDS - now))
        timer = threading.Timer(delay, _fire_later, args=(key, name, kwargs))
        timer.name = f"drivelog-job-{name}"
        timer.daemon = True
        _later[key] = (timer, first)
        timer.start()


def cancel_delayed() -> None:
    """Avbryter fördröjda jobb som inte har startat (när processen stängs). Jobb som körs får
    gå klart; det som inte hann köras tas igen efter start (se learning.recompute)."""
    with _later_lock:
        for timer, _first in _later.values():
            timer.cancel()
        _later.clear()


def _fire_later(key: tuple[str, tuple[tuple[str, Any], ...]], name: str, kwargs: dict[str, Any]) -> None:
    with _later_lock:
        waiting = _later.get(key)
        if waiting is not None and waiting[0] is threading.current_thread():
            del _later[key]
    _run_now(name, kwargs)


def enqueue(name: str, *, countdown: float | None = None, **kwargs: Any) -> None:
    """Köar ett bakgrundsjobb. Fel vid köläggning får aldrig fälla anropet."""
    if _test_queue is not None:
        _test_queue.append((name, kwargs))
        return
    cfg = get_config()
    if cfg.jobs_eager and countdown:
        _run_later(name, kwargs, float(countdown))
        return
    if cfg.jobs_eager:
        owner = _session_with_write_lock()
        if owner is not None:
            # Körs när den anropande transaktionen är klar (commit, rollback eller close).
            owner.info.setdefault(_DEFERRED_KEY, []).append((name, kwargs))
            return
        _run_now(name, kwargs)
        return
    if cfg.environment == "test":
        _run_now(name, kwargs)
        return
    try:
        from app.worker.celery_app import celery

        celery.send_task(f"drivelog.{name}", kwargs=kwargs, countdown=countdown)
    except Exception:
        logger.exception("Kunde inte köa jobbet %s", name)
