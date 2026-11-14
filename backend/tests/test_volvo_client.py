"""Volvo API-klienten och insamlaren: rate limiting, fel, kapabiliteter, observationer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import IntegrationStatus, ReviewKind, ReviewState
from app.services import settings as settings_svc
from app.services.volvo import client as vc
from app.services.volvo import collector
from app.services.volvo import constants as C
from tests.test_volvo_support import T0, VIN, FakeCar, Recorder, connect, poll


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(vc, "_sleep", slept.append)
    vc._limiters.clear()
    return slept


def test_requests_carry_required_headers_and_never_log_secrets(db, user, vehicle, caplog):
    conn = connect(db, user, vehicle)
    rec = Recorder()
    with caplog.at_level("DEBUG"):
        result = collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    assert result["status"] == "ok"
    req = rec.api_calls()[0]
    assert req.headers["authorization"] == "Bearer AT-gammal-0000000000"
    assert req.headers["vcc-api-key"] == "vcc-nyckel-abc"
    assert req.headers["accept"] == "application/json"
    paths = {c.url.path for c in rec.api_calls()}
    assert f"/connected-vehicle/v2/vehicles/{VIN}/odometer" in paths
    assert f"/location/v1/vehicles/{VIN}/location" in paths
    assert f"/energy/v2/vehicles/{VIN}/state" in paths
    for secret in ("AT-gammal", "vcc-nyckel", VIN, "klienthemlighet"):
        assert secret not in caplog.text
    obs = db.execute(select(M.VehicleObservation)).scalar_one()
    assert VIN not in str(obs.raw)
    assert conn.capabilities["quota"]["calls"]["connected-vehicle"] >= 4


def test_429_sets_backoff_and_no_calls_are_made_during_backoff(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"odometer": httpx.Response(429, headers={"Retry-After": "120"}, json={})})
    result = collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    assert result["status"] == "throttled"
    assert len(rec.api_calls()) == 1  # inget omedelbart nytt försök, övriga endpoints hoppas över
    assert conn.backoff_until >= T0 + timedelta(seconds=120)
    assert conn.status == IntegrationStatus.CONNECTED  # anropsgräns är inget anslutningsfel
    run = db.execute(select(M.IntegrationSyncRun)).scalar_one()
    assert run.status == "throttled" and run.http_status == 429

    again = collector.poll_vehicle(db, vehicle, now=T0 + timedelta(seconds=30), transport=rec.transport)
    assert again["status"] == "throttled" and len(rec.api_calls()) == 1
    assert collector.poll_due(db, now=T0 + timedelta(seconds=60), transport=rec.transport)["skipped"] == 1
    assert len(rec.api_calls()) == 1


def test_backoff_grows_exponentially_with_jitter_without_retry_after():
    first = vc.backoff_seconds(0)
    third = vc.backoff_seconds(2)
    assert 60 <= first <= 75 and 240 <= third <= 300
    assert vc.backoff_seconds(20) <= 3600 * 1.25
    assert 120 <= vc.backoff_seconds(0, retry_after=120) <= 125


def test_repeated_throttling_increases_backoff(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"odometer": httpx.Response(429, json={})})
    collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    first = conn.backoff_until - T0
    t1 = conn.backoff_until + timedelta(seconds=1)
    collector.poll_vehicle(db, vehicle, now=t1, transport=rec.transport)
    assert conn.backoff_until - t1 > first
    assert len(rec.api_calls()) == 2


def test_5xx_is_retried_with_jitter_then_succeeds(db, user, vehicle, _no_sleep):
    conn = connect(db, user, vehicle)
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return httpx.Response(503, json={"error": {"message": "Service Unavailable"}})
        return httpx.Response(200, json={"data": {"odometer": {"value": 2000, "unit": "km"}}})

    client = collector.build_client(db, conn, transport=Recorder(overrides={"odometer": flaky}).transport)
    result = client.fetch("odometer", VIN)
    assert result.ok and result.attempts == 3
    assert len(_no_sleep) == 2 and _no_sleep[1] > _no_sleep[0] >= 1.0


def test_5xx_gives_up_after_limited_retries(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"odometer": httpx.Response(500, json={})})
    result = collector.build_client(db, conn, transport=rec.transport).fetch("odometer", VIN)
    assert result.status == "error" and len(rec.api_calls()) == vc.MAX_RETRIES + 1


def test_network_error_is_retried(db, user, vehicle):
    conn = connect(db, user, vehicle)
    calls = {"n": 0}

    def broken(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("nätverket nere", request=request)
        return httpx.Response(200, json={"data": {"odometer": {"value": 1, "unit": "km"}}})

    result = collector.build_client(db, conn, transport=Recorder(overrides={"odometer": broken}).transport).fetch("odometer", VIN)
    assert result.ok and calls["n"] == 2


def test_404_and_403_mark_capability_unsupported_not_error(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={
        "statistics": httpx.Response(404, json={"error": {"message": "Not found"}}),
        "state": httpx.Response(403, json={"error": {"message": "Forbidden"}}),
    })
    result = collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    assert result["status"] == "ok"
    eps = vehicle.capabilities["endpoints"]
    assert eps["statistics"]["state"] == C.CAP_UNSUPPORTED and eps["statistics"]["http_status"] == 404
    assert eps["energy_state"]["state"] == C.CAP_UNSUPPORTED
    assert eps["odometer"]["state"] == C.CAP_SUPPORTED and eps["odometer"]["label"] == "Mätarställning"
    assert conn.status == IntegrationStatus.CONNECTED and conn.last_error is None
    # Unsupported endpoints pollas inte igen förrän efter omprovningstiden.
    before = len(rec.api_calls())
    second = collector.poll_vehicle(db, vehicle, now=T0 + timedelta(minutes=5), transport=rec.transport)
    assert second["endpoints"]["statistics"] == "skipped_unsupported"
    assert len(rec.api_calls()) == before + len(C.POLL_ENDPOINTS) - 2


def test_location_404_means_no_position(db, user, vehicle):
    connect(db, user, vehicle)
    rec = Recorder(overrides={"location": httpx.Response(404, json={})})
    collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    assert vehicle.capabilities["endpoints"]["location"]["state"] == C.CAP_NO_DATA
    obs = db.execute(select(M.VehicleObservation)).scalar_one()
    assert obs.latitude is None and obs.odometer_km == 1234


def test_probe_capabilities_records_every_endpoint(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"diagnostics": httpx.Response(403, json={}),
                              "capabilities": httpx.Response(200, json={"batteryChargeLevel": {"isSupported": True},
                                                                         "chargingPower": {"isSupported": False}})})
    caps = collector.probe_capabilities(db, conn, vehicle, now=T0, transport=rec.transport)
    assert set(caps["endpoints"]) == set(C.ENDPOINTS)
    assert caps["endpoints"]["diagnostics"]["state"] == C.CAP_UNSUPPORTED
    assert caps["energy"] == {"batteryChargeLevel": True, "chargingPower": False}


def test_minute_limiter_waits_instead_of_exceeding(_no_sleep):
    clock = {"t": 0.0}
    limiter = vc.MinuteLimiter(per_minute=3, clock=lambda: clock["t"])
    for _ in range(3):
        assert limiter.acquire() == 0
    assert limiter.wait_time() == pytest.approx(60.0)
    clock["t"] = 30.0
    assert limiter.acquire() == pytest.approx(30.0)
    assert C.REQUESTS_PER_MINUTE < 100


def test_daily_budget_stops_calls(db, user, vehicle):
    conn = connect(db, user, vehicle)
    conn.capabilities = {"quota": {"date": datetime.now(UTC).strftime("%Y-%m-%d"),
                                   "calls": {"connected-vehicle": C.DAILY_BUDGET_PER_API}}}
    rec = Recorder()
    result = collector.build_client(db, conn, transport=rec.transport).fetch("odometer", VIN)
    assert result.status == "throttled" and rec.api_calls() == []
    assert C.DAILY_BUDGET_PER_API < C.DOCUMENTED_DAILY_LIMIT == 10000


def test_values_may_be_numbers_or_strings():
    assert vc.parse_number("1234") == 1234 and vc.parse_number(12.5) == 12.5 and vc.parse_number("5,4") == 5.4
    assert vc.parse_number("okänt") is None and vc.parse_number(True) is None
    assert vc.to_km("10", "mi") == pytest.approx(16.1)
    fields = collector.parse_results({
        "odometer": vc.ApiResult("ok", data={"data": {"odometer": {"value": "4321", "unit": "km", "timestamp": "2026-10-05T06:00:00Z"}}}),
        "location": vc.ApiResult("ok", data={"data": {"properties": {"heading": "129", "timestamp": "2026-10-05T06:01:00.000Z"},
                                                      "geometry": {"coordinates": ["15.5", "59.5", 0]}}}),
    })
    assert fields["odometer_km"] == 4321 and fields["latitude"] == 59.5 and fields["longitude"] == 15.5
    assert fields["heading"] == 129 and fields["position_reported_at"] == datetime(2026, 10, 5, 6, 1, tzinfo=UTC)


def test_identical_content_is_deduplicated(db, user, vehicle):
    connect(db, user, vehicle)
    car = FakeCar()
    poll(db, vehicle, car, T0, detect=False)
    poll(db, vehicle, car, T0 + timedelta(minutes=5), detect=False)
    poll(db, vehicle, car, T0 + timedelta(minutes=10), detect=False)
    (obs,) = db.execute(select(M.VehicleObservation)).scalars().all()
    assert obs.seen_count == 3
    assert obs.fetched_at == T0 and obs.last_seen_at == T0 + timedelta(minutes=10)
    assert len(db.execute(select(M.IntegrationSyncRun)).scalars().all()) == 3  # varje körning loggas
    car.odometer += 3
    car.odo_ts = T0 + timedelta(minutes=14)
    poll(db, vehicle, car, T0 + timedelta(minutes=15), detect=False)
    assert len(db.execute(select(M.VehicleObservation)).scalars().all()) == 2


def test_both_fetch_time_and_vehicle_timestamps_are_stored(db, user, vehicle):
    connect(db, user, vehicle)
    car = FakeCar(numbers_as_strings=True)
    poll(db, vehicle, car, T0, detect=False)
    obs = db.execute(select(M.VehicleObservation)).scalar_one()
    assert obs.fetched_at == T0
    assert obs.odometer_reported_at == car.odo_ts and obs.position_reported_at == car.pos_ts
    assert obs.engine_reported_at == car.engine_ts and obs.fuel_reported_at == car.odo_ts
    assert obs.odometer_km == 1000 and obs.fuel_liters == 40.0 and obs.engine_running is False
    assert obs.source == "volvo_api" and obs.sync_run_id is not None


def test_odometer_snapshot_is_saved_when_reading_changes(db, user, vehicle):
    connect(db, user, vehicle)
    car = FakeCar()
    poll(db, vehicle, car, T0, detect=False)
    poll(db, vehicle, car, T0 + timedelta(minutes=5), detect=False)
    car.moving(12, T0 + timedelta(minutes=9))
    poll(db, vehicle, car, T0 + timedelta(minutes=10), detect=False)
    snaps = db.execute(select(M.VehicleOdometerSnapshot).order_by(M.VehicleOdometerSnapshot.at)).scalars().all()
    assert [(s.odometer_km, s.source) for s in snaps] == [(1000, "api"), (1012, "api")]
    assert snaps[1].at == T0 + timedelta(minutes=9)  # fordonets egen tidsstämpel


def test_stale_data_is_flagged_and_resolved_when_fresh(db, user, vehicle):
    connect(db, user, vehicle)
    settings_svc.update_section(db, user.id, "volvo", {"stale_after_minutes": 120})
    car = FakeCar(odo_ts=T0 - timedelta(hours=5), pos_ts=T0 - timedelta(hours=5), engine_ts=T0 - timedelta(hours=5))
    result = poll(db, vehicle, car, T0)
    assert result["freshness"]["stale"] is True
    assert vehicle.capabilities["data_freshness"]["stale"] is True
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.ANOMALY)).scalar_one()
    assert item.data["type"] == "stale_data" and item.status == ReviewState.OPEN
    poll(db, vehicle, car, T0 + timedelta(minutes=5))  # samma data → fortfarande ett ärende
    assert len(db.execute(select(M.ReviewItem)).scalars().all()) == 1
    car.engine_ts = T0 + timedelta(minutes=9)
    result = poll(db, vehicle, car, T0 + timedelta(minutes=10))
    assert result["freshness"]["stale"] is False
    db.refresh(item)
    assert item.status == ReviewState.RESOLVED


def test_poll_due_respects_interval_and_skips_demo(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder()
    out = collector.poll_due(db, now=T0, transport=rec.transport)
    assert out["polled"] == 1
    n = len(rec.api_calls())
    assert collector.poll_due(db, now=T0 + timedelta(minutes=1), transport=rec.transport)["skipped"] == 1
    assert len(rec.api_calls()) == n
    assert collector.poll_due(db, now=T0 + timedelta(minutes=6), transport=rec.transport)["polled"] == 1
    conn.status = IntegrationStatus.NEEDS_REAUTH
    db.commit()
    assert collector.poll_due(db, now=T0 + timedelta(hours=1), transport=rec.transport)["polled"] == 0


def test_discover_and_select_vehicle_masks_vin(db, user, vehicle):
    conn = connect(db, user, vehicle)
    vehicle.vin = None
    rec = Recorder()
    found = collector.discover_vehicles(db, conn, transport=rec.transport)
    assert found[0]["vin"] == VIN
    public = conn.config["vehicles"][0]
    assert VIN not in str(conn.config) and public["vin_masked"].endswith(VIN[-4:])
    collector.select_vehicle(db, conn, vehicle, public["ref"])
    assert vehicle.vin == VIN and vehicle.volvo_connection_id == conn.id
    assert vehicle.registration_number == "ABC123" and vehicle.model == "V90 Recharge"
