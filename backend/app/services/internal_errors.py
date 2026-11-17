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
  varken nycklar, token, VIN eller kalenderlänk följer med. Dessutom tvättas användarens egna
  kända värden bort ordagrant och oavsett skiftläge (``private_values``: anslutningarnas
  hemligheter, kalenderlänken, bilarnas VIN och registreringsnummer, sparade platser och de
  senaste resornas adresser), liksom mönster för gatuadresser, postnummer med ort, koordinater
  och registreringsnummer. En adress som varken är känd eller följer mönstren kan ändå finnas
  kvar i meddelandet. Samma fel (steg, typ och kodställen) tar högst en plats i listan: posten
  får ``count`` och ``first_at`` i stället för att tränga ut äldre fel. Felsökningsfilen
  (GET /api/v1/volvo/diagnostics) visar dem under ``internal_errors``.
* **Skyddsnätet får aldrig själv fälla något**: ``describe`` och ``note_failure`` kastar aldrig,
  inte ens för ett undantag vars text inte går att läsa.

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
# Sätts när en efterbearbetning (pipeline.after_trips_changed med isolerade steg) har körts klart.
_PIPELINE_DONE_KEY = "drivelog_pipeline_done"
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
    try:
        logger.exception("Delsteget %s misslyckades och hoppades över", step, exc_info=exc)
    except Exception:  # noqa: S110 – loggningen får aldrig fälla skyddsnätet
        pass
    try:
        outer = traceback.extract_stack(sys._getframe(1))[:-1]  # anroparens ram finns redan i undantaget
        entry = describe(step, exc, outer=outer)
    except Exception:
        # Sista utvägen: steg och typ räcker för att se att något hände.
        entry = _minimal_entry(step, exc)
    db.info.setdefault(_FAILURES_KEY, []).append(entry)
    return entry


def take_failures(db: Session) -> list[dict[str, Any]]:
    """Sessionens anteckningar om misslyckade steg (och glömmer dem)."""
    return list(db.info.pop(_FAILURES_KEY, None) or [])


def failure_count(db: Session) -> int:
    """Antalet antecknade misslyckade steg i sessionen (utan att glömma dem)."""
    return len(db.info.get(_FAILURES_KEY) or [])


def mark_pipeline_done(db: Session) -> None:
    """Efterbearbetningen har körts klart (med eller utan överhoppade steg)."""
    db.info[_PIPELINE_DONE_KEY] = True


def take_pipeline_done(db: Session) -> bool:
    """Har en efterbearbetning körts klart sedan förra frågan? (Och glömmer svaret.)"""
    return bool(db.info.pop(_PIPELINE_DONE_KEY, False))


# ----------------------------------------------------------------------------- beskrivning

# Utöver loggens maskering: koordinater, registreringsnummer (svenska och liknande), VIN med
# gemener, gatuadresser och postnummer med ort.
_COORD = re.compile(r"(?<![:\d])\d{1,3}\.\d{4,}")
# Ett koordinatpar med tre decimaler (59.090,15.000 eller 59.090, 15.000).
_COORD_PAIR = re.compile(r"(?<![\w.:])-?\d{1,3}\.\d{3,}\s*[,;]\s*-?\d{1,3}\.\d{3,}(?![\d.])")
# Versaler med eller utan mellanrum (ABC 123, ABC12D), gemener bara utan (abc123): "got 100" är inget regnr.
_PLATE = re.compile(r"\b(?:[A-ZÅÄÖ]{3} ?\d{2}[A-ZÅÄÖ0-9]|[A-Za-zÅÄÖåäö]{3}\d{2}[A-Za-zÅÄÖåäö0-9])\b")
_VIN_ANY = re.compile(
    r"(?i)\b(?=[a-hj-npr-z0-9]{17}\b)(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-hj-npr-z0-9]{17}\b"
)
_STREET = re.compile(
    r"(?i)\b[\wåäöéü\-]*(?:gatan|gata|vägen|väg|gränden|gränd|torget|torg|allén|allé|stigen|backen|leden"
    r"|platsen|plan|stranden|strand|kajen|esplanaden|street|road|avenue)\s+\d{1,4}(?:\s?[a-z](?![\wåäö]))?"
)
_POSTCODE_CITY = re.compile(r"\b\d{3} ?\d{2}\s+[A-ZÅÄÖ][\wåäöÅÄÖéü\-]+")
_TYPE_RE = re.compile(r"^[A-Za-z_][\w.]{0,120}$")
_WHERE_RE = re.compile(r"^app/[\w/.\-]+\.py:\d{1,6}:[\w<>]{1,80}$")
_STEP_RE = re.compile(r"^[a-z0-9_.\-]{1,60}$")


def clean_message(text: str, *, extra: tuple[str | None, ...] | list[str] = ()) -> str:
    """Kända värden (``extra``, oavsett skiftläge), loggens maskering, VIN, koordinater,
    registreringsnummer och adressmönster bort, kapat till MAX_MESSAGE."""
    from app.logging_setup import redact

    out = str(text)
    for value in sorted({v for v in extra if isinstance(v, str) and len(v.strip()) >= 3}, key=len, reverse=True):
        out = re.sub(re.escape(value.strip()), "[DOLD]", out, flags=re.IGNORECASE)
    out = redact(out)
    out = _VIN_ANY.sub("[DOLD]", out)
    out = _COORD_PAIR.sub("[DOLD]", out)
    out = _COORD.sub("[DOLD]", out)
    out = _PLATE.sub("[DOLD]", out)
    out = _STREET.sub("[DOLD]", out)
    out = _POSTCODE_CITY.sub("[DOLD]", out)
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


def _exception_text(exc: BaseException) -> str:
    """Undantagets text – eller en ersättning om ``__str__`` själv kastar."""
    try:
        return str(exc)
    except Exception:
        return f"<{type(exc).__name__}: texten kunde inte läsas>"


def _now_iso() -> str:
    from app.db import utcnow

    return utcnow().isoformat(timespec="seconds")


def _minimal_entry(step: str, exc: BaseException) -> dict[str, Any]:
    try:
        type_ = _type_name(exc)
    except Exception:
        type_ = "Exception"
    return {"at": _now_iso(), "step": step, "type": type_, "message": "", "where": []}


def describe(step: str, exc: BaseException, *, outer: list[traceback.FrameSummary] | None = None) -> dict[str, Any]:
    """Posten för ett fel. Kastar aldrig: går något fält inte att ta fram blir posten minimal."""
    try:
        return {
            "at": _now_iso(),
            "step": step,
            "type": _type_name(exc),
            "message": clean_message(_exception_text(exc)),
            "where": _frames(exc, outer),
        }
    except Exception:
        logger.exception("Felet kunde inte beskrivas fullt ut")
        return _minimal_entry(step, exc)


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step)


# Raderna under "Senaste fel" om en misslyckad bearbetning börjar alla så här (collector känner
# igen dem och låter dem stå kvar tills bearbetningen har lyckats).
PROCESSING_PREFIX = "Avläsningen hämtades, men "
DETECTION_MESSAGE = (
    PROCESSING_PREFIX + "resan kunde inte bearbetas (steg: resedetekteringen). "
    "Avläsningarna prövas igen vid nästa hämtning. Ladda ner felsökningsfilen."
)


def processing_message(failures: list[dict[str, Any]]) -> str:
    """Raden under "Senaste fel" i Volvo-panelen."""
    steps = [f["step"] for f in failures]
    if "detection" in steps:
        return DETECTION_MESSAGE
    labels = ", ".join(dict.fromkeys(step_label(s) for s in steps))
    return (
        f"{PROCESSING_PREFIX}ett steg i bearbetningen av resorna misslyckades (steg: {labels}). "
        "Resorna finns i journalen. Ladda ner felsökningsfilen."
    )


# ----------------------------------------------------------------------------- lagring


def private_values(db: Session, user_id: Any) -> list[str]:
    """Användarens egna värden som aldrig får följa med ett felmeddelande: anslutningarnas
    hemligheter (åtkomst- och förnyelsetoken, klienthemlighet, API-nyckel, kalenderlänken),
    bilarnas VIN och registreringsnummer, sparade platsers namn och adresser och de senaste
    resornas adresser (hela och varje kommaseparerad del). Körs bara när ett fel antecknas eller
    felsökningsfilen hämtas."""
    from sqlalchemy import or_, select

    from app.models import IntegrationConnection, KnownLocation, LogicalTrip, Vehicle
    from app.services import integrations

    values: list[str] = []

    def add(value: Any, *, parts: bool = False) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
            if parts:
                values.extend(p.strip() for p in value.split(",") if len(p.strip()) >= 4)

    def add_secret(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                add_secret(v)
        elif isinstance(value, str) and len(value) >= 6:
            add(value)

    for conn in db.execute(select(IntegrationConnection).where(IntegrationConnection.user_id == user_id)).scalars():
        add_secret(integrations.secrets(conn))
    vehicle_ids = []
    for vehicle in db.execute(select(Vehicle).where(Vehicle.owner_user_id == user_id)).scalars():
        vehicle_ids.append(vehicle.id)
        add(vehicle.vin)
        add(vehicle.registration_number)
    for loc in db.execute(select(KnownLocation).where(KnownLocation.owner_user_id == user_id)).scalars():
        if len((loc.name or "").strip()) >= 4:
            add(loc.name)
        add(loc.address, parts=True)
    if vehicle_ids:
        rows = db.execute(
            select(LogicalTrip.start_address, LogicalTrip.end_address)
            .where(LogicalTrip.vehicle_id.in_(vehicle_ids),
                   or_(LogicalTrip.start_address.is_not(None), LogicalTrip.end_address.is_not(None)))
            .order_by(LogicalTrip.started_at.desc())
            .limit(300)
        ).all()
        for start, end in rows:
            add(start, parts=True)
            add(end, parts=True)
    return values


def _same_error(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a.get("step"), a.get("type"), list(a.get("where") or [])) == (
        b.get("step"), b.get("type"), list(b.get("where") or [])
    )


def save_failures(db: Session, user_id: Any, failures: list[dict[str, Any]], *, origin: str | None = None) -> None:
    """Sparar felen först i användarens lista (högst MAX_ENTRIES, nyaste först).

    Samma fel (steg, typ och kodställen) tar högst en plats: den äldre posten tas bort och den
    nya får ``first_at`` (när felet sågs första gången) och ``count`` (hur många gånger), så att
    ett fel som upprepas vid varje hämtning inte tränger ut andra fel. Meddelandet tvättas en
    gång till med användarens kända värden (``private_values``)."""
    if not failures:
        return
    from app.services import settings as settings_svc

    current = settings_svc.get_section(db, user_id, SECTION).get(KEY)
    stored = [e for e in current if isinstance(e, dict)] if isinstance(current, list) else []
    try:
        known = private_values(db, user_id)
    except Exception:
        logger.exception("De kända värdena kunde inte läsas; meddelandet utelämnas")
        known = None
    for failure in failures:  # äldst först; varje ny post hamnar överst
        entry = {**failure, "origin": origin} if origin else dict(failure)
        entry["message"] = clean_message(str(entry.get("message") or ""), extra=known) if known is not None else ""
        entry.setdefault("first_at", entry.get("at"))
        entry.setdefault("count", 1)
        for i, old in enumerate(stored):
            if _same_error(old, entry):
                stored.pop(i)
                entry["first_at"] = old.get("first_at") or old.get("at")
                previous = old.get("count")
                entry["count"] = (previous if isinstance(previous, int) and previous > 0 else 1) + 1
                break
        stored.insert(0, entry)
    settings_svc.update_section(db, user_id, SECTION, {KEY: stored[:MAX_ENTRIES]}, allow_read_only=True)


def record_after_rollback(
    db: Session, user_id: Any, step: str, exc: BaseException, *, origin: str | None = None,
    connection_id: Any = None, now: Any = None,
) -> None:
    """Efter en tillbakarullad transaktion: antecknar felet i en egen kort transaktion och
    (om ``connection_id`` anges) visar det under "Senaste fel". Kastar aldrig.

    Tillbakarullningen ångrade också ``last_sync_at``; den sätts igen (som ``mark_error`` gör),
    så att schemaläggaren väntar sitt vanliga intervall i stället för att anropa Volvo vid varje
    tick (60 s) så länge felet består. ``last_success_at`` – "Senast synkad" – rörs inte."""
    try:
        entry = describe(step, exc)
        save_failures(db, user_id, [entry], origin=origin)
        if connection_id is not None:
            from app.db import utcnow
            from app.models import IntegrationConnection

            conn = db.get(IntegrationConnection, connection_id)
            if conn is not None:
                when = now or utcnow()
                conn.last_error = (
                    f"Ett internt fel inträffade (steg: {step_label(step)}). Ladda ner felsökningsfilen."
                )
                conn.last_error_at = when
                conn.last_sync_at = when
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


def _iso_or_none(value: Any) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9T:+\-.Z]{10,40}", text) else None


def public_entries(db: Session, user_id: Any, *, scrub: tuple[str | None, ...] = ()) -> list[dict[str, Any]]:
    """Felen för felsökningsfilen. Varje fält prövas och tvättas på nytt (även poster som
    sparats av en äldre version): bara kända former släpps igenom, och meddelandet tvättas med
    användarens kända värden (``private_values``) och ``scrub``. Går de kända värdena inte att
    läsa utelämnas meddelandet."""
    stored = entries(db, user_id)[:MAX_ENTRIES]
    if not stored:
        return []
    try:
        known: list[str] | None = [*private_values(db, user_id), *(v for v in scrub if v)]
    except Exception:
        logger.exception("De kända värdena kunde inte läsas; meddelandena utelämnas")
        known = None
    out: list[dict[str, Any]] = []
    for e in stored:
        step = str(e.get("step") or "")
        type_ = str(e.get("type") or "")
        origin = str(e.get("origin") or "")
        count = e.get("count")
        at = _iso_or_none(e.get("at"))
        out.append({
            "at": at,
            "first_at": _iso_or_none(e.get("first_at")) or at,
            "count": count if isinstance(count, int) and not isinstance(count, bool) and 0 < count < 10**9 else 1,
            "step": step if _STEP_RE.fullmatch(step) else None,
            "origin": origin if _STEP_RE.fullmatch(origin) else None,
            "type": type_ if _TYPE_RE.fullmatch(type_) else None,
            "message": clean_message(str(e.get("message") or ""), extra=known) if known is not None else "",
            "where": [w for w in (e.get("where") or []) if isinstance(w, str) and _WHERE_RE.fullmatch(w)][-MAX_FRAMES:],
        })
    return out


__all__ = [
    "MAX_ENTRIES",
    "MAX_MESSAGE",
    "clean_message",
    "describe",
    "entries",
    "failure_count",
    "is_tolerant",
    "mark_pipeline_done",
    "note_failure",
    "private_values",
    "processing_message",
    "public_entries",
    "record_after_rollback",
    "run_step",
    "save_failures",
    "step_label",
    "take_failures",
    "take_pipeline_done",
    "tolerant_mode",
]
