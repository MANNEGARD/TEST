"""Kom igång: markera bostaden med bilens position, larmet och månadsskiftets upplysning.

Bostaden markeras med ``POST /locations/home-from-vehicle`` (bilens senaste avläsning).
Först då låses adressuppslagningen upp (places.geocode._user_provider). Inga riktiga
nätverksanrop: geokodarens transport är offline eller en MockTransport. Alla koordinater,
adresser och namn är påhittade.
"""

# ruff: noqa: F811  (fixturerna importeras från andra testmoduler)

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

import app.models as M
from app.db import utcnow
from app.models.enums import (
    Category,
    GeocodeStatus,
    IntegrationProvider,
    IntegrationStatus,
    LocationType,
    NotificationStatus,
    TripSource,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.export import month_close
from app.services.ha import alerts, messages, notifier
from app.services.places import geocode
from tests.test_ha_support import fake_ha, setup_push  # noqa: F401
from tests.test_s1_adresser import (  # noqa: F401
    PROVIDER,
    Recorder,
    enable,
    fast_clock,
    live_trip,
    no_dns,
    pending_place,
    reload,
    use,
)
from tests.test_volvo_support import A, B

API = "/api/v1"
ROUTE = f"{API}/locations/home-from-vehicle"
REPORTED = datetime(2026, 11, 3, 20, 14, tzinfo=UTC)


def observe(db, vehicle, pos=A, *, reported=REPORTED, fetched=None) -> M.VehicleObservation:
    fetched = fetched or reported + timedelta(minutes=1)
    row = M.VehicleObservation(
        vehicle_id=vehicle.id, fetched_at=fetched, last_seen_at=fetched,
        latitude=pos[0] if pos else None, longitude=pos[1] if pos else None,
        position_reported_at=reported if pos else None, content_hash=f"h-{fetched.isoformat()}-{vehicle.id.hex[:6]}",
    )
    db.add(row)
    db.flush()
    return row


def homes(db, user) -> list[M.KnownLocation]:
    db.expire_all()
    return list(db.execute(
        select(M.KnownLocation).where(
            M.KnownLocation.owner_user_id == user.id, M.KnownLocation.type == LocationType.HOME,
            M.KnownLocation.merged_into_id.is_(None), M.KnownLocation.is_demo.is_(False),
        )
    ).scalars())


# ----------------------------------------------------------------------------- rutten


def test_no_position_gives_a_clear_swedish_error(client, db, vehicle):
    observe(db, vehicle, pos=None)  # en avläsning utan position räknas inte
    db.commit()
    r = client.post(ROUTE, json={})
    assert r.status_code == 400
    assert r.json()["code"] == "no_position"
    assert r.json()["detail"].startswith("Bilen har inte rapporterat någon position än")


def test_creates_confirmed_home_from_newest_position(client, db, user, vehicle, offline_geocoder):
    observe(db, vehicle, B, reported=REPORTED - timedelta(days=2))
    observe(db, vehicle, A, reported=REPORTED)
    db.commit()
    assert client.get(f"{API}/locations/meta").json()["home_has_coordinate"] is False

    r = client.post(ROUTE, json={"address": "Påhittade vägen 3, Exempelstad"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "created" and body["absorbed"] == 0
    assert datetime.fromisoformat(body["position_reported_at"].replace("Z", "+00:00")) == REPORTED
    assert body["vehicle_id"] == str(vehicle.id) and body["vehicle_name"] == "Testbil"
    assert body["location"]["name"] == "Hem" and body["location"]["type"] == "home"
    assert body["geocoding_unlocked"] is False  # adressuppslagningen är av i de gamla standardvärdena

    [home] = homes(db, user)
    assert home.confirmed and (home.latitude, home.longitude) == A and home.coordinate_source == "user"
    assert home.radius_m == 150 and home.address == "Påhittade vägen 3, Exempelstad"
    assert home.created_from == "user"
    meta = client.get(f"{API}/locations/meta").json()
    assert meta["has_confirmed_home"] is True and meta["home_has_coordinate"] is True
    ev = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "location.home_from_vehicle")).scalar_one()
    assert ev.after["action"] == "created" and "latitude" not in str(ev.after)
    assert offline_geocoder.requests == []


def test_own_name_is_used(client, db, user, vehicle):
    observe(db, vehicle)
    db.commit()
    assert client.post(ROUTE, json={"name": "Bostaden"}).json()["location"]["name"] == "Bostaden"


def test_completes_existing_home_without_coordinate(client, db, user, vehicle, trips):
    imported = trips.location("Hemvägen 1, Exempelstad", name="Hemma", type=LocationType.HOME, confirmed=True)
    observe(db, vehicle)
    db.commit()
    r = client.post(ROUTE, json={})
    assert r.status_code == 200 and r.json()["action"] == "completed"
    [home] = homes(db, user)
    assert home.id == imported.id and home.name == "Hemma" and (home.latitude, home.longitude) == A
    assert home.address == "Hemvägen 1, Exempelstad"  # importens adress står kvar


def test_completes_unconfirmed_home(client, db, user, vehicle, trips):
    guess = trips.location("Hemvägen 1, Exempelstad", type=LocationType.HOME, confirmed=False)
    observe(db, vehicle)
    db.commit()
    r = client.post(ROUTE, json={"name": "Hem"})
    assert r.status_code == 200 and r.json()["action"] == "completed"
    [home] = homes(db, user)
    assert home.id == guess.id and home.confirmed and home.name == "Hem"


def test_existing_home_with_coordinate_needs_replace(client, db, user, vehicle, trips):
    old = trips.location("Hemvägen 1, Exempelstad", type=LocationType.HOME, confirmed=True, lat=B[0], lon=B[1])
    observe(db, vehicle, A)
    db.commit()
    r = client.post(ROUTE, json={})
    assert r.status_code == 409 and r.json()["code"] == "home_exists"
    assert (reload(db, old).latitude, reload(db, old).longitude) == B

    r = client.post(ROUTE, json={"replace": True})
    assert r.status_code == 200 and r.json()["action"] == "replaced"
    [home] = homes(db, user)
    assert home.id == old.id and (home.latitude, home.longitude) == A


def test_address_owned_by_another_place_is_refused(client, db, user, vehicle, trips):
    trips.location("Kundvägen 5, Exempelby", type=LocationType.CUSTOMER)
    observe(db, vehicle)
    db.commit()
    r = client.post(ROUTE, json={"address": "Kundvägen 5, Exempelby"})
    assert r.status_code == 409 and r.json()["code"] == "address_exists"
    assert homes(db, user) == []


def test_demo_and_other_users_vehicles_are_ignored(client, db, user, vehicle):
    demo = M.Vehicle(owner_user_id=user.id, name="Demobilen", is_demo=True)
    stranger = M.User(email="annan@example.com", display_name="Annan", password_hash="x")
    db.add_all([demo, stranger])
    db.flush()
    theirs = M.Vehicle(owner_user_id=stranger.id, name="Annans bil")
    db.add(theirs)
    db.flush()
    observe(db, demo, A)
    observe(db, theirs, B)
    db.commit()
    assert client.post(ROUTE, json={}).json()["code"] == "no_position"
    assert client.post(ROUTE, json={"vehicle_id": str(demo.id)}).json()["code"] == "demo_vehicle"
    assert client.post(ROUTE, json={"vehicle_id": str(theirs.id)}).status_code == 404


def test_requires_session_and_csrf_and_refuses_api_tokens(client, anon_client, db, user, vehicle):
    observe(db, vehicle)
    db.commit()
    assert anon_client.post(ROUTE, json={}).status_code == 401
    token = client.post(f"{API}/auth/tokens", json={"name": "Maskin", "scopes": ["ha"]}).json()["token"]
    r = anon_client.post(ROUTE, json={}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and r.json()["code"] == "session_required"
    csrf = client.headers.pop("X-CSRF-Token")
    try:
        r = client.post(ROUTE, json={})
        assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    finally:
        client.headers["X-CSRF-Token"] = csrf
    assert homes(db, user) == []


def test_route_makes_no_network_traffic(client, db, vehicle, monkeypatch, offline_geocoder):
    def boom(*_a, **_k):  # pragma: no cover - ska aldrig anropas
        raise AssertionError("nätverksanrop i rutten")

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(geocode, "_getaddrinfo", boom)
    observe(db, vehicle)
    db.commit()
    assert client.post(ROUTE, json={}).status_code == 200
    assert offline_geocoder.requests == []


# ----------------------------------------------------------------------------- upplåsningen


@pytest.mark.new_defaults
def test_marking_home_unlocks_lookups_but_never_sends_the_home(client, db, user, vehicle, monkeypatch, no_dns):
    enable(db, user)
    at_home = pending_place(db, user, A)  # bilen har redan stått hemma: en obekräftad plats där
    elsewhere = pending_place(db, user, B)
    trip = live_trip(db, vehicle, at_home, elsewhere)
    observe(db, vehicle, A)
    db.commit()
    rec = use(monkeypatch, Recorder())
    out = geocode.run_pending(now=utcnow())
    assert rec.requests == [] and out.get("waiting_for_home") == 2
    assert client.get(f"{API}/locations/meta").json()["home_has_coordinate"] is False

    r = client.post(ROUTE, json={"address": "Påhittade vägen 3, Exempelstad"})
    assert r.status_code == 200, r.text
    assert r.json()["geocoding_unlocked"] is True and r.json()["absorbed"] == 1
    assert rec.requests == []  # rutten själv skickar ingenting
    [home] = homes(db, user)
    assert reload(db, at_home).merged_into_id == home.id
    raw = db.get(M.RawTrip, reload(db, trip).segments[0].raw_trip_id)
    assert raw.start_location_id == home.id and raw.start_address == "Påhittade vägen 3, Exempelstad"

    out = geocode.run_pending(now=utcnow())
    assert len(rec.requests) == 1 and out.get("ok") == 1
    sent = rec.requests[0].url.params
    assert (float(sent["lat"]), float(sent["lon"])) == B  # bara den andra platsen, aldrig hemmet
    assert reload(db, elsewhere).geocode_status == GeocodeStatus.OK
    assert reload(db, elsewhere).address == "Exempelgatan 5, 123 45 Exempelstad"


# ----------------------------------------------------------------------------- larmet

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)  # utanför tysta timmar
SINCE = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_alert_clock(monkeypatch):
    monkeypatch.setattr(alerts, "_last_run_at", None)


def self_driving(db, user, *, since=SINCE):
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": since.isoformat()}, allow_read_only=True)
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True, "geocoder_url": PROVIDER})
    db.flush()


def volvo(db, user, status=IntegrationStatus.CONNECTED):
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.VOLVO)
    conn.status, conn.last_success_at = status, NOW - timedelta(minutes=5)
    integrations.update_config(conn, {"mode": "oauth"})
    db.flush()
    return conn


def home_alerts(db) -> list[M.NotificationRequest]:
    return list(db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.dedupe_key.like("alert:home_missing:%"))
        .order_by(M.NotificationRequest.created_at)
    ).scalars())


def home_sent(fake_ha) -> list[dict]:
    return [m for m in fake_ha.sent if m["title"].endswith("Markera din bostad")]


def tick(db, user, now=NOW):
    out = alerts.run(db, now=now, force=True)
    notifier.deliver_user(db, user.id, now)
    db.flush()
    return out


def test_home_missing_alert_one_push_per_episode_and_removed_when_marked(db, user, vehicle, fake_ha, trips):
    setup_push(db, user)
    self_driving(db, user)
    volvo(db, user)
    observe(db, vehicle, A, reported=SINCE + timedelta(hours=5))
    tick(db, user)
    [req] = home_alerts(db)
    assert req.title.endswith("Markera din bostad")
    assert req.message == ("DriveLog kan inte slå upp adresser än: markera din bostad i DriveLog "
                           "(Översikt → Kom igång).")
    assert len(home_sent(fake_ha)) == 1
    assert home_sent(fake_ha)[0]["data"]["actions"][0]["uri"] == "http://testserver/"

    for hours in (1, 6, 30):  # ingen påminnelse
        tick(db, user, NOW + timedelta(hours=hours))
    assert len(home_alerts(db)) == 1 and len(home_sent(fake_ha)) == 1

    trips.location("Hemvägen 1, Exempelstad", type=LocationType.HOME, confirmed=True, lat=A[0], lon=A[1])
    tick(db, user, NOW + timedelta(hours=31))
    assert home_alerts(db)[0].status == NotificationStatus.CANCELLED  # borttaget, ingen ny push
    assert len(fake_ha.clears) == 1
    assert len(home_sent(fake_ha)) == 1


@pytest.mark.parametrize("case", ["manual", "not_connected", "nothing_since", "geocoding_off", "demo_only", "home"])
def test_home_missing_alert_conditions(db, user, vehicle, fake_ha, trips, case):
    setup_push(db, user)
    self_driving(db, user)
    volvo(db, user, IntegrationStatus.ERROR if case == "not_connected" else IntegrationStatus.CONNECTED)
    if case == "manual":
        settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    if case == "geocoding_off":
        settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    if case == "nothing_since":
        observe(db, vehicle, A, reported=SINCE - timedelta(days=1))  # bara före påslaget
    elif case == "demo_only":
        demo = M.Vehicle(owner_user_id=user.id, name="Demobilen", is_demo=True)
        db.add(demo)
        db.flush()
        observe(db, demo, A, reported=SINCE + timedelta(hours=5))
    else:
        observe(db, vehicle, A, reported=SINCE + timedelta(hours=5))
    if case == "home":
        trips.location("Hemvägen 1, Exempelstad", type=LocationType.HOME, confirmed=True, lat=A[0], lon=A[1])
    tick(db, user)
    assert home_alerts(db) == []


def test_home_missing_alert_also_after_a_live_trip(db, user, vehicle, fake_ha):
    setup_push(db, user)
    self_driving(db, user)
    volvo(db, user)
    end = geocode.place_for_position(db, user_id=user.id, lat=B[0], lon=B[1], demo=False)
    live_trip(db, vehicle, None, end)
    tick(db, user)
    assert len(home_alerts(db)) == 1


# ----------------------------------------------------------------------------- månadsskiftet

OCT = datetime(2026, 10, 1).date()
AFTER_OCT = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


@pytest.mark.new_defaults
def test_month_status_and_notice_say_why_addresses_are_missing(db, user, vehicle, trips):
    enable(db, user)
    place = pending_place(db, user, B)
    t = trips.trip("Hemvägen 1, Exempelstad", "Kundvägen 5, Exempelby", category=Category.WORK,
                   purpose="Kundbesök", km=20.0, start_at=datetime(2026, 10, 14, 6, 0, tzinfo=UTC),
                   source=TripSource.VOLVO_API)
    raw = t.segments[0].raw_trip
    raw.end_location_id, raw.end_address = place.id, None
    t.end_location_id = place.id
    db.flush()

    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=OCT, now=AFTER_OCT)
    notes = {n.code: n for n in status.notes}
    assert notes["home_missing"].text == month_close.HOME_MISSING_TEXT
    assert notes["home_missing"].trip_ids == [str(t.id)]
    waiting = messages.month_waiting("2026-10", status.blockers, status.notes)
    assert "markerat din bostad (Översikt → Kom igång)." in waiting["message"]
    ready = messages.month_ready("2026-10", summary={"trips": 1, "km": 20, "mil": 2}, notes=status.notes,
                                 export_id="x")
    assert "markerat din bostad" in ready["message"]

    # Med markerad bostad försvinner upplysningen.
    trips.location("Hemvägen 1, Exempelstad", type=LocationType.HOME, confirmed=True, lat=A[0], lon=A[1])
    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=OCT, now=AFTER_OCT)
    assert "home_missing" not in {n.code for n in status.notes}


@pytest.mark.new_defaults
def test_month_note_not_shown_when_geocoding_is_off(db, user, vehicle, trips):
    enable(db, user)
    place = pending_place(db, user, B)
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": False})
    t = trips.trip("Hemvägen 1, Exempelstad", "Kundvägen 5, Exempelby", category=Category.WORK,
                   purpose="Kundbesök", km=20.0, start_at=datetime(2026, 10, 14, 6, 0, tzinfo=UTC),
                   source=TripSource.VOLVO_API)
    t.end_location_id = place.id
    db.flush()
    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=OCT, now=AFTER_OCT)
    assert "home_missing" not in {n.code for n in status.notes}
    assert db.scalar(select(func.count()).select_from(M.KnownLocation)) >= 1
