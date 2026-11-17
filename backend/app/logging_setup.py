"""Gemensam loggkonfiguration med maskering av hemligheter.

Samma filter används i API-processen (FastAPI/uvicorn), i Celery-worker och i beat så att
tokens, nycklar, lösenord och VIN aldrig skrivs i klartext i någon logg.
"""

from __future__ import annotations

import logging
import re

from app.config import get_config

# uvicorn har egna handlers (propagate=False) – filtret läggs även på dem.
_EXTRA_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "celery", "celery.task", "celery.worker")

_SECRET_KV = re.compile(
    r"(?i)(authorization|vcc-api-key|x-api-key|api[_-]?key|password|client_secret|refresh_token|access_token"
    r"|id_token|code_verifier)(\"?\s*[:=]\s*\"?)(?:bearer\s+|basic\s+)?([^\s\",&]+)"
)
# Frågesträngens parametrar i adresser (åtkomstloggen): OAuth-återkomstens code och state m.fl.
# Bara efter ? eller &, så att felkoder i vanlig text ("code=invalid_credentials") lämnas orörda.
_QUERY_SECRET = re.compile(r"(?i)([?&](?:code|state|session_state|token|secret)=)[^&\s\"']+")
# JWT utan "Bearer" framför.
_JWT = re.compile(r"\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+")
_BEARER = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/\-]{12,}=*)")
# VIN: 17 tecken (utan I, O, Q) med både siffror och bokstäver. Sista fyra visas.
_VIN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]{17}\b)(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])"
                  r"[A-HJ-NPR-Z0-9]{13}([A-HJ-NPR-Z0-9]{4})\b")
# Nedladdningslänken till underlaget: själva token är behörigheten och får aldrig loggas.
_PUBLIC_LINK = re.compile(r"(/public/exports/)[^\s\"'?#]+")
# Kalenderlänken (ICS) ger läsrätt till kalendern: hela adressen döljs. Alla webcal-adresser,
# och https-adresser som ser ut som en delad/publicerad kalender (Outlook) eller en .ics-fil.
# Adresserna plockas ut som hela ord (allt fram till blanksteg eller citattecken) och prövas
# sedan med vanliga strängsökningar – linjär tid även på elak indata, som en frågesträng full av
# "https://" i accessloggen.
_URL_TOKEN = re.compile(r"(?i)\b(?:webcals?|https?)://[^\s\"'<>]*")
_CALENDAR_PATHS = (
    "/owa/calendar/", "/calendar/published/",
    # iCloud (https://pNN-caldav.icloud.com/published/…), Nextcloud (…/public-calendars/…),
    # Google (…/calendar/ical/…) och andra CalDAV-tjänster (…/calendars/…).
    "/published/", "/public-calendars/", "/ical/", "/calendars/",
)
_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def _is_calendar_link(url: str) -> bool:
    """Hela ordet döljs om det någonstans innehåller en kalenderadress – även en som ligger
    inbäddad i en annan adress (``https://x/?u=https://…/owa/calendar/…``). Hellre en adress för
    mycket dold än en hemlighet i loggen."""
    lower = url.lower()
    if "webcal://" in lower or "webcals://" in lower or any(m in lower for m in _CALENDAR_PATHS):
        return True
    i = lower.find(".ics")
    while i >= 0:
        nxt = lower[i + 4:i + 5]
        if i > 0 and (not nxt or nxt not in _WORD_CHARS):
            return True
        i = lower.find(".ics", i + 1)
    return False


def _mask_calendar_link(match: re.Match[str]) -> str:
    url = match.group(0)
    return "[KALENDERLÄNK DOLD]" if _is_calendar_link(url) else url


def redact(text: str) -> str:
    """Maskerar hemligheter och VIN i en loggtext."""
    out = _URL_TOKEN.sub(_mask_calendar_link, text)  # först, så att resten inte delar upp adressen
    out = _SECRET_KV.sub(r"\1\2[DOLD]", out)
    out = _QUERY_SECRET.sub(r"\1[DOLD]", out)
    out = _JWT.sub("[DOLD]", out)
    out = _BEARER.sub(r"\1[DOLD]", out)
    out = _PUBLIC_LINK.sub(r"\1[DOLD]", out)
    return _VIN.sub(r"[VIN …\1]", out)


def _redact_arg(value: object) -> object:
    """Maskerar ett enskilt loggargument. Tal och None lämnas orörda (formatet kan vara %d)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    redacted = redact(text)
    if redacted == text:
        return value
    return redacted


def _redact_traceback(record: logging.LogRecord) -> None:
    """Undantagets text och stackinformationen maskeras också: formatteraren lägger till dem efter
    filtret. Tracebacken formateras en gång och läggs maskerad i ``exc_text``, som formatteraren
    då använder i stället för att formatera om ``exc_info``."""
    try:
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
    except Exception:  # noqa: S110 – loggningen får aldrig falla på maskeringen
        pass


class RedactingFilter(logging.Filter):
    """Tar bort uppenbara hemligheter ur loggrader (tokens, nycklar, lösenord, VIN, länkar).

    ``record.args`` nollas aldrig när det är en tupel: uvicorns ``AccessFormatter`` packar
    upp fem fält ur den. Varje element maskeras i stället för sig. Räcker inte det (hemligheten
    uppstår först när texten sätts ihop) ersätts formatsträngen med den maskerade texten och
    argumenten förbrukas utan att skrivas ut, så att tupeln behåller sin längd.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_traceback(record)
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact(msg)
        if redacted == msg:
            return True
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(_redact_arg(a) for a in args)
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            try:
                still = record.getMessage()
            except Exception:
                still = None
            if still is None or redact(still) != still:
                # "%.0s" förbrukar ett argument av vilken typ som helst utan att skriva ut det.
                record.msg = redacted.replace("%", "%%") + "%.0s" * len(args)
        elif isinstance(args, dict) and args:
            record.args = {k: _redact_arg(v) for k, v in args.items()}
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            try:
                still = record.getMessage()
            except Exception:
                still = None
            if still is None or redact(still) != still:
                record.msg, record.args = redacted, None
        else:
            record.msg, record.args = redacted, None
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Lägger maskeringsfiltret på alla handlers för rotloggern (eller ``logger``) och de
    loggrar som har egna handlers (uvicorn, celery)."""
    targets = [logger] if logger is not None else []
    targets += [logging.getLogger()] + [logging.getLogger(n) for n in _EXTRA_LOGGERS]
    for target in targets:
        for handler in target.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())


def configure_logging() -> None:
    cfg = get_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_redaction()
    logging.getLogger("httpx").setLevel(logging.WARNING)


def setup_celery_logging(logger: logging.Logger | None = None, **_: object) -> None:
    """Koppling till Celerys signaler ``after_setup_logger``/``after_setup_task_logger``."""
    install_redaction(logger)
    logging.getLogger("httpx").setLevel(logging.WARNING)


__all__ = ["RedactingFilter", "configure_logging", "install_redaction", "redact", "setup_celery_logging"]
