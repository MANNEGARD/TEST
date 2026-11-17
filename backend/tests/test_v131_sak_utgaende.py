"""1.3.1 säkerhet: utgående anrop (kalenderlänken, geokodaren, Graph, Volvo, Home Assistant)
och sammanslagning via adressalias.

Inga riktiga nätverksanrop: transporter byts mot MockTransport, och DNS och sockets är
utbytta. Alla adresser, VIN och nycklar är påhittade.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from typing import Any

import httpcore
import httpx
import pytest

import app.models as M
from app.db import utcnow
from app.models.enums import GeocodeStatus, LocationType
from app.services import settings as settings_svc
from app.services.calendar import graph, ics_fetch
from app.services.ha import client as ha_client
from app.services.places import geocode
from app.services.places import registry as places
from app.services.volvo import client as vc

# ----------------------------------------------------------------------------- _check_ip (utgaende-4)


@pytest.mark.parametrize(
    "value",
    [
        "::7f00:1",  # IPv4-kompatibel 127.0.0.1
        "::a9fe:a9fe",  # IPv4-kompatibel 169.254.169.254
        "64:ff9b::7f00:1",  # NAT64 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 169.254.169.254
        "64:ff9b::a00:5",  # NAT64 10.0.0.5
        "::ffff:0:a9fe:a9fe",  # SIIT 169.254.169.254
        "fec0::1",  # föråldrad site-local
    ],
)
def test_embedded_internal_ipv4_and_site_local_are_refused(value):
    with pytest.raises(ValueError):
        geocode._check_ip(value)


@pytest.mark.parametrize("value", ["64:ff9b::808:808", "2606:4700::1111", "93.184.216.34", "::ffff:93.184.216.34"])
def test_global_addresses_are_still_allowed(value):
    geocode._check_ip(value)


# ------------------------------------------------------------- anslutning till prövad adress (utgaende-3)


class _FakeSocket:
    def __init__(self, address):
        self.address = address

    def setsockopt(self, *args):
        pass

    def close(self):
        pass


def test_real_transport_checks_the_address_before_connecting(monkeypatch):
    """Namnet slås upp en gång i anslutningssteget; en intern adress ansluts aldrig till."""
    connects: list[tuple[str, int]] = []
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
    ])
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: connects.append(addr) or _FakeSocket(addr))
    backend = geocode._real_transport()._pool._network_backend
    with pytest.raises(geocode.GeocodeError):
        backend.connect_tcp("kalender.example.com", 443, timeout=1.0)
    assert connects == []


def test_real_transport_connects_to_the_checked_address(monkeypatch):
    connects: list[tuple[str, int]] = []
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
    ])
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: connects.append(addr) or _FakeSocket(addr))
    backend = geocode._real_transport()._pool._network_backend
    stream = backend.connect_tcp("kalender.example.com", 443, timeout=1.0)
    assert connects == [("93.184.216.34", 443)]
    assert stream is not None


def test_real_transport_tries_the_next_address_like_before(monkeypatch):
    connects: list[tuple[str, int]] = []
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
    ])

    def create(addr, *a, **k):
        connects.append(addr)
        if ":" in addr[0]:
            raise OSError("inget IPv6")
        return _FakeSocket(addr)

    monkeypatch.setattr(socket, "create_connection", create)
    backend = geocode._real_transport()._pool._network_backend
    backend.connect_tcp("kalender.example.com", 443, timeout=1.0)
    assert connects == [("2606:4700::1111", 443), ("93.184.216.34", 443)]


def test_unresolvable_name_is_a_connect_error(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("okänt namn")

    monkeypatch.setattr(geocode, "_getaddrinfo", fail)
    backend = geocode._real_transport()._pool._network_backend
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("kalender.example.com", 443, timeout=1.0)


def test_calendar_link_rebinding_gives_the_local_address_text(monkeypatch):
    """Första uppslaget (kontrollen) ger en global adress, andra (anslutningen) en intern."""
    answers = iter([
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))],
    ])
    connects: list[Any] = []
    monkeypatch.setattr(ics_fetch, "_default_transport", None)
    monkeypatch.setattr(ics_fetch, "_getaddrinfo", lambda *a, **k: next(answers))
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda *a, **k: next(answers))
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: connects.append(addr) or _FakeSocket(addr))
    with pytest.raises(ics_fetch.IcsError) as err:
        ics_fetch.fetch("https://kalender.example.com/owa/calendar/abc/reachcalendar.ics")
    assert err.value.message == ics_fetch.MSG_LOCAL
    assert connects == []


# ----------------------------------------------------------------- Graph: nextLink (utgaende-5)


def _graph_router(seen: list[httpx.Request], next_link: str):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host != "graph.microsoft.com":
            return httpx.Response(200, json={"value": [{"id": "stulen", "subject": "fel"}]})
        if "skip" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "sida2", "subject": "Möte 2"}]})
        return httpx.Response(200, json={"value": [{"id": "sida1", "subject": "Möte 1"}], "@odata.nextLink": next_link})

    return httpx.MockTransport(handler)


def test_graph_next_link_to_another_host_is_not_followed():
    seen: list[httpx.Request] = []
    client = graph.GraphClient("graph-token-pahittad", transport=_graph_router(seen, "https://evil.example/x"))
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)
    events = client.calendar_view("kal-1", start, end)
    calendars = client.list_calendars()
    assert [e["id"] for e in events] == ["sida1"]
    assert [c["id"] for c in calendars] == ["sida1"]
    assert all(r.url.host == "graph.microsoft.com" for r in seen)


def test_graph_next_link_on_graph_is_followed_as_before():
    seen: list[httpx.Request] = []
    nxt = f"{graph.GRAPH_BASE}/me/calendars/kal-1/calendarView?$skip=200"
    client = graph.GraphClient("graph-token-pahittad", transport=_graph_router(seen, nxt))
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)
    events = client.calendar_view("kal-1", start, end)
    assert [e["id"] for e in events] == ["sida1", "sida2"]


# ------------------------------------------------------------------ Volvo: VIN i sökvägen (utgaende-6)

GOOD_VIN = "YV1ZZZ98765432100"


def _volvo(handler) -> vc.VolvoClient:
    return vc.VolvoClient(access_token="AT-pahittad", api_key="nyckel-pahittad", transport=httpx.MockTransport(handler))


def test_vehicle_list_skips_entries_that_are_not_vins(monkeypatch):
    monkeypatch.setattr(vc, "_sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"vin": GOOD_VIN}, {"vin": "../../location/v1/vehicles/x"}, {"vin": "A?b=1"}, {"vin": "kort"},
        ]})

    with _volvo(handler) as api:
        assert api.list_vehicles() == [GOOD_VIN]


def test_vin_is_quoted_in_the_path(monkeypatch):
    monkeypatch.setattr(vc, "_sleep", lambda s: None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {}})

    with _volvo(handler) as api:
        api.vehicle_details("A?b=1")
        api.fetch("odometer", "../x")
        api.vehicle_details(GOOD_VIN)
    assert seen[0].url.raw_path.decode().endswith("/vehicles/A%3Fb%3D1")
    assert seen[1].url.raw_path.decode().endswith("/vehicles/..%2Fx/odometer")
    assert seen[2].url.path.endswith(f"/vehicles/{GOOD_VIN}")


# --------------------------------------------------------------- Volvo: felmeddelandet (logik-7)


def test_volvo_error_message_masks_vin_and_strips_control_characters():
    resp = httpx.Response(404, json={"error": {"message": f"vehicle {GOOD_VIN} not found\x07"}})
    msg = vc._error_message(resp)
    assert GOOD_VIN not in msg
    assert "\x07" not in msg
    assert msg == "vehicle …2100 not found"


def test_volvo_error_message_without_vin_is_unchanged():
    resp = httpx.Response(403, json={"error": {"message": "Access denied for this resource"}})
    assert vc._error_message(resp) == "Access denied for this resource"


# ----------------------------------------------------------- storleksgräns på svar (utgaende-9)


def _big(n: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(b"[" + b"1," * (n // 2) + b"1]"))

    return httpx.MockTransport(handler)


def test_home_assistant_response_has_a_size_limit():
    with ha_client.HAClient("http://ha.example.lan:8123", "ha-token-pahittad",
                            transport=_big(ha_client.MAX_RESPONSE_BYTES + 10)) as ha:
        with pytest.raises(ha_client.HAError) as err:
            ha.notify_services()
    assert err.value.kind == "invalid"


def test_home_assistant_normal_response_is_read_as_before():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"domain": "notify", "services": {"mobile_app_telefon": {}}}])

    with ha_client.HAClient("http://ha.example.lan:8123", "ha-token-pahittad",
                            transport=httpx.MockTransport(handler)) as ha:
        assert ha.notify_services() == ["mobile_app_telefon"]


def test_volvo_response_has_a_size_limit(monkeypatch):
    monkeypatch.setattr(vc, "_sleep", lambda s: None)
    with vc.VolvoClient(access_token="AT-pahittad", api_key="nyckel-pahittad",
                        transport=_big(vc.MAX_RESPONSE_BYTES + 10)) as api:
        result = api.vehicle_details(GOOD_VIN)
    assert result.status == "error"


# ------------------------------------------- sammanslagning via adressalias (lucka4-…-1 och -n1)

PROVIDER = "https://nominatim.example.org"
HOME_POS = (59.3301, 18.0688)
FAR_POS = (57.700012, 11.970034)  # en annan ort, ca 400 km bort
STREET = {
    "place_id": 1, "category": "building", "type": "house", "name": "",
    "address": {"house_number": "1", "road": "Hemvägen", "city": "Exempelstad", "postcode": "111 11"},
}


@pytest.fixture()
def geo(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(geocode, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(geocode, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(geocode, "_last_call", None)
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda *a, **k: pytest.fail("DNS-uppslag i testet"))

    def handler(request: httpx.Request) -> httpx.Response:
        body: Any = [] if request.url.path.endswith("/search") else STREET
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(body).encode()))

    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(handler))


def _enable(db, user) -> None:
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True, "geocoder_url": PROVIDER})


def _home(db, user) -> M.KnownLocation:
    loc = M.KnownLocation(owner_user_id=user.id, name="Hemmet", type=LocationType.HOME, confirmed=True,
                          address="Hemvägen 1, 111 11 Exempelstad", city="Exempelstad", latitude=HOME_POS[0],
                          longitude=HOME_POS[1], coordinate_source="user", created_from="user")
    db.add(loc)
    db.flush()
    places.add_alias(db, loc, loc.address)
    return loc


def _stop(db, user, pos) -> M.KnownLocation:
    loc = M.KnownLocation(owner_user_id=user.id, name=geocode.UNRESOLVED_NAME, created_from="api",
                          coordinate_source="api", latitude=pos[0], longitude=pos[1],
                          geocode_status=GeocodeStatus.PENDING)
    db.add(loc)
    db.commit()
    return loc


def _reload(db, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


def test_stop_far_from_home_with_the_home_address_is_not_merged_into_home(db, user, geo):
    _enable(db, user)
    home = _home(db, user)
    stop = _stop(db, user, FAR_POS)
    geocode.run_pending(now=utcnow())
    stop = _reload(db, stop)
    assert stop.merged_into_id is None
    assert stop.address == "Hemvägen 1, 111 11 Exempelstad"
    assert _reload(db, home).latitude == HOME_POS[0]


def test_stop_near_home_with_the_home_address_is_merged_as_before(db, user, geo):
    _enable(db, user)
    home = _home(db, user)
    stop = _stop(db, user, (HOME_POS[0] + 0.004, HOME_POS[1]))  # ca 450 m bort, utanför radien
    geocode.run_pending(now=utcnow())
    assert _reload(db, stop).merged_into_id == home.id


def test_imported_place_without_position_is_merged_with_a_far_stop_as_before(db, user, geo):
    _enable(db, user)
    _home(db, user)
    db.execute(M.LocationAlias.__table__.delete())  # bostaden utan alias: importplatsen äger adressen
    imported = places.resolve_address(db, user.id, "Hemvägen 1, 111 11 Exempelstad", created_from="import")
    assert imported is not None and imported.latitude is None
    stop = _stop(db, user, FAR_POS)
    geocode.run_pending(now=utcnow())
    stop, imported = _reload(db, stop), _reload(db, imported)
    assert stop.merged_into_id == imported.id
    assert (imported.latitude, imported.longitude) == FAR_POS


def test_imported_place_with_looked_up_position_is_merged_and_corrected_as_before(db, user, geo):
    _enable(db, user)
    _home(db, user)
    db.execute(M.LocationAlias.__table__.delete())
    imported = places.resolve_address(db, user.id, "Hemvägen 1, 111 11 Exempelstad", created_from="import")
    imported.latitude, imported.longitude, imported.coordinate_source = 59.0, 15.0, geocode.COORDINATE_GEOCODED
    db.flush()
    stop = _stop(db, user, FAR_POS)
    geocode.run_pending(now=utcnow())
    stop, imported = _reload(db, stop), _reload(db, imported)
    assert stop.merged_into_id == imported.id
    assert (imported.latitude, imported.longitude) == FAR_POS
