"""Företagslogga i underlaget (DEL 11, 1.3.0).

Ägaren kan ladda upp en logga (PNG eller JPEG) som läggs uppe till höger i det enkla
underlagets sidhuvud – också i månadens automatiska underlag. Säkerhet:

* bara PNG och JPEG, avgjort av filens innehåll (inte filnamn eller angiven typ); SVG, HTML och
  allt annat avvisas;
* högst ``MAX_BYTES`` (1 MB) och ``MAX_SIDE`` × ``MAX_SIDE`` bildpunkter;
* filen sparas under datakatalogen (``branding/``) med ett eget slumpat namn och rättigheterna
  600, följer med i säkerhetskopian och lämnas bara ut till den inloggade ägaren.

Uppgifterna om loggan (typ, mått, storlek, när) sparas i den skrivskyddade inställningen
``export.logo``; filnamnet på servern visas aldrig i API:t. Ingen bildbehandling görs här:
xlsxwriter lägger in och skalar bilden själv.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.security import crypto
from app.services import settings as settings_svc

MAX_BYTES = 1024 * 1024
MAX_SIDE = 4000
DIR_NAME = "branding"
FILE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}\.(png|jpg)")
_PNG = b"\x89PNG\r\n\x1a\n"
# JPEG-markörer med bildens mått (SOF), utom DHT/JPG/DAC som delar nummerserien.
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


class LogoError(ValueError):
    pass


@dataclass(frozen=True)
class Image:
    content_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class Logo:
    """En sparad logga som ska med i underlaget."""

    path: Path
    width: int
    height: int


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # utfyllnad
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markörer utan längd
            i += 2
            continue
        if marker == 0xD9:
            return None
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return None
        if marker in _SOF:
            if i + 9 > len(data):
                return None
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        i += 2 + length
    return None


def inspect(data: bytes) -> Image:
    """Avgör bildtyp och mått ur innehållet. Kastar ``LogoError`` med en text för ägaren."""
    if not data:
        raise LogoError("Filen är tom.")
    if len(data) > MAX_BYTES:
        raise LogoError("Loggan får vara högst 1 MB.")
    size: tuple[int, int] | None = None
    if data[:8] == _PNG and data[12:16] == b"IHDR" and len(data) >= 24:
        size = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        image = ("image/png", "png")
    elif data[:3] == b"\xff\xd8\xff":
        size = _jpeg_size(data)
        image = ("image/jpeg", "jpg")
    else:
        raise LogoError("Loggan måste vara en PNG- eller JPEG-bild.")
    if size is None or size[0] < 1 or size[1] < 1:
        raise LogoError("Loggan måste vara en PNG- eller JPEG-bild.")
    if size[0] > MAX_SIDE or size[1] > MAX_SIDE:
        raise LogoError(f"Loggan får vara högst {MAX_SIDE} × {MAX_SIDE} bildpunkter.")
    return Image(content_type=image[0], extension=image[1], width=size[0], height=size[1])


def directory() -> Path:
    return get_config().data_dir / DIR_NAME


def _meta(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    value = settings_svc.get_section(db, user_id, "export").get("logo")
    return value if isinstance(value, dict) else {}


def _path_of(meta: dict[str, Any]) -> Path | None:
    name = meta.get("file")
    if not isinstance(name, str) or not FILE_RE.fullmatch(name):
        return None
    path = directory() / name
    return path if path.is_file() else None


def public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Det som visas i API:t (aldrig filnamnet)."""
    if not meta.get("file"):
        return {}
    return {k: meta.get(k) for k in ("content_type", "width", "height", "size_bytes", "uploaded_at")}


def info(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    meta = _meta(db, user_id)
    present = _path_of(meta) is not None
    shown = bool(settings_svc.get_section(db, user_id, "export").get("show_logo", True))
    pub = public_meta(meta) if present else {}
    return {
        "has_logo": present,
        "content_type": pub.get("content_type"),
        "width": pub.get("width"),
        "height": pub.get("height"),
        "size_bytes": pub.get("size_bytes"),
        "uploaded_at": pub.get("uploaded_at"),
        "show_in_export": shown,
    }


def read(db: Session, user_id: uuid.UUID) -> tuple[bytes, str] | None:
    meta = _meta(db, user_id)
    path = _path_of(meta)
    if path is None:
        return None
    return path.read_bytes(), str(meta.get("content_type") or "application/octet-stream")


def save(db: Session, user_id: uuid.UUID, data: bytes) -> dict[str, Any]:
    """Kontrollerar och sparar en ny logga (ersätter en tidigare)."""
    image = inspect(data)
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{crypto.random_token(18)}.{image.extension}"
    path = folder / name
    path.write_bytes(data)
    path.chmod(0o600)
    old = _path_of(_meta(db, user_id))
    meta = {
        "file": name, "content_type": image.content_type, "width": image.width, "height": image.height,
        "size_bytes": len(data), "uploaded_at": utcnow().isoformat(),
    }
    settings_svc.update_section(db, user_id, "export", {"logo": meta}, allow_read_only=True)
    if old is not None and old != path:
        old.unlink(missing_ok=True)
    return info(db, user_id)


def delete(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    old = _path_of(_meta(db, user_id))
    settings_svc.update_section(db, user_id, "export", {"logo": {}}, allow_read_only=True)
    if old is not None:
        old.unlink(missing_ok=True)
    return info(db, user_id)


def for_export(db: Session, user_id: uuid.UUID) -> Logo | None:
    """Loggan som ska med i underlaget, eller None (ingen logga eller "Visa loggan" av)."""
    if not settings_svc.get_section(db, user_id, "export").get("show_logo", True):
        return None
    meta = _meta(db, user_id)
    path = _path_of(meta)
    if path is None:
        return None
    try:
        image = inspect(path.read_bytes())
    except (LogoError, OSError):
        return None
    return Logo(path=path, width=image.width, height=image.height)
