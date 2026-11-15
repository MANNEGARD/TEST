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
_BEARER = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/\-]{12,}=*)")
# VIN: 17 tecken (utan I, O, Q) med både siffror och bokstäver. Sista fyra visas.
_VIN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]{17}\b)(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])"
                  r"[A-HJ-NPR-Z0-9]{13}([A-HJ-NPR-Z0-9]{4})\b")


def redact(text: str) -> str:
    """Maskerar hemligheter och VIN i en loggtext."""
    out = _SECRET_KV.sub(r"\1\2[DOLD]", text)
    out = _BEARER.sub(r"\1[DOLD]", out)
    return _VIN.sub(r"[VIN …\1]", out)


class RedactingFilter(logging.Filter):
    """Tar bort uppenbara hemligheter ur loggrader (tokens, nycklar, lösenord, VIN)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact(msg)
        if redacted != msg:
            record.msg, record.args = redacted, ()
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
