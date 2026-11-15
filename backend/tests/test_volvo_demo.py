"""Demoläget: egen demobil, samma pipeline, isolerat från riktiga fordon och anslutningar."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

import app.models as M
from app.models.enums import IntegrationStatus, TimeQuality, TripSource
from app.services import integrations
from app.services.volvo import demo, oauth
from app.services.volvo.client import VolvoError
from tests.test_volvo_support import T0, FakeCar, connect, poll


def count(db, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def test_enable_creates_separate_demo_vehicle_and_demo_status(db, user, vehicle):
    demo_car = demo.enable(db, user.id, now=T0)
    assert demo_car.is_demo and demo_car.id != vehicle.id and demo_car.vin == demo.DEMO_VIN
    conn = integrations.get(db, user.id, "volvo")
    assert conn.status == IntegrationStatus.DEMO
    assert not oauth.has_real_credentials(conn) and conn.secrets_encrypted is None
    assert count(db, M.VehicleObservation, M.VehicleObservation.vehicle_id == demo_car.id) == 1
    assert demo_car.capabilities["endpoints"]["diagnostics"]["state"] == "unsupported"
    locs = db.execute(select(M.KnownLocation).where(M.KnownLocation.owner_user_id == user.id)).scalars().all()
    assert locs and all(loc.is_demo for loc in locs)


@pytest.mark.parametrize("scenario", list(demo.SCENARIOS))
def test_simulated_trips_go_through_the_pipeline_on_demo_vehicle_only(db, user, vehicle, scenario):
    demo_car = demo.enable(db, user.id, now=T0)
    result = demo.simulate_trip(db, user.id, scenario, now=T0 + timedelta(hours=2))
    assert result["new_trip_ids"], result
    raws = db.execute(select(M.RawTrip)).scalars().all()
    assert raws and all(r.vehicle_id == demo_car.id and r.source == TripSource.DEMO for r in raws)
    # Starttiden är antingen okänd (bara gränser) eller uträknad och märkt, inom gränserna.
    for r in raws:
        if r.started_at is None:
            assert r.start_time_quality == TimeQuality.BOUNDED
        else:
            assert r.start_time_quality == TimeQuality.ESTIMATED and r.start_estimate_basis["method"] != "trip_meter"
            assert r.start_earliest <= r.started_at <= r.start_latest
    if scenario == "commute":
        assert all(r.start_time_quality == TimeQuality.ESTIMATED for r in raws)  # observerad under färd
    assert count(db, M.VehicleObservation, M.VehicleObservation.vehicle_id == vehicle.id) == 0
    assert count(db, M.LogicalTrip, M.LogicalTrip.vehicle_id == vehicle.id) == 0
    if scenario == "round_trip":
        assert any(r.possible_missed_trips for r in raws)
    if scenario == "silent":
        assert all(r.start_earliest is not None and r.start_latest is not None for r in raws)
        assert all(r.started_at is None for r in raws)  # bara slutavläsning och K1 avstängd


def test_demo_never_makes_real_connection_look_connected(db, user, vehicle):
    conn = connect(db, user, vehicle, status=IntegrationStatus.ERROR)
    conn.last_error = "Riktigt fel"
    demo.enable(db, user.id, now=T0)
    demo.simulate_trip(db, user.id, "commute", now=T0 + timedelta(hours=2))
    assert conn.status == IntegrationStatus.ERROR and conn.last_error == "Riktigt fel"
    assert conn.last_success_at is None
    assert integrations.secrets(conn)["client_secret"] == "klienthemlighet-xyz"  # skrivs aldrig över


def test_demo_does_not_override_configured_status(db, user):
    conn = oauth.connection(db, user.id)
    oauth.save_credentials(db, conn, client_id="cid", client_secret="hemligt-1", vcc_api_key="nyckel-1")
    demo.enable(db, user.id, now=T0)
    assert conn.status == IntegrationStatus.CONFIGURED


def test_disable_removes_all_demo_data_but_keeps_real_data(db, user, vehicle):
    connect(db, user, vehicle)
    real_car = FakeCar()
    poll(db, vehicle, real_car, T0)
    demo.enable(db, user.id, now=T0)
    demo.simulate_trip(db, user.id, "customer_visit", now=T0 + timedelta(hours=3))
    removed = demo.disable(db, user.id)
    assert removed["trips"] >= 1 and removed["observations"] >= 1 and removed["locations"] >= 1
    assert count(db, M.Vehicle, M.Vehicle.is_demo.is_(True)) == 0
    assert count(db, M.RawTrip) == 0 and count(db, M.LogicalTrip) == 0
    assert count(db, M.KnownLocation, M.KnownLocation.is_demo.is_(True)) == 0
    assert count(db, M.VehicleObservation, M.VehicleObservation.vehicle_id == vehicle.id) == 1
    conn = integrations.get(db, user.id, "volvo")
    assert conn.status == IntegrationStatus.CONNECTED and "demo" not in (conn.config or {})


def test_disable_without_real_connection_returns_to_not_configured(db, user):
    demo.enable(db, user.id, now=T0)
    demo.disable(db, user.id)
    conn = integrations.get(db, user.id, "volvo")
    assert conn.status == IntegrationStatus.NOT_CONFIGURED
    with pytest.raises(VolvoError):
        demo.simulate_trip(db, user.id, "commute")


def test_tick_polls_demo_vehicle_without_real_quota(db, user):
    demo_car = demo.enable(db, user.id, now=T0)
    out = demo.tick(db, now=T0 + timedelta(minutes=30))
    assert out["polled"] == 1
    assert demo.tick(db, now=T0 + timedelta(minutes=31))["polled"] == 0  # pollintervallet respekteras
    conn = integrations.get(db, user.id, "volvo")
    assert "quota" not in (conn.capabilities or {})
    obs = db.execute(
        select(M.VehicleObservation).where(M.VehicleObservation.vehicle_id == demo_car.id)
    ).scalar_one()
    assert obs.source == "demo" and obs.seen_count == 2


def test_disconnect_keeps_demo_mode(db, user, vehicle):
    conn = connect(db, user, vehicle)
    demo.enable(db, user.id, now=T0)
    oauth.disconnect(db, conn)
    assert conn.status == IntegrationStatus.DEMO and demo.is_enabled(conn)
    assert integrations.secrets(conn) == {}


def test_unknown_scenario_is_rejected(db, user):
    demo.enable(db, user.id, now=T0)
    with pytest.raises(VolvoError) as err:
        demo.simulate_trip(db, user.id, "teleport")
    assert err.value.code == "unknown_scenario"
