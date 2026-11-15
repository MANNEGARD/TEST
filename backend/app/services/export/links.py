"""Signerade nedladdningslänkar för underlaget (byggkontraktet C.6 och E.2).

STUBBE (G0): ``revoke_all`` är färdig (räknar upp ``link_epoch``). Länkar skapas och
godtas inte förrän S1e fyller modulen: ``verify`` avvisar allt som ogiltigt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import ExportJob, User

LINK_VALID = timedelta(days=7)
LINK_MAX_DOWNLOADS = 10

INVALID_TEXT = "Länken fungerar inte. Öppna den senaste notisen eller logga in i DriveLog."
EXPIRED_TEXT = "Länken har gått ut. Öppna den senaste notisen eller logga in i DriveLog."
SUPERSEDED_TEXT = "En nyare fil finns – öppna senaste notisen"


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


def make_token(job: ExportJob, user: User, exp: datetime) -> str | None:
    """``{id.hex}.{exp}.{sig}``; None när filen saknar sha256. (Stubbe: ingen länk.)"""
    return None


def make_url(job: ExportJob, user: User, exp: datetime) -> str | None:
    """Hela länken till ``/api/v1/public/exports/{token}``. (Stubbe: ingen länk.)"""
    return None


def verify(db: Session, token: str, now: datetime) -> ExportJob:
    """Prövar en länk; kastar LinkInvalid eller LinkExpired. (Stubbe: avvisar allt.)"""
    raise LinkInvalid()


def revoke_all(db: Session, user: User) -> None:
    """Stänger alla nedladdningslänkar för användaren (signaturen bygger på ``link_epoch``)."""
    user.link_epoch = int(user.link_epoch or 0) + 1
    db.flush()
