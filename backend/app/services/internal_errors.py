"""Skyddsnätet (1.4.1): delsteg som får misslyckas utan att resten går förlorat.

Bakgrund: i 1.4.0 låg lagringen av bilens avläsning, skapandet av resan och hela
efterbearbetningen i samma transaktion. Ett ohanterat undantag i ett enda delsteg rullade
tillbaka allt – också avläsningen – och samma sak upprepades vid varje hämtning, utan att
något syntes i Volvo-panelen.

Här finns det gemensamma:

* **Strömbrytaren** ``is_tolerant()``. I testmiljön (``DRIVELOG_ENVIRONMENT=test``) är läget
  strängt: delstegen körs precis som förut och undantagen kastas vidare, så att testsviten
  fortfarande faller på fel. Ett test slår på det toleranta läget med ``tolerant_mode()``.
  I alla andra miljöer är läget tolerant.
* **Sparpunktshjälparen** ``run_step()``. I det toleranta läget körs steget i en egen sparpunkt
  (``Session.begin_nested``). Kraschar det rullas bara stegets egna ändringar tillbaka (objekt
  som steget ändrat läses om från databasen, nya objekt tas bort ur sessionen), felet loggas
  och antecknas i sessionen, och anroparen fortsätter med stegets standardvärde.
* **Antecknandet** av interna fel. De senaste ``MAX_ENTRIES`` sparas per användare i den dolda
  inställningen ``system.internal_errors`` (ingen databasmigrering), med tid, steg, undantagets
  typ, kodställena i appens egna filer (fil:rad:funktion) och meddelandet kapat till
  ``MAX_MESSAGE`` tecken och tvättat med loggens maskering (``logging_setup.redact``) – så att
  varken nycklar, token, VIN eller kalenderlänk följer med. Felsökningsfilen
  (GET /api/v1/volvo/diagnostics) visar dem under ``internal_errors``.

SQLite: pysqlite inleder transaktionen först vid första ändringen. En sparpunkt utan pågående
transaktion blir då själva transaktionen, och när sparpunkten släpps checkas allt in – också
det som anroparen ville kunna rulla tillbaka. ``run_step`` inleder därför transaktionen först
när ingen pågår (samma mönster som de befintliga sparpunkterna i notifier, alerts och
month_close, som alltid körs efter en skrivning).
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_config

logger = logging.getLogger(__name__)

# Miljöer där undantagen alltid kastas vidare (om inte testet slår på det toleranta läget).
STRICT_ENVIRONMENTS = frozenset({"test"})
# Inställningen där felen sparas (sektionen ``system`` i settings, skrivskyddad för API:t).
SECTION, KEY = "system", "internal_errors"
MAX_ENTRIES = 20
MAX_MESSAGE = 300
MAX_FRAMES = 16

# Sessionens anteckningar om misslyckade steg (inte transaktionell: överlever en tillbakarullning).
_FAILURES_KEY = "drivelog_step_failures"
# Sessionens köade efterarbete som ett misslyckat steg inte ska lämna efter sig: jobb som väntar
# på commit (app.jobs) och beställda omräkningar (learning.recompute).
_INFO_KEYS = ("drivelog_deferred_jobs", "drivelog_recompute_vehicles")

_APP_DIR = Path(__file__).resolve().parents[1]  # backend/app

# Stegens namn i klartext för raden under "Senaste fel".
STEP_LABELS: dict[str, str] = {
    "detection": "resedetekteringen",
    "pipeline.start_time": "uppskattad starttid",
    "pipeline.consumption": "uppskattad förbrukning",
    "pipeline.merge": "sammanslagning",
    "pipeline.gaps": "luckor i mätarställningen",
    "pipeline.places": "platsstatistik",
    "pipeline.patterns": "mönster",
    "pipeline.suggestions": "platsförslag",
    "pipeline.calendar": "kalendern",
    "pipeline.classify": "klassning",
    "pipeline.journeys": "resekedjor",
    "pipeline.autopilot": "autopiloten",
    "pipeline.review_items": "granskningsärenden",
    "pipeline.close_orphaned": "stängning av gamla ärenden",
    "pipeline.anomalies": "stängning av avvikelser",
    "pipeline.notify": "notiser",
    "volvo.poll": "hämtningen",
}


# ----------------------------------------------------------------------------- strömbrytaren

_tolerant_override: bool | None = None


def is_tolerant() -> bool:
    """Ska ett kraschat delsteg hoppas över (sant) eller kastas vidare (falskt)?

    Strängt i testmiljön, tolerant i alla andra – om inte ``tolerant_mode()`` säger annat.
    """
    if _tolerant_override is not None:
        return _tolerant_override
    return get_config().environment not in STRICT_ENVIRONMENTS


@contextmanager
def tolerant_mode(enabled: bool = True) -> Iterator[None]:
    """Slår på (eller av) det toleranta läget, t.ex. i ett test."""
    global _tolerant_override
    previous = _tolerant_override
    _tolerant_override = enabled
    try:
        yield
    finally:
        _tolerant_override = previous


# ----------------------------------------------------------------------------- sparpunkter


def _ensure_transaction(db: Session) -> None:
    """SQLite: inled transaktionen om ingen pågår, så att sparpunkten aldrig blir den yttersta."""
    conn = db.connection()
    if conn.dialect.name != "sqlite":
        return
    dbapi = conn.connection.dbapi_connection
    if dbapi is not None and not dbapi.in_transaction:
        conn.exec_driver_sql("BEGIN")


def _snapshot_info(db: Session) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _INFO_KEYS:
        value = db.info.get(key)
        out[key] = None if value is None else type(value)(value)
    return out


def _restore_info(db: Session, saved: dict[str, Any]) -> None:
    for key, value in saved.items():
        if value is None:
            db.info.pop(key, None)
        else:
            db.info[key] = value


def run_step(
    db: Session,
    step: str,
    fn: Callable[..., Any],
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    """Kör ett delsteg. Strängt läge: som ett vanligt anrop. Tolerant läge: i en egen sparpunkt;
    kraschar steget rullas bara dess ändringar tillbaka, felet loggas och antecknas
    (``take_failures``) och ``default`` returneras."""
    if not is_tolerant():
        return fn(*args, **kwargs)
    # Det som redan är ändrat hör till anroparen: ett fel här är inte stegets.
    db.flush()
    _ensure_transaction(db)
    saved = _snapshot_info(db)
    try:
        with db.begin_nested():
            return fn(*args, **kwargs)
    except Exception as exc:
        _restore_info(db, saved)
        note_failure(db, step, exc)
        return default


def note_failure(db: Session, step: str, exc: BaseException) -> dict[str, Any]:
    """Loggar felet och antecknar det i sessionen (sparas av ``save_failures``).

    Kodställena tas både ur undantaget (från anroparen och inåt) och ur anropsstacken utanför,
    så att det syns vilket steg i vilken väg som kraschade."""
    logger.exception("Delsteget %s misslyckades och hoppades över", step, exc_info=exc)
    outer = traceback.extract_stack(sys._getframe(1))[:-1]  # anroparens ram finns redan i undantaget
    entry = describe(step, exc, outer=outer)
    db.info.setdefault(_FAILURES_KEY, []).append(entry)
    return entry


def take_failures(db: Session) -> list[dict[str, Any]]:
    """Sessionens anteckningar om misslyckade steg (och glömmer dem)."""
    return list(db.info.pop(_FAILURES_KEY, None) or [])


# ----------------------------------------------------------------------------- beskrivning

# Utöver loggens maskering: koordinater och registreringsnummer (svenska och liknande).
_COORD = re.compile(r"(?<![:\d])\d{1,3}\.\d{4,}")
_PLATE = re.compile(r"\b[A-ZÅÄÖ]{3} ?\d{2}[A-ZÅÄÖ0-9]\b")
_TYPE_RE = re.compile(r"^[A-Za-z_][\w.]{0,120}$")
_WHERE_RE = re.compile(r"^app/[\w/.\-]+\.py:\d{1,6}:[\w<>]{1,80}$")
_STEP_RE = re.compile(r"^[a-z0-9_.\-]{1,60}$")


def clean_message(text: str, *, extra: tuple[str | None, ...] = ()) -> str:
    """Loggens maskering, koordinater och registreringsnummer bort, kapat till MAX_MESSAGE."""
    from app.logging_setup import redact

    out = str(text)
    for value in extra:
        if value and len(value) >= 3:
            out = out.replace(value, "[DOLD]")
    out = redact(out)
    out = _COORD.sub("[DOLD]", out)
    out = _PLATE.sub("[DOLD]", out)
    out = " ".join(out.split())
    if len(out) > MAX_MESSAGE:
        out = out[: MAX_MESSAGE - 1] + "…"
    return out


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    return cls.__qualname__ if module in ("builtins", "__main__") else f"{module}.{cls.__qualname__}"


def _frames(exc: BaseException, outer: list[traceback.FrameSummary] | None = None) -> list[str]:
    """Kodställena i appens egna filer, innersta sist (fil:rad:funktion)."""
    out: list[str] = []
    for frame in [*(outer or []), *traceback.extract_tb(exc.__traceback__)]:
        try:
            rel = Path(frame.filename).resolve().relative_to(_APP_DIR)
        except (ValueError, OSError):
            continue
        out.append(f"app/{rel.as_posix()}:{frame.lineno}:{frame.name}")
    return out[-MAX_FRAMES:]


def describe(step: str, exc: BaseException, *, outer: list[traceback.FrameSummary] | None = None) -> dict[str, Any]:
    from app.db import utcnow

    return {
        "at": utcnow().isoformat(timespec="seconds"),
        "step": step,
        "type": _type_name(exc),
        "message": clean_message(str(exc)),
        "where": _frames(exc, outer),
    }


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step)


def processing_message(failures: list[dict[str, Any]]) -> str:
    """Raden under "Senaste fel" i Volvo-panelen."""
    steps = [f["step"] for f in failures]
    if "detection" in steps:
        return (
            "Avläsningen hämtades, men resan kunde inte bearbetas (steg: resedetekteringen). "
            "Avläsningarna prövas igen vid nästa hämtning. Ladda ner felsökningsfilen."
        )
    labels = ", ".join(dict.fromkeys(step_label(s) for s in steps))
    return (
        f"Avläsningen hämtades, men ett steg i bearbetningen av resorna misslyckades (steg: {labels}). "
        "Resorna finns i journalen. Ladda ner felsökningsfilen."
    )


# ----------------------------------------------------------------------------- lagring


def save_failures(db: Session, user_id: Any, failures: list[dict[str, Any]], *, origin: str | None = None) -> None:
    """Sparar felen först i användarens lista (högst MAX_ENTRIES, nyaste först)."""
    if not failures:
        return
    from app.services import settings as settings_svc

    current = settings_svc.get_section(db, user_id, SECTION).get(KEY)
    entries = list(current) if isinstance(current, list) else []
    new = [{**f, "origin": origin} if origin else dict(f) for f in reversed(failures)]
    settings_svc.update_section(db, user_id, SECTION, {KEY: (new + entries)[:MAX_ENTRIES]}, allow_read_only=True)


def record_after_rollback(
    db: Session, user_id: Any, step: str, exc: BaseException, *, origin: str | None = None,
    connection_id: Any = None,
) -> None:
    """Efter en tillbakarullad transaktion: antecknar felet i en egen kort transaktion och
    (om ``connection_id`` anges) visar det under "Senaste fel". Kastar aldrig."""
    try:
        entry = describe(step, exc)
        save_failures(db, user_id, [entry], origin=origin)
        if connection_id is not None:
            from app.db import utcnow
            from app.models import IntegrationConnection

            conn = db.get(IntegrationConnection, connection_id)
            if conn is not None:
                conn.last_error = (
                    f"Ett internt fel inträffade (steg: {step_label(step)}). Ladda ner felsökningsfilen."
                )
                conn.last_error_at = utcnow()
        db.commit()
    except Exception:
        logger.exception("Kunde inte anteckna det interna felet")
        try:
            db.rollback()
        except Exception:  # noqa: S110 – antecknandet får aldrig ge ett nytt fel
            pass


def entries(db: Session, user_id: Any) -> list[dict[str, Any]]:
    from app.services import settings as settings_svc

    value = settings_svc.get_section(db, user_id, SECTION).get(KEY)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def public_entries(db: Session, user_id: Any, *, scrub: tuple[str | None, ...] = ()) -> list[dict[str, Any]]:
    """Felen för felsökningsfilen. Varje fält prövas och tvättas på nytt (även poster som
    sparats av en äldre version): bara kända former släpps igenom."""
    out: list[dict[str, Any]] = []
    for e in entries(db, user_id)[:MAX_ENTRIES]:
        step = str(e.get("step") or "")
        type_ = str(e.get("type") or "")
        at = str(e.get("at") or "")
        origin = str(e.get("origin") or "")
        out.append({
            "at": at if re.fullmatch(r"[0-9T:+\-.Z]{10,40}", at) else None,
            "step": step if _STEP_RE.fullmatch(step) else None,
            "origin": origin if _STEP_RE.fullmatch(origin) else None,
            "type": type_ if _TYPE_RE.fullmatch(type_) else None,
            "message": clean_message(str(e.get("message") or ""), extra=scrub),
            "where": [w for w in (e.get("where") or []) if isinstance(w, str) and _WHERE_RE.fullmatch(w)][-MAX_FRAMES:],
        })
    return out


__all__ = [
    "MAX_ENTRIES",
    "MAX_MESSAGE",
    "clean_message",
    "describe",
    "entries",
    "is_tolerant",
    "note_failure",
    "processing_message",
    "public_entries",
    "record_after_rollback",
    "run_step",
    "save_failures",
    "step_label",
    "take_failures",
    "tolerant_mode",
]
