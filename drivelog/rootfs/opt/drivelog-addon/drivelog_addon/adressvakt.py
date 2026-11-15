"""Adressvakten: säger till när domänen inte längre leder hem – när STRATO DynDNS är avslaget.

Utan DynDNS sätts A-posten för hand hos STRATO. Byter hemmet publik adress slutar sidan fungera,
och ingenting märks förrän någon försöker öppna den. Vakten kontrollerar därför regelbundet att
domänen leder hit – med samma kontroll som DynDNS-delen (``Uppdaterare.leder_hit``, kontroll.py):
domänens A-post slås upp, tillägget ansluter till adressen och hämtar
``/.well-known/drivelog-dyndns?n=<slumpvärde>``. Bara den egna proxyn kan svara rätt, och bara
globala adresser räknas. Ingen annan tjänst tillfrågas (ingen "vad är min IP", ingen extern DNS).

Per varv (var 15:e minut, första gången strax efter att Caddy har startat):

- **leder hit** – någon global A-adress svarar rätt. Loggas på INFO första gången efter start och
  efter ett avbrott. Att kontrollen har lyckats någon gång sparas.
- **leder inte hit** – A-posten finns men ingen DriveLog svarar där: A-posten pekar fel, port
  80/443 skickas inte vidare, eller routern klarar inte hårnåls-NAT.
- **ingen A-post** eller **bara lokala adresser**.

Loggen får en rad med åtgärd vid första misslyckade kontrollen och sedan högst en gång per dygn
medan läget består. En avisering i Home Assistant (avisering.py) skickas bara om kontrollen har
lyckats någon gång tidigare – då fungerar hårnåls-NAT och felet är verkligt – och nu har
misslyckats ``LARM_EFTER`` varv i rad. Den tas bort när kontrollen lyckas igen. Bara lokala
adresser (t.ex. när routerns DNS medvetet svarar med maskinens lokala adress) går inte att
kontrollera och ger ingen avisering.

AAAA-kontrollen (``Uppdaterare.kontrollera_aaaa``) körs i varje varv; samma varning upprepas inte.

Tillståndet som överlever omstart ligger i ``/data/adressvakt.json`` (root, 600): domänen, om
kontrollen har lyckats någon gång och om en avisering är utskickad. Filen skrivs atomärt, följer
aldrig symlänkar, och en trasig, osäker eller saknad fil betyder "har aldrig lyckats, ingen
avisering". Byts domänen börjar tillståndet om (en utskickad avisering tas ändå bort vid nästa
lyckade kontroll).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import socket
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import dyndns

logger = logging.getLogger("drivelog.adressvakt")

INTERVALL = 15 * 60
LARM_EFTER = 3  # varv i rad (≈ 45 minuter) – en omstart av routern ska inte larma
PAMINNELSE = 24 * 60 * 60
TILLSTANDSFIL = "adressvakt.json"

ATGARD = (
    "Ändra A-posten hos STRATO till ditt hems publika IPv4-adress – den står som WAN- eller "
    "internetadress på routerns statussida – och kontrollera att port 80 och 443 skickas vidare till "
    "Home Assistant-maskinen."
)

LEDER_HIT = "leder hit"
FEL_ADRESS = "fel adress"
INGEN_A_POST = "ingen A-post"
BARA_LOKAL = "bara lokal"


# --- Tillståndet i /data ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Tillstand:
    doman: str
    lyckats: bool = False
    aviserad: bool = False


def las_tillstand(data_katalog: Path, doman: str) -> Tillstand:
    """Läser tillståndet utan att följa symlänkar. Allt oväntat = "har aldrig lyckats"."""
    path = data_katalog / TILLSTANDSFIL
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return Tillstand(doman)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            return Tillstand(doman)
        data = json.loads(os.read(fd, 4096).decode("utf-8"))
    except (OSError, ValueError):
        return Tillstand(doman)
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return Tillstand(doman)
    aviserad = data.get("aviserad") is True
    if data.get("doman") != doman:
        # Ny domän: börja om. En avisering för den gamla tas bort vid nästa lyckade kontroll.
        return Tillstand(doman, lyckats=False, aviserad=aviserad)
    return Tillstand(doman, lyckats=data.get("lyckats") is True, aviserad=aviserad)


def spara_tillstand(data_katalog: Path, tillstand: Tillstand) -> None:
    """Skriver atomärt (600). En symlänk på filens plats ersätts, följs aldrig."""
    tmp = data_katalog / f".{TILLSTANDSFIL}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fil:
            json.dump(asdict(tillstand), fil)
            fil.flush()
            os.fsync(fil.fileno())
        os.replace(tmp, data_katalog / TILLSTANDSFIL)
    finally:
        tmp.unlink(missing_ok=True)


# --- Vakten ------------------------------------------------------------------------------------------


def _tolkade(adresser: list[str]) -> list[str]:
    """Bara adresser som ipaddress kan tolka hamnar i logg och avisering."""
    ut: list[str] = []
    for adress in adresser:
        try:
            ut.append(str(ipaddress.ip_address(adress.split("%", 1)[0])))
        except ValueError:
            continue
    return ut


class Adressvakt:
    """Ett varv i taget (``kor_ett_varv``) så att logiken går att pröva utan att vänta."""

    def __init__(
        self,
        doman: str,
        kontrollnyckel: str,
        *,
        data_katalog: Path,
        avisera: Callable[[str, str | None, str | None], bool] | None = None,
        version: str = "okänd",
        fordrojning: float = 30.0,
        kontroll_schema: str = "https",
        kontroll_port: int = 443,
        hamta: Callable[..., tuple[int, str]] = dyndns.http_get,
        sla_upp: Callable[[str, int], list[str]] = dyndns.sla_upp,
        klocka: Callable[[], float] = time.monotonic,
        godta_adress: Callable[[str], bool] = dyndns.ar_global,
    ) -> None:
        self.doman = doman
        self.data_katalog = data_katalog
        # avisera(atgard, titel, text): atgard "skapa" eller "ta_bort"; True om det gick.
        self._avisera = avisera
        self.fordrojning = fordrojning
        self._sla_upp = sla_upp
        self._klocka = klocka
        self._godta_adress = godta_adress
        # Samma kontroll (och AAAA-varning) som DynDNS-delen – men utan STRATO-uppgifter.
        self._kontroll = dyndns.Uppdaterare(
            doman, "", "", kontrollnyckel, version=version, kontroll_schema=kontroll_schema,
            kontroll_port=kontroll_port, hamta=hamta, sla_upp=sla_upp, godta_adress=godta_adress,
        )
        self.tillstand = las_tillstand(data_katalog, doman)
        self.misslyckade = 0
        self._lage: str | None = None  # senaste utfallet
        self._loggat: tuple[str, tuple[str, ...]] | None = None  # (utfall, adresser) senast loggat
        self._loggat_nar: float | None = None
        self._skickad_denna_start = False
        self._sparfel_loggat = False

    # Hjälpare -----------------------------------------------------------------------------------

    def _spara(self, **andringar) -> None:
        nytt = Tillstand(**{**asdict(self.tillstand), **andringar})
        if nytt == self.tillstand:
            return
        self.tillstand = nytt
        try:
            spara_tillstand(self.data_katalog, nytt)
        except OSError as exc:
            if not self._sparfel_loggat:
                logger.warning("Adressvaktens tillstånd kunde inte sparas i %s (%s).", self.data_katalog, exc.strerror)
                self._sparfel_loggat = True

    def _undersok(self) -> tuple[str, list[str]]:
        a_poster = self._sla_upp(self.doman, socket.AF_INET)
        globala = [a for a in a_poster if self._godta_adress(a)]
        if not globala:
            return (BARA_LOKAL, _tolkade(a_poster)) if a_poster else (INGEN_A_POST, [])
        for adress in globala:
            if self._kontroll.leder_hit(adress):
                return LEDER_HIT, _tolkade([adress])
        return FEL_ADRESS, _tolkade(globala)

    def _loggrad(self, utfall: str, adresser: list[str]) -> tuple[int, str]:
        lista = ", ".join(adresser)
        if utfall == BARA_LOKAL:
            return logging.INFO, (
                f"{self.doman} ger bara lokala adresser här ({lista}), så adressvakten kan inte kontrollera "
                "den publika A-posten och varnar inte om den. Det är väntat om routerns DNS medvetet svarar "
                "med Home Assistant-maskinens lokala adress."
            )
        if utfall == INGEN_A_POST:
            rad = f"{self.doman} har ingen A-post (IPv4-adress) – DriveLog kan inte nås från internet. {ATGARD}"
        else:
            rad = f"{self.doman} leder inte hit: A-posten pekar på {lista}, men DriveLog svarar inte där. {ATGARD}"
            if not self.tillstand.lyckats:
                rad += (
                    " Kontrollen har aldrig lyckats här: en router utan hårnåls-NAT (som inte når sin egen "
                    f"publika adress inifrån) ger samma utfall fast sidan fungerar utifrån – pröva "
                    f"https://{self.doman} från mobilnätet med wifi avslaget."
                )
        return logging.WARNING, rad + " Påminnelse i loggen högst en gång per dygn medan det består."

    def _skicka_avisering(self, utfall: str, adresser: list[str]) -> None:
        if self._avisera is None:
            return
        titel = f"DriveLog: {self.doman} leder inte längre hem"
        if utfall == INGEN_A_POST:
            orsak = f"{self.doman} har inte längre någon A-post."
        else:
            orsak = f"A-posten för {self.doman} pekar på {', '.join(adresser)}, men DriveLog svarar inte där."
        text = (
            f"DriveLog har inte gått att nå via {self.doman} de senaste {LARM_EFTER * INTERVALL // 60} minuterna. "
            f"{orsak} {ATGARD} Aviseringen försvinner av sig själv när domänen leder hem igen."
        )
        if self._avisera("skapa", titel, text):
            self._skickad_denna_start = True
            self._spara(aviserad=True)

    # Ett varv -----------------------------------------------------------------------------------

    def kor_ett_varv(self) -> float:
        """Kontrollerar en gång. Returnerar sekunder till nästa varv."""
        nu = self._klocka()
        self._kontroll.kontrollera_aaaa()
        utfall, adresser = self._undersok()
        foregaende, self._lage = self._lage, utfall

        if utfall == LEDER_HIT:
            if foregaende != LEDER_HIT:
                igen = foregaende is not None or self.tillstand.aviserad
                logger.info("%s leder hit%s (A-posten %s).", self.doman, " igen" if igen else "", adresser[0])
            self.misslyckade = 0
            self._loggat = self._loggat_nar = None
            self._skickad_denna_start = False
            if self.tillstand.aviserad and self._avisera is not None and self._avisera("ta_bort", None, None):
                self._spara(lyckats=True, aviserad=False)
            else:
                self._spara(lyckats=True)
            return float(INTERVALL)

        nyckel = (utfall, tuple(adresser))
        if nyckel != self._loggat or self._loggat_nar is None or nu - self._loggat_nar >= PAMINNELSE:
            niva, rad = self._loggrad(utfall, adresser)
            logger.log(niva, rad)
            self._loggat, self._loggat_nar = nyckel, nu

        if utfall == BARA_LOKAL:
            self.misslyckade = 0  # går inte att kontrollera – räknas varken som fel eller som lyckat
            return float(INTERVALL)
        self.misslyckade += 1
        if self.tillstand.lyckats and self.misslyckade >= LARM_EFTER and not self._skickad_denna_start:
            self._skicka_avisering(utfall, adresser)
        return float(INTERVALL)

    def kor(self, stopp: threading.Event) -> None:
        """Kör tills ``stopp`` sätts. Fel stoppar aldrig resten av tillägget."""
        if stopp.wait(self.fordrojning):
            return
        while not stopp.is_set():
            try:
                vanta = self.kor_ett_varv()
            except Exception:  # noqa: BLE001 - vakten får aldrig fälla DriveLog
                logger.exception("Oväntat fel i adressvakten")
                vanta = float(INTERVALL)
            stopp.wait(vanta)
