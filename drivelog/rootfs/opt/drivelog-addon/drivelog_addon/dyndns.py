"""Håller domänens A-post hos STRATO uppdaterad (DynDNS).

Belagt i STRATOS hjälpartikel "So einfach richten Sie DynDNS für Ihre Domains ein"
(strato.de/faq, hämtad 2026-11-15):

- uppdateringsadress ``https://dyndns.strato.com/nic/update?hostname=<domän>&myip=<ip>``,
- protokollet är DynDNS v2 (dyndns.org), användarnamnet är en domän i paketet och lösenordet
  DynDNS-lösenordet (sätts i STRATOS kundinloggning under "Sicherheit" → "Passwörter festlegen"),
- ``myip`` får utelämnas – då används avsändarens adress.

Svarskoderna följer DynDNS v2 (Dyn, "Return codes"): ``good <ip>`` och ``nochg <ip>`` lyckades;
``badauth``, ``nohost``, ``notfqdn``, ``numhost``, ``abuse``, ``badagent`` och ``!donator`` kräver
att ägaren rättar något och får inte upprepas automatiskt (det räknas som missbruk); ``dnserr`` och
``911`` är tillfälliga och klienten ska vänta minst 30 minuter. ``badsys`` nämns av STRATO. STRATO
beskriver inte själv fler koder – okända svar behandlas som tillfälliga fel med växande väntetid.

Hur adressen kontrolleras – utan någon annan tjänst än STRATO
-------------------------------------------------------------
Ägaren vill inte bero på fler tjänster, så tillägget frågar ingen "vad är min IP"-tjänst. I stället:

1. Den adress domänen har (A-posten, eller adressen STRATO senast svarade med) prövas: tillägget
   ansluter dit på port 443 och hämtar ``/.well-known/drivelog-dyndns``. Svarar den egna proxyn med
   det slumpvärde som skapades vid starten leder domänen fortfarande hit, och ingenting skickas.
2. Svarar den inte (adressen har bytts) skickas en uppdatering till STRATO utan ``myip`` – över
   IPv4 – så att STRATO själv sätter hemmets publika IPv4-adress. STRATO svarar med adressen.
3. Var 24:e timme skickas ändå en uppdatering (gles säkerhetsuppdatering).

Kontrollen kräver att routern klarar "hårnåls-NAT" (att nå sin egen publika adress inifrån).
Gör den inte det svarar STRATO ``nochg`` med samma adress som prövades; tillägget märker det,
loggar en förklaring och uppdaterar då i stället högst varannan timme när kontrollen misslyckas.

Domänen får inte ha en AAAA-post (IPv6) som leder någon annanstans: Let's Encrypt föredrar IPv6
och certifikatet kan då inte utfärdas. STRATO lägger in en sådan för sin parkeringssida.
Tillägget varnar om en AAAA-post finns och inte leder till den egna proxyn.
"""

from __future__ import annotations

import base64
import http.client
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from .caddyfil import DYNDNS_KONTROLLSOKVAG

logger = logging.getLogger("drivelog.dyndns")

STRATO_URL = "https://dyndns.strato.com/nic/update"

KONTROLLINTERVALL = 5 * 60
SAKERHETSINTERVALL = 24 * 60 * 60
HARNAL_INTERVALL = 2 * 60 * 60
TILLFALLIG_VANTAN = 30 * 60
MAX_BACKOFF = 30 * 60

LYCKADES = {"good", "nochg"}
TILLFALLIGA = {"dnserr", "911"}
ATGARD = {
    "badauth": "STRATO godtog inte användarnamn eller lösenord. Kontrollera 'strato_dyndns_user' "
    "(domänen i ditt STRATO-paket, t.ex. smgd.se) och DynDNS-lösenordet.",
    "nohost": "STRATO känner inte till domänen för de här uppgifterna, eller så är DynDNS inte "
    "påslaget för den. Slå på DynDNS för domänen i STRATOS kundinloggning.",
    "notfqdn": "STRATO godtog inte domännamnet – kontrollera inställningen 'domain'.",
    "numhost": "STRATO svarade 'numhost' (flera poster eller för många namn). Kontrollera DNS-posterna hos STRATO.",
    "abuse": "STRATO har spärrat uppdateringar för domänen ('abuse'). Vänta och kontakta STRATO vid behov.",
    "badagent": "STRATO avvisade klienten ('badagent').",
    "!donator": "STRATO avvisade en inställning som inte ingår i paketet ('!donator').",
    "badsys": "STRATO avvisade parametern 'system' ('badsys').",
}


@dataclass(frozen=True)
class Svar:
    kod: str
    adress: str | None
    text: str


def tolka_svar(text: str) -> Svar:
    """``good 1.2.3.4`` → Svar("good", "1.2.3.4"). Tar första raden (en rad per värdnamn)."""
    rad = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    delar = rad.split()
    kod = delar[0].lower() if delar else ""
    adress = delar[1] if len(delar) > 1 else None
    return Svar(kod=kod, adress=adress, text=rad[:200])


# --- HTTP utan andra beroenden än standardbiblioteket -------------------------------------------


class _Anslutning(http.client.HTTPSConnection):
    """HTTPS-anslutning som kan tvingas till en viss adressfamilj eller IP-adress och ändå
    skicka rätt servernamn (SNI) – behövs för att pröva en adress för domänen."""

    def __init__(self, host, port, *, ansluta_till, familj, context, timeout):
        super().__init__(host, port, context=context, timeout=timeout)
        self._ansluta_till = ansluta_till
        self._familj = familj

    def connect(self):
        self.sock = _oppna_socket(self._ansluta_till or self.host, self.port, self._familj, self.timeout)
        if self._tunnel_host:  # pragma: no cover - ingen proxy används
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _HttpAnslutning(http.client.HTTPConnection):
    def __init__(self, host, port, *, ansluta_till, familj, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ansluta_till = ansluta_till
        self._familj = familj

    def connect(self):
        self.sock = _oppna_socket(self._ansluta_till or self.host, self.port, self._familj, self.timeout)


def _oppna_socket(vard: str, port: int, familj: int, timeout: float) -> socket.socket:
    fel: OSError | None = None
    for fam, typ, proto, _namn, adress in socket.getaddrinfo(vard, port, familj, socket.SOCK_STREAM):
        sock = socket.socket(fam, typ, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(adress)
            return sock
        except OSError as exc:
            fel = exc
            sock.close()
    raise fel or OSError(f"Ingen adress för {vard}")


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 15,
    ansluta_till: str | None = None,
    familj: int = socket.AF_UNSPEC,
    verifiera: bool = True,
) -> tuple[int, str]:
    """Enkel GET. ``ansluta_till`` ansluter till en given IP men behåller värdnamnet i URL:en
    som Host och servernamn. Returnerar (statuskod, text)."""
    delar = urlsplit(url)
    vard = delar.hostname or ""
    sokvag = (delar.path or "/") + (f"?{delar.query}" if delar.query else "")
    if delar.scheme == "https":
        context = ssl.create_default_context()
        if not verifiera:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        conn: http.client.HTTPConnection = _Anslutning(
            vard, delar.port or 443, ansluta_till=ansluta_till, familj=familj, context=context, timeout=timeout
        )
    elif delar.scheme == "http":
        conn = _HttpAnslutning(vard, delar.port or 80, ansluta_till=ansluta_till, familj=familj, timeout=timeout)
    else:
        raise ValueError(f"Okänt protokoll i {delar.scheme!r}")
    try:
        conn.request("GET", sokvag, headers=headers or {})
        svar = conn.getresponse()
        return svar.status, svar.read(4096).decode("utf-8", "replace")
    finally:
        conn.close()


def sla_upp(doman: str, familj: int) -> list[str]:
    """Domänens adresser av en viss familj (AF_INET/AF_INET6). Tom lista om det inte finns några."""
    try:
        svar = socket.getaddrinfo(doman, None, familj, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    adresser: list[str] = []
    for *_ignorerat, adress in svar:
        if adress[0] not in adresser:
            adresser.append(adress[0])
    return adresser


# --- Uppdateraren --------------------------------------------------------------------------------


class Uppdaterare:
    """Ett varv i taget (``kor_ett_varv``) så att logiken går att pröva utan att vänta."""

    def __init__(
        self,
        doman: str,
        anvandare: str,
        losenord: str,
        token: str,
        *,
        version: str = "okänd",
        strato_url: str = STRATO_URL,
        kontroll_schema: str = "https",
        kontroll_port: int = 443,
        hamta: Callable[..., tuple[int, str]] = http_get,
        sla_upp: Callable[[str, int], list[str]] = sla_upp,
        klocka: Callable[[], float] = time.monotonic,
    ) -> None:
        self.doman = doman
        self._anvandare = anvandare
        self._losenord = losenord
        self._token = token
        self.strato_url = strato_url
        self.kontroll_schema = kontroll_schema
        self.kontroll_port = kontroll_port
        self._hamta = hamta
        self._sla_upp = sla_upp
        self._klocka = klocka
        self.user_agent = f"SMGD-DriveLog-HA-tillagg/{version}"

        self.kand_adress: str | None = None
        self.senast_bekraftad: float | None = None
        self.senast_skickad: float | None = None
        self.harnal_saknas = False
        self.stoppad = False
        self.antal_fel = 0
        self._forsta_varv: float | None = None
        self._varnade_aaaa: tuple[str, ...] = ()

    # Hjälpare -----------------------------------------------------------------------------------

    def _auth(self) -> str:
        par = f"{self._anvandare}:{self._losenord}".encode()
        return "Basic " + base64.b64encode(par).decode("ascii")

    def leder_hit(self, adress: str) -> bool:
        """Svarar den egna proxyn på ``adress``? (Se modulens beskrivning.)"""
        vard = f"[{adress}]" if ":" in adress else adress
        url = f"{self.kontroll_schema}://{self.doman}:{self.kontroll_port}{DYNDNS_KONTROLLSOKVAG}"
        try:
            status, text = self._hamta(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=6,
                ansluta_till=adress,
                verifiera=False,  # certifikatet kanske inte är utfärdat än; slumpvärdet räcker
            )
        except (OSError, http.client.HTTPException, ValueError) as exc:
            logger.debug("Kontroll av %s misslyckades: %s", vard, exc)
            return False
        return status == 200 and text.strip() == self._token

    def kontrollera_aaaa(self) -> None:
        adresser = tuple(self._sla_upp(self.doman, socket.AF_INET6))
        if not adresser:
            self._varnade_aaaa = ()
            return
        fel = tuple(a for a in adresser if not self.leder_hit(a))
        if fel and fel != self._varnade_aaaa:
            logger.warning(
                "%s har en AAAA-post (IPv6) som inte leder hit: %s. Let's Encrypt försöker då nå den "
                "adressen och certifikatet kan inte utfärdas. Ta bort AAAA-posten hos STRATO "
                "(Domänverwaltung → DNS → AAAA-Record) – STRATO lägger in en för sin parkeringssida.",
                self.doman,
                ", ".join(fel),
            )
        self._varnade_aaaa = fel

    def _skicka(self) -> Svar:
        url = f"{self.strato_url}?{urlencode({'hostname': self.doman})}"
        status, text = self._hamta(
            url,
            headers={"Authorization": self._auth(), "User-Agent": self.user_agent},
            timeout=20,
            familj=socket.AF_INET,  # STRATO ska se hemmets IPv4-adress som avsändare
        )
        svar = tolka_svar(text)
        if svar.kod in LYCKADES or svar.kod in ATGARD or svar.kod in TILLFALLIGA:
            return svar
        if status in (401, 403):
            # Provat mot dyndns.strato.com: utan giltig inloggning svarar servern med en vanlig
            # HTTP 401-sida i stället för "badauth". Det behandlas likadant – inga nya försök.
            return Svar(kod="badauth", adress=None, text=f"HTTP {status}")
        raise OSError(f"Oväntat svar från STRATO (HTTP {status})")

    def _backoff(self) -> float:
        self.antal_fel += 1
        return float(min(60 * 2 ** (self.antal_fel - 1), MAX_BACKOFF))

    # Ett varv -----------------------------------------------------------------------------------

    def kor_ett_varv(self) -> float | None:
        """Kontrollerar och uppdaterar vid behov. Returnerar sekunder till nästa varv, eller
        ``None`` om uppdateraren har stoppats (fel som ägaren måste rätta)."""
        if self.stoppad:
            return None
        nu = self._klocka()
        if self._forsta_varv is None:
            self._forsta_varv = nu
        self.kontrollera_aaaa()

        adress = self.kand_adress
        if adress is None:
            a_poster = self._sla_upp(self.doman, socket.AF_INET)
            adress = a_poster[0] if a_poster else None

        orsak: str | None = None
        kontroll_ok = False
        if adress is None:
            orsak = "domänen har ingen A-post"
        else:
            kontroll_ok = self.leder_hit(adress)
            if kontroll_ok:
                self.kand_adress = adress
                if self.harnal_saknas:
                    logger.info("Kontrollen av domänens adress fungerar nu.")
                    self.harnal_saknas = False
                if self.senast_bekraftad is None:
                    logger.info("%s leder hit (%s) – ingen uppdatering behövs.", self.doman, adress)
                    self.senast_bekraftad = nu
                senaste = self.senast_skickad if self.senast_skickad is not None else self._forsta_varv
                if nu - senaste >= SAKERHETSINTERVALL:
                    orsak = "gles säkerhetsuppdatering"
            elif self.harnal_saknas and self.senast_skickad is not None and nu - self.senast_skickad < HARNAL_INTERVALL:
                orsak = None
            else:
                orsak = f"{adress} leder inte längre hit"

        if orsak is None:
            return float(KONTROLLINTERVALL)

        logger.info("Uppdaterar %s hos STRATO (%s).", self.doman, orsak)
        try:
            svar = self._skicka()
        except (OSError, http.client.HTTPException) as exc:
            vanta = self._backoff()
            logger.warning(
                "Kunde inte nå STRATO (%s). Försöker igen om %d minuter.", type(exc).__name__, vanta // 60
            )
            return vanta

        self.senast_skickad = nu
        if svar.kod in LYCKADES:
            self.antal_fel = 0
            if svar.kod == "good":
                logger.info("STRATO har satt %s till %s.", self.doman, svar.adress or "avsändarens adress")
            else:
                logger.info("STRATO: %s pekar redan på %s (oförändrat).", self.doman, svar.adress or "?")
            if svar.kod == "nochg" and adress is not None and not kontroll_ok and svar.adress == adress:
                if not self.harnal_saknas:
                    logger.warning(
                        "Domänen pekar rätt men tillägget når inte sig självt via %s. Antingen är "
                        "certifikatet inte klart än, eller så klarar routern inte hårnåls-NAT. Tills "
                        "kontrollen fungerar uppdateras STRATO högst varannan timme.",
                        adress,
                    )
                self.harnal_saknas = True
            if svar.adress:
                self.kand_adress = svar.adress
            self.senast_bekraftad = nu
            return float(KONTROLLINTERVALL)
        if svar.kod in TILLFALLIGA:
            logger.warning("STRATO har ett tillfälligt fel ('%s'). Försöker igen om 30 minuter.", svar.kod)
            return float(TILLFALLIG_VANTAN)
        self.stoppad = True
        logger.error("%s DynDNS stoppas tills tillägget startas om med rättade inställningar.", ATGARD[svar.kod])
        return None

    def kor(self, stopp: threading.Event) -> None:
        """Kör tills ``stopp`` sätts. Fel stoppar aldrig resten av tillägget."""
        while not stopp.is_set():
            try:
                vanta = self.kor_ett_varv()
            except Exception:  # noqa: BLE001 - DynDNS får aldrig fälla DriveLog
                logger.exception("Oväntat fel i DynDNS-uppdateraren")
                vanta = self._backoff()
            if vanta is None:
                return
            stopp.wait(vanta)
