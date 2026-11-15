"""Tilläggets nycklar: skapas slumpmässigt första gången, sparas i /data och återanvänds.

- ``secret_key``: DRIVELOG_SECRET_KEY (sessioner och signaturer).
- ``encryption_keys``: DRIVELOG_ENCRYPTION_KEYS, kommaseparerade Fernet-nycklar. Den första
  krypterar; övriga finns kvar för att kunna läsa äldre data efter en nyckelrotation.

Kontrakt (se docs/home-assistant-tillagg.md):

- Nycklarna ligger i ``/data/hemligheter/`` som ägs av root (700); filerna ägs av root (600). /data
  ägs av root (711) och API:t har en egen katalog, /data/app, så API-användaren kan varken flytta,
  ersätta eller länka något här.
- Katalogen och filerna kontrolleras före läsning: symlänkar, fel ägare, andra filtyper och filer
  med flera hårda länkar avvisas (``os.lstat`` och ``O_NOFOLLOW``). För vida rättigheter stramas åt.
- Nycklarna skrivs aldrig över. En saknad nyckel skapas BARA när databasen inte finns (första
  starten): finns databasen men inte nyckeln avbryts starten – med en ny krypteringsnyckel går de
  krypterade uppgifterna (t.ex. Volvo- och Outlook-anslutningarna) inte att läsa, och det får aldrig
  ske tyst. Då återställs tillägget från en säkerhetskopia.
- Home Assistants säkerhetskopior av tillägget tar med hela /data och därmed nycklarna.

Märket för administratörens lösenord (``admin_losenord_marke``, samma katalog, 600) säger vilka
uppgifter i fälten "Administratörens e-post"/"Administratörens lösenord" som senast tillämpades. Det
är en nyckelad hash (HMAC-SHA256 med en nyckel härledd ur ``secret_key``) av adressen och lösenordet
– aldrig lösenordet självt – och går inte att använda för att pröva gissningar utan nyckeln. Till
skillnad från nycklarna skrivs märket över varje gång nya uppgifter har tillämpats.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
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
ADMIN_MARKE_FIL = "admin_losenord_marke"
_MARKE_VERSION = "v1"


class TrasigaHemligheter(RuntimeError):
    """En nyckel är oläslig, saknas trots att databasen finns, eller ligger osäkert. Ingenting
    skrivs över automatiskt."""


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


def _skriv_ny(katalog: Path, namn: str, innehall: str) -> None:
    """Skriver filen atomärt med rättigheterna 600. Finns den redan skrivs den inte över."""
    path = katalog / namn
    tmp = katalog / f".{namn}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fil:
            fil.write(innehall + "\n")
            fil.flush()
            os.fsync(fil.fileno())
        # link() misslyckas om någon annan hann skapa filen – då vinner den befintliga.
        os.link(tmp, path, follow_symlinks=False)
    finally:
        tmp.unlink(missing_ok=True)


def _kontrollera_katalog(katalog: Path) -> None:
    try:
        st = os.lstat(katalog)
    except FileNotFoundError:
        os.mkdir(katalog, 0o700)
        st = os.lstat(katalog)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise TrasigaHemligheter(f"{katalog} är inte en vanlig katalog (symlänk?). Starten avbryts.")
    if st.st_uid != os.geteuid():
        raise TrasigaHemligheter(
            f"{katalog} ägs av uid {st.st_uid}, inte av startprogrammet (uid {os.geteuid()}). Starten avbryts."
        )
    nuvarande = stat.S_IMODE(st.st_mode)
    if nuvarande != 0o700:
        logger.warning("Rättigheterna för %s var %o – sätts till 700.", katalog, nuvarande)
        os.chmod(katalog, 0o700, follow_symlinks=False)


def _las(path: Path) -> str:
    """Läser en nyckelfil utan att följa symlänkar och bara om den ligger säkert."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise TrasigaHemligheter(f"{path} kan inte öppnas säkert ({exc.strerror}; symlänk?). Starten avbryts.") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TrasigaHemligheter(f"{path} är inte en vanlig fil. Starten avbryts.")
        if st.st_uid != os.geteuid():
            raise TrasigaHemligheter(
                f"{path} ägs av uid {st.st_uid}, inte av startprogrammet (uid {os.geteuid()}). Starten avbryts."
            )
        if st.st_nlink != 1:
            raise TrasigaHemligheter(f"{path} har {st.st_nlink} hårda länkar. Starten avbryts.")
        nuvarande = stat.S_IMODE(st.st_mode)
        if nuvarande != 0o600:
            logger.warning("Rättigheterna för %s var %o – sätts till 600.", path, nuvarande)
            os.fchmod(fd, 0o600)
        data = os.read(fd, 65536)
    finally:
        os.close(fd)
    try:
        return data.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""


def _finns(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _las_eller_skapa(katalog: Path, namn: str, skapa, kontroll, beskrivning: str, nyskapade: list[str],
                     databas: Path | None) -> str:
    path = katalog / namn
    if not _finns(path):
        if databas is not None and _finns(databas):
            raise TrasigaHemligheter(
                f"Nyckeln {path} saknas men databasen finns ({databas}) – återställ tillägget från en "
                "säkerhetskopia. Ingen ny nyckel skapas: då skulle de krypterade uppgifterna i databasen "
                "inte gå att läsa."
            )
        _skriv_ny(katalog, namn, skapa())
        nyskapade.append(beskrivning)
    varde = _las(path)
    if not kontroll(varde):
        raise TrasigaHemligheter(
            f"{path} är oläslig eller har fel format. Filen skrivs inte över automatiskt eftersom "
            "data kan gå förlorade – återställ den från en säkerhetskopia av tillägget."
        )
    return varde


def las_eller_skapa(data_katalog: Path, *, databas: Path | None = None) -> Hemligheter:
    """Returnerar nycklarna. Saknade nycklar skapas bara om ``databas`` inte finns."""
    katalog = data_katalog / KATALOG
    _kontrollera_katalog(katalog)
    nyskapade: list[str] = []
    secret_key = _las_eller_skapa(
        katalog,
        SECRET_KEY_FIL,
        lambda: secrets.token_urlsafe(48),
        lambda v: len(v) >= 32 and v.isascii() and "\n" not in v,
        "DRIVELOG_SECRET_KEY",
        nyskapade,
        databas,
    )
    nycklar = _las_eller_skapa(
        katalog,
        ENCRYPTION_KEYS_FIL,
        ny_fernet_nyckel,
        lambda v: bool(v) and all(ar_fernet_nyckel(k.strip()) for k in v.split(",")),
        "DRIVELOG_ENCRYPTION_KEYS",
        nyskapade,
        databas,
    )
    return Hemligheter(secret_key=secret_key, encryption_keys=nycklar, nyskapade=tuple(nyskapade))


# Märket för administratörens lösenord --------------------------------------------------------------

SAKNAS = "saknas"
TRASIGT = "trasigt"


def admin_marke(secret_key: str, epost: str, losenord: str) -> str:
    """Nyckelad hash av (adress, lösenord). Nyckeln härleds ur ``secret_key`` för just detta syfte."""
    nyckel = hmac.new(secret_key.encode("utf-8"), b"drivelog-addon/admin-losenord/v1", hashlib.sha256).digest()
    meddelande = epost.strip().lower().encode("utf-8") + b"\0" + losenord.encode("utf-8", "surrogatepass")
    return f"{_MARKE_VERSION}:{hmac.new(nyckel, meddelande, hashlib.sha256).hexdigest()}"


def _ar_marke(varde: str) -> bool:
    prefix, _, hexdel = varde.partition(":")
    return prefix == _MARKE_VERSION and len(hexdel) == 64 and all(c in "0123456789abcdef" for c in hexdel)


def las_admin_marke(data_katalog: Path) -> str:
    """Det sparade märket, ``SAKNAS`` eller ``TRASIGT`` (oläsligt, fel format eller osäkert lagrat)."""
    path = data_katalog / KATALOG / ADMIN_MARKE_FIL
    if not _finns(path):
        return SAKNAS
    try:
        varde = _las(path)
    except TrasigaHemligheter as exc:
        logger.warning("%s", exc)
        return TRASIGT
    return varde if _ar_marke(varde) else TRASIGT


def stammer_admin_marke(sparat: str, secret_key: str, epost: str, losenord: str) -> bool:
    return _ar_marke(sparat) and hmac.compare_digest(sparat, admin_marke(secret_key, epost, losenord))


def spara_admin_marke(data_katalog: Path, marke: str) -> None:
    """Skriver märket atomärt (600) och ersätter ett tidigare – även en symlänk byts ut, följs aldrig."""
    if not _ar_marke(marke):
        raise ValueError("ogiltigt märke")
    katalog = data_katalog / KATALOG
    _kontrollera_katalog(katalog)
    tmp = katalog / f".{ADMIN_MARKE_FIL}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fil:
            fil.write(marke + "\n")
            fil.flush()
            os.fsync(fil.fileno())
        os.replace(tmp, katalog / ADMIN_MARKE_FIL)
    finally:
        tmp.unlink(missing_ok=True)
