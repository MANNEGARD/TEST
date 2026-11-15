"""Tilläggets nycklar: skapas slumpmässigt första gången, sparas i /data och återanvänds.

- ``secret_key``: DRIVELOG_SECRET_KEY (sessioner och signaturer).
- ``encryption_keys``: DRIVELOG_ENCRYPTION_KEYS, kommaseparerade Fernet-nycklar. Den första
  krypterar; övriga finns kvar för att kunna läsa äldre data efter en nyckelrotation.

Filerna skrivs med rättigheterna 600 i en katalog med 700. De skrivs aldrig över: förloras
krypteringsnyckeln går de krypterade uppgifterna i databasen (t.ex. Volvo- och Outlook-
anslutningarna) inte att läsa. Home Assistants säkerhetskopior av tillägget tar med /data och
därmed nycklarna.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("drivelog.addon")

KATALOG = "hemligheter"
SECRET_KEY_FIL = "secret_key"  # noqa: S105 - filnamn, inte ett lösenord
ENCRYPTION_KEYS_FIL = "encryption_keys"


class TrasigaHemligheter(RuntimeError):
    """En sparad nyckel är oläslig. Den skrivs inte över automatiskt."""


@dataclass(frozen=True)
class Hemligheter:
    secret_key: str = field(repr=False)
    encryption_keys: str = field(repr=False)
    nyskapade: tuple[str, ...] = ()


def ny_fernet_nyckel() -> str:
    """Samma format som ``cryptography.fernet.Fernet.generate_key()``: 32 slumpbyte, base64url."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def ar_fernet_nyckel(varde: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(varde.encode("ascii"))) == 32 and len(varde) == 44
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False


def _skriv_ny(path: Path, innehall: str) -> None:
    """Skriver filen atomärt med rättigheterna 600. Finns den redan skrivs den inte över."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fil:
            fil.write(innehall + "\n")
            fil.flush()
            os.fsync(fil.fileno())
        # link() misslyckas om någon annan hann skapa filen – då vinner den befintliga.
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _strama_at(path: Path, rattighet: int) -> None:
    nuvarande = stat.S_IMODE(path.stat().st_mode)
    if nuvarande != rattighet:
        logger.warning("Rättigheterna för %s var %o – sätts till %o.", path, nuvarande, rattighet)
        path.chmod(rattighet)


def _las_eller_skapa(path: Path, skapa, kontroll, beskrivning: str, nyskapade: list[str]) -> str:
    if not path.exists():
        _skriv_ny(path, skapa())
        nyskapade.append(beskrivning)
    _strama_at(path, 0o600)
    varde = path.read_text(encoding="ascii").strip()
    if not kontroll(varde):
        raise TrasigaHemligheter(
            f"{path} är oläslig eller har fel format. Filen skrivs inte över automatiskt eftersom "
            "data kan gå förlorade – återställ den från en säkerhetskopia av tillägget."
        )
    return varde


def las_eller_skapa(data_katalog: Path) -> Hemligheter:
    """Returnerar nycklarna och skapar dem som saknas."""
    katalog = data_katalog / KATALOG
    katalog.mkdir(mode=0o700, parents=True, exist_ok=True)
    _strama_at(katalog, 0o700)
    nyskapade: list[str] = []
    secret_key = _las_eller_skapa(
        katalog / SECRET_KEY_FIL,
        lambda: secrets.token_urlsafe(48),
        lambda v: len(v) >= 32 and v.isascii() and "\n" not in v,
        "DRIVELOG_SECRET_KEY",
        nyskapade,
    )
    nycklar = _las_eller_skapa(
        katalog / ENCRYPTION_KEYS_FIL,
        ny_fernet_nyckel,
        lambda v: bool(v) and all(ar_fernet_nyckel(k.strip()) for k in v.split(",")),
        "DRIVELOG_ENCRYPTION_KEYS",
        nyskapade,
    )
    return Hemligheter(secret_key=secret_key, encryption_keys=nycklar, nyskapade=tuple(nyskapade))
