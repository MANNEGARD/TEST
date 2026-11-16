"""Signerade nedladdningslänkar för underlaget (byggkontraktet C.6 och E.2).

Token: ``{export_id.hex}.{exp_unix}.{sig}`` där
``sig = base64url(HMAC_SHA256(k, "dl-export:v1:{id.hex}:{exp}:{sha256}:{link_epoch}"))`` och
``k = sha256(b"drivelog-export-link:" + secret_key)``.

Signaturen binder länken till exporten, filens innehåll och användarens ``link_epoch``.
Länken stängs alltså när filen ersätts eller raderas, när lösenordet byts eller när
ägaren trycker "Stäng alla nedladdningslänkar" – utan att ``secret_key`` behöver roteras.
Själva länken sparas aldrig; den byggs på nytt ur notisens ``exp``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_config
from app.models import ExportJob, User

LINK_VALID = timedelta(days=7)
LINK_MAX_DOWNLOADS = 10

INVALID_TEXT = "Länken fungerar inte. Öppna den senaste notisen eller logga in i DriveLog."
EXPIRED_TEXT = "Länken har gått ut. Öppna den senaste notisen eller logga in i DriveLog."
SUPERSEDED_TEXT = "En nyare fil finns – öppna senaste notisen"
REVOKED_TEXT = "Filen är återkallad – månaden har inga tjänsteresor längre"

PUBLIC_PATH = "/api/v1/public/exports/"
_TOKEN_RE = re.compile(r"^([0-9a-f]{32})\.([0-9]{1,12})\.([A-Za-z0-9_-]{20,100})$")


class LinkInvalid(Exception):
    """Ogiltig länk (svaret blir 404)."""

    def __init__(self, message: str = INVALID_TEXT) -> None:
        super().__init__(message)
        self.message = message


class LinkExpired(Exception):
    """Utgången eller ersatt länk (svaret blir 410)."""

    def __init__(self, message: str = EXPIRED_TEXT) -> None:
        super().__init__(message)
        self.message = message


def _key() -> bytes:
    secret = get_config().secret_key
    return hashlib.sha256(b"drivelog-export-link:" + secret.encode("utf-8")).digest()


def _signature(job_id: uuid.UUID, exp: int, sha256: str, epoch: int) -> str:
    message = f"dl-export:v1:{job_id.hex}:{exp}:{sha256}:{epoch}".encode()
    digest = hmac.new(_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _exp_unix(exp: datetime) -> int:
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return int(exp.timestamp())


def make_token(job: ExportJob, user: User, exp: datetime) -> str | None:
    """``{id.hex}.{exp}.{sig}``; None när filen saknar sha256 (då finns ingen länk).

    Ingen länk byggs heller till en export som inte har någon fil (inte klar, raderad eller
    utan sökväg): den skulle aldrig gå att öppna.
    """
    if not job.sha256 or job.user_id != user.id:
        return None
    if job.status != "done" or job.deleted_at is not None or not job.file_path:
        return None
    exp_unix = _exp_unix(exp)
    sig = _signature(job.id, exp_unix, job.sha256, int(user.link_epoch or 0))
    return f"{job.id.hex}.{exp_unix}.{sig}"


def make_url(job: ExportJob, user: User, exp: datetime) -> str | None:
    """Hela länken till ``/api/v1/public/exports/{token}``, eller None utan token."""
    token = make_token(job, user, exp)
    if token is None:
        return None
    return f"{get_config().base_url.rstrip('/')}{PUBLIC_PATH}{token}"


def verify(db: Session, token: str, now: datetime) -> ExportJob:
    """Prövar en länk och returnerar exporten; kastar LinkInvalid (404) eller LinkExpired (410).

    Ordningen följer kontraktet: format → giltighetstid → exporten finns, är klar och inte
    raderad → signaturen → antalet hämtningar → filen finns. Antalet hämtningar räknas
    upp av routen, inte här.
    """
    from app.services.export import service as export_svc

    m = _TOKEN_RE.match(token or "")
    if m is None:
        raise LinkInvalid()
    job_hex, exp_text, sig = m.groups()
    exp = int(exp_text)
    if exp <= _exp_unix(now):
        raise LinkExpired()
    job = db.get(ExportJob, uuid.UUID(hex=job_hex))
    if job is None or job.status != "done" or job.deleted_at is not None or not job.sha256:
        raise LinkInvalid()
    user = db.get(User, job.user_id)
    if user is None or not user.is_active:
        raise LinkInvalid()
    expected = _signature(job.id, exp, job.sha256, int(user.link_epoch or 0))
    if not hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii")):
        raise LinkInvalid()
    if job.superseded_by_id is not None:
        # En återkallad fil (månaden blev tom) pekar på sig själv.
        raise LinkExpired(REVOKED_TEXT if job.superseded_by_id == job.id else SUPERSEDED_TEXT)
    if int(job.link_downloads or 0) >= LINK_MAX_DOWNLOADS:
        raise LinkInvalid()
    try:
        export_svc.export_path(job)
    except export_svc.ExportError as exc:
        raise LinkInvalid() from exc
    return job


def revoke_all(db: Session, user: User) -> None:
    """Stänger alla nedladdningslänkar för användaren (signaturen bygger på ``link_epoch``)."""
    user.link_epoch = int(user.link_epoch or 0) + 1
    db.flush()
