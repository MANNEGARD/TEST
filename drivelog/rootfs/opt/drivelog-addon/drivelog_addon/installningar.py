"""Läser och kontrollerar tilläggets inställningar (/data/options.json).

Supervisor skriver filen före varje start utifrån schemat i config.yaml. Här görs de
kontroller som schemat inte klarar: att domänen är ett riktigt värdnamn, att STRATO-uppgifterna
finns när DynDNS är påslaget, att de extra domänerna går att skriva in i Caddyfilen utan risk och
liknande. Felmeddelandena är skrivna för ägaren och innehåller aldrig lösenord.
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
MINSTA_LOSENORD = 12
_EPOST = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Värden bakom en extra domän: 127.0.0.1 eller ett container-/DNS-namn på Dockers nät (t.ex.
# "transcribe"). Strikt eftersom namnet skrivs in i Caddyfilen: inget schema, ingen sökväg, inga
# mellanslag, klamrar eller radbrytningar.
_EXTRA_VARD = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_EXTRA_PORT = re.compile(r"^[0-9]{1,5}$")


class OgiltigaInstallningar(ValueError):
    """Inställningarna går inte att använda. Meddelandet visas i tilläggets logg."""


@dataclass(frozen=True)
class ExtraDoman:
    """En extra domän som Caddy hämtar certifikat för och skickar vidare till en annan tjänst."""

    doman: str
    vard: str
    port: int

    @property
    def uppstrom(self) -> str:
        return f"{self.vard}:{self.port}"


@dataclass(frozen=True)
class Installningar:
    domain: str
    admin_email: str | None = None
    admin_password: str | None = field(default=None, repr=False)
    strato_dyndns: bool = False
    strato_dyndns_user: str | None = None
    strato_dyndns_password: str | None = field(default=None, repr=False)
    log_level: str = "info"
    extra_domains: tuple[ExtraDoman, ...] = ()
    varningar: tuple[str, ...] = ()
    # Varför administratörsuppgifterna inte kan användas (ogiltig adress, för kort lösenord).
    # Stoppar inte starten: finns redan ett konto ska DriveLog starta som vanligt.
    adminproblem: str | None = None

    @property
    def har_adminuppgifter(self) -> bool:
        """Båda fälten är ifyllda och går att använda."""
        return bool(self.admin_email and self.admin_password) and self.adminproblem is None

    @property
    def ar_lokal(self) -> bool:
        """``localhost`` används för provkörning (lokalt certifikat, ingen DynDNS)."""
        return self.domain == "localhost"

    @property
    def python_loggniva(self) -> int:
        return LOGGNIVAER[self.log_level]


def normalisera_doman(varde: str, nyckel: str = "domain") -> str:
    """Gör om t.ex. ``https://DriveLog.smgd.se/`` till ``drivelog.smgd.se`` och kontrollerar den.

    ``nyckel`` är inställningen som nämns i felmeddelandena."""
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
            f"'{nyckel}' ska bara vara ett värdnamn som drivelog.smgd.se – utan sökväg eller port (fick '{doman}')."
        )
    try:
        ipaddress.ip_address(doman)
    except ValueError:
        pass
    else:
        raise OgiltigaInstallningar(
            f"'{nyckel}' måste vara ett domännamn, inte en IP-adress – certifikatet utfärdas för ett namn."
        )
    if doman == "localhost":
        return doman
    if len(doman) > 253 or "." not in doman:
        raise OgiltigaInstallningar(
            f"'{doman}' är inte ett fullständigt domännamn. Ange t.ex. drivelog.smgd.se."
        )
    etiketter = doman.split(".")
    if not all(_ETIKETT.fullmatch(e) for e in etiketter) or etiketter[-1].isdigit():
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


def _extra_domaner(data: dict, huvuddoman: str) -> tuple[ExtraDoman, ...]:
    """Kontrollerar 'extra_domains': en lista med ``"<domän>=<värd>:<port>"``."""
    lista = data.get("extra_domains")
    if lista is None:
        return ()
    if not isinstance(lista, list) or not all(isinstance(rad, str) for rad in lista):
        raise OgiltigaInstallningar(
            "Inställningen 'extra_domains' ska vara en lista med rader som transcribe.smgd.se=transcribe:8080."
        )
    resultat: list[ExtraDoman] = []
    sedda = {huvuddoman}
    for rad in lista:
        rad = rad.strip()
        if not rad:
            continue  # en tom rad betyder "inte ifyllt", som de andra fälten
        doman, likhet, uppstrom = rad.partition("=")
        vard, kolon, port = uppstrom.strip().lower().rpartition(":")
        if not likhet or not kolon or not doman.strip():
            raise OgiltigaInstallningar(
                f"'extra_domains': '{rad}' ska skrivas <domän>=<värd>:<port>, t.ex. transcribe.smgd.se=transcribe:8080."
            )
        try:
            doman = normalisera_doman(doman, "extra_domains")
        except OgiltigaInstallningar as exc:
            if "'extra_domains'" in str(exc):
                raise
            raise OgiltigaInstallningar(f"'extra_domains': {exc}") from None
        if doman == "localhost":
            raise OgiltigaInstallningar(
                "'extra_domains': 'localhost' kan inte vara en extra domän – ange ett riktigt domännamn."
            )
        if doman == huvuddoman:
            raise OgiltigaInstallningar(
                f"'extra_domains': {doman} är redan DriveLogs egen domän ('domain') – en extra domän måste vara en annan."
            )
        if doman in sedda:
            raise OgiltigaInstallningar(f"'extra_domains': {doman} står med mer än en gång.")
        sedda.add(doman)
        if vard == "localhost":
            raise OgiltigaInstallningar(
                f"'extra_domains': skriv 127.0.0.1 i stället för localhost ('{rad}')."
            )
        if not _EXTRA_VARD.fullmatch(vard):
            raise OgiltigaInstallningar(
                f"'extra_domains': värden i '{rad}' ska vara 127.0.0.1 eller namnet på en container, t.ex. "
                "transcribe (bara a–z, 0–9, punkt, bindestreck och understreck – utan http:// och utan sökväg)."
            )
        if not _EXTRA_PORT.fullmatch(port) or not 1 <= int(port) <= 65535:
            raise OgiltigaInstallningar(
                f"'extra_domains': porten i '{rad}' ska vara ett tal mellan 1 och 65535."
            )
        resultat.append(ExtraDoman(doman=doman, vard=vard, port=int(port)))
    return tuple(resultat)


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
    adminproblem = None
    if bool(epost) != bool(losenord):
        varningar.append(
            "Både 'Administratörens e-post' (admin_email) och 'Administratörens lösenord' (admin_password) "
            "behövs för att skapa kontot eller återställa lösenordet – bara den ena är ifylld."
        )
    elif epost and losenord:
        if not _EPOST.match(epost):
            adminproblem = f"'Administratörens e-post' (admin_email) ser inte ut som en e-postadress ('{epost}')."
        elif len(losenord) < MINSTA_LOSENORD:
            adminproblem = (
                f"'Administratörens lösenord' (admin_password) måste vara minst {MINSTA_LOSENORD} tecken långt "
                "(kontot nås från internet)."
            )
        elif "\x00" in losenord:
            adminproblem = "'Administratörens lösenord' (admin_password) innehåller ett otillåtet tecken."

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

    extra = _extra_domaner(data, doman)

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
        extra_domains=extra,
        varningar=tuple(varningar),
        adminproblem=adminproblem,
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
