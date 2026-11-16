"""S1a: adresser för bilens stopp (omvänd geokodning), byggkontraktet A.1, C.1 och E.1.

Inga riktiga nätverksanrop: geokodarens transport byts ut mot en MockTransport, och
DNS-svaren ersätts. Adresser och namn är påhittade.
"""

from __future__ import annotations

import gzip
import json
import socket
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app import __version__
from app.db import utcnow
from app.models.enums import GeocodeStatus, LocationType, TripSource
from app.services import settings as settings_svc
from app.services.places import geocode
from app.services.places import registry as places
from app.services.trips import segments as seg
from tests.test_volvo_support import T0, A, B, FakeCar, connect, poll, poll_every

PROVIDER = "https://nominatim.example.org"

DAGAB = {
    "place_id": 1,
    "category": "building",
    "type": "warehouse",
    "name": "Dagab",
    "display_name": "Dagab, 5, Exempelgatan, Exempelstad, 123 45, Sverige",
    "address": {
        "house_number": "5",
        "road": "Exempelgatan",
        "city": "Exempelstad",
        "postcode": "123 45",
        "country": "Sverige",
    },
}


class FakeStream:
    """Ersätter httpcore:s nätverksström så att motpartens adress kan prövas."""

    def __init__(self, ip: str) -> None:
        self.ip = ip

    def get_extra_info(self, info: str) -> Any:
        return (self.ip, 443) if info == "server_addr" else None


class Recorder:
    """MockTransport-hanterare som sparar anropen och svarar med en vald kropp."""

    def __init__(self, body: Any = DAGAB, *, status: int = 200, headers: dict[str, str] | None = None,
                 stream: httpx.SyncByteStream | None = None, extensions: dict[str, Any] | None = None) -> None:
        self.body, self.status, self.headers = body, status, headers
        self.stream, self.extensions = stream, extensions
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"content-type": "application/json; charset=utf-8", **(self.headers or {})}
        if self.stream is not None:
            return httpx.Response(self.status, headers=headers, stream=self.stream, extensions=self.extensions or {})
        content = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        # Som en riktig transport: kroppen strömmas och läses inte i förväg.
        return httpx.Response(self.status, headers=headers, stream=httpx.ByteStream(content),
                              extensions=self.extensions or {})


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch) -> list[float]:
    """Takten prövas med en påhittad monoton klocka i stället för riktiga pauser."""
    clock = {"t": 1000.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(geocode, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(geocode, "_sleep", sleep)
    monkeypatch.setattr(geocode, "_last_call", None)
    return sleeps


@pytest.fixture()
def no_dns(monkeypatch) -> None:
    def fail(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - ska aldrig anropas
        raise AssertionError("DNS-uppslag i testet")

    monkeypatch.setattr(geocode, "_getaddrinfo", fail)


def use(monkeypatch, handler: Recorder) -> Recorder:
    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(handler))
    return handler


def enable(db, user, *, url: str = PROVIDER) -> None:
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True, "geocoder_url": url})


def home(db, user, pos: tuple[float, float] = A) -> M.KnownLocation:
    loc = places.resolve_address(db, user.id, "Hemvägen 1, Exempelstad", created_from="user")
    assert loc is not None
    loc.type, loc.confirmed, loc.name = LocationType.HOME, True, "Hemmet"
    loc.latitude, loc.longitude, loc.coordinate_source = pos[0], pos[1], "user"
    db.flush()
    return loc


def pending_place(db, user, pos: tuple[float, float] = B) -> M.KnownLocation:
    loc = geocode.place_for_position(db, user_id=user.id, lat=pos[0], lon=pos[1], demo=False)
    assert loc is not None and loc.geocode_status == GeocodeStatus.PENDING
    return loc


def live_trip(db, vehicle, start: M.KnownLocation | None, end: M.KnownLocation) -> M.LogicalTrip:
    raw = M.RawTrip(
        vehicle_id=vehicle.id, source=TripSource.VOLVO_API, source_key=f"k-{end.id}",
        ended_at=T0, start_time_quality="bounded", end_time_quality="reported",
        start_earliest=T0 - timedelta(minutes=40), start_latest=T0 - timedelta(minutes=10),
        odometer_start=1000, odometer_end=1014, distance_km=14, distance_quality="odometer",
        start_address=geocode.address_for(start), end_address=geocode.address_for(end),
        start_lat=A[0], start_lon=A[1], end_lat=end.latitude, end_lon=end.longitude,
        start_location_id=start.id if start else None, end_location_id=end.id,
        updated_by="system",
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    return seg.create_logical_for_raw(db, raw, actor="system")


def reload(db, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


# --------------------------------------------------------------------------- detekteringen


@pytest.mark.new_defaults
def test_detection_creates_pending_place_without_network(db, user, vehicle, offline_geocoder):
    connect(db, user, vehicle)
    car = FakeCar()
    home(db, user)
    poll_every(db, vehicle, car, T0, T0 + timedelta(minutes=5))
    car.moving(5, T0 + timedelta(minutes=9))
    poll(db, vehicle, car, T0 + timedelta(minutes=10))
    car.drive_to(B, 9, T0 + timedelta(minutes=18))
    poll_every(db, vehicle, car, T0 + timedelta(minutes=20), T0 + timedelta(minutes=40))

    (raw,) = db.execute(select(M.RawTrip)).scalars().all()
    end = db.get(M.KnownLocation, raw.end_location_id)
    assert end is not None
    assert end.type == LocationType.UNKNOWN and end.confirmed is False
    assert end.created_from == "api" and end.coordinate_source == "api" and end.radius_m == 75
    assert end.geocode_status == GeocodeStatus.PENDING
    assert (end.latitude, end.longitude) == B
    assert raw.end_address is None  # ingen adress hittas på
    assert db.get(M.KnownLocation, raw.start_location_id).type == LocationType.HOME
    assert offline_geocoder.requests == []


def test_manual_without_geocoding_is_unchanged(db, user):
    # legacy_defaults: läget manual och geokodningen av → som före 1.1.0, ingen ny plats.
    assert geocode.place_for_position(db, user_id=user.id, lat=B[0], lon=B[1], demo=False) is None
    assert db.execute(select(M.KnownLocation)).first() is None


@pytest.mark.new_defaults
def test_known_place_is_reused_and_demo_never_looks_up(db, user):
    h = home(db, user)
    assert geocode.place_for_position(db, user_id=user.id, lat=A[0], lon=A[1] + 0.0003, demo=False).id == h.id
    demo = geocode.place_for_position(db, user_id=user.id, lat=B[0], lon=B[1], demo=True)
    assert demo is not None and demo.is_demo and demo.geocode_status == GeocodeStatus.OFF


@pytest.mark.new_defaults
def test_geocoding_off_in_self_driving_mode_gives_off_place(db, user):
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    loc = geocode.place_for_position(db, user_id=user.id, lat=B[0], lon=B[1], demo=False)
    assert loc is not None and loc.geocode_status == GeocodeStatus.OFF and loc.address is None


# --------------------------------------------------------------------------- run_pending


def test_run_pending_fills_address_kind_and_company(db, user, vehicle, monkeypatch, no_dns):
    enable(db, user)
    h = home(db, user)
    loc = pending_place(db, user)
    trip = live_trip(db, vehicle, h, loc)
    db.commit()
    rec = use(monkeypatch, Recorder())

    out = geocode.run_pending(now=utcnow())

    assert out.get("looked_up") == 1 and out.get("ok") == 1
    loc = reload(db, loc)
    assert loc.geocode_status == GeocodeStatus.OK and loc.geocoded_at is not None
    assert loc.address == "Exempelgatan 5, 123 45 Exempelstad"  # utan POI-namn
    assert loc.name == "Dagab" and loc.city == "Exempelstad"
    assert loc.company == "Dagab" and loc.place_kind == "work" and loc.osm_class == "building=warehouse"
    assert loc.type == LocationType.UNKNOWN and loc.confirmed is False  # aldrig type eller confirmed
    trip = db.get(M.LogicalTrip, trip.id)
    assert trip.end_address == "Exempelgatan 5, 123 45 Exempelstad"
    raw = db.execute(select(M.RawTrip)).scalar_one()
    assert raw.end_address == trip.end_address and raw.start_address == "Hemvägen 1, Exempelstad"
    cache = db.execute(select(M.GeocodeCache)).scalar_one()
    assert cache.status == GeocodeStatus.OK and cache.provider_host == "nominatim.example.org"
    assert (cache.lat_key, cache.lon_key) == (590900, 150000)
    assert len(rec.requests) == 1


def test_request_contains_only_the_cell_and_a_neutral_user_agent(db, user, vehicle, monkeypatch, no_dns):
    enable(db, user)
    home(db, user)
    pending_place(db, user, (59.123456, 15.987654))
    db.commit()
    rec = use(monkeypatch, Recorder())
    geocode.run_pending(now=utcnow())

    (req,) = rec.requests
    assert req.method == "GET" and req.url.scheme == "https" and req.url.host == "nominatim.example.org"
    assert req.url.path == "/reverse"
    assert dict(req.url.params) == {
        "lat": "59.1235", "lon": "15.9877", "format": "jsonv2", "zoom": "18",
        "addressdetails": "1", "accept-language": "sv",
    }
    ua = req.headers["user-agent"]
    assert ua == geocode.USER_AGENT == f"SMGD-DriveLog/{__version__} (self-hosted logbook; reverse geocoding)"
    assert "@" not in ua and user.email not in ua and "forare" not in ua.lower()
    assert "referer" not in req.headers and "cookie" not in req.headers and "authorization" not in req.headers
    assert req.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("category", "typ", "kind"),
    [
        ("office", "company", "work"), ("craft", "carpenter", "work"), ("industrial", "factory", "work"),
        ("man_made", "works", "work"), ("building", "warehouse", "work"), ("building", "office", "work"),
        ("landuse", "industrial", "work"),
        ("shop", "supermarket", "never"), ("amenity", "fuel", "never"), ("leisure", "park", "never"),
        ("tourism", "hotel", "never"), ("healthcare", "clinic", "never"), ("building", "house", "never"),
        ("building", "apartments", "never"), ("landuse", "residential", "never"), ("place", "house", "never"),
        ("building", "yes", "unclear"), ("highway", "residential", "unclear"), (None, None, "unclear"),
    ],
)
def test_place_kind_table(category, typ, kind):
    assert geocode.classify_place(category, typ) == kind


def test_parse_response_shop_has_no_company_and_suggests_type():
    data = {**DAGAB, "category": "amenity", "type": "fuel", "name": "Exempel Bensin"}
    addr = geocode.parse_response(data)
    assert addr is not None and addr.place_kind == "never" and addr.poi_name == "Exempel Bensin"
    # En gata är inget POI-namn.
    road = geocode.parse_response({**DAGAB, "category": "highway", "type": "residential", "name": "Exempelgatan"})
    assert road is not None and road.poi_name is None and road.place_kind == "unclear"
    assert geocode.parse_response({"error": "Unable to geocode"}) is None
    assert geocode.parse_response({"category": "building", "type": "yes", "address": {}}) is None


def test_parse_response_sanitizes_text():
    rlo, zwsp, ls, nul = chr(0x202E), chr(0x200B), chr(0x2028), chr(0)
    data = {
        **DAGAB,
        "name": f"Da{rlo}gab{zwsp}\nAB",
        "address": {**DAGAB["address"], "road": f"Exempel{ls}gatan{nul}", "extra": "x" * 9000},
    }
    addr = geocode.parse_response(data)
    assert addr is not None
    assert addr.poi_name == "Dagab AB"
    assert addr.street == "Exempelgatan"  # U+2028 och NUL tas bort
    assert rlo not in addr.label and nul not in addr.label and ls not in addr.label
    # En ogiltig kategori sparas inte, och utan kategori finns inget POI-namn.
    odd = geocode.parse_response({**DAGAB, "category": f"building{rlo}"})
    assert odd is not None and odd.osm_class is None and odd.poi_name is None and odd.place_kind == "unclear"


def test_fuel_station_is_only_a_suggestion(db, user, vehicle, monkeypatch, no_dns):
    enable(db, user)
    home(db, user)
    loc = pending_place(db, user)
    db.commit()
    use(monkeypatch, Recorder({**DAGAB, "category": "amenity", "type": "fuel", "name": "Exempel Bensin"}))
    geocode.run_pending(now=utcnow())
    loc = reload(db, loc)
    assert loc.suggested_type == LocationType.FUEL_STATION and loc.type == LocationType.UNKNOWN
    assert loc.company is None and loc.place_kind == "never" and loc.confirmed is False


def test_no_request_before_home_has_coordinate(db, user, monkeypatch, no_dns):
    enable(db, user)
    loc = pending_place(db, user)
    db.commit()
    rec = use(monkeypatch, Recorder())
    out = geocode.run_pending(now=utcnow())
    assert rec.requests == [] and out.get("waiting_for_home") == 1
    assert reload(db, loc).geocode_status == GeocodeStatus.PENDING

    # Hemmet utan koordinat räcker inte heller.
    h = home(db, user)
    h.latitude = h.longitude = None
    db.commit()
    geocode.run_pending(now=utcnow())
    assert rec.requests == []

    h = reload(db, h)
    h.latitude, h.longitude = A
    db.commit()
    geocode.run_pending(now=utcnow())
    assert len(rec.requests) == 1


def test_geocoding_turned_off_sets_off_without_request(db, user, monkeypatch, no_dns):
    enable(db, user)
    home(db, user)
    loc = pending_place(db, user)
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    db.commit()
    rec = use(monkeypatch, Recorder())
    geocode.run_pending(now=utcnow())
    assert rec.requests == [] and reload(db, loc).geocode_status == GeocodeStatus.OFF


def test_cache_same_cell_is_never_looked_up_twice(db, user, monkeypatch, no_dns):
    enable(db, user)
    home(db, user)
    first = pending_place(db, user, (59.09001, 15.00001))
    db.commit()
    rec = use(monkeypatch, Recorder({"error": "Unable to geocode"}))
    geocode.run_pending(now=utcnow())
    assert reload(db, first).geocode_status == GeocodeStatus.NOT_FOUND
    assert reload(db, first).address is None

    # Ny plats i samma cell (en annan dag, utanför den förstas radie): svaret tas ur cachen.
    second = M.KnownLocation(
        owner_user_id=user.id, name=geocode.UNRESOLVED_NAME, type=LocationType.UNKNOWN, created_from="api",
        latitude=59.09003, longitude=15.00004, radius_m=75, geocode_status=GeocodeStatus.PENDING,
    )
    db.add(second)
    db.commit()
    out = geocode.run_pending(now=utcnow())
    assert len(rec.requests) == 1 and out.get("from_cache") == 1
    assert reload(db, second).geocode_status == GeocodeStatus.NOT_FOUND


def test_two_places_in_same_cell_give_one_request(db, user, monkeypatch, no_dns):
    enable(db, user)
    home(db, user)
    for lon in (15.00001, 15.00003):
        db.add(M.KnownLocation(
            owner_user_id=user.id, name=geocode.UNRESOLVED_NAME, type=LocationType.UNKNOWN, created_from="api",
            latitude=59.09001, longitude=lon, radius_m=75, geocode_status=GeocodeStatus.PENDING,
        ))
    db.commit()
    rec = use(monkeypatch, Recorder())
    out = geocode.run_pending(now=utcnow())
    assert len(rec.requests) == 1 and out.get("ok") == 2


def test_rate_limit_one_call_per_1_1_seconds(db, user, monkeypatch, no_dns, fast_clock):
    enable(db, user)
    home(db, user)
    for i in range(3):
        pending_place(db, user, (59.1 + i * 0.01, 15.0))
    db.commit()
    rec = use(monkeypatch, Recorder())
    geocode.run_pending(now=utcnow())
    assert len(rec.requests) == 3
    assert len(fast_clock) == 2 and all(abs(s - 1.1) < 1e-9 for s in fast_clock)


def test_limit_per_run_and_per_day(db, user, monkeypatch, no_dns):
    enable(db, user)
    home(db, user)
    for i in range(4):
        pending_place(db, user, (59.1 + i * 0.01, 15.0))
    db.commit()
    rec = use(monkeypatch, Recorder())
    geocode.run_pending(now=utcnow(), limit=2)
    assert len(rec.requests) == 2

    # Dygnstaket: 500 uppslag det senaste dygnet stoppar fler anrop.
    now = utcnow()
    for i in range(geocode.DAILY_LIMIT):
        db.add(M.GeocodeCache(owner_user_id=user.id, provider_host="annan.example.org", lat_key=i, lon_key=0,
                              status="ok", fetched_at=now - timedelta(hours=1), attempts=1))
    db.commit()
    geocode.run_pending(now=now)
    assert len(rec.requests) == 2


def test_failed_lookup_is_retried_on_schedule(db, user, offline_geocoder, no_dns):
    enable(db, user)
    home(db, user)
    loc = pending_place(db, user)
    db.commit()
    t = utcnow()
    out = geocode.run_pending(now=t)
    assert out.get("failed") == 1 and len(offline_geocoder.requests) == 1
    assert reload(db, loc).geocode_status == GeocodeStatus.FAILED and reload(db, loc).address is None
    cache = db.execute(select(M.GeocodeCache)).scalar_one()
    assert cache.attempts == 1 and cache.next_attempt_at == t + timedelta(minutes=5)

    geocode.run_pending(now=t + timedelta(minutes=4))
    assert len(offline_geocoder.requests) == 1
    for at in (t + timedelta(minutes=6), t + timedelta(hours=2), t + timedelta(hours=9), t + timedelta(hours=34)):
        geocode.run_pending(now=at)
    assert len(offline_geocoder.requests) == 5
    cache = reload(db, cache)
    # Efter de fem snabba försöken: ett försök per dygn (karttjänsten kan vara nere länge).
    assert cache.attempts == 5 and cache.next_attempt_at == t + timedelta(hours=34) + geocode.SLOW_DELAY
    geocode.run_pending(now=t + timedelta(hours=50))
    assert len(offline_geocoder.requests) == 5
    geocode.run_pending(now=t + timedelta(hours=60))
    assert len(offline_geocoder.requests) == 6


def test_match_by_address_merges_with_imported_place(db, user, vehicle, monkeypatch, no_dns):
    enable(db, user)
    h = home(db, user)
    imported = places.resolve_address(db, user.id, "Exempelgatan 5, Exempelstad", created_from="import")
    assert imported is not None and imported.latitude is None
    loc = pending_place(db, user)
    trip = live_trip(db, vehicle, h, loc)
    db.commit()
    use(monkeypatch, Recorder())

    out = geocode.run_pending(now=utcnow())

    assert out.get("merged") == 1
    loc, imported = reload(db, loc), db.get(M.KnownLocation, imported.id)
    assert loc.merged_into_id == imported.id
    assert (imported.latitude, imported.longitude) == B and imported.coordinate_source == "api"
    raw = db.execute(select(M.RawTrip)).scalar_one()
    assert raw.end_location_id == imported.id and raw.end_address == "Exempelgatan 5, Exempelstad"
    trip = db.get(M.LogicalTrip, trip.id)
    assert trip.end_location_id == imported.id and trip.end_address == "Exempelgatan 5, Exempelstad"
    assert imported.name == "Exempelgatan 5, Exempelstad"  # importens namn behålls


def test_reconsider_is_called_with_the_trips(db, user, vehicle, monkeypatch, no_dns):
    from app.services.classify import autopilot

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(autopilot, "reconsider", lambda db, **kw: calls.append(kw))
    enable(db, user)
    h = home(db, user)
    loc = pending_place(db, user)
    trip = live_trip(db, vehicle, h, loc)
    db.commit()
    use(monkeypatch, Recorder())
    geocode.run_pending(now=utcnow())
    assert len(calls) == 1 and calls[0]["user_id"] == user.id and calls[0]["trip_ids"] == [trip.id]


# --------------------------------------------------------------------------- URL-skydd


@pytest.mark.parametrize(
    "url",
    [
        "http://nominatim.example.org",
        "https://0.0.0.0",
        "https://10.1.2.3",
        "https://100.64.0.1",
        "https://[::ffff:127.0.0.1]",
        "https://169.254.169.254",
        "https://localhost",
        "https://nominatim.example.org:8443",
        "https://user:pw@nominatim.example.org",
        "https://nominatim.example.org/?q=1",
        "https://nominatim.example.org/#x",
        "https://" + "a" * 600 + ".example.org",
    ],
)
def test_url_guard_rejects_static(url):
    with pytest.raises(ValueError):
        geocode.validate_runtime_url(url, resolve=False)
    with pytest.raises(ValueError):
        geocode.reverse(provider_url=url, lat_key=590900, lon_key=150000, transport=httpx.MockTransport(Recorder()))


@pytest.mark.parametrize("answer", ["10.0.0.5", "127.0.0.1", "::ffff:127.0.0.1", "169.254.169.254", "224.0.0.1", "100.64.1.1"])
def test_url_guard_checks_every_dns_answer(monkeypatch, answer):
    family = socket.AF_INET6 if ":" in answer else socket.AF_INET

    def fake(host, port, **_k):
        assert host == "nominatim.example.org" and port == 443
        return [(family, socket.SOCK_STREAM, 6, "", (answer, 443))]

    monkeypatch.setattr(geocode, "_getaddrinfo", fake)
    with pytest.raises(ValueError):
        geocode.validate_runtime_url(PROVIDER, resolve=True)


def test_url_guard_dns_failure(monkeypatch):
    def fail(*_a, **_k):
        raise socket.gaierror("ingen sådan värd")

    monkeypatch.setattr(geocode, "_getaddrinfo", fail)
    with pytest.raises(ValueError):
        geocode.validate_runtime_url(PROVIDER, resolve=True)


def test_real_transport_checks_dns_before_connecting(monkeypatch):
    # Utan utbytt transport prövas DNS-svaret först; en lokal adress stoppar anropet.
    monkeypatch.setattr(geocode, "_default_transport", None)
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda *_a, **_k: [(socket.AF_INET, 1, 6, "", ("10.0.0.5", 443))])

    def no_transport():  # pragma: no cover - ska aldrig anropas
        raise AssertionError("anslutning trots lokal adress")

    monkeypatch.setattr(geocode, "_real_transport", no_transport)
    with pytest.raises(ValueError):
        geocode.reverse(provider_url=PROVIDER, lat_key=590900, lon_key=150000)


def reverse_with(handler: Recorder) -> geocode.GeoAddress | None:
    return geocode.reverse(provider_url=PROVIDER, lat_key=590900, lon_key=150000, transport=httpx.MockTransport(handler))


def test_reverse_ok():
    addr = reverse_with(Recorder())
    assert addr is not None and addr.label == "Exempelgatan 5, 123 45 Exempelstad" and addr.poi_name == "Dagab"


def test_redirect_is_not_followed():
    rec = Recorder(b"", status=302, headers={"location": "https://10.0.0.1/reverse"})
    with pytest.raises(geocode.GeocodeError):
        reverse_with(rec)
    assert len(rec.requests) == 1


class SlowStream(httpx.SyncByteStream):
    def __init__(self, clock_advance) -> None:
        self.advance = clock_advance

    def __iter__(self):
        for _ in range(10):
            self.advance(1.0)  # varje bit tar en sekund
            yield b" "
        yield json.dumps(DAGAB).encode()


def test_slow_server_hits_deadline(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(geocode, "_monotonic", lambda: clock["t"])

    def advance(s: float) -> None:
        clock["t"] += s

    with pytest.raises(geocode.GeocodeError, match="i tid"):
        reverse_with(Recorder(stream=SlowStream(advance)))


def test_gzip_bomb_is_rejected():
    bomb = gzip.compress(b"0" * 5_000_000)
    with pytest.raises(geocode.GeocodeError):
        reverse_with(Recorder(bomb, headers={"content-encoding": "gzip"}))


def test_body_over_64_kb_is_rejected():
    big = {**DAGAB, "extratags": {"x": "a" * 70_000}}
    with pytest.raises(geocode.GeocodeError, match="för stort"):
        reverse_with(Recorder(big))


def test_non_json_is_rejected():
    with pytest.raises(geocode.GeocodeError):
        reverse_with(Recorder(b"<html></html>", headers={"content-type": "text/html"}))
    with pytest.raises(geocode.GeocodeError):
        reverse_with(Recorder(b"[1, 2]"))


@pytest.mark.parametrize("peer", ["10.0.0.1", "127.0.0.1", "::ffff:127.0.0.1", "169.254.169.254"])
def test_peer_address_is_checked(peer):
    with pytest.raises(geocode.GeocodeError):
        reverse_with(Recorder(extensions={"network_stream": FakeStream(peer)}))


def test_peer_address_required_for_real_transport(monkeypatch):
    # Den riktiga transporten måste kunna visa motpartens adress; annars avbryts anropet.
    monkeypatch.setattr(geocode, "_default_transport", None)
    monkeypatch.setattr(geocode, "validate_runtime_url", lambda url, resolve=True: PROVIDER)
    monkeypatch.setattr(geocode, "_real_transport", lambda: httpx.MockTransport(Recorder()))
    with pytest.raises(geocode.GeocodeError, match="Motpartens"):
        geocode.reverse(provider_url=PROVIDER, lat_key=590900, lon_key=150000)


def test_offline_failure_becomes_geocode_error(offline_geocoder):
    with pytest.raises(geocode.GeocodeError):
        geocode.reverse(provider_url=PROVIDER, lat_key=590900, lon_key=150000)
    assert len(offline_geocoder.requests) == 1


def test_default_resolve_follows_transport(monkeypatch, no_dns):
    # Med utbytt transport (testerna) görs inget DNS-uppslag; den riktiga gör alltid ett.
    assert geocode.validate_runtime_url(PROVIDER + "/") == PROVIDER
    monkeypatch.setattr(geocode, "_default_transport", None)
    with pytest.raises(AssertionError, match="DNS-uppslag"):
        geocode.validate_runtime_url(PROVIDER)


def test_nothing_to_do_returns_empty(db, user):
    db.commit()
    assert geocode.run_pending(now=utcnow()) == {}
