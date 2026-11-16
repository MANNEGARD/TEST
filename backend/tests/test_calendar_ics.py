"""Kalenderlänk (ICS): adress, hämtning, tolkning, synk, två källor, matchning, schema, larm och API.

Allt är offline och påhittat: länkar på example.com/.invalid, inga riktiga kalenderuppgifter.
Hämtningen prövas med en injicerad transport (``httpx.MockTransport``) och utbytt DNS.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

import app.models as M
from app.logging_setup import RedactingFilter, redact
from app.models.enums import Category, IntegrationProvider, IntegrationStatus, LocationType
from app.security import crypto
from app.services import audit, integrations
from app.services import settings as settings_svc
from app.services.calendar import ics, ics_fetch, matching, sync
from app.services.calendar.ics_fetch import IcsError
from app.services.classify import engine
from app.services.ha import alerts
from tests.conftest import BASE_TIME

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "ics"
TOKEN = "HEMLIGTOKEN123"
LINK = f"https://calendar.example.com/owa/calendar/abc@example.invalid/{TOKEN}/calendar.ics"
LINK_PATH = f"/owa/calendar/abc@example.invalid/{TOKEN}/calendar.ics"
SECRETS = (LINK, TOKEN, LINK_PATH, "abc@example.invalid")
STHLM = ZoneInfo("Europe/Stockholm")
W_START = datetime(2026, 8, 1, tzinfo=UTC)
W_END = datetime(2026, 10, 31, tzinfo=UTC)

HOME = "Hemvägen 1, Hemby"
BRANCH = "Verkstadsvägen 1, Sjöstad"
OTHER = "Hamngatan 5, Västerstad"


# ----------------------------------------------------------------------------- hjälpare


def vevent(uid: str | None = "e@example.invalid", **props: str | None) -> str:
    """En VEVENT. Utan SUMMARY får den rubriken "Möte" (annars räknas den som bara upptagen-tid);
    ``SUMMARY=None`` utelämnar rubriken."""
    props.setdefault("SUMMARY", "Möte")
    lines = ["BEGIN:VEVENT"]
    if uid:
        lines.append(f"UID:{uid}")
    for key, value in props.items():
        if value is None:
            continue
        name = key.replace("__", ";").replace("_", "-")
        lines.append(f"{name}={value}" if "__" in key else f"{name}:{value}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def calendar(*events: str, extra: str = "") -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//Test//SV", extra, *events,
                        "END:VCALENDAR"])
    return "\r\n".join(line for line in body.split("\r\n") if line).encode("utf-8")


def parse(data: bytes, *, include_body: bool = False, start: datetime = W_START, end: datetime = W_END,
          tz: ZoneInfo = STHLM) -> ics.ParseResult:
    return ics.parse(data, window_start=start, window_end=end, tz=tz, include_body=include_body)


def one(data: bytes, **kw: Any) -> dict[str, Any]:
    (ev,) = parse(data, **kw).events
    return ev


def outlook() -> bytes:
    return (FIXTURES / "outlook.ics").read_bytes()


class FakeStream:
    """Ersätter httpcore:s nätverksström så att motpartens adress kan prövas."""

    def __init__(self, ip: str) -> None:
        self.ip = ip

    def get_extra_info(self, info: str) -> Any:
        return (self.ip, 443) if info == "server_addr" else None


def ok(content: bytes | None = None, *, status: int = 200, headers: dict[str, str] | None = None,
       extensions: dict[str, Any] | None = None) -> httpx.Response:
    """Svar som från en riktig transport: kroppen strömmas och läses inte i förväg."""
    return httpx.Response(status, headers=headers or {"content-type": "text/calendar; charset=utf-8"},
                          stream=httpx.ByteStream(outlook() if content is None else content),
                          extensions=extensions or {})


class Server:
    """MockTransport-hanterare: svarar per sökväg och sparar anropen."""

    def __init__(self, routes: dict[str, Any] | None = None, default: Any = None) -> None:
        self.routes = routes or {}
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get(request.url.path, self.default)
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return ok(answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def serve(content: bytes | None = None, **kw: Any) -> httpx.MockTransport:
    return Server(default=content, **kw).transport()


def assert_no_secret(*values: Any) -> None:
    for value in values:
        text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        for secret in SECRETS:
            assert secret not in text, f"länken läckte: {text[:200]}"


def assert_clean_exc(exc: BaseException) -> None:
    assert_no_secret(str(exc), repr(exc), getattr(exc, "message", ""))
    assert exc.__cause__ is None
    assert exc.__suppress_context__ or exc.__context__ is None
    if exc.__context__ is not None:
        assert_no_secret(str(exc.__context__), repr(exc.__context__))
    for arg in exc.args:
        assert not isinstance(arg, (httpx.Request, httpx.Response))


def fetch_error(url: str = LINK, transport: httpx.BaseTransport | None = None) -> IcsError:
    with pytest.raises(IcsError) as info:
        ics_fetch.fetch(url, transport=transport)
    assert_clean_exc(info.value)
    return info.value


# ----------------------------------------------------------------------------- 1. adressen


@pytest.mark.parametrize("raw", ["webcal://calendar.example.com/a/b.ics", "WEBCALS://calendar.example.com/a/b.ics",
                                 "  https://calendar.example.com/a/b.ics  "])
def test_webcal_becomes_https(raw):
    assert ics_fetch.normalize_link(raw) == "https://calendar.example.com/a/b.ics"


def test_fragment_removed_and_query_kept():
    url = ics_fetch.normalize_link("https://calendar.example.com/x.ics?token=abc&x=1#frag")
    assert url == "https://calendar.example.com/x.ics?token=abc&x=1"
    assert ics_fetch.host_of(url) == "calendar.example.com"
    assert ics_fetch.host_of("https://Calendar.EXAMPLE.com:443/x") == "calendar.example.com"


@pytest.mark.parametrize(("raw", "text"), [
    ("", "Klistra in kalenderlänken."),
    ("   ", "Klistra in kalenderlänken."),
    ("https://calendar.example.com/" + "a" * 2000, "Länken är för lång."),
    ("https://calendar.example.com/a b.ics", "Länken får inte innehålla mellanslag."),
    ("https://calendar.example.com/a\tb.ics", "Länken får inte innehålla mellanslag."),
    ("http://calendar.example.com/a.ics", "Länken måste börja med https:// eller webcal://."),
    ("ftp://calendar.example.com/a.ics", "Länken måste börja med https:// eller webcal://."),
    ("calendar.example.com/a.ics", "Länken måste börja med https:// eller webcal://."),
    ("https://user:pass@calendar.example.com/a.ics", "Länken får inte innehålla användarnamn eller lösenord."),
    ("https://user@calendar.example.com/a.ics", "Länken får inte innehålla användarnamn eller lösenord."),
    ("https://calendar.example.com:8443/a.ics", "Länken måste använda den vanliga https-porten."),
    ("https://calendar.example.com:abc/a.ics", "Länken måste använda den vanliga https-porten."),
    ("https:///a.ics", "Länken saknar värdnamn."),
    ("https://localhost/a.ics", "Länken pekar på en lokal eller reserverad adress."),
    ("https://kalender.localhost/a.ics", "Länken pekar på en lokal eller reserverad adress."),
])
def test_invalid_links_are_rejected(raw, text):
    with pytest.raises(IcsError) as info:
        ics_fetch.normalize_link(raw)
    assert info.value.message == text and info.value.code == "invalid_url"
    assert_clean_exc(info.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                                  "[::1]", "[fd00::1]", "[::ffff:127.0.0.1]", "192.0.2.10", "0.0.0.1"])
def test_local_and_reserved_ip_literals_are_rejected(host):
    with pytest.raises(IcsError) as info:
        ics_fetch.normalize_link(f"https://{host}/{TOKEN}/calendar.ics")
    assert info.value.message == "Länken pekar på en lokal eller reserverad adress."
    assert_clean_exc(info.value)


def test_port_443_is_allowed():
    assert ics_fetch.normalize_link("https://calendar.example.com:443/a.ics").startswith("https://calendar.example.com")


# ----------------------------------------------------------------------------- 2–3. DNS och motpart


def no_network(monkeypatch) -> list[str]:
    """Den riktiga vägen (ingen injicerad transport) utan att något nätverk kan nås."""
    calls: list[str] = []
    monkeypatch.setattr(ics_fetch, "_default_transport", None)

    def forbidden() -> httpx.BaseTransport:
        calls.append("transport")
        return httpx.MockTransport(lambda r: ok())

    monkeypatch.setattr(ics_fetch, "_real_transport", forbidden)
    return calls


@pytest.mark.parametrize("ip", ["192.168.1.5", "10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "100.64.0.9"])
def test_dns_answer_to_local_address_is_rejected(monkeypatch, ip):
    calls = no_network(monkeypatch)
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    seen: list[tuple] = []

    def fake_dns(host, port, **kw):
        seen.append((host, port, kw))
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(ics_fetch, "_getaddrinfo", fake_dns)
    exc = fetch_error()
    assert exc.message == "Länken pekar på en lokal eller reserverad adress." and exc.code == "invalid_url"
    assert seen == [("calendar.example.com", 443, {"type": socket.SOCK_STREAM})]
    assert calls == []  # ingen anslutning försöktes


def test_dns_failure_gives_unreachable(monkeypatch):
    calls = no_network(monkeypatch)

    def fail(*_a, **_k):
        raise socket.gaierror(f"kunde inte slå upp {LINK}")

    monkeypatch.setattr(ics_fetch, "_getaddrinfo", fail)
    exc = fetch_error()
    assert exc.code == "ics_unreachable" and exc.message.startswith("Kalenderlänken gick inte att nå")
    monkeypatch.setattr(ics_fetch, "_getaddrinfo", lambda *a, **k: [])
    assert fetch_error().code == "ics_unreachable"
    assert calls == []


PUBLIC = "192.0.2.10"


def public_stand_in(monkeypatch) -> None:
    """Låter dokumentationsadressen 192.0.2.10 stå för en publik adress (alla riktiga publika
    adresser tillhör någon). Alla andra adresser prövas som vanligt."""
    real_ip, real_peer = ics_fetch._ip_allowed, ics_fetch._peer_allowed

    def peer(resp, *, required):
        stream = resp.extensions.get("network_stream")
        if stream is not None and (stream.get_extra_info("server_addr") or ("",))[0] == PUBLIC:
            return True
        return real_peer(resp, required=required)

    monkeypatch.setattr(ics_fetch, "_ip_allowed", lambda value: value == PUBLIC or real_ip(value))
    monkeypatch.setattr(ics_fetch, "_peer_allowed", peer)


def dns(monkeypatch, answers: dict[str, str]) -> list[str]:
    asked: list[str] = []

    def fake(host, port, **kw):
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 443))]

    monkeypatch.setattr(ics_fetch, "_getaddrinfo", fake)
    return asked


def test_real_path_with_public_dns_and_peer_is_accepted(monkeypatch):
    calls = no_network(monkeypatch)
    public_stand_in(monkeypatch)
    asked = dns(monkeypatch, {"calendar.example.com": PUBLIC})
    server = Server({LINK_PATH: lambda r: ok(extensions={"network_stream": FakeStream(PUBLIC)})})
    monkeypatch.setattr(ics_fetch, "_real_transport", server.transport)
    assert ics_fetch.fetch(LINK).startswith(b"BEGIN:VCALENDAR")
    assert asked == ["calendar.example.com"] and calls == []


def test_real_path_dns_rebinding_is_rejected(monkeypatch):
    # DNS-svaret var publikt, men anslutningen hamnade på en intern adress.
    no_network(monkeypatch)
    public_stand_in(monkeypatch)
    dns(monkeypatch, {"calendar.example.com": PUBLIC})
    server = Server({LINK_PATH: lambda r: ok(extensions={"network_stream": FakeStream("10.0.0.5")})})
    monkeypatch.setattr(ics_fetch, "_real_transport", server.transport)
    exc = fetch_error()
    assert exc.message == "Länken pekar på en lokal eller reserverad adress." and exc.code == "invalid_url"


def test_real_transport_requires_peer_address(monkeypatch):
    # Den riktiga transporten måste kunna visa motpartens adress; annars avbryts anropet.
    calls = no_network(monkeypatch)
    public_stand_in(monkeypatch)
    dns(monkeypatch, {"calendar.example.com": PUBLIC})
    exc = fetch_error()
    assert exc.message == "Länken pekar på en lokal eller reserverad adress."
    assert calls == ["transport"]


@pytest.mark.parametrize("peer", ["10.0.0.5", "127.0.0.1", "::ffff:127.0.0.1", "169.254.169.254", "192.0.2.10"])
def test_dns_rebinding_is_caught_by_peer_check(peer):
    # Injicerad transport: DNS prövas inte, men motpartens adress prövas när den finns.
    # 192.0.2.0/24 är en dokumentationsadress och räknas också som reserverad.
    transport = httpx.MockTransport(
        lambda r: ok(extensions={"network_stream": FakeStream(peer)}))
    exc = fetch_error(transport=transport)
    assert exc.message == "Länken pekar på en lokal eller reserverad adress." and exc.code == "invalid_url"


def test_injected_transport_without_stream_is_accepted():
    assert ics_fetch.fetch(LINK, transport=serve()).startswith(b"BEGIN:VCALENDAR")


# ----------------------------------------------------------------------------- 4. omdirigeringar


def redirect(location: str, status: int = 302, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={"location": location, **headers})


def test_three_redirects_are_followed_with_relative_location():
    server = Server({
        LINK_PATH: redirect("/hop1", 301, **{"set-cookie": "sid=abc; Path=/"}),
        "/hop1": redirect("https://calendar2.example.com/hop2", 307),
        "/hop2": redirect("hop3?x=1", 308),
        "/hop3": ok(),
    })
    assert ics_fetch.fetch(LINK, transport=server.transport()).startswith(b"BEGIN:VCALENDAR")
    urls = [str(r.url) for r in server.requests]
    assert urls == [LINK, "https://calendar.example.com/hop1", "https://calendar2.example.com/hop2",
                    "https://calendar2.example.com/hop3?x=1"]
    assert all(r.method == "GET" for r in server.requests)
    assert all(r.headers.get("accept-encoding") == "identity" for r in server.requests)
    assert all("cookie" not in r.headers for r in server.requests)  # Set-Cookie i hopp 1 följer inte med
    assert all(r.headers["user-agent"].startswith("SMGD-DriveLog/") for r in server.requests)
    assert all("text/calendar" in r.headers["accept"] for r in server.requests)


def test_fourth_redirect_is_rejected():
    server = Server({LINK_PATH: redirect("/1"), "/1": redirect("/2"), "/2": redirect("/3"), "/3": redirect("/4"),
                     "/4": ok()})
    exc = fetch_error(transport=server.transport())
    assert exc.message == "Kalenderlänken skickade vidare för många gånger." and exc.code == "ics_unreachable"
    assert len(server.requests) == 4


@pytest.mark.parametrize(("location", "text"), [
    ("http://calendar.example.com/plain.ics", "Länken måste börja med https:// eller webcal://."),
    ("https://127.0.0.1/x.ics", "Länken pekar på en lokal eller reserverad adress."),
    ("https://169.254.169.254/latest/meta-data/", "Länken pekar på en lokal eller reserverad adress."),
    ("https://u:p@calendar.example.com/x.ics", "Länken får inte innehålla användarnamn eller lösenord."),
    ("https://calendar.example.com:8443/x.ics", "Länken måste använda den vanliga https-porten."),
    ("https://localhost/x.ics", "Länken pekar på en lokal eller reserverad adress."),
])
def test_redirect_to_forbidden_address_is_rejected(location, text):
    server = Server({LINK_PATH: redirect(location)})
    exc = fetch_error(transport=server.transport())
    assert exc.message == text
    assert len(server.requests) == 1  # det otillåtna hoppet anropades aldrig


def test_redirect_to_private_dns_name_is_rejected(monkeypatch):
    no_network(monkeypatch)
    public_stand_in(monkeypatch)
    asked = dns(monkeypatch, {"calendar.example.com": PUBLIC, "internal.example.com": "10.1.2.3"})
    server = Server({LINK_PATH: lambda r: httpx.Response(
        302, headers={"location": "https://internal.example.com/x.ics"},
        extensions={"network_stream": FakeStream(PUBLIC)})})
    monkeypatch.setattr(ics_fetch, "_real_transport", server.transport)
    exc = fetch_error()
    assert exc.message == "Länken pekar på en lokal eller reserverad adress."
    assert len(server.requests) == 1 and asked == ["calendar.example.com", "internal.example.com"]


def test_redirect_without_location_is_http_error():
    exc = fetch_error(transport=Server({LINK_PATH: httpx.Response(302)}).transport())
    assert exc.code == "ics_http"


# ----------------------------------------------------------------------------- 5. gränser och svar


def test_too_large_stream_is_rejected_without_reading_everything(monkeypatch):
    monkeypatch.setattr(ics_fetch, "MAX_BYTES", 1000)
    produced: list[int] = []

    def chunks():
        for i in range(10_000):
            produced.append(i)
            yield b"x" * 100

    exc = fetch_error(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=chunks())))
    assert exc.message == "Kalenderfilen är större än 10 MB." and exc.code == "ics_too_large"
    assert len(produced) < 20


def test_compressed_response_is_rejected():
    resp = ok(b"\x1f\x8b...", headers={"content-encoding": "gzip"})
    exc = fetch_error(transport=Server({LINK_PATH: resp}).transport())
    assert exc.message == "Kalendertjänsten skickade ett packat svar som inte kan läsas." and exc.code == "ics_invalid"


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_gone_link_statuses(status):
    exc = fetch_error(transport=Server({LINK_PATH: httpx.Response(status, text=LINK)}).transport())
    assert exc.message == (f"Kalenderlänken fungerar inte längre (servern svarade {status}). "
                           "Skapa en ny länk i Outlook och klistra in den.")
    assert exc.code == "ics_http"


@pytest.mark.parametrize("status", [500, 503, 429, 204])
def test_other_statuses(status):
    exc = fetch_error(transport=Server({LINK_PATH: httpx.Response(status)}).transport())
    assert exc.message == f"Kalendertjänsten svarade med fel {status}. Försök igen senare." and exc.code == "ics_http"


def test_timeout_and_connection_errors():
    def timeout(request):
        raise httpx.ReadTimeout(f"timeout for {request.url}", request=request)

    def refused(request):
        raise httpx.ConnectError(f"refused {request.url}", request=request)

    exc = fetch_error(transport=httpx.MockTransport(timeout))
    assert exc.message == "Kalendern svarade inte i tid." and exc.code == "ics_unreachable"
    exc = fetch_error(transport=httpx.MockTransport(refused))
    assert exc.message == "Kalenderlänken gick inte att nå. Kontrollera länken och försök igen."


def test_offline_default_transport_gives_unreachable(offline_calendar_link):
    exc = fetch_error()
    assert exc.code == "ics_unreachable" and len(offline_calendar_link.requests) == 1


def test_total_deadline(monkeypatch):
    clock = iter([0.0, 1.0, 5.0, 30.0, 31.0, 32.0, 33.0])
    monkeypatch.setattr(ics_fetch, "_monotonic", lambda: next(clock))

    def chunks():
        yield b"BEGIN:VCALENDAR\r\n"
        yield b"x" * 100
        yield b"y" * 100

    exc = fetch_error(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=chunks())))
    assert exc.message == "Kalendern svarade inte i tid." and exc.code == "ics_unreachable"


# ----------------------------------------------------------------------------- 6. hemligheten i loggarna


@pytest.mark.parametrize("text", [
    f"hämtar {LINK} nu",
    "fel: https://calendar.example.com/x/y/kalender.ics?token=HEMLIGTOKEN123&a=1 slut",
    f"webcal://calendar.example.com/a/{TOKEN}/basic.ics",
    f"https://calendar.example.com/calendar/published/{TOKEN}/index.html",
    f"Location: {LINK}",
    f"GET /x?u=https://calendar.example.com/owa/calendar/a/{TOKEN}/c HTTP/1.1",
    f"https://example.com/?next=https://calendar.example.com/owa/calendar/a/{TOKEN}/c",
])
def test_redact_hides_calendar_links(text):
    out = redact(text)
    assert "[KALENDERLÄNK DOLD]" in out
    assert TOKEN not in out and "calendar.example.com" not in out


def test_redact_leaves_ordinary_urls():
    assert redact("se https://example.com/page") == "se https://example.com/page"


def test_redact_is_linear_on_many_urls_without_spaces():
    text = "GET /x?" + "https://" * 4000  # cirka 32 kB, som en elak frågesträng i accessloggen
    began = time.perf_counter()
    redact(text)
    assert time.perf_counter() - began < 0.3
    began = time.perf_counter()
    redact("GET /x?" + "https://calendar.example.com/a" * 1000 + ".ic")
    assert time.perf_counter() - began < 0.3


def test_redacting_filter_masks_log_args():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "Hämtning av %s misslyckades", (LINK,), None)
    RedactingFilter().filter(record)
    assert TOKEN not in record.getMessage()


# ----------------------------------------------------------------------------- 7. tolkning


def test_utc_event():
    ev = one(calendar(vevent(DTSTART="20260915T080000Z", DTEND="20260915T093000Z", SUMMARY="Möte")))
    assert ev["start_at"] == datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
    assert ev["end_at"] == datetime(2026, 9, 15, 9, 30, tzinfo=UTC)
    assert ev["subject"] == "Möte" and ev["ical_uid"] == "e@example.invalid"
    assert ev["is_all_day"] is False and ev["is_cancelled"] is False
    assert ev["show_as"] == "busy" and ev["sensitivity"] == "normal" and ev["is_online"] is False
    assert ev["online_provider"] is None and ev["location_name"] is None and ev["body_preview"] is None
    assert ev["series_master_id"] is None
    assert ev["location_address"] is ev["location_city"] is ev["latitude"] is ev["longitude"] is None


def test_iana_tzid():
    ev = one(calendar(vevent(DTSTART__TZID="America/New_York:20260915T080000",
                             DTEND__TZID="America/New_York:20260915T090000")))
    assert ev["start_at"] == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("local", "utc_hour"), [("20260701T100000", 8), ("20261201T100000", 9)])
def test_windows_tzid_without_vtimezone(local, utc_hour):
    ev = one(calendar(vevent(DTSTART__TZID=f"W. Europe Standard Time:{local}")),
             start=datetime(2026, 6, 1, tzinfo=UTC), end=datetime(2026, 12, 31, tzinfo=UTC))
    assert ev["start_at"].hour == utc_hour and ev["start_at"].tzinfo is not None


def test_custom_vtimezone():
    tz = "\r\n".join([
        "BEGIN:VTIMEZONE", "TZID:Customized Time Zone",
        "BEGIN:STANDARD", "DTSTART:16010101T030000", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100",
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10", "END:STANDARD",
        "BEGIN:DAYLIGHT", "DTSTART:16010101T020000", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200",
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3", "END:DAYLIGHT", "END:VTIMEZONE",
    ])
    data = calendar(vevent("s@example.invalid", DTSTART__TZID="Customized Time Zone:20260916T080000"),
                    vevent("w@example.invalid", DTSTART__TZID="Customized Time Zone:20261216T080000"), extra=tz)
    starts = [e["start_at"] for e in parse(data, end=datetime(2026, 12, 31, tzinfo=UTC)).events]
    assert starts == [datetime(2026, 9, 16, 6, 0, tzinfo=UTC), datetime(2026, 12, 16, 7, 0, tzinfo=UTC)]


def test_floating_time_and_unknown_tzid_use_owner_timezone():
    data = calendar(vevent("f@example.invalid", DTSTART="20260915T080000"),
                    vevent("u@example.invalid", DTSTART__TZID="Nowhere/Invented:20260916T080000"))
    starts = [e["start_at"] for e in parse(data).events]
    assert starts == [datetime(2026, 9, 15, 6, 0, tzinfo=UTC), datetime(2026, 9, 16, 6, 0, tzinfo=UTC)]
    ny = parse(data, tz=ZoneInfo("America/New_York")).events
    assert ny[0]["start_at"] == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def test_all_day_value_date_and_microsoft_flag():
    data = calendar(
        vevent("d@example.invalid", DTSTART__VALUE="DATE:20260915", SUMMARY="Heldag"),
        vevent("m@example.invalid", DTSTART="20260916T000000Z", DTEND="20260917T000000Z",
               X_MICROSOFT_CDO_ALLDAYEVENT="TRUE"),
    )
    a, b = parse(data).events
    assert a["is_all_day"] and a["start_at"] == datetime(2026, 9, 14, 22, 0, tzinfo=UTC)  # lokal midnatt
    assert a["end_at"] == datetime(2026, 9, 15, 22, 0, tzinfo=UTC)
    assert b["is_all_day"] and b["start_at"] == datetime(2026, 9, 16, 0, 0, tzinfo=UTC)


def test_duration():
    ev = one(calendar(vevent(DTSTART="20260915T080000Z", DURATION="PT1H45M")))
    assert ev["end_at"] == datetime(2026, 9, 15, 9, 45, tzinfo=UTC)
    ev = one(calendar(vevent(DTSTART="20260915T080000Z")))
    assert ev["end_at"] == ev["start_at"]


def test_rrule_exdate_rdate():
    data = calendar(vevent(
        "r@example.invalid", DTSTART="20260901T080000Z", DTEND="20260901T090000Z",
        RRULE="FREQ=DAILY;COUNT=5", EXDATE="20260903T080000Z", RDATE="20260910T080000Z", SUMMARY="Serie",
    ))
    events = parse(data).events
    assert [e["start_at"].day for e in events] == [1, 2, 4, 5, 10]
    assert all(e["series_master_id"] == "r@example.invalid" for e in events)
    assert len({e["external_id"] for e in events}) == 5


def test_outlook_file_with_windows_zone_exception_and_moved_occurrence():
    result = parse(outlook(), include_body=True)
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for ev in result.events:
        by_subject.setdefault(ev["subject"], []).append(ev)
    series = [e for e in result.events if e["series_master_id"]]
    starts = [e["start_at"] for e in series]
    # 7/9, (14/9 undantag), 21/9 flyttad till 22/9 13:00, 28/9 inställd, 5/10, 12/10 – sommartid UTC+2
    assert starts == [
        datetime(2026, 9, 7, 8, 0, tzinfo=UTC), datetime(2026, 9, 22, 11, 0, tzinfo=UTC),
        datetime(2026, 9, 28, 8, 0, tzinfo=UTC), datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 12, 8, 0, tzinfo=UTC),
    ]
    moved = by_subject["Veckomöte Sjöstad (flyttat)"][0]
    assert moved["external_id"].endswith(":20260921T080000Z")  # nyckeln är den ursprungliga tiden
    cancelled = by_subject["Inställt: Veckomöte Sjöstad"][0]
    assert cancelled["is_cancelled"] is True
    assert by_subject["Veckomöte Sjöstad"][0]["location_name"] == "Sjöstadskontoret"
    assert by_subject["Veckomöte Sjöstad"][0]["body_preview"] == "Dagordning: genomgång av veckan."
    kund = by_subject["Kundbesök"][0]
    assert kund["location_name"] == "Hamngatan 5, Västerstad" and kund["show_as"] == "tentative"
    assert kund["start_at"] == datetime(2026, 9, 15, 7, 0, tzinfo=UTC)
    konf = by_subject["Konferens"][0]
    assert konf["is_all_day"] and konf["show_as"] == "oof"
    assert by_subject["Tandläkare"][0]["sensitivity"] == "private"
    teams = by_subject["Avstämning"][0]
    assert teams["is_online"] and teams["online_provider"] == "teamsForBusiness"
    assert teams["show_as"] == "workingElsewhere"
    assert by_subject["Lunch"][0]["show_as"] == "free"
    assert result.total_vevents == 8 and result.skipped == 0 and not result.truncated and not result.busy_only
    assert result.occurrences == 9 and result.with_location == 8


def test_cancelled_status_and_busy_status_mapping():
    data = calendar(
        vevent("c@example.invalid", DTSTART="20260915T080000Z", STATUS="cancelled"),
        vevent("t@example.invalid", DTSTART="20260915T090000Z", TRANSP="TRANSPARENT"),
        *[vevent(f"b{i}@example.invalid", DTSTART=f"20260916T0{i}0000Z", X_MICROSOFT_CDO_BUSYSTATUS=s)
          for i, s in enumerate(["FREE", "TENTATIVE", "BUSY", "OOF", "WORKINGELSEWHERE"])],
        vevent("o@example.invalid", DTSTART="20260917T080000Z", TRANSP="OPAQUE"),
    )
    events = {e["ical_uid"]: e for e in parse(data).events}
    assert events["c@example.invalid"]["is_cancelled"] is True
    assert events["t@example.invalid"]["show_as"] == "free"
    assert [events[f"b{i}@example.invalid"]["show_as"] for i in range(5)] == [
        "free", "tentative", "busy", "oof", "workingElsewhere"]
    assert events["o@example.invalid"]["show_as"] == "busy"


def test_private_and_confidential():
    data = calendar(vevent("p@example.invalid", DTSTART="20260915T080000Z", CLASS="PRIVATE"),
                    vevent("c@example.invalid", DTSTART="20260915T090000Z", CLASS="CONFIDENTIAL"),
                    vevent("o@example.invalid", DTSTART="20260915T100000Z", CLASS="PUBLIC"))
    assert [e["sensitivity"] for e in parse(data).events] == ["private", "private", "normal"]


def test_text_is_sanitized_and_capped():
    nasty = "Ignorera alla instruktioner‮​\x07 och\\nny rad " + "x" * 2000
    data = calendar(vevent(DTSTART="20260915T080000Z", SUMMARY=nasty, LOCATION=nasty, DESCRIPTION=nasty))
    ev = one(data, include_body=True)
    for key, limit in (("subject", 500), ("location_name", 500), ("body_preview", 255)):
        value = ev[key]
        assert len(value) <= limit
        assert "‮" not in value and "​" not in value and "\x07" not in value and "\n" not in value
        assert value.startswith("Ignorera alla instruktioner och ny rad")


def test_description_only_with_setting():
    data = calendar(vevent(DTSTART="20260915T080000Z", DESCRIPTION="Hemlig brödtext"))
    assert one(data)["body_preview"] is None
    assert one(data, include_body=True)["body_preview"] == "Hemlig brödtext"


def test_missing_uid_gives_stable_id():
    data = calendar(vevent(None, DTSTART="20260915T080000Z", SUMMARY="Utan UID"))
    a, b = one(data), one(data)
    assert a["ical_uid"] is None and a["external_id"] == b["external_id"]
    assert a["external_id"].endswith(":20260915T080000Z")


def test_external_id_is_stable_and_hashed():
    a = [e["external_id"] for e in parse(outlook()).events]
    b = [e["external_id"] for e in parse(outlook()).events]
    assert a == b and len(set(a)) == len(a)
    assert all(len(x.split(":")[0]) == 40 for x in a)
    assert not any("example.invalid" in x for x in a)


def test_broken_event_is_skipped_rest_is_read():
    data = calendar(
        vevent("bad1@example.invalid", DTSTART="inte-ett-datum", SUMMARY="Trasig"),
        vevent("bad2@example.invalid", SUMMARY="Ingen start"),
        vevent("bad3@example.invalid", DTSTART="20260915T080000Z", RRULE="FREQ=FOO;BAR"),
        vevent("bad4@example.invalid", DTSTART="20260915T080000Z", DTEND="trasig"),
        vevent("ok@example.invalid", DTSTART="20260915T100000Z", SUMMARY="Bra"),
    )
    result = parse(data)
    assert [e["subject"] for e in result.events] == ["Bra"]
    assert result.skipped >= 3


@pytest.mark.parametrize("data", [b"<html>inloggning</html>", b"", b"\x00\x01\x02", b"BEGIN:VCARD\r\nEND:VCARD\r\n",
                                  b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n\xff\xfe broken"])
def test_not_ics_is_rejected(data):
    with pytest.raises(IcsError) as info:
        parse(data)
    if data.startswith(b"BEGIN:VCALENDAR"):
        # Ett trasigt slut kan biblioteket tolka välvilligt; då får det inte bli ett annat fel.
        pass
    assert info.value.code == "ics_invalid" and info.value.message == "Filen bakom länken är ingen kalender (ICS)."
    assert_clean_exc(info.value)


def test_nested_and_other_components_are_ignored():
    hidden = vevent("dold@example.invalid", DTSTART="20260901T080000Z", RRULE="FREQ=SECONDLY", SUMMARY="Gömd")
    todo = "\r\n".join(["BEGIN:VTODO", "UID:todo@example.invalid", "SUMMARY:Att göra", hidden, "END:VTODO"])
    data = calendar(todo, vevent("ok@example.invalid", DTSTART="20260915T080000Z", SUMMARY="Synlig"))
    began = time.perf_counter()
    result = parse(data)
    assert time.perf_counter() - began < 2.0
    assert [e["subject"] for e in result.events] == ["Synlig"]
    assert result.total_vevents == 2  # den gömda räknas mot taket men expanderas aldrig


def test_bom_and_leading_whitespace_are_accepted():
    data = b"\xef\xbb\xbf\r\n  " + calendar(vevent(DTSTART="20260915T080000Z"))
    assert len(parse(data).events) == 1


def test_too_many_vevents(monkeypatch):
    monkeypatch.setattr(ics, "MAX_VEVENTS", 3)
    data = calendar(*[vevent(f"{i}@example.invalid", DTSTART="20260915T080000Z") for i in range(4)])
    with pytest.raises(IcsError) as info:
        parse(data)
    assert info.value.message == "Kalendern innehåller för många händelser (högst 25 000)."


def test_too_many_occurrences_truncates(monkeypatch):
    monkeypatch.setattr(ics, "MAX_OCCURRENCES", 10)
    data = calendar(vevent("d@example.invalid", DTSTART="20260801T080000Z", RRULE="FREQ=DAILY",
                           LOCATION="Kontoret", SUMMARY="Daglig"))
    result = parse(data)
    assert result.truncated and len(result.events) == 10
    assert result.events[0]["start_at"] == datetime(2026, 8, 1, 8, 0, tzinfo=UTC)  # de tidigaste behålls


def test_expensive_recurrences_are_skipped_quickly():
    data = calendar(
        vevent("s@example.invalid", DTSTART="20260901T080000Z", RRULE="FREQ=SECONDLY", SUMMARY="Sekund"),
        vevent("m@example.invalid", DTSTART="20260901T080000Z", RRULE="FREQ=MINUTELY", SUMMARY="Minut"),
        vevent("h@example.invalid", DTSTART="20260901T080000Z", RRULE="FREQ=DAILY;BYHOUR=1,2,3", SUMMARY="Timme"),
        vevent("o@example.invalid", DTSTART="10000101T080000Z", RRULE="FREQ=YEARLY;BYDAY=MO,TU,WE,TH,FR,SA,SU",
               SUMMARY="Varje dag sedan år 1000"),
        vevent("u@example.invalid", DTSTART="20200101T080000Z", RRULE="FREQ=DAILY;UNTIL=20200201T000000Z",
               SUMMARY="Slut före fönstret"),
        vevent("ok@example.invalid", DTSTART="20260915T080000Z", SUMMARY="Vanligt möte"),
    )
    began = time.perf_counter()
    result = parse(data)
    assert time.perf_counter() - began < 2.0
    assert [e["subject"] for e in result.events] == ["Vanligt möte"]
    assert result.skipped == 5


def test_many_rdates_count_against_the_budget(monkeypatch):
    monkeypatch.setattr(ics, "MAX_WINDOW_ESTIMATE", 5)
    many = ",".join(f"202609{d:02d}T080000Z" for d in range(1, 21))
    data = calendar(vevent("r@example.invalid", DTSTART="20260901T080000Z", RDATE=many, SUMMARY="Många"),
                    vevent("ok@example.invalid", DTSTART="20260915T090000Z", SUMMARY="Vanligt"))
    result = parse(data)
    assert [e["subject"] for e in result.events] == ["Vanligt"] and result.skipped == 1


def test_daily_since_2000_is_still_read():
    data = calendar(vevent("d@example.invalid", DTSTART="20000103T080000Z", RRULE="FREQ=WEEKLY;BYDAY=MO",
                           SUMMARY="Måndagsmöte", LOCATION="Kontoret"))
    result = parse(data)
    assert result.skipped == 0 and len(result.events) == 13


def test_total_step_budget(monkeypatch):
    monkeypatch.setattr(ics, "MAX_TOTAL_STEPS", 150)  # en daglig serie i fönstret ≈ 92 steg
    data = calendar(*[vevent(f"{i}@example.invalid", DTSTART="20260801T080000Z", RRULE="FREQ=DAILY",
                             SUMMARY=f"Serie {i}") for i in range(3)])
    result = parse(data)
    assert result.skipped == 2 and {e["subject"] for e in result.events} == {"Serie 0"}


def _cost(rrule: str, dtstart: str = "19000101T000000Z") -> tuple[int, int]:
    data = calendar(vevent("c@example.invalid", DTSTART=dtstart, RRULE=rrule))
    import icalendar
    comp = next(c for c in icalendar.Calendar.from_ical(data).subcomponents if c.name == "VEVENT")
    cost = ics._series_cost(comp, W_START, W_END, STHLM)
    assert cost is not None
    return cost


def test_byday_without_ordinal_counts_every_such_day_in_the_period():
    # FREQ=YEARLY;BYDAY=MO..SU är varje dag – inte sju dagar per år.
    steps, in_window = _cost("FREQ=YEARLY;BYDAY=MO,TU,WE,TH,FR,SA,SU")
    assert steps >= 126 * 360 and in_window >= 90
    steps, _ = _cost("FREQ=MONTHLY;BYDAY=MO")  # varje måndag i månaden ≈ 4–5
    assert steps >= 126 * 12 * 4
    steps, _ = _cost("FREQ=YEARLY;BYDAY=1MO")  # med ordningstal: en per år
    assert steps <= 130
    steps, _ = _cost("FREQ=YEARLY;BYMONTH=3;BYDAY=MO")  # varje måndag i mars
    assert 126 * 4 <= steps <= 126 * 6


def test_yearly_every_day_bomb_is_bounded():
    data = calendar(*[vevent(f"b{i}@example.invalid", DTSTART="19000101T000000Z",
                             RRULE="FREQ=YEARLY;BYDAY=MO,TU,WE,TH,FR,SA,SU", SUMMARY="X") for i in range(60)])
    began = time.process_time()
    result = parse(data)
    assert time.process_time() - began < 10.0
    assert result.skipped >= 30  # stegbudgeten räcker bara till en del av serierna


def test_expansion_stops_at_the_occurrence_cap(monkeypatch):
    monkeypatch.setattr(ics, "MAX_EXPANDED", 50)
    data = calendar(*[vevent(f"d{i}@example.invalid", DTSTART="20260801T080000Z", RRULE="FREQ=DAILY",
                             SUMMARY=f"Daglig {i}", LOCATION="Kontoret") for i in range(10)])
    result = parse(data)
    assert result.truncated and len(result.events) <= 50


@pytest.mark.parametrize("rrule", [
    "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",  # 30 februari finns inte – biblioteket letar till år 9999
    "FREQ=WEEKLY;BYMONTH=4,6;BYMONTHDAY=31",
    "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=6",
    "FREQ=YEARLY;BYMONTH=6;BYYEARDAY=1",
    "FREQ=MONTHLY;BYDAY=6MO",
    "FREQ=DAILY;BYSETPOS=2",
    "FREQ=YEARLY;BYWEEKNO=1;BYMONTH=6",
])
def test_rules_that_can_never_occur_are_skipped_quickly(rrule):
    data = calendar(*[vevent(f"n{i}@example.invalid", DTSTART="20260101T080000Z", RRULE=rrule, SUMMARY="Aldrig")
                      for i in range(3)],
                    vevent("ok@example.invalid", DTSTART="20260915T080000Z", SUMMARY="Vanligt möte"))
    began = time.process_time()
    result = parse(data)
    assert time.process_time() - began < 2.0
    assert [e["subject"] for e in result.events] == ["Vanligt möte"] and result.skipped == 3


def test_feasible_filtered_rules_are_still_read():
    data = calendar(
        vevent("a@example.invalid", DTSTART="20260101T080000Z", RRULE="FREQ=YEARLY;BYMONTH=9;BYMONTHDAY=30",
               SUMMARY="Årsmöte"),
        vevent("b@example.invalid", DTSTART="20260101T080000Z", RRULE="FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
               SUMMARY="Sista vardagen"),
        vevent("c@example.invalid", DTSTART="20260101T080000Z", RRULE="FREQ=MONTHLY;BYMONTHDAY=-1",
               SUMMARY="Sista dagen"),
        vevent("d@example.invalid", DTSTART="20260101T080000Z", RRULE="FREQ=YEARLY;BYWEEKNO=40;BYDAY=TU;BYMONTH=9,10",
               SUMMARY="Vecka 40"),
        vevent("e@example.invalid", DTSTART="20260101T080000Z", RRULE="FREQ=YEARLY;BYWEEKNO=1;BYMONTH=12",
               SUMMARY="Vecka 1 i december"),  # kan aldrig ligga i fönstret, men är möjlig
    )
    result = parse(data)
    assert result.skipped == 0
    assert sorted({e["subject"] for e in result.events}) == ["Sista dagen", "Sista vardagen", "Vecka 40", "Årsmöte"]


def test_expansion_stops_at_the_time_budget(monkeypatch):
    monkeypatch.setattr(ics, "EXPAND_SECONDS", 0.0)
    data = calendar(*[vevent(f"d{i}@example.invalid", DTSTART="20260801T080000Z", RRULE="FREQ=DAILY",
                             SUMMARY=f"Daglig {i}", LOCATION="Kontoret") for i in range(5)])
    result = parse(data)
    assert result.truncated and len({e["subject"] for e in result.events}) < 5


def test_ended_count_series_do_not_use_the_window_budget():
    old = [vevent(f"old{i}@example.invalid", DTSTART=f"{2016 + i % 7}0104T080000Z",
                  RRULE="FREQ=WEEKLY;COUNT=30;BYDAY=MO,WE", SUMMARY=f"Gammal {i}") for i in range(900)]
    current = vevent("current@example.invalid", DTSTART="20260101T080000Z", RRULE="FREQ=WEEKLY;BYDAY=TU",
                     SUMMARY="Pågående", LOCATION="Kontoret")
    start, end = datetime(2026, 9, 17, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)
    result = parse(calendar(*old, current), start=start, end=end)
    assert [e["subject"] for e in result.events] == ["Pågående"] * 10
    assert result.skipped == 0


def test_latin1_file_is_read_as_windows_1252():
    data = calendar(vevent(DTSTART="20260915T080000Z", SUMMARY="Möte på kontoret", LOCATION="Göteborg"))
    ev = one(data.decode("utf-8").encode("cp1252"))
    assert ev["subject"] == "Möte på kontoret" and ev["location_name"] == "Göteborg"


def test_same_uid_twice_without_recurrence_id_keeps_both():
    data = calendar(vevent("dup@example.invalid", DTSTART="20260903T080000Z", SUMMARY="Dup A"),
                    vevent("dup@example.invalid", DTSTART="20260904T080000Z", SUMMARY="Dup B"))
    events = parse(data).events
    assert [e["subject"] for e in events] == ["Dup A", "Dup B"]
    assert {e["ical_uid"] for e in events} == {"dup@example.invalid"}
    assert len({e["external_id"] for e in events}) == 2


def test_busy_only_file_gives_no_events():
    result = parse((FIXTURES / "ledig-upptagen.ics").read_bytes())
    assert result.busy_only and result.events == [] and result.occurrences == 3 and result.with_location == 0


def test_last_duplicate_wins():
    data = calendar(vevent("x@example.invalid", DTSTART="20260915T080000Z", SUMMARY="Första"),
                    vevent("x@example.invalid", DTSTART="20260915T080000Z", SUMMARY="Andra"))
    result = parse(data)
    assert len(result.events) == 1


def test_window_overlap_rules():
    data = calendar(vevent("a@example.invalid", DTSTART="20260731T230000Z", DTEND="20260801T010000Z"),
                    vevent("b@example.invalid", DTSTART="20260731T200000Z", DTEND="20260731T210000Z"),
                    vevent("c@example.invalid", DTSTART="20261031T000000Z"),
                    vevent("d@example.invalid", DTSTART="20261031T010000Z"))
    assert {e["ical_uid"] for e in parse(data).events} == {"a@example.invalid", "c@example.invalid"}


# ----------------------------------------------------------------------------- 8. synk


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # fönstret 1/8–14/10 rymmer hela outlook.ics


def save(db, user, content: bytes | None = None, *, now: datetime = NOW, url: str = LINK) -> dict[str, Any]:
    return ics.save_link(db, user.id, url, now=now, transport=serve(content))


def ics_events(db, user, *, active: bool = True) -> list[M.CalendarEvent]:
    q = select(M.CalendarEvent).where(M.CalendarEvent.user_id == user.id, M.CalendarEvent.calendar_id == "ics")
    if active:
        q = q.where(M.CalendarEvent.removed_at.is_(None))
    return list(db.execute(q.order_by(M.CalendarEvent.start_at)).scalars())


def test_save_link_stores_encrypted_and_creates_events(db, user):
    out = save(db, user)
    conn = integrations.get(db, user.id, IntegrationProvider.CALENDAR_ICS)
    assert conn is not None and conn.status == IntegrationStatus.CONNECTED
    assert out["has_link"] and out["host"] == "calendar.example.com" and out["status"] == "connected"
    assert LINK.encode() not in (conn.secrets_encrypted or "").encode()
    assert crypto.decrypt_json(conn.secrets_encrypted) == {"url": LINK}
    assert conn.config == {"host": "calendar.example.com"}
    assert conn.capabilities == {"events": 9, "with_location": 8, "busy_only": False, "skipped": 0,
                                 "truncated": False}
    events = ics_events(db, user)
    assert len(events) == 10 and all(e.connection_id == conn.id and not e.is_demo for e in events)
    assert out["stored_events"] == 10 and out["events_with_location"] == 8 and out["tip"] is None
    run = db.execute(select(M.IntegrationSyncRun).where(M.IntegrationSyncRun.connection_id == conn.id)).scalar_one()
    assert run.kind == "calendar_ics_sync" and run.status == "ok"
    assert_no_secret(out, conn.config, conn.capabilities, run.stats, run.error or "")


def test_second_sync_updates_and_marks_removed(db, user):
    save(db, user)
    data = outlook().replace(b"SUMMARY;LANGUAGE=sv-SE:Kundbes\xc3\xb6k", b"SUMMARY;LANGUAGE=sv-SE:Kundm\xc3\xb6te")
    start = data.index(b"BEGIN:VEVENT\r\nUID:040000008200E00074C5B7101A82E00800000000PRIVAT0004")
    end = data.index(b"END:VEVENT", start) + len(b"END:VEVENT\r\n")
    data = data[:start] + data[end:]
    result = ics.sync_user(db, user.id, now=NOW + timedelta(hours=1), transport=serve(data))
    assert result["status"] == "ok" and result["removed"] == 1 and result["created"] == 0
    assert result["updated"] == 9 and result["events"] == 8 and result["tip"] is None
    subjects = [e.subject for e in ics_events(db, user)]
    assert "Kundmöte" in subjects and "Kundbesök" not in subjects and "Tandläkare" not in subjects
    gone = [e for e in ics_events(db, user, active=False) if e.removed_at is not None]
    assert [e.subject for e in gone] == ["Tandläkare"]
    # Tillbaka igen → aktiv
    result = ics.sync_user(db, user.id, now=NOW + timedelta(hours=2), transport=serve(outlook()))
    assert result["created"] == 0 and any(e.subject == "Tandläkare" for e in ics_events(db, user))


def test_event_outside_window_is_left_alone(db, user):
    save(db, user)
    old = M.CalendarEvent(user_id=user.id, calendar_id="ics", external_id="old:1", subject="Gammalt",
                          start_at=NOW - timedelta(days=200), end_at=NOW - timedelta(days=200, hours=-1))
    db.add(old)
    db.flush()
    ics.sync_user(db, user.id, now=NOW, transport=serve(calendar()))
    assert old.removed_at is None
    assert all(e.removed_at is not None for e in ics_events(db, user, active=False) if e.id != old.id)


def test_busy_only_link_stores_nothing_and_gives_tip(db, user):
    out = save(db, user, (FIXTURES / "ledig-upptagen.ics").read_bytes())
    assert ics_events(db, user) == []
    assert out["tip"] == ics.TIP_BUSY_ONLY and out["stored_events"] == 0
    conn = integrations.get(db, user.id, ics.PROVIDER)
    assert conn.capabilities["busy_only"] is True and conn.capabilities["events"] == 3


def test_tips_for_empty_and_no_location(db, user):
    out = save(db, user, calendar())
    assert out["tip"] == ics.TIP_EMPTY
    data = calendar(vevent(DTSTART="20260915T080000Z", SUMMARY="Planering"))
    result = ics.sync_user(db, user.id, now=NOW, transport=serve(data))
    assert result["tip"] == ics.TIP_NO_LOCATION
    assert ics.status(db, user.id)["events_with_location"] == 0


def test_tip_for_truncated(db, user, monkeypatch):
    monkeypatch.setattr(ics, "MAX_OCCURRENCES", 5)
    data = calendar(vevent("d@example.invalid", DTSTART="20260901T080000Z", RRULE="FREQ=DAILY",
                           SUMMARY="Daglig", LOCATION="Kontoret"))
    assert save(db, user, data)["tip"] == ics.TIP_TRUNCATED


def test_duplicate_of_graph_event_is_skipped(db, user):
    graph_ev = M.CalendarEvent(
        user_id=user.id, calendar_id="AAMkAGraphKalender==", external_id="graph-1",
        ical_uid="040000008200E00074C5B7101A82E00800000000KUND0002@example.invalid", subject="Kundbesök",
        start_at=datetime(2026, 9, 15, 7, 0, tzinfo=UTC), end_at=datetime(2026, 9, 15, 8, 30, tzinfo=UTC),
    )
    db.add(graph_ev)
    db.flush()
    save(db, user)
    subjects = [e.subject for e in ics_events(db, user)]
    assert "Kundbesök" not in subjects and len(subjects) == 9
    assert integrations.get(db, user.id, ics.PROVIDER).capabilities["skipped"] == 1


def test_recurring_graph_occurrence_with_own_ical_uid_is_skipped(db, user):
    # Graph ger varje förekomst i en serie ett eget iCalUId; ICS har seriens UID.
    data = calendar(vevent("serie@example.invalid", DTSTART="20260915T080000Z", DTEND="20260915T090000Z",
                           RRULE="FREQ=WEEKLY;COUNT=2", SUMMARY="Veckomöte", LOCATION="Kontoret"))
    db.add(M.CalendarEvent(
        user_id=user.id, calendar_id="AAMkAGraphKalender==", external_id="graph-occ-1",
        ical_uid="040000008200E00074C5B7101A82E008000000OCC1@example.invalid", subject=" veckomöte ",
        series_master_id="AAMkAGraphSerie==",
        start_at=datetime(2026, 9, 15, 8, 0, tzinfo=UTC), end_at=datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
    ))
    db.flush()
    save(db, user, data)
    assert [e.start_at.replace(tzinfo=UTC) for e in ics_events(db, user)] == [datetime(2026, 9, 22, 8, 0, tzinfo=UTC)]


def test_sync_error_keeps_events_and_counts_failures(db, user):
    save(db, user)
    before = len(ics_events(db, user))
    conn = integrations.get(db, user.id, ics.PROVIDER)
    for i in range(3):
        with pytest.raises(IcsError) as info:
            ics.sync_user(db, user.id, now=NOW, transport=Server(default=httpx.Response(404)).transport())
        assert_clean_exc(info.value)
        assert conn.consecutive_failures == i + 1
    assert conn.status == IntegrationStatus.ERROR
    assert conn.last_error.startswith("Kalenderlänken fungerar inte längre (servern svarade 404)")
    assert len(ics_events(db, user)) == before
    runs = db.execute(select(M.IntegrationSyncRun).where(M.IntegrationSyncRun.status == "error")).scalars().all()
    assert len(runs) == 3 and all(r.kind == "calendar_ics_sync" for r in runs)
    assert_no_secret(conn.last_error, [r.error for r in runs])
    ics.sync_user(db, user.id, now=NOW, transport=serve())
    assert conn.status == IntegrationStatus.CONNECTED and conn.last_error is None


def test_sync_without_link(db, user):
    with pytest.raises(IcsError) as info:
        ics.sync_user(db, user.id)
    assert info.value.code == "not_configured" and info.value.message == "Ingen kalenderlänk är sparad."


def test_failed_save_changes_nothing_and_keeps_previous_link(db, user):
    with pytest.raises(IcsError):
        save(db, user, b"<html>inte en kalender</html>")
    assert integrations.get(db, user.id, ics.PROVIDER) is None
    save(db, user)
    conn = integrations.get(db, user.id, ics.PROVIDER)
    other = "https://other.example.com/owa/calendar/x/ANNANTOKEN/calendar.ics"
    with pytest.raises(IcsError):
        ics.save_link(db, user.id, other, now=NOW, transport=Server(default=httpx.Response(500)).transport())
    assert integrations.secrets(conn) == {"url": LINK} and conn.last_error is None
    assert conn.config == {"host": "calendar.example.com"}


def test_changing_link_retires_old_events(db, user):
    save(db, user)
    other = "webcal://other.example.com/calendar/published/x/ANNANTOKEN/calendar.ics"
    data = calendar(vevent("ny@example.invalid", DTSTART="20260915T080000Z", SUMMARY="Nytt", LOCATION="Kontoret"))
    out = ics.save_link(db, user.id, other, now=NOW, transport=serve(data))
    assert out["host"] == "other.example.com" and [e.subject for e in ics_events(db, user)] == ["Nytt"]
    conn = integrations.get(db, user.id, ics.PROVIDER)
    assert integrations.secrets(conn)["url"].startswith("https://other.example.com/")


def test_remove_link_deletes_events_matches_and_secret(db, user, trips):
    save(db, user)
    t = trips.trip(HOME, BRANCH, km=200, minutes=130, start_at=datetime(2026, 9, 7, 5, 0, tzinfo=UTC))
    ev = ics_events(db, user)[0]
    db.add(M.CalendarMatch(logical_trip_id=t.id, calendar_event_id=ev.id, relation="arrival", score=0.9,
                           time_score=1.0, geo_score=0.8))
    db.flush()
    assert ics.remove_link(db, user.id) is True
    assert db.execute(select(func.count(M.CalendarEvent.id))).scalar_one() == 0
    assert db.execute(select(func.count(M.CalendarMatch.id))).scalar_one() == 0
    conn = integrations.get(db, user.id, ics.PROVIDER)
    assert conn.secrets_encrypted is None and conn.config == {} and conn.capabilities == {}
    assert conn.status == IntegrationStatus.NOT_CONFIGURED
    assert ics.remove_link(db, user.id) is False  # redan borta: ingenting händer
    st = ics.status(db, user.id)
    assert st["has_link"] is False and st["host"] is None and st["tip"] is None and st["stored_events"] == 0


def test_status_without_link(db, user):
    assert ics.status(db, user.id) == {
        "provider": "calendar_ics", "status": "not_configured", "has_link": False, "host": None,
        "last_sync_at": None, "last_success_at": None, "last_error": None, "last_error_at": None,
        "stored_events": 0, "events_with_location": None, "tip": None, "stores_body_preview": False,
        "sync_days_back": 60, "sync_days_forward": 14,
    }


def test_body_preview_follows_setting(db, user):
    save(db, user)
    assert all(e.body_preview is None for e in ics_events(db, user))
    settings_svc.update_section(db, user.id, "calendar", {"store_body_preview": True})
    ics.sync_user(db, user.id, now=NOW, transport=serve())
    assert any(e.body_preview == "Dagordning: genomgång av veckan." for e in ics_events(db, user))


# ----------------------------------------------------------------------------- 9. två källor


def graph_event(db, user, subject="Graph-möte", calendar_id="AAMkAGraphKalender==") -> M.CalendarEvent:
    ev = M.CalendarEvent(user_id=user.id, calendar_id=calendar_id, external_id=f"g-{subject}", subject=subject,
                         start_at=NOW, end_at=NOW + timedelta(hours=1))
    db.add(ev)
    db.flush()
    return ev


def test_graph_retire_unselected_and_disconnect_leave_ics_alone(db, user):
    save(db, user)
    count = len(ics_events(db, user))
    g = graph_event(db, user)
    assert sync.retire_unselected(db, user.id, ["AAMkAnnan=="]) == 1
    assert g.removed_at is not None and len(ics_events(db, user)) == count
    conn = sync.save_credentials(db, user.id, client_id="app-id-123", client_secret="hemligt", tenant="common")
    integrations.set_secrets(conn, {"access_token": "at", "refresh_token": "rt"})
    conn.status = IntegrationStatus.CONNECTED
    db.flush()
    sync.disconnect(db, user.id)
    assert len(ics_events(db, user)) == count
    assert db.execute(select(func.count(M.CalendarEvent.id)).where(M.CalendarEvent.calendar_id != "ics")).scalar_one() == 0


def test_ics_removal_leaves_graph_events(db, user):
    save(db, user)
    g = graph_event(db, user)
    ics.remove_link(db, user.id)
    assert db.get(M.CalendarEvent, g.id) is not None


def test_graph_status_counts_only_graph_and_select_calendars_keeps_ics(client, db, user, monkeypatch):
    monkeypatch.setattr(ics_fetch, "_default_transport", serve(dynamic_calendar()))
    assert client.put(f"{API}/integrations/microsoft/ics", json={"url": LINK}).status_code == 200
    graph_event(db, user, calendar_id="AAMkAGraphKalender==")
    db.commit()
    assert client.get(f"{API}/integrations/microsoft/status").json()["stored_events"] == 1
    r = client.put(f"{API}/integrations/microsoft/calendars", json={"calendar_ids": ["AAMkAnnan=="]})
    assert r.status_code == 200
    assert client.get(f"{API}/integrations/microsoft/ics").json()["stored_events"] == 2


# ----------------------------------------------------------------------------- 10. matchningen


TRIP_START = BASE_TIME  # måndag 14/9 06:00 UTC; resan slutar 08:10 UTC


@pytest.fixture()
def places(trips):
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "branch": trips.location(BRANCH, name="Sjöstadskontoret", type=LocationType.OFFICE),
    }


def match_file(db, user, *events: str) -> None:
    ics.save_link(db, user.id, LINK, now=TRIP_START, transport=serve(calendar(*events)))


def test_ics_meeting_matches_trip_and_classifies_as_work(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130, start_at=TRIP_START)
    match_file(db, user, vevent("m@example.invalid", SUMMARY="Verksamhetsmöte Sjöstad", LOCATION="Sjöstadskontoret",
                                DTSTART__TZID="W. Europe Standard Time:20260914T103000",
                                DTEND__TZID="W. Europe Standard Time:20260914T123000"))
    (m,) = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert m.is_selected and m.relation == "arrival" and m.score > 0.8
    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM)
    assert a.category == Category.WORK and "calendar_match" in {s.kind for s in a.signals}


def test_private_ics_meeting_points_to_private(db, user, trips, places):
    t = trips.trip(HOME, OTHER, km=30, minutes=30, start_at=TRIP_START)
    match_file(db, user, vevent("p@example.invalid", SUMMARY="Tandläkare", LOCATION="Västerstad", CLASS="PRIVATE",
                                DTSTART=(t.ended_at + timedelta(minutes=10)).strftime("%Y%m%dT%H%M%SZ")))
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM)
    assert any(s.kind == "calendar_private" and s.direction == Category.PRIVATE for s in a.signals)


def test_all_day_cancelled_and_free_ics_events_are_not_matched(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130, start_at=TRIP_START)
    at = (t.ended_at + timedelta(minutes=10)).strftime("%Y%m%dT%H%M%SZ")
    match_file(
        db, user,
        vevent("c@example.invalid", SUMMARY="Inställt Sjöstad", LOCATION="Sjöstadskontoret", DTSTART=at,
               STATUS="CANCELLED"),
        vevent("f@example.invalid", SUMMARY="Ledig Sjöstad", LOCATION="Sjöstadskontoret", DTSTART=at,
               X_MICROSOFT_CDO_BUSYSTATUS="FREE"),
        vevent("a@example.invalid", SUMMARY="Heldag Sjöstad", LOCATION="Sjöstadskontoret", DTSTART=at,
               X_MICROSOFT_CDO_ALLDAYEVENT="TRUE"),
    )
    assert matching.match_trip(db, t, user_id=user.id, is_demo=False) == []


# ----------------------------------------------------------------------------- 11. schemat


def connected(db, user, *, last_sync: datetime | None, status=IntegrationStatus.CONNECTED) -> M.IntegrationConnection:
    conn = integrations.get_or_create(db, user.id, ics.PROVIDER)
    integrations.set_secrets(conn, {"url": LINK}, merge=False)
    integrations.update_config(conn, {"host": "calendar.example.com"})
    conn.status = status
    conn.last_sync_at = last_sync
    db.flush()
    return conn


def test_sync_all_due_respects_intervals(db, user, monkeypatch):
    monkeypatch.setattr(ics_fetch, "_default_transport", serve())
    conn = connected(db, user, last_sync=NOW - timedelta(minutes=10))
    assert ics.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 0}
    conn.last_sync_at = NOW - timedelta(minutes=40)
    assert ics.sync_all_due(db, now=NOW) == {"synced": 1, "failed": 0}
    conn.status = IntegrationStatus.ERROR
    conn.last_sync_at = NOW - timedelta(hours=1)
    assert ics.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 0}
    conn.last_sync_at = NOW - timedelta(hours=3)
    assert ics.sync_all_due(db, now=NOW) == {"synced": 1, "failed": 0}
    assert conn.status == IntegrationStatus.CONNECTED


def test_sync_all_due_logs_without_link_and_rematches(db, user, vehicle, monkeypatch, caplog):
    calls: list[str] = []
    monkeypatch.setattr(sync, "rematch_after_sync", lambda db_, uid, now: calls.append("rematch"))
    connected(db, user, last_sync=None)
    monkeypatch.setattr(ics_fetch, "_default_transport", Server(default=httpx.Response(410)).transport())
    with caplog.at_level(logging.DEBUG):
        assert ics.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 1}
    assert "Kalenderlänken kunde inte synkas" in caplog.text and calls == []
    assert_no_secret(caplog.text)
    monkeypatch.setattr(ics_fetch, "_default_transport", serve())
    later = integrations.get(db, user.id, ics.PROVIDER).last_sync_at + timedelta(hours=1)  # felet stämplas med nu
    assert ics.sync_all_due(db, now=later) == {"synced": 1, "failed": 0}
    assert calls == ["rematch"]
    assert_no_secret(caplog.text)


def test_rematch_after_sync_runs_matching_and_reclassification(db, user, vehicle, monkeypatch):
    from app.services.learning import feedback

    calls: list[str] = []
    monkeypatch.setattr(matching, "match_vehicle", lambda db_, *, user_id, vehicle, since=None: calls.append("match"))
    monkeypatch.setattr(feedback, "after_decisions",
                        lambda db_, *, user_id, vehicle_id, actor: calls.append("reclassify"))
    sync.rematch_after_sync(db, user.id, NOW)
    assert calls == ["match", "reclassify"]


def test_graph_sync_all_due_return_value_is_unchanged(db, user):
    assert sync.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 0}


def test_calendar_job_runs_graph_and_ics_and_isolates_errors(engine, monkeypatch):
    from app.worker import tasks

    order: list[str] = []
    monkeypatch.setattr(sync, "sync_all_due", lambda db: order.append("graph") or {"synced": 0, "failed": 0})

    def broken(db):
        order.append("ics")
        raise RuntimeError("trasigt")

    monkeypatch.setattr(ics, "sync_all_due", broken)
    assert tasks.calendar_sync_due() == {"synced": 0, "failed": 0, "ics": {"error": True}}

    def graph_broken(db):
        order.append("graph")
        raise RuntimeError("trasigt")

    monkeypatch.setattr(sync, "sync_all_due", graph_broken)
    monkeypatch.setattr(ics, "sync_all_due", lambda db: order.append("ics") or {"synced": 1, "failed": 0})
    assert tasks.calendar_sync_due() == {"error": True, "ics": {"synced": 1, "failed": 0}}
    assert order == ["graph", "ics", "graph", "ics"]


# ----------------------------------------------------------------------------- 12. larm


def calendar_alerts(db, user, now):
    return alerts._calendar_alerts(db, user.id, now, STHLM)


def test_alert_for_broken_link_without_graph(db, user):
    conn = connected(db, user, last_sync=NOW, status=IntegrationStatus.ERROR)
    conn.last_success_at = NOW - timedelta(days=3)
    (a,) = calendar_alerts(db, user, NOW)
    assert a.title == "Kalendern synkas inte"
    assert a.message == "Kalenderlänken har inte gått att synka. Kontrollera länken under Integrationer → Microsoft Outlook."


def test_alert_for_stale_and_never_synced_link(db, user):
    conn = connected(db, user, last_sync=NOW)
    conn.last_success_at = NOW - timedelta(hours=30)
    (a,) = calendar_alerts(db, user, NOW)
    assert a.message.startswith("Kalenderlänken har inte synkats sedan ") and "försöker igen" in a.message
    conn.last_success_at = None
    (a,) = calendar_alerts(db, user, NOW)
    assert a.message == "Kalenderlänken har inte gått att synka. DriveLog försöker igen av sig självt."


def test_healthy_link_gives_no_alert_and_graph_takes_precedence(db, user):
    conn = connected(db, user, last_sync=NOW)
    conn.last_success_at = NOW - timedelta(hours=1)
    assert calendar_alerts(db, user, NOW) == []
    settings_svc.update_section(db, user.id, "calendar", {"enabled": False})
    conn.last_success_at = None
    assert calendar_alerts(db, user, NOW) == []


def test_alert_run_includes_link_only_users(db, user):
    conn = connected(db, user, last_sync=NOW, status=IntegrationStatus.ERROR)
    conn.last_success_at = NOW - timedelta(days=2)
    stats = alerts.run(db, NOW, force=True)
    assert stats.get("users") == 1


# ----------------------------------------------------------------------------- 13. API


def dynamic_calendar() -> bytes:
    """Två händelser runt nu (API-testerna använder riktig tid)."""
    now = datetime.now(UTC)
    fmt = "%Y%m%dT%H%M%SZ"
    return calendar(
        vevent("a1@example.invalid", SUMMARY="Kundmöte", LOCATION="Hamngatan 5, Västerstad",
               DTSTART=(now - timedelta(days=2)).strftime(fmt), DTEND=(now - timedelta(days=2, hours=-1)).strftime(fmt)),
        vevent("a2@example.invalid", SUMMARY="Planering", DTSTART=(now + timedelta(days=1)).strftime(fmt)),
    )


def api_link(monkeypatch, content: bytes | None = None, response: httpx.Response | None = None) -> Server:
    server = Server(default=response if response is not None else (content or dynamic_calendar()))
    monkeypatch.setattr(ics_fetch, "_default_transport", server.transport())
    return server


def audit_rows(db) -> list[M.AuditEvent]:
    db.expire_all()
    return list(db.execute(select(M.AuditEvent).order_by(M.AuditEvent.at)).scalars())


def test_api_status_without_link(client):
    r = client.get(f"{API}/integrations/microsoft/ics")
    assert r.status_code == 200
    body = r.json()
    assert body["has_link"] is False and body["status"] == "not_configured" and body["host"] is None
    assert set(body) == {"provider", "status", "has_link", "host", "last_sync_at", "last_success_at", "last_error",
                         "last_error_at", "stored_events", "events_with_location", "tip", "stores_body_preview",
                         "sync_days_back", "sync_days_forward"}


def test_api_save_sync_and_remove(client, db, user, monkeypatch, caplog):
    api_link(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        r = client.put(f"{API}/integrations/microsoft/ics", json={"url": LINK})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["has_link"] and body["host"] == "calendar.example.com" and body["status"] == "connected"
        assert body["stored_events"] == 2 and body["events_with_location"] == 1
        assert "has_secrets" not in body and "config" not in body
        assert_no_secret(r.text)

        r = client.get(f"{API}/integrations/microsoft/ics")
        assert r.status_code == 200 and r.json()["has_link"]
        assert_no_secret(r.text)

        r = client.post(f"{API}/integrations/microsoft/ics/sync")
        assert r.status_code == 200, r.text
        sync_body = r.json()
        assert set(sync_body) == {"status", "created", "updated", "removed", "events", "skipped", "truncated", "tip",
                                  "trips_with_match"}
        assert sync_body["status"] == "ok" and sync_body["updated"] == 2 and sync_body["created"] == 0
        assert_no_secret(r.text)

        overview = client.get(f"{API}/integrations")
        assert overview.status_code == 200
        assert_no_secret(overview.text)
        events = client.get(f"{API}/calendar/events", params={"from": (datetime.now(UTC) - timedelta(days=5)).isoformat()})
        assert {e["subject"] for e in events.json()} == {"Kundmöte", "Planering"}

        r = client.delete(f"{API}/integrations/microsoft/ics")
        assert r.status_code == 200
        assert r.json()["has_link"] is False and r.json()["stored_events"] == 0 and r.json()["status"] == "not_configured"
        r = client.delete(f"{API}/integrations/microsoft/ics")  # idempotent
        assert r.status_code == 200
    rows = audit_rows(db)
    actions = [a.action for a in rows if a.entity_type == "integration"]
    assert actions == ["integration.connected", "integration.disconnected"]
    for a in rows:
        assert_no_secret(a.before or {}, a.after or {})
        assert "calendar.example.com" not in json.dumps(a.after or {})
    assert_no_secret(caplog.text)
    conn = integrations.get(db, user.id, ics.PROVIDER)
    runs = db.execute(select(M.IntegrationSyncRun).where(M.IntegrationSyncRun.connection_id == conn.id)).scalars().all()
    assert_no_secret([r.stats for r in runs], [r.error for r in runs])


def test_api_invalid_link_400(client, db, user):
    r = client.put(f"{API}/integrations/microsoft/ics", json={"url": f"http://calendar.example.com/{TOKEN}.ics"})
    assert r.status_code == 400
    assert r.json() == {"detail": "Länken måste börja med https:// eller webcal://.", "code": "invalid_url"} | {
        k: v for k, v in r.json().items() if k not in ("detail", "code")}
    assert_no_secret(r.text)
    r = client.put(f"{API}/integrations/microsoft/ics", json={"url": ""})
    assert r.status_code == 422 and TOKEN not in r.text
    r = client.put(f"{API}/integrations/microsoft/ics", json={"url": f"https://calendar.example.com/{TOKEN}" * 200})
    assert r.status_code == 422 and TOKEN not in r.text


def test_api_fetch_error_502_saves_nothing_and_keeps_previous(client, db, user, monkeypatch, caplog):
    api_link(monkeypatch, response=httpx.Response(404))
    with caplog.at_level(logging.DEBUG):
        r = client.put(f"{API}/integrations/microsoft/ics", json={"url": LINK})
    assert r.status_code == 502 and r.json()["code"] == "ics_http"
    assert "fungerar inte längre" in r.json()["detail"]
    assert_no_secret(r.text, caplog.text)
    db.expire_all()
    assert integrations.get(db, user.id, ics.PROVIDER) is None
    api_link(monkeypatch)
    assert client.put(f"{API}/integrations/microsoft/ics", json={"url": LINK}).status_code == 200
    api_link(monkeypatch, content=b"<html></html>")
    other = "https://other.example.com/owa/calendar/x/ANNANTOKEN/calendar.ics"
    r = client.put(f"{API}/integrations/microsoft/ics", json={"url": other})
    assert r.status_code == 502 and r.json()["code"] == "ics_invalid"
    assert "ANNANTOKEN" not in r.text
    db.expire_all()
    conn = integrations.get(db, user.id, ics.PROVIDER)
    assert integrations.secrets(conn) == {"url": LINK} and conn.last_error is None


def test_api_sync_without_link_409_and_error_502_saved(client, db, user, monkeypatch, caplog):
    r = client.post(f"{API}/integrations/microsoft/ics/sync")
    assert r.status_code == 409 and r.json()["detail"] == "Ingen kalenderlänk är sparad."
    assert r.json()["code"] == "not_configured"
    api_link(monkeypatch)
    client.put(f"{API}/integrations/microsoft/ics", json={"url": LINK})

    def boom(request):
        raise httpx.ConnectError(f"kunde inte ansluta till {request.url}", request=request)

    monkeypatch.setattr(ics_fetch, "_default_transport", httpx.MockTransport(boom))
    with caplog.at_level(logging.DEBUG):
        r = client.post(f"{API}/integrations/microsoft/ics/sync")
    assert r.status_code == 502 and r.json()["code"] == "ics_unreachable"
    assert_no_secret(r.text, caplog.text)
    status_body = client.get(f"{API}/integrations/microsoft/ics").json()
    assert status_body["last_error"] == "Kalenderlänken gick inte att nå. Kontrollera länken och försök igen."
    assert status_body["last_error_at"] is not None and status_body["has_link"]


def test_api_changes_require_web_session(client, anon_client, monkeypatch):
    api_link(monkeypatch)
    token = client.post(f"{API}/auth/tokens", json={"name": "Home Assistant", "scopes": ["ha"]})
    assert token.status_code == 201, token.text
    headers = {"Authorization": f"Bearer {token.json()['token']}"}
    for method, path, body in (("GET", "", None), ("PUT", "", {"url": LINK}), ("DELETE", "", None),
                               ("POST", "/sync", None)):
        r = anon_client.request(method, f"{API}/integrations/microsoft/ics{path}", json=body, headers=headers)
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.json()["code"] in ("insufficient_scope", "session_required")
    for method, path in (("GET", ""), ("PUT", ""), ("DELETE", ""), ("POST", "/sync")):
        assert anon_client.request(method, f"{API}/integrations/microsoft/ics{path}").status_code == 401


def test_api_rematches_after_save(client, db, user, vehicle, monkeypatch):
    calls: list[str] = []
    real = matching.match_vehicle

    def spy(*a, **k):
        calls.append("match")
        return real(*a, **k)

    monkeypatch.setattr(matching, "match_vehicle", spy)
    api_link(monkeypatch)
    assert client.put(f"{API}/integrations/microsoft/ics", json={"url": LINK}).status_code == 200
    assert calls == ["match"]


# ----------------------------------------------------------------------------- 14. databasen från 1.1.0


def test_existing_110_database_needs_no_migration(tmp_path, monkeypatch):
    """1.2.0 har ingen schemaändring: en databas migrerad till 1.1.0:s huvud (0002) räcker,
    och en kalenderlänk med händelser kan sparas i den."""
    from alembic.script import ScriptDirectory

    from alembic import command
    from app import db as dbm
    from app.config import reset_config_cache
    from tests.test_enprocess_sqlite import alembic_config, contract_env

    url = contract_env(monkeypatch, tmp_path / "data")
    try:
        cfg = alembic_config(url)
        # 1.2.2 lade till 0003 (omräkning efter beslut); 1.2.0 i sig behövde ingen migrering.
        assert ScriptDirectory.from_config(cfg).get_revision("0003").down_revision == "0002"
        command.upgrade(cfg, "0002")
        with dbm.session_scope() as db:
            u = M.User(email="forare@example.com", display_name="Förare", password_hash="x", is_admin=True)
            db.add(u)
            db.flush()
            out = ics.save_link(db, u.id, LINK, now=NOW, transport=serve())
            assert out["has_link"] and out["stored_events"] == 10
        with dbm.session_scope() as db:
            assert db.execute(select(func.count(M.CalendarEvent.id)).where(
                M.CalendarEvent.calendar_id == "ics")).scalar_one() == 10
    finally:
        dbm.dispose_engine()
        reset_config_cache()
