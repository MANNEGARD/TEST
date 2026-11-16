"""Ny version: en notis när DriveLog har startat med en annan version än senast (DEL 9).

Versionen som DriveLog senast startade med sparas per användare i den dolda inställningen
``system.last_version``. Vid varje start (``main.lifespan``, alla driftlägen) jämförs den med
``app.__version__``:

* samma version → inget (vanlig omstart);
* tomt värde → ny användare eller ny installation: värdet sätts, ingen notis;
* annan version → en notis "DriveLog är uppdaterad till <ny>" (``notices.queue_release``) och
  värdet sätts, i samma transaktion.

Migreringen 0004 sätter värdet till den förra versionen för befintliga användare, så att första
starten efter uppgraderingen ger notisen men en helt ny installation inte gör det. Notisens
dedupe-nyckel ``release:<version>`` och ett radlås på inställningen gör att samma version aldrig
ger mer än en notis, även när två processer startar samtidigt. Ett fel här stoppar aldrig starten.

Nyheterna är de fetstilta inledningarna i versionens punkter i ändringsloggen (CHANGELOG.md,
som kopieras in i tilläggets avbild). Saknas filen eller avsnittet visas versionen utan nyheter.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db import session_scope, utcnow
from app.models import Setting, User
from app.services import settings as settings_svc

logger = logging.getLogger(__name__)

SECTION, KEY = "system", "last_version"
NOTICE_ITEMS = 3

# Avbilden (drivelog/Dockerfile) först, sedan repots egen fil vid utveckling.
CHANGELOG_PATHS: tuple[Path, ...] = (
    Path("/app/CHANGELOG.md"),
    Path(__file__).resolve().parents[3] / "drivelog" / "CHANGELOG.md",
)

_ITEM_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_LEAD_RE = re.compile(r"^\*\*(.+?)\*\*")


def _changelog_text() -> str | None:
    for path in CHANGELOG_PATHS:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def changelog_items(text: str, version: str) -> list[str]:
    """De fetstilta inledningarna i versionens punkter, i ordning (utan avslutande punkt)."""
    items: list[str] = []
    current: list[str] | None = None
    inside = False

    def flush() -> None:
        if current:
            lead = _LEAD_RE.match(" ".join(part.strip() for part in current))
            if lead:
                text_ = " ".join(lead.group(1).split()).rstrip(".:").strip()
                if text_:
                    items.append(text_)

    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == version
            continue
        if not inside:
            continue
        match = _ITEM_RE.match(line)
        if match and not line.startswith((" ", "\t")):
            flush()
            current = [match.group(1)]
        elif current is not None and line.strip():
            current.append(line)
        elif not line.strip():
            flush()
            current = None
    if inside:
        flush()
    return items


def changelog_section(version: str, limit: int | None = NOTICE_ITEMS) -> list[str]:
    """Nyheterna för ``version`` ur ändringsloggen (högst ``limit``), eller tom lista."""
    text = _changelog_text()
    if not text:
        return []
    items = changelog_items(text, version)
    return items if limit is None else items[:limit]


def current_version() -> str:
    return __version__


def whats_new(version: str | None = None, limit: int | None = None) -> dict[str, object] | None:
    """``{"version", "items"}`` för "Om DriveLog", eller None när avsnittet saknas."""
    version = version or current_version()
    items = changelog_section(version, limit=limit)
    return {"version": version, "items": items} if items else None


def _locked_row(db: Session, user_id: uuid.UUID) -> Setting | None:
    """Inställningsraden, låst för resten av transaktionen (PostgreSQL; SQLite skriver en åt gången)."""
    return db.execute(
        select(Setting).where(Setting.user_id == user_id, Setting.key == SECTION).with_for_update()
    ).scalar_one_or_none()


def check_user(db: Session, user_id: uuid.UUID, *, version: str | None = None, now: datetime | None = None) -> bool:
    """Jämför och sparar versionen för en användare. Sant när en notis köades."""
    from app.services.ha import notices

    version = version or current_version()
    row = _locked_row(db, user_id)
    last = ""
    if row is not None and isinstance(row.value, dict):
        last = str(row.value.get(KEY) or "")
    if last == version:
        return False
    queued = False
    if last:
        req = notices.queue_release(db, user_id, last, version, changelog_section(version), now or utcnow())
        queued = req is not None
    settings_svc.update_section(db, user_id, SECTION, {KEY: version}, allow_read_only=True)
    return queued


def on_startup(now: datetime | None = None) -> int:
    """Körs vid varje start. Returnerar antalet köade notiser. Kastar aldrig."""
    count = 0
    try:
        with session_scope() as db:
            user_ids = list(db.execute(select(User.id)).scalars())
        for user_id in user_ids:
            try:
                with session_scope() as db:
                    if check_user(db, user_id, now=now):
                        count += 1
            except Exception as exc:  # en användares fel får inte stoppa de andra eller starten
                logger.warning("Kunde inte jämföra versionen för en användare vid starten (%s).", type(exc).__name__)
    except Exception as exc:
        logger.warning("Kunde inte jämföra DriveLogs version vid starten (%s).", type(exc).__name__)
    if count:
        logger.info("DriveLog %s: %d notis(er) om den nya versionen köades.", current_version(), count)
    return count
