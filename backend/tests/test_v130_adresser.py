"""1.3.0 DEL 8 och precision 1–2: positioner ur adresser, exakt position vid adressuppslag,
bostadens adress automatiskt, och saknad adress per rad i underlagets förhandsvisning.

Inga riktiga nätverksanrop: geokodarens transport byts ut mot en MockTransport som svarar
på /search (adress → position) och /reverse (position → adress). Adresserna är påhittade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app import __version__
from app.db import utcnow
from app.models.enums import Category, GeocodeStatus, LocationType, TripSource
from app.services import settings as settings_svc
from app.services.export import service as export_svc
from app.services.places import geocode
from app.services.places import registry as places
from app.services.trips import segments as seg

PROVIDER = "https://nominatim.example.org"
NORRBY = "Lagergatan 4, 333 44 Norrby"
HIT = {"lat": "59.3301234", "lon": "18.0687654", "category": "building", "type": "office"}
NEAR = {"lat": "59.3302000", "lon": "18.0689000", "category": "building", "type": "yes"}  # ca 10 m bort
FAR = {"lat": "57.7000000", "lon": "11.9700000", "category": "place", "type": "house"}  # en annan ort
HOME_POS = (59.3301, 18.0688)
STREET = {
    "place_id": 1, "category": "building", "type": "house", "name": "",
    "address": {"house_number": "1", "road": "Hemvägen", "city": "Exempelstad", "postcode": "111 11"},
}


class Router:
    """Svarar på /search och /reverse och sparar anropen."""

    def __init__(self, search: Any = None, reverse: Any = None, *, status: int = 200) -> None:
        self.search = [HIT] if search is None else search
        self.reverse = {"error": "Unable to geocode"} if reverse is None else reverse
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.search if request.url.path.endswith("/search") else self.reverse
        return httpx.Response(self.status, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(body).encode()))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch) -> list[float]:
    clock = {"t": 1000.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(geocode, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(geocode, "_sleep", sleep)
    monkeypatch.setattr(geocode, "_last_call", None)
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda *a, **k: pytest.fail("DNS-uppslag i testet"))
    return sleeps


def use(monkeypatch, router: Router) -> Router:
    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(router))
    return router


def enable(db, user) -> None:
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True, "geocoder_url": PROVIDER})


def home(db, user, *, address: str | None = "Hemvägen 1, 111 11 Exempelstad", pos=HOME_POS,
         source: str = "user") -> M.KnownLocation:
    loc = M.KnownLocation(owner_user_id=user.id, name="Hemmet", type=LocationType.HOME, confirmed=True,
                          address=address, city="Exempelstad" if address else None, latitude=pos[0],
                          longitude=pos[1], coordinate_source=source, created_from="user")
    db.add(loc)
    db.flush()
    if address:
        places.add_alias(db, loc, address)
    return loc


def imported_place(db, user, address: str = NORRBY) -> M.KnownLocation:
    loc = places.resolve_address(db, user.id, address, created_from="import")
    assert loc is not None and loc.latitude is None
    return loc


def reload(db, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


def run(now: datetime | None = None) -> dict[str, Any]:
    return geocode.run_pending(now=now or utcnow())


# ----------------------------------------------------------------------------- adress → position


def test_forward_lookup_sets_an_approximate_position(db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    loc = imported_place(db, user)
    loc.name, loc.type, loc.confirmed = "Beta AB", LocationType.CUSTOMER, True
    db.commit()
    router = use(monkeypatch, Router())
    out = run()
    assert out.get("forward_ok") == 1
    (req,) = router.requests
    assert req.url.path == "/search"
    assert dict(req.url.params) == {
        "q": NORRBY, "format": "jsonv2", "limit": "2", "addressdetails": "1", "countrycodes": "se",
        "accept-language": "sv",
    }
    assert req.headers["user-agent"] == f"SMGD-DriveLog/{__version__} (self-hosted logbook; reverse geocoding)"
    loc = reload(db, loc)
    assert (loc.latitude, loc.longitude) == (59.330123, 18.068765)
    assert loc.coordinate_source == "geocoded" and loc.forward_status == "ok" and loc.forward_at is not None
    assert (loc.name, loc.type, loc.confirmed, loc.address) == ("Beta AB", LocationType.CUSTOMER, True, NORRBY)
    assert loc.osm_class == "building=office" and loc.place_kind == "work"
    assert run() == {}  # inget mer att slå upp


def test_city_is_added_only_when_missing(db, user):
    loc = M.KnownLocation(owner_user_id=user.id, name="X", address="Storgatan 5", city="Jönköping")
    assert geocode.forward_query(loc) == "Storgatan 5, Jönköping"
    loc.address = "Storgatan 5, 553 20 Jönköping"
    assert geocode.forward_query(loc) == "Storgatan 5, 553 20 Jönköping"


@pytest.mark.parametrize(("hits", "status"), [([], "not_found"), ([HIT, FAR], "ambiguous")])
def test_not_found_and_ambiguous_are_left_without_position_and_not_retried(db, user, monkeypatch, hits, status):
    enable(db, user)
    home(db, user)
    loc = imported_place(db, user)
    db.commit()
    router = use(monkeypatch, Router(search=hits))
    run()
    loc = reload(db, loc)
    assert loc.latitude is None and loc.forward_status == status
    run(utcnow() + timedelta(days=40))
    assert len(router.requests) == 1


def test_two_hits_close_together_are_one_place(db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    loc = imported_place(db, user)
    db.commit()
    use(monkeypatch, Router(search=[HIT, NEAR]))
    run()
    assert reload(db, loc).forward_status == "ok"


def test_errors_are_retried_and_then_given_up(db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    loc = imported_place(db, user)
    db.commit()
    router = use(monkeypatch, Router(status=500))
    now = utcnow()
    run(now)
    loc = reload(db, loc)
    assert (loc.forward_status, loc.forward_attempts) == ("failed", 1)
    assert loc.forward_next_at == now + geocode.RETRY_DELAYS[0]
    run(now + timedelta(minutes=1))
    assert len(router.requests) == 1  # inte förrän tiden har gått
    t = now
    for _ in range(geocode.MAX_ATTEMPTS - 1):
        t += timedelta(days=2)
        run(t)
    loc = reload(db, loc)
    assert loc.forward_status == "not_found" and loc.forward_attempts == geocode.MAX_ATTEMPTS
    assert len(router.requests) == geocode.MAX_ATTEMPTS
    assert loc.latitude is None


def test_daily_limit_is_shared_with_reverse_lookups(db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    imported_place(db, user)
    now = utcnow()
    for i in range(geocode.DAILY_LIMIT):
        db.add(M.GeocodeCache(owner_user_id=user.id, provider_host="nominatim.example.org", lat_key=i, lon_key=i,
                              status=GeocodeStatus.OK, fetched_at=now - timedelta(hours=1), attempts=1))
    db.commit()
    router = use(monkeypatch, Router())
    run(now)
    assert router.requests == []


def test_at_most_three_per_tick_and_throttled(db, user, monkeypatch, fast_clock):
    enable(db, user)
    home(db, user)
    for i in range(5):
        imported_place(db, user, f"Gatan {i + 1}, 333 44 Norrby")
    db.commit()
    router = use(monkeypatch, Router())
    run()
    assert len(router.requests) == geocode.FORWARD_PER_TICK
    assert len(fast_clock) == geocode.FORWARD_PER_TICK - 1
    assert all(s >= geocode.MIN_INTERVAL_S - 1e-9 for s in fast_clock)


def test_nothing_is_sent_without_home_position_or_with_geocoding_off(db, user, monkeypatch):
    enable(db, user)
    imported_place(db, user)
    db.commit()
    router = use(monkeypatch, Router())
    run()
    assert router.requests == []  # bostaden har ingen position än
    home(db, user)
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    db.commit()
    run()
    assert router.requests == []


def test_owner_and_vehicle_positions_are_never_overwritten(db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    own = imported_place(db, user, "Egenvägen 2, 333 44 Norrby")
    own.latitude, own.longitude, own.coordinate_source = 59.1, 18.1, "user"
    car = imported_place(db, user, "Bilvägen 3, 333 44 Norrby")
    car.latitude, car.longitude, car.coordinate_source = 59.2, 18.2, "api"
    db.commit()
    router = use(monkeypatch, Router())
    run()
    assert router.requests == []
    assert (reload(db, own).latitude, reload(db, car).latitude) == (59.1, 59.2)


def test_changed_address_resets_the_lookup(client, db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    loc = imported_place(db, user)
    loc.forward_status, loc.forward_attempts = "not_found", 3
    other = imported_place(db, user, "Annangatan 1, 333 44 Norrby")
    other.latitude, other.longitude, other.coordinate_source, other.forward_status = 59.5, 18.5, "geocoded", "ok"
    db.commit()
    r = client.patch(f"/api/v1/locations/{loc.id}", json={"address": "Nygatan 9, 333 44 Norrby"})
    assert r.status_code == 200, r.text
    assert r.json()["position_lookup"] == "pending"
    loc = reload(db, loc)
    assert (loc.forward_status, loc.forward_attempts, loc.forward_at) == (None, 0, None)
    r = client.patch(f"/api/v1/locations/{other.id}", json={"address": "Nygatan 10, 333 44 Norrby"})
    other = reload(db, other)
    assert other.latitude is None and other.coordinate_source is None  # uppslagen ur den gamla adressen
    # Ägarens egen position behålls.
    r = client.patch(f"/api/v1/locations/{other.id}", json={"latitude": 59.6, "longitude": 18.6})
    r = client.patch(f"/api/v1/locations/{other.id}", json={"address": "Nygatan 11, 333 44 Norrby"})
    assert reload(db, other).latitude == 59.6


# ----------------------------------------------------------------------------- sammanslagning med känd plats


def test_place_at_home_without_address_is_merged_and_home_gets_the_address(db, user, vehicle, monkeypatch, trips):
    enable(db, user)
    h = home(db, user, address=None)
    raw = trips.raw("Hemgatan 7, 111 11 Exempelstad", NORRBY, source=TripSource.VOLVO_IMPORT)
    imported = db.get(M.KnownLocation, raw.start_location_id)
    imported.latitude = None
    db.commit()
    # Kartan har ingen adress för bostadens position, men hittar den importerade adressen vid bostaden.
    use(monkeypatch, Router(search=[{"lat": "59.3301500", "lon": "18.0688500", "category": "building",
                                     "type": "house"}]))
    run()
    db.expire_all()
    imported = db.get(M.KnownLocation, imported.id)
    h = db.get(M.KnownLocation, h.id)
    assert imported.merged_into_id == h.id
    assert h.address == "Hemgatan 7, 111 11 Exempelstad" and h.type == LocationType.HOME
    assert places.find_by_address(db, user.id, "Hemgatan 7, 111 11 Exempelstad").id == h.id
    assert db.get(M.RawTrip, raw.id).start_location_id == h.id
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "location.merged_by_address")).scalar_one()


def test_different_addresses_only_give_a_suggestion(db, user, monkeypatch):
    enable(db, user)
    h = home(db, user)
    loc = imported_place(db, user, "Grannvägen 3, 111 11 Exempelstad")
    db.commit()
    use(monkeypatch, Router(search=[{"lat": "59.3301500", "lon": "18.0688500", "category": "building",
                                     "type": "house"}]))
    run()
    loc = reload(db, loc)
    assert loc.merged_into_id is None and loc.coordinate_source == "geocoded"
    groups = places.merge_suggestions(db, user.id)
    reasons = [g["reason"] for g in groups]
    assert "Adressen Grannvägen 3, 111 11 Exempelstad ligger vid Hemmet – kan vara samma plats." in reasons
    group = next(g for g in groups if g["reason"].startswith("Adressen Grannvägen"))
    assert {m["id"] for m in group["locations"]} == {str(h.id), str(loc.id)}


def test_customers_are_never_merged(db, user, monkeypatch):
    enable(db, user)
    home(db, user)
    customer = M.KnownLocation(owner_user_id=user.id, name="Acme AB", type=LocationType.CUSTOMER, confirmed=True,
                               latitude=59.33015, longitude=18.06885, coordinate_source="user")
    db.add(customer)
    loc = imported_place(db, user, "Grannvägen 3, 111 11 Norrby")
    db.commit()
    use(monkeypatch, Router(search=[{"lat": "59.3301500", "lon": "18.0688500"}]))
    run()
    assert reload(db, loc).merged_into_id is None


# ----------------------------------------------------------------------------- precision 1 och 2


def test_reverse_sends_the_exact_position_but_caches_per_cell(db, user, vehicle, monkeypatch):
    enable(db, user)
    home(db, user)
    first = geocode.place_for_position(db, user_id=user.id, lat=59.400012, lon=18.100034, demo=False)
    assert first is not None
    # En annan plats i samma kartcell (4 decimaler) besvaras ur cachen.
    second = M.KnownLocation(owner_user_id=user.id, name=geocode.UNRESOLVED_NAME, created_from="api",
                             coordinate_source="api", latitude=59.400041, longitude=18.100029,
                             geocode_status=GeocodeStatus.PENDING)
    db.add(second)
    db.commit()
    router = use(monkeypatch, Router(reverse=STREET))
    run()
    reverse = [r for r in router.requests if r.url.path == "/reverse"]
    assert len(reverse) == 1
    assert (reverse[0].url.params["lat"], reverse[0].url.params["lon"]) == ("59.400012", "18.100034")
    cells = db.execute(select(M.GeocodeCache)).scalars().all()
    assert [(c.lat_key, c.lon_key) for c in cells] == [(geocode.cell_key(59.400012), geocode.cell_key(18.100034))]
    run(utcnow() + timedelta(minutes=1))
    assert len([r for r in router.requests if r.url.path == "/reverse"]) == 1
    second = reload(db, second)
    assert second.geocode_status == GeocodeStatus.OK or second.merged_into_id == first.id


def test_home_address_is_looked_up_automatically(db, user, monkeypatch):
    enable(db, user)
    h = home(db, user, address=None)
    db.commit()
    router = use(monkeypatch, Router(reverse=STREET))
    out = run()
    assert out.get("ok") == 1
    (req,) = router.requests
    assert req.url.path == "/reverse"
    assert (req.url.params["lat"], req.url.params["lon"]) == (f"{HOME_POS[0]:.6f}", f"{HOME_POS[1]:.6f}")
    h = reload(db, h)
    assert h.address == "Hemvägen 1, 111 11 Exempelstad" and h.city == "Exempelstad"
    assert h.geocode_status == GeocodeStatus.OK and h.geocoded_at is not None
    assert h.name == "Hemmet" and h.osm_class is None and h.place_kind is None and h.suggested_type is None
    assert places.find_by_address(db, user.id, "Hemvägen 1, 111 11 Exempelstad").id == h.id
    assert run() == {}  # inget mer att göra


# ----------------------------------------------------------------------------- API och underlaget


def test_position_lookup_in_the_api(client, db, user, vehicle, monkeypatch, trips):
    enable(db, user)
    home(db, user)
    raw = trips.raw("Hemvägen 1, 111 11 Exempelstad", NORRBY, source=TripSource.VOLVO_IMPORT)
    trip = seg.create_logical_for_raw(db, raw, category=Category.WORK, category_source="import",
                                      review_status="imported")
    db.commit()
    detail = client.get(f"/api/v1/trips/{trip.id}").json()
    end = detail["map"]["stops"][-1]
    assert end["position_lookup"] == "pending" and end["lat"] is None
    assert end["location"]["position_lookup"] == "pending"
    assert client.get(f"/api/v1/locations/{raw.end_location_id}").json()["position_lookup"] == "pending"

    use(monkeypatch, Router())
    run()
    detail = client.get(f"/api/v1/trips/{trip.id}").json()
    end = detail["map"]["stops"][-1]
    assert end["coordinate_basis"] == "address_lookup" and end["lat"] == 59.330123
    assert end["position_lookup"] is None
    body = client.get(f"/api/v1/locations/{raw.end_location_id}").json()
    assert body["coordinate_source"] == "geocoded" and body["position_lookup"] is None

    loc = db.get(M.KnownLocation, raw.end_location_id)
    loc.latitude = loc.longitude = None
    loc.forward_status = "ambiguous"
    db.commit()
    assert client.get(f"/api/v1/locations/{loc.id}").json()["position_lookup"] == "ambiguous"
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    loc.forward_status = None
    db.commit()
    assert client.get(f"/api/v1/locations/{loc.id}").json()["position_lookup"] is None


def test_address_missing_per_row_without_changing_the_fingerprint(db, user, vehicle, trips):
    tz = settings_svc.user_timezone(db, user.id)
    customer = trips.location("Kund utan adress", name="Kund AB", type=LocationType.CUSTOMER)
    customer.address, customer.city = None, None
    trip = trips.trip("Hemvägen 1, 111 11 Exempelstad", "Kund utan adress", category=Category.WORK,
                      purpose="Kundbesök", start_at=datetime(2026, 9, 15, 7, tzinfo=UTC))
    trip.end_location_id = customer.id
    db.flush()
    start, end = datetime(2026, 8, 31, 22, tzinfo=UTC), datetime(2026, 9, 30, 22, tzinfo=UTC)
    journal, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz)
    (row,) = journal.as_dict()["rows"]
    assert row["address_missing"] == ["to"]
    before = export_svc.rows_fingerprint(journal)
    customer.address, customer.city = "Kundgatan 1, 222 22 Kundby", "Kundby"
    db.flush()
    fixed, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz)
    assert fixed.as_dict()["rows"][0]["address_missing"] == []
    # Avtrycket räknas utan fältet: samma rader med och utan markering ger samma avtryck.
    from dataclasses import replace

    marked = replace(fixed, rows=[replace(r, address_missing=("from", "to")) for r in fixed.rows])
    assert export_svc.rows_fingerprint(marked) == export_svc.rows_fingerprint(fixed)
    assert before != export_svc.rows_fingerprint(fixed)  # adressen står i filen
