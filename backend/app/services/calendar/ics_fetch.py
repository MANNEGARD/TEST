"""Hämtning av kalenderlänken (ICS) – adressen och nätverket.

Länken är en hemlighet: den som har den kan läsa kalendern. Därför lämnar den aldrig den
här modulen i en feltext, ett undantag eller en loggrad. Varje fel blir ett ``IcsError``
med en fast svensk text, kastat ``from None`` så att varken ``__cause__``, ``__context__``
eller en traceback kan bära länken (eller ett ``httpx.Request`` med länken i).

Skydden är samma som för geokodaren (``app.services.places.geocode``) och återanvänder dess
hjälpfunktioner: bara https på port 443, ingen användarinfo, varje DNS-svar och motpartens
adress efter anslutningen måste vara globala (stänger DNS-ombindning), inga proxyinställningar
från miljön, bara GET, tidsgräns och storleksgräns med strömmad läsning, packade svar avvisas.
Omdirigeringar följs för hand, högst tre, och varje hopp prövas på nytt med en ny klient
(inga kakor följer med).
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from app import __version__
from app.services.places.geocode import GeocodeError, _check_ip, _check_peer, _real_transport

MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
DEADLINE_S = 20.0
MAX_LINK_LENGTH = 2000
USER_AGENT = f"SMGD-DriveLog/{__version__} (self-hosted logbook; calendar link)"

# Transport som används när anroparen inte anger någon. Testerna sätter en "offline"-
# transport här (tests/conftest.py), så att ingen test kan nå nätet.
_default_transport: httpx.BaseTransport | None = None
# Utbytbara i tester.
_getaddrinfo = socket.getaddrinfo
_monotonic = time.monotonic

_REDIRECTS = {301, 302, 303, 307, 308}
_GONE = {401, 403, 404, 410}

MSG_EMPTY = "Klistra in kalenderlänken."
MSG_TOO_LONG = "Länken är för lång."
MSG_SPACES = "Länken får inte innehålla mellanslag."
MSG_SCHEME = "Länken måste börja med https:// eller webcal://."
MSG_USERINFO = "Länken får inte innehålla användarnamn eller lösenord."
MSG_PORT = "Länken måste använda den vanliga https-porten."
MSG_NO_HOST = "Länken saknar värdnamn."
MSG_LOCAL = "Länken pekar på en lokal eller reserverad adress."
MSG_UNREACHABLE = "Kalenderlänken gick inte att nå. Kontrollera länken och försök igen."
MSG_TIMEOUT = "Kalendern svarade inte i tid."
MSG_REDIRECTS = "Kalenderlänken skickade vidare för många gånger."
MSG_GONE = "Kalenderlänken fungerar inte längre (servern svarade {status}). Skapa en ny länk i Outlook och klistra in den."
MSG_HTTP = "Kalendertjänsten svarade med fel {status}. Försök igen senare."
MSG_ENCODED = "Kalendertjänsten skickade ett packat svar som inte kan läsas."
MSG_TOO_LARGE = "Kalenderfilen är större än 10 MB."


class IcsError(Exception):
    """Kalenderlänken gick inte att använda. ``message`` är en fast svensk text utan länken."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def _invalid(message: str) -> IcsError:
    return IcsError(message, "invalid_url")


def normalize_link(raw: str) -> str:
    """Statisk kontroll av länken (ingen DNS). Returnerar den normaliserade https-adressen."""
    text = (raw or "").strip()
    if not text:
        raise _invalid(MSG_EMPTY)
    if len(text) > MAX_LINK_LENGTH:
        raise _invalid(MSG_TOO_LONG)
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _invalid(MSG_SPACES)
    lowered = text.lower()
    for prefix in ("webcal://", "webcals://"):
        if lowered.startswith(prefix):
            text = "https://" + text[len(prefix):]
            break
    error: IcsError | None = None
    try:
        parts = urlsplit(text)
        port = parts.port
        host = (parts.hostname or "").strip().rstrip(".").lower()
    except ValueError:  # t.ex. ogiltig port eller trasig IPv6-adress – texten kan bära länken
        error = _invalid(MSG_PORT if _looks_like_bad_port(text) else MSG_NO_HOST)
    if error is not None:
        raise error  # utanför except: inget __context__ som bär länken
    if parts.scheme.lower() != "https":
        raise _invalid(MSG_SCHEME)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _invalid(MSG_USERINFO)
    if port not in (None, 443):
        raise _invalid(MSG_PORT)
    if not host:
        raise _invalid(MSG_NO_HOST)
    if host == "localhost" or host.endswith(".localhost"):
        raise _invalid(MSG_LOCAL)
    if _is_ip_literal(host) and not _ip_allowed(host):
        raise _invalid(MSG_LOCAL)
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def _ip_allowed(value: str) -> bool:
    try:
        _check_ip(value)
    except ValueError:
        return False
    return True


def _looks_like_bad_port(text: str) -> bool:
    netloc = text.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return ":" in netloc.rsplit("]", 1)[-1]


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def host_of(url: str) -> str:
    """Värdnamnet i gemener (ingen sökväg, ingen frågesträng)."""
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def _resolve_check(host: str) -> None:
    if _is_ip_literal(host):
        return  # redan prövad i normalize_link
    infos = None
    try:
        infos = _getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        infos = None
    if not infos:
        raise IcsError(MSG_UNREACHABLE, "ics_unreachable")
    if not all(_ip_allowed(str(info[4][0])) for info in infos):
        raise _invalid(MSG_LOCAL)


def fetch(url: str, *, transport: httpx.BaseTransport | None = None) -> bytes:
    """Hämtar kalenderfilen bakom länken och returnerar den rå. Kastar ``IcsError``.

    Felet kastas först utanför ``except``-blocket, så att det varken har ``__cause__`` eller
    ``__context__`` – det ursprungliga undantaget från httpx bär ofta hela adressen.
    """
    error: IcsError | None = None
    try:
        return _fetch(url, transport)
    except IcsError as exc:
        error = IcsError(exc.message, exc.code)
    except httpx.TimeoutException:
        error = IcsError(MSG_TIMEOUT, "ics_unreachable")
    except Exception:  # httpx, socket, ssl, URL-fel … – texten kan bära länken och följer aldrig med
        error = IcsError(MSG_UNREACHABLE, "ics_unreachable")
    raise error


def _fetch(url: str, transport: httpx.BaseTransport | None) -> bytes:
    injected = transport or _default_transport
    current = normalize_link(url)
    deadline = _monotonic() + DEADLINE_S
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/calendar, text/plain;q=0.5, */*;q=0.1",
        "Accept-Encoding": "identity",
    }
    for _hop in range(1 + MAX_REDIRECTS):  # första anropet plus högst tre omdirigeringar
        current = normalize_link(current)  # varje hopp prövas på nytt
        if injected is None:
            _resolve_check(host_of(current))
        result = _get_once(current, injected, headers, deadline)
        if isinstance(result, bytes):
            return result
        try:
            current = str(httpx.URL(current).join(result))
        except (httpx.InvalidURL, ValueError, TypeError):
            current = "invalid:"  # ger felet från den statiska kontrollen i nästa varv
    raise IcsError(MSG_REDIRECTS, "ics_unreachable")


def _peer_allowed(resp: httpx.Response, *, required: bool) -> bool:
    try:
        _check_peer(resp, required=required)
    except GeocodeError:
        return False
    return True


def _get_once(
    url: str, injected: httpx.BaseTransport | None, headers: dict[str, str], deadline: float
) -> bytes | str:
    """Ett hopp: kroppen (bytes) vid 200, annars nästa adress (str) vid omdirigering."""
    try:
        return _get_once_inner(url, injected, headers, deadline)
    except GeocodeError:
        # Den riktiga transporten prövar adressen redan vid anslutningen (geocode._CheckedBackend).
        error = _invalid(MSG_LOCAL)
    raise error  # utanför except: inget __context__


def _get_once_inner(
    url: str, injected: httpx.BaseTransport | None, headers: dict[str, str], deadline: float
) -> bytes | str:
    with httpx.Client(
        transport=injected or _real_transport(),
        trust_env=False,
        verify=True,
        follow_redirects=False,
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers=headers,
    ) as client:
        with client.stream("GET", url) as resp:
            if not _peer_allowed(resp, required=injected is None):
                raise _invalid(MSG_LOCAL)
            if _monotonic() > deadline:
                raise IcsError(MSG_TIMEOUT, "ics_unreachable")
            status = resp.status_code
            if status in _REDIRECTS:
                location = resp.headers.get("location", "").strip()
                if not location:
                    raise IcsError(MSG_HTTP.format(status=status), "ics_http")
                return location
            if status in _GONE:
                raise IcsError(MSG_GONE.format(status=status), "ics_http")
            if status != 200:
                raise IcsError(MSG_HTTP.format(status=status), "ics_http")
            encoding = resp.headers.get("content-encoding", "").strip().lower()
            if encoding not in ("", "identity"):
                raise IcsError(MSG_ENCODED, "ics_invalid")
            body = bytearray()
            for chunk in resp.iter_raw():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise IcsError(MSG_TOO_LARGE, "ics_too_large")
                if _monotonic() > deadline:
                    raise IcsError(MSG_TIMEOUT, "ics_unreachable")
            return bytes(body)


__all__ = ["IcsError", "fetch", "host_of", "normalize_link"]
