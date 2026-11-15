"""Fråga och svar för kontrollen "leder domänen hit?" (DynDNS och AAAA-kontrollen).

Den som kontrollerar en adress skickar ett nytt slumpvärde (``n``) varje gång och godtar bara
svaret ``HMAC-SHA256(nyckel, n)``. Nyckeln skapas vid varje start och lämnar aldrig
startprogrammet, så en annan server kan inte svara rätt – inte ens om den har sett tidigare svar
(som en statisk text i Caddy kunde återanvändas av den som läst den).

Svaret räknas ut av en liten HTTP-server i startprogrammet som bara lyssnar på 127.0.0.1; Caddy
skickar ``/.well-known/drivelog-dyndns`` dit. Servern svarar bara på GET med ett giltigt
slumpvärde och läser ingenting annat ur anropet.

Kvarvarande begränsning: en server som själv vidarebefordrar frågan till den här maskinens
nuvarande adress kan förmedla ett rätt svar. Det kräver att den vet den nya adressen, och värsta
följden är att uppdateringen dröjer till nästa säkerhetsuppdatering (dyndns.SAKERHETSINTERVALL).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("drivelog.addon")

SOKVAG = "/.well-known/drivelog-dyndns"
PORT = 8099
_SLUMPVARDE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def ny_nyckel() -> str:
    return secrets.token_urlsafe(32)


def nytt_slumpvarde() -> str:
    return secrets.token_urlsafe(24)


def svar(nyckel: str, slumpvarde: str) -> str:
    return hmac.new(nyckel.encode("ascii"), slumpvarde.encode("ascii"), hashlib.sha256).hexdigest()


def ar_ratt_svar(nyckel: str, slumpvarde: str, text: str) -> bool:
    return hmac.compare_digest(text.strip().encode("ascii", "replace"), svar(nyckel, slumpvarde).encode("ascii"))


def slumpvarde_ur(sokvag: str) -> str | None:
    """``/.well-known/drivelog-dyndns?n=…`` → slumpvärdet, eller None om anropet inte är giltigt."""
    delar = urlsplit(sokvag)
    if delar.path != SOKVAG:
        return None
    varden = parse_qs(delar.query).get("n", [])
    if len(varden) != 1 or not _SLUMPVARDE.match(varden[0]):
        return None
    return varden[0]


class Kontrollserver:
    """Svarar på kontrollfrågan på 127.0.0.1:``port`` i en bakgrundstråd."""

    def __init__(self, nyckel: str, *, vard: str = "127.0.0.1", port: int = PORT) -> None:
        nyckel_ = nyckel

        class Hanterare(BaseHTTPRequestHandler):
            server_version = "drivelog-kontroll"
            sys_version = ""

            def do_GET(self) -> None:  # noqa: N802 - http.server
                slump = slumpvarde_ur(self.path)
                if slump is None:
                    self.send_error(404)
                    return
                data = svar(nyckel_, slump).encode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=ascii")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:  # tyst: anropen kommer från internet via Caddy
                pass

        self.httpd = ThreadingHTTPServer((vard, port), Hanterare)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._trad = threading.Thread(target=self.httpd.serve_forever, name="kontroll", daemon=True)

    def starta(self) -> Kontrollserver:
        self._trad.start()
        return self

    def stang(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
