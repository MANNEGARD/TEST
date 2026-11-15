"""Läser och kontrollerar tilläggets inställningar (/data/options.json).

Supervisor skriver filen före varje start utifrån schemat i config.yaml. Här görs de
kontroller som schemat inte klarar: att domänen är ett riktigt värdnamn, att STRATO-uppgifterna
finns när DynDNS är påslaget och liknande. Felmeddelandena är skrivna för ägaren och innehåller
aldrig lösenord.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

LOGGNIVAER = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_ETIKETT = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_EPOST = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OgiltigaInstallningar(ValueError):
    """Inställningarna går inte att använda. Meddelandet visas i tilläggets logg."""


@dataclass(frozen=True)
class Installningar:
    domain: str
    admin_email: str | None = None
    admin_password: str | None = field(default=None, repr=False)
    strato_dyndns: bool = False
    strato_dyndns_user: str | None = None
    strato_dyndns_password: str | None = field(default=None, repr=False)
    log_level: str = "info"
    varningar: tuple[str, ...] = ()

    @property
    def har_adminuppgifter(self) -> bool:
        return bool(self.admin_email and self.admin_password)

    @property
    def ar_lokal(self) -> bool:
        """``localhost`` används för provkörning (lokalt certifikat, ingen DynDNS)."""
        return self.domain == "localhost"

    @property
    def python_loggniva(self) -> int:
        return LOGGNIVAER[self.log_level]


def normalisera_doman(varde: str) -> str:
    """Gör om t.ex. ``https://DriveLog.smgd.se/`` till ``drivelog.smgd.se`` och kontrollerar den."""
    doman = (varde or "").strip().lower()
    for prefix in ("https://", "http://"):
        if doman.startswith(prefix):
            doman = doman[len(prefix) :]
    doman = doman.rstrip("/").rstrip(".")
    if not doman:
        raise OgiltigaInstallningar(
            "Fyll i inställningen 'domain' med den adress DriveLog ska nås på, t.ex. drivelog.smgd.se."
        )
    if "/" in doman or ":" in doman or "@" in doman:
        raise OgiltigaInstallningar(
            f"'domain' ska bara vara ett värdnamn som drivelog.smgd.se – utan sökväg eller port (fick '{doman}')."
        )
    try:
        ipaddress.ip_address(doman)
    except ValueError:
        pass
    else:
        raise OgiltigaInstallningar(
            "'domain' måste vara ett domännamn, inte en IP-adress – certifikatet utfärdas för ett namn."
        )
    if doman == "localhost":
        return doman
    if len(doman) > 253 or "." not in doman:
        raise OgiltigaInstallningar(
            f"'{doman}' är inte ett fullständigt domännamn. Ange t.ex. drivelog.smgd.se."
        )
    etiketter = doman.split(".")
    if not all(_ETIKETT.match(e) for e in etiketter) or etiketter[-1].isdigit():
        raise OgiltigaInstallningar(
            f"'{doman}' är inte ett giltigt domännamn (bara a–z, 0–9 och bindestreck, punkt mellan delarna)."
        )
    return doman


def _text(data: dict, nyckel: str) -> str | None:
    varde = data.get(nyckel)
    if varde is None:
        return None
    if not isinstance(varde, str):
        raise OgiltigaInstallningar(f"Inställningen '{nyckel}' ska vara text.")
    varde = varde.strip()
    return varde or None


def tolka(data: dict) -> Installningar:
    """Kontrollerar en redan inläst options.json."""
    if not isinstance(data, dict):
        raise OgiltigaInstallningar("options.json har fel format.")
    varningar: list[str] = []
    doman = normalisera_doman(_text(data, "domain") or "")

    epost = _text(data, "admin_email")
    losenord = data.get("admin_password") or None
    if losenord is not None and not isinstance(losenord, str):
        raise OgiltigaInstallningar("Inställningen 'admin_password' ska vara text.")
    if epost and not _EPOST.match(epost):
        raise OgiltigaInstallningar(f"'admin_email' ser inte ut som en e-postadress ('{epost}').")
    if bool(epost) != bool(losenord):
        varningar.append(
            "Både 'admin_email' och 'admin_password' behövs för att skapa det första kontot – "
            "bara den ena är ifylld."
        )
    elif losenord and len(losenord) < 12:
        raise OgiltigaInstallningar(
            "'admin_password' måste vara minst 12 tecken långt (kontot nås från internet)."
        )

    dyndns = data.get("strato_dyndns", False)
    if not isinstance(dyndns, bool):
        raise OgiltigaInstallningar("Inställningen 'strato_dyndns' ska vara på eller av.")
    anvandare = _text(data, "strato_dyndns_user")
    strato_losenord = data.get("strato_dyndns_password") or None
    if strato_losenord is not None and not isinstance(strato_losenord, str):
        raise OgiltigaInstallningar("Inställningen 'strato_dyndns_password' ska vara text.")
    if dyndns:
        if doman == "localhost":
            raise OgiltigaInstallningar("STRATO DynDNS kan inte användas med domänen 'localhost'.")
        if not anvandare or not strato_losenord:
            raise OgiltigaInstallningar(
                "STRATO DynDNS är påslaget men 'strato_dyndns_user' eller 'strato_dyndns_password' saknas. "
                "Användarnamnet är domänen i ditt STRATO-paket (t.ex. smgd.se) och lösenordet är "
                "DynDNS-lösenordet du har satt hos STRATO."
            )
        anvandare = anvandare.lower()
        if doman != anvandare and not doman.endswith("." + anvandare):
            varningar.append(
                f"'strato_dyndns_user' ({anvandare}) är inte samma domän som eller en överordnad domän till "
                f"{doman}. STRATO vill normalt ha domänen i paketet som användarnamn."
            )

    niva = (_text(data, "log_level") or "info").lower()
    if niva not in LOGGNIVAER:
        raise OgiltigaInstallningar(f"Okänd loggnivå '{niva}' (debug, info, warning eller error).")

    return Installningar(
        domain=doman,
        admin_email=epost.lower() if epost else None,
        admin_password=losenord,
        strato_dyndns=dyndns,
        strato_dyndns_user=anvandare,
        strato_dyndns_password=strato_losenord,
        log_level=niva,
        varningar=tuple(varningar),
    )


def las(path: Path) -> Installningar:
    """Läser och kontrollerar options.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OgiltigaInstallningar(f"{path} saknas – startas tillägget utanför Home Assistant?") from exc
    except json.JSONDecodeError as exc:
        raise OgiltigaInstallningar(f"{path} är inte giltig JSON.") from exc
    return tolka(data)
