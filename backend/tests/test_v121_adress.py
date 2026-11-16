"""1.2.1: underlaget anger alltid start och mål med gatuadress och ort – aldrig "Hem".

En bekräftad plats med eget namn skrivs "Namn, adress", bostaden med sin adress. Saknar
bostaden (eller en annan bekräftad plats) adress syns det i förhandskontrollen som ett fel som
hindrar månadens automatiska underlag, och texten säger var adressen fylls i. Adressen kan
skrivas på platsen, eller hämtas från kartan en gång efter ägarens tryck. Alla adresser, namn
och koordinater är påhittade; geokodarens transport är fejkad.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import openpyxl
import pytest

import app.models as M
from app.models.enums import Category, DistanceQuality, LocationType, ReviewStatus, TimeQuality, TripSource
from app.services import settings as settings_svc
from app.services.export import checks as checks_mod
from app.services.export import month_close
from app.services.export import service as export_svc
from app.services.export import simple
from app.services.places import geocode
from app.services.places import registry as places
from app.services.stats import common
from app.services.trips import merge
from app.services.trips import segments as seg

TZ = ZoneInfo("Europe/Stockholm")
SEPT = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
HOME_ADDR = "Låtsasgatan 4, 123 45 Påhittby"
ACME_ADDR = "Kundvägen 2, 234 56 Exempelstad"
STORE_ADDR = "Lagerstigen 8, 345 67 Fiktivköping"
PERIOD = {"from": "2026-09-01", "to": "2026-09-30"}


def place(db, user, *, name: str, type: str, address: str | None, confirmed: bool = True,
          city: str | None = None, lat: float = 59.0, lon: float = 15.0) -> M.KnownLocation:
    loc = M.KnownLocation(owner_user_id=user.id, name=name, type=type, address=address, city=city,
                          confirmed=confirmed, created_from="user", latitude=lat, longitude=lon,
                          coordinate_source="user")
    db.add(loc)
    db.flush()
    if address:
        places.add_alias(db, loc, address)
    return loc


def live_trip(db, vehicle, start: M.KnownLocation, end: M.KnownLocation, *, at: datetime, km: float = 12.0,
              odo: float = 20000.0, work: bool = True) -> M.LogicalTrip:
    """En godkänd tjänsteresa från bilen; resans text är det bilen fick: platsens adress eller namn."""
    raw = M.RawTrip(
        vehicle_id=vehicle.id, source=TripSource.VOLVO_API, source_key=uuid.uuid4().hex,
        started_at=at, ended_at=at + timedelta(minutes=18), start_time_quality=TimeQuality.REPORTED,
        end_time_quality=TimeQuality.REPORTED, odometer_start=odo, odometer_end=odo + km,
        distance_km=km, distance_quality=DistanceQuality.ODOMETER,
        start_address=geocode.address_for(start), end_address=geocode.address_for(end),
        start_location_id=start.id, end_location_id=end.id, updated_by="system",
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    return seg.create_logical_for_raw(
        db, raw, category=Category.WORK if work else Category.PRIVATE, purpose="Service" if work else None,
        purpose_source="user" if work else None, purpose_confirmed=work, review_status=ReviewStatus.APPROVED,
    )


@pytest.fixture()
def home(db, user) -> M.KnownLocation:
    return place(db, user, name="Hem", type=LocationType.HOME, address=HOME_ADDR)


@pytest.fixture()
def acme(db, user) -> M.KnownLocation:
    return place(db, user, name="Acme AB", type=LocationType.CUSTOMER, address=ACME_ADDR, lat=59.2)


def journal(db, vehicle) -> simple.SimpleJournal:
    j, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ)
    return j


def table_texts(path: Path) -> list[str]:
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


def all_texts(path: Path) -> list[str]:
    book = openpyxl.load_workbook(path, data_only=True)
    return [str(c.value) for ws in book.worksheets for row in ws.iter_rows() for c in row if c.value is not None]


def create(db, user, vehicle, actor, **kw) -> Path:
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=SEPT.start, end=SEPT.end,
                                   actor=actor, tz=TZ, now=NOW, **kw)
    return Path(job.file_path)


T1 = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------- texterna i underlaget


def test_home_is_written_with_its_address_and_named_place_with_name_and_address(db, user, vehicle, actor, home, acme):
    live_trip(db, vehicle, home, acme, at=T1)
    live_trip(db, vehicle, acme, home, at=T1 + timedelta(hours=3), odo=20012)
    rows = journal(db, vehicle).rows
    assert (rows[0].from_address, rows[0].to_address) == (HOME_ADDR, f"Acme AB, {ACME_ADDR}")
    assert (rows[1].from_address, rows[1].to_address) == (f"Acme AB, {ACME_ADDR}", HOME_ADDR)
    texts = table_texts(create(db, user, vehicle, actor))
    assert HOME_ADDR in texts and f"Acme AB, {ACME_ADDR}" in texts
    assert "Hem" not in texts


def test_home_without_address_is_never_written_as_hem(db, user, vehicle, acme):
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    trip = live_trip(db, vehicle, home, acme, at=T1)
    assert trip.start_address == "Hem"  # appen visar platsens namn
    row = journal(db, vehicle).rows[0]
    assert row.from_address == "" and "Hem" not in row.as_dict().values()


def test_unconfirmed_place_gets_only_the_address_and_city_is_added(db, user, vehicle, home):
    poi = place(db, user, name="Kartnamn", type=LocationType.UNKNOWN, address=STORE_ADDR, confirmed=False)
    street_only = place(db, user, name="Verkstaden", type=LocationType.CUSTOMER, address="Smedjegatan 1",
                        city="Låtsasby", lat=59.4)
    live_trip(db, vehicle, home, poi, at=T1)
    live_trip(db, vehicle, poi, street_only, at=T1 + timedelta(hours=2), odo=20012)
    rows = journal(db, vehicle).rows
    assert rows[0].to_address == STORE_ADDR
    assert rows[1].to_address == "Verkstaden, Smedjegatan 1, Låtsasby"


def test_name_already_in_the_address_is_not_repeated(db, user, vehicle, home):
    station = place(db, user, name="Lagerstigen 8", type=LocationType.FUEL_STATION, address=STORE_ADDR, lat=59.3)
    live_trip(db, vehicle, home, station, at=T1)
    assert journal(db, vehicle).rows[0].to_address == STORE_ADDR


def test_imported_trip_keeps_its_original_address(db, user, vehicle, trips):
    trips.trip("Importgatan 5, 456 78 Låtsasby", "Annangatan 6, 456 78 Låtsasby", category=Category.WORK,
               purpose="Leverans", start_at=T1)
    row = journal(db, vehicle).rows[0]
    assert (row.from_address, row.to_address) == ("Importgatan 5, 456 78 Låtsasby", "Annangatan 6, 456 78 Låtsasby")


def test_merged_trip_via_named_place_and_private_stop(db, user, vehicle, actor, home, acme):
    store = place(db, user, name="Lager Nord", type=LocationType.CUSTOMER, address=STORE_ADDR, lat=59.3)
    t1 = live_trip(db, vehicle, home, store, at=T1)
    t2 = live_trip(db, vehicle, store, acme, at=T1 + timedelta(minutes=26), odo=20012)
    merged = merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)
    rows = journal(db, vehicle).rows
    assert len(rows) == 1
    assert rows[0].from_address == HOME_ADDR
    assert rows[0].to_text == f"Acme AB, {ACME_ADDR}\nvia Lager Nord, {STORE_ADDR}"
    # Ett mellanstopp hemma är fortfarande "Privat stopp", aldrig "Hem".
    t3 = live_trip(db, vehicle, acme, home, at=T1 + timedelta(hours=4), odo=20024)
    t4 = live_trip(db, vehicle, home, store, at=T1 + timedelta(hours=4, minutes=30), odo=20036)
    merge.merge_trips(db, [t3, t4], trigger="manual", actor=actor)
    rows = journal(db, vehicle).rows
    assert rows[1].to_text == f"Lager Nord, {STORE_ADDR}\nvia {simple.PRIVATE_STOP}"
    # Den detaljerade arbetsboken skriver samma platser, även i anteckningen om mellanstoppen.
    texts = all_texts(create(db, user, vehicle, actor, layout="detailed"))
    assert HOME_ADDR in texts and f"Acme AB, {ACME_ADDR}" in texts and "Hem" not in texts
    assert any(f"via Lager Nord, {STORE_ADDR}" in t for t in texts)
    del merged


def test_trip_with_private_part_uses_the_same_places(db, user, vehicle, home, acme):
    trip = live_trip(db, vehicle, home, acme, at=T1)
    raw = trip.segments[0].raw_trip
    seg.allocate_raw_trip(db, trip, raw.id, [{"distance_km": 8, "category": Category.WORK},
                                             {"distance_km": 4, "category": Category.PRIVATE}], actor="test")
    row = journal(db, vehicle).rows[0]
    assert (row.from_address, row.to_address, row.km) == (HOME_ADDR, f"Acme AB, {ACME_ADDR}", 8.0)


def test_preview_api_shows_the_same_texts(client, db, user, vehicle, home, acme):
    live_trip(db, vehicle, home, acme, at=T1)
    db.commit()
    body = client.get("/api/v1/exports/preview", params={**PERIOD, "vehicle_id": str(vehicle.id)}).json()
    assert body["rows"][0]["from"] == HOME_ADDR and body["rows"][0]["to"] == f"Acme AB, {ACME_ADDR}"


# ----------------------------------------------------------------------------- förhandskontrollen och månadsskiftet


def test_missing_home_address_is_an_error_that_says_where(db, user, vehicle, acme):
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    trip = live_trip(db, vehicle, home, acme, at=T1)
    found = [c for c in checks_mod.reported_quality_checks(db, [trip]) if c["code"] == "place_address_missing"]
    assert len(found) == 1
    c = found[0]
    assert c["severity"] == checks_mod.ERROR
    assert c["title"] == "Bostaden saknar adress – fyll i den under Platser"
    assert "Platser" in c["message"] and "Hämta adressen från kartan" in c["message"]
    assert c["data"]["location_ids"] == [str(home.id)] and c["trip_ids"] == [str(trip.id)]
    checks = checks_mod.run_checks(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ)
    assert "place_address_missing" in {c["code"] for c in checks} and not checks_mod.is_complete(checks)


def test_other_confirmed_place_without_address(db, user, vehicle, home):
    office = place(db, user, name="Kontoret", type=LocationType.OFFICE, address=None, lat=59.5)
    store = place(db, user, name="Förrådet", type=LocationType.CUSTOMER, address=None, lat=59.6)
    t1 = live_trip(db, vehicle, home, office, at=T1)
    t2 = live_trip(db, vehicle, office, store, at=T1 + timedelta(hours=1), odo=20012)
    c = next(c for c in checks_mod.reported_quality_checks(db, [t1, t2]) if c["code"] == "place_address_missing")
    assert c["title"] == "2 platser saknar adress – fyll i dem under Platser"
    assert set(c["data"]["location_ids"]) == {str(office.id), str(store.id)}


def test_no_error_when_addresses_are_there(db, user, vehicle, home, acme):
    trip = live_trip(db, vehicle, home, acme, at=T1)
    assert "place_address_missing" not in {c["code"] for c in checks_mod.reported_quality_checks(db, [trip])}


def test_missing_home_address_stops_the_automatic_month(db, user, vehicle, acme):
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    live_trip(db, vehicle, home, acme, at=T1)
    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=date(2026, 9, 1), now=NOW)
    blocker = next(b for b in status.blockers if b.code == "export_errors")
    assert blocker.text == "Bostaden saknar adress – fyll i den under Platser"
    assert status.state == "waiting"
    # Med adressen ifylld står bostadens adress i underlaget och månaden är klar.
    home.address, home.city = HOME_ADDR, "Påhittby"
    db.flush()
    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=date(2026, 9, 1), now=NOW)
    assert not [b for b in status.blockers if b.code == "export_errors"]


# ----------------------------------------------------------------------------- adressen på platsen


def test_patch_sets_address_and_city(client, db, user):
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    db.commit()
    r = client.patch(f"/api/v1/locations/{home.id}", json={"address": " Låtsasgatan 4,  123 45 Påhittby "})
    assert r.status_code == 200, r.text
    assert r.json()["address"] == HOME_ADDR and r.json()["city"] == "Påhittby"
    db.expire_all()
    assert places.find_by_address(db, user.id, HOME_ADDR).id == home.id
    # Tom text tar bort adressen.
    r = client.patch(f"/api/v1/locations/{home.id}", json={"address": ""})
    assert r.status_code == 200 and r.json()["address"] is None


def test_patch_address_of_another_place_is_refused(client, db, user, acme):
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    db.commit()
    r = client.patch(f"/api/v1/locations/{home.id}", json={"address": ACME_ADDR})
    assert r.status_code == 409 and r.json()["code"] == "address_exists"


NOMINATIM = {
    "category": "building", "type": "house", "name": None,
    "address": {"house_number": "4", "road": "Låtsasgatan", "town": "Påhittby", "postcode": "123 45"},
}


class Recorder:
    def __init__(self, body: Any = NOMINATIM) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(self.body).encode()))


@pytest.fixture()
def lookup(monkeypatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(rec))
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DNS")))
    monkeypatch.setattr(geocode, "_last_call", None)
    return rec


def test_lookup_address_for_one_place_after_the_owners_press(client, db, user, lookup):
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True,
                                                         "geocoder_url": "https://nominatim.example.org"})
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None, lat=59.123456, lon=15.654321)
    db.commit()
    r = client.post(f"/api/v1/locations/{home.id}/lookup-address")
    assert r.status_code == 200, r.text
    assert r.json() == {"address": HOME_ADDR, "city": "Påhittby"}
    assert len(lookup.requests) == 1
    params = dict(lookup.requests[0].url.params)
    assert (params["lat"], params["lon"]) == ("59.1235", "15.6543")
    # Förslaget sparas inte förrän ägaren sparar platsen.
    db.expire_all()
    assert db.get(M.KnownLocation, home.id).address is None


def test_lookup_address_is_refused_without_position_when_off_and_for_demo(client, db, user, lookup):
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    with_pos = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    no_pos = place(db, user, name="Kund", type=LocationType.CUSTOMER, address=None, lat=59.3)
    no_pos.latitude = no_pos.longitude = None
    demo = place(db, user, name="Demohem", type=LocationType.OTHER, address=None, lat=59.4)
    demo.is_demo = True
    db.commit()
    r = client.post(f"/api/v1/locations/{with_pos.id}/lookup-address")
    assert r.status_code == 400 and r.json()["code"] == "geocoding_off"
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True})
    db.commit()
    assert client.post(f"/api/v1/locations/{no_pos.id}/lookup-address").json()["code"] == "no_position"
    assert client.post(f"/api/v1/locations/{demo.id}/lookup-address").json()["code"] == "demo_place"
    assert lookup.requests == []


def test_lookup_address_when_the_map_has_none(client, db, user, monkeypatch, lookup):
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True})
    lookup.body = {"error": "Unable to geocode"}
    home = place(db, user, name="Hem", type=LocationType.HOME, address=None)
    db.commit()
    r = client.post(f"/api/v1/locations/{home.id}/lookup-address")
    assert r.status_code == 404 and r.json()["code"] == "address_not_found"
