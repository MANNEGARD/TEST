"""1.5.0: "bilen används" – Volvos command-accessibility hämtas vid varje hämtning.

Statusen (availabilityStatus), orsaken (unavailableReason) och bilens tidsstämpel sparas på
avläsningen, med ett härlett ``car_in_use`` (sant bara för UNAVAILABLE + CAR_IN_USE). Att den
riktiga bilen svarar CAR_IN_USE under färd är INTE prövat; testerna bygger på Volvos
dokumenterade svarsform och på påhittade svar.

Förloppet i detekteringstestet följer den avidentifierade tidslinjen från ägarens bil
(hämtning var 2:a minut, motorstart rapporterad direkt, mätare och position först vid
ankomsten), med påhittade koordinater.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, inspect, make_url, select, text

import app.models as M
from alembic import command
from app.api.routes import volvo as volvo_routes
from app.services.volvo import client as vc
from app.services.volvo import collector, demo
from app.services.volvo import constants as C
from app.services.volvo.client import ApiResult
from tests.test_enprocess_sqlite import alembic_config
from tests.test_volvo_support import T0, VIN, FakeCar, Recorder, connect, iso, poll

EP = C.ACCESSIBILITY_ENDPOINT


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(vc, "_sleep", lambda s: None)
    vc._limiters.clear()


def body(value: Any = "AVAILABLE", reason: Any = None, ts: Any = "2026-10-05T06:15:00Z") -> dict[str, Any]:
    item: dict[str, Any] = {"value": value, "timestamp": ts}
    if reason is not None:
        item["unavailableReason"] = reason
    return {"data": {"availabilityStatus": item}}


# ----------------------------------------------------------------------------- tolkning


def test_available_is_read_and_not_in_use():
    status, reason, ts = vc.accessibility_field(body("AVAILABLE"))
    assert (status, reason, ts) == ("AVAILABLE", None, datetime(2026, 10, 5, 6, 15, tzinfo=UTC))
    assert vc.is_car_in_use(status, reason) is False


def test_unavailable_car_in_use_is_in_use():
    status, reason, _ = vc.accessibility_field(body("UNAVAILABLE", "CAR_IN_USE"))
    assert (status, reason) == ("UNAVAILABLE", "CAR_IN_USE")
    assert vc.is_car_in_use(status, reason) is True


@pytest.mark.parametrize("reason", ["NO_INTERNET", "POWER_SAVING_MODE", "UNSPECIFIED", None])
def test_unavailable_for_other_reasons_is_not_in_use(reason):
    status, got, _ = vc.accessibility_field(body("UNAVAILABLE", reason))
    assert status == "UNAVAILABLE" and got == reason
    assert vc.is_car_in_use(status, got) is False


def test_unknown_values_are_kept_as_they_are_and_not_in_use():
    status, reason, _ = vc.accessibility_field(body("SOMETHING_NEW", "CAR_IN_USE"))
    assert (status, reason) == ("SOMETHING_NEW", "CAR_IN_USE")
    assert vc.is_car_in_use(status, reason) is False
    assert vc.is_car_in_use("UNSPECIFIED", None) is False


def test_reason_beside_the_status_and_value_objects_are_tolerated():
    payload = {"data": {"availabilityStatus": {"value": "UNAVAILABLE"},
                        "unavailableReason": {"value": "CAR_IN_USE", "timestamp": "2026-10-05T06:15:00Z"}}}
    status, reason, ts = vc.accessibility_field(payload)
    assert (status, reason) == ("UNAVAILABLE", "CAR_IN_USE") and ts is not None
    assert vc.accessibility_field({"data": {"availabilityStatus": "AVAILABLE"}})[0] == "AVAILABLE"


@pytest.mark.parametrize(
    "payload",
    [None, [], "x", {}, {"data": None}, {"data": []}, {"data": {}}, {"data": {"availabilityStatus": None}},
     {"data": {"availabilityStatus": {"value": None}}}, {"data": {"availabilityStatus": {"value": 3}}},
     {"data": {"availabilityStatus": {"value": "   "}}}, {"data": {"availabilityStatus": [1, 2]}}],
)
def test_broken_responses_give_nothing(payload):
    assert vc.accessibility_field(payload) == (None, None, None)
    fields = collector.parse_results({EP: ApiResult("ok", 200, data=payload)})
    assert fields["accessibility_status"] is None and fields["car_in_use"] is None


def test_odd_types_and_long_or_dirty_values_are_cleaned():
    status, reason, ts = vc.accessibility_field(body("UNAVAILABLE\x00\n", {"x": 1}, ts=12345))
    assert status == "UNAVAILABLE" and reason is None and ts is None
    long_status, _, _ = vc.accessibility_field(body("X" * 500))
    assert long_status is not None and len(long_status) <= 40


# ----------------------------------------------------------------------------- sparas


@dataclass
class InUseCar(FakeCar):
    """FakeCar som också svarar på command-accessibility."""

    access: str = "AVAILABLE"
    reason: str | None = None
    access_ts: datetime = T0 - timedelta(hours=1)
    access_result: ApiResult | None = None

    def fetch(self, endpoint_name: str, vin: str) -> ApiResult:
        if endpoint_name != EP:
            return super().fetch(endpoint_name, vin)
        self.calls.append(endpoint_name)
        if self.access_result is not None:
            return self.access_result
        return ApiResult("ok", 200, data=body(self.access, self.reason, iso(self.access_ts)))

    def start(self, at: datetime) -> None:
        """Tändningen slås på: motorstart och "används" rapporteras, mätare och position inte."""
        self.engine, self.engine_ts = "RUNNING", at
        self.access, self.reason, self.access_ts = "UNAVAILABLE", "CAR_IN_USE", at


def _rows(db, vehicle) -> list[M.VehicleObservation]:
    return list(db.execute(
        select(M.VehicleObservation).where(M.VehicleObservation.vehicle_id == vehicle.id)
        .order_by(M.VehicleObservation.fetched_at)
    ).scalars())


def test_status_is_stored_and_a_changed_status_gives_a_new_row(db, user, vehicle):
    connect(db, user, vehicle)
    car = InUseCar()
    poll(db, vehicle, car, T0)
    poll(db, vehicle, car, T0 + timedelta(minutes=2))
    rows = _rows(db, vehicle)
    assert len(rows) == 1 and rows[0].seen_count == 2
    assert (rows[0].accessibility_status, rows[0].accessibility_reason, rows[0].car_in_use) == ("AVAILABLE", None, False)
    assert rows[0].accessibility_reported_at == T0 - timedelta(hours=1)

    # Bara statusen ändras (ingen motorstart rapporterad): ny rad med egen hämtningstid.
    car.access, car.reason, car.access_ts = "UNAVAILABLE", "CAR_IN_USE", T0 + timedelta(minutes=3)
    poll(db, vehicle, car, T0 + timedelta(minutes=4))
    rows = _rows(db, vehicle)
    assert len(rows) == 2
    new = rows[1]
    assert new.fetched_at == T0 + timedelta(minutes=4)
    assert (new.accessibility_status, new.accessibility_reason, new.car_in_use) == ("UNAVAILABLE", "CAR_IN_USE", True)
    assert new.accessibility_reported_at == T0 + timedelta(minutes=3)
    assert new.odometer_km == rows[0].odometer_km and new.latitude == rows[0].latitude


def test_only_a_new_timestamp_does_not_give_a_new_row(db, user, vehicle):
    connect(db, user, vehicle)
    car = InUseCar()
    poll(db, vehicle, car, T0)
    car.access_ts = T0  # samma status, ny tidsstämpel
    poll(db, vehicle, car, T0 + timedelta(minutes=2))
    rows = _rows(db, vehicle)
    assert len(rows) == 1 and rows[0].seen_count == 2
    assert rows[0].accessibility_reported_at == T0 - timedelta(hours=1)  # övergångens tid står kvar


def test_without_a_status_the_hash_is_the_same_as_before():
    fields = collector.parse_results({"odometer": ApiResult("ok", 200, data={"data": {"odometer": {"value": 5}}})})
    with_empty = collector.parse_results({
        "odometer": ApiResult("ok", 200, data={"data": {"odometer": {"value": 5}}}),
        EP: ApiResult("ok", 200, data={"data": {}}),
    })
    assert collector.content_hash(fields) == collector.content_hash(with_empty)
    with_status = dict(fields, accessibility_status="AVAILABLE")
    assert collector.content_hash(with_status) != collector.content_hash(fields)


def test_only_an_accessibility_answer_stores_no_row(db, user, vehicle):
    connect(db, user, vehicle)
    car = InUseCar(unsupported={"odometer", "location", "fuel", "statistics", "engine_status", "energy_state"})
    out = poll(db, vehicle, car, T0)
    assert out["observation"] == "none" and _rows(db, vehicle) == []


# ----------------------------------------------------------------------------- fel och kvot


@pytest.mark.parametrize("http", [403, 404])
def test_403_404_stops_polling_without_failing_the_poll(db, user, vehicle, http):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"command-accessibility": httpx.Response(http, json={"error": {"message": "Nej"}})})
    out = collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    assert out["status"] == "ok" and out["observation"] == "new"
    assert vehicle.capabilities["endpoints"][EP]["state"] == C.CAP_UNSUPPORTED
    assert conn.last_error is None
    second = collector.poll_vehicle(db, vehicle, now=T0 + timedelta(minutes=2), transport=rec.transport)
    assert second["status"] == "ok" and second["endpoints"][EP] == "skipped_unsupported"
    paths = [c.url.path for c in rec.api_calls()]
    assert sum(p.endswith("/command-accessibility") for p in paths) == 1
    # Efter omprovningstiden provas den igen.
    later = T0 + timedelta(hours=C.REPROBE_UNSUPPORTED_HOURS, minutes=1)
    collector.poll_vehicle(db, vehicle, now=later, transport=rec.transport)
    assert sum(c.url.path.endswith("/command-accessibility") for c in rec.api_calls()) == 2


def test_an_error_in_this_endpoint_never_fails_the_poll(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"command-accessibility": httpx.Response(500, json={})})
    out = collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    assert out["status"] == "ok"  # inte "partial" och inte "error"
    assert conn.last_error is None
    run = db.execute(select(M.IntegrationSyncRun)).scalar_one()
    assert run.status == "ok"
    obs = _rows(db, vehicle)[0]
    assert obs.odometer_km == 1234 and obs.accessibility_status is None and obs.car_in_use is None


def test_one_more_connected_vehicle_call_per_poll_is_counted(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"command-accessibility": httpx.Response(200, json=body("AVAILABLE"))})
    collector.poll_vehicle(db, vehicle, now=T0, transport=rec.transport)
    cv_paths = [c.url.path for c in rec.api_calls() if c.url.path.startswith("/connected-vehicle/")]
    assert len(cv_paths) == 5 and any(p.endswith("/command-accessibility") for p in cv_paths)
    assert conn.capabilities["quota"]["calls"]["connected-vehicle"] == 5
    assert C.ENDPOINTS[EP].polled and C.POLL_ENDPOINTS[-1] == EP  # sist: övriga hämtas först
    # Var 120:e sekund: 5 × 720 = 3600 anrop/dygn mot connected-vehicle, under budgeten.
    assert 5 * (86400 // 120) == 3600 < C.DAILY_BUDGET_PER_API
    obs = _rows(db, vehicle)[0]
    assert obs.accessibility_status == "AVAILABLE" and VIN not in str(obs.raw)


def test_a_supported_endpoint_needs_several_404_before_it_is_dropped(db, user, vehicle):
    connect(db, user, vehicle)
    car = InUseCar()
    poll(db, vehicle, car, T0)
    car.access_result = ApiResult("unsupported", 404, message="Not found")
    for i in range(1, C.UNSUPPORTED_CONFIRMATIONS):
        out = poll(db, vehicle, car, T0 + timedelta(minutes=2 * i))
        assert out["status"] == "ok"
        assert vehicle.capabilities["endpoints"][EP]["state"] == C.CAP_SUPPORTED
    poll(db, vehicle, car, T0 + timedelta(minutes=2 * C.UNSUPPORTED_CONFIRMATIONS))
    assert vehicle.capabilities["endpoints"][EP]["state"] == C.CAP_UNSUPPORTED


# ----------------------------------------------------------------------------- detekteringen


# Påhittade koordinater: A–B ca 7 km, A–C ca 144 km fågelväg (som i tidslinjen).
PA = (59.000, 15.000)
PB = (59.063, 15.000)
PC = (60.294, 15.000)


def test_a_row_with_only_a_changed_status_gives_no_trip(db, user, vehicle):
    connect(db, user, vehicle)
    car = InUseCar(lat=PA[0], lon=PA[1])
    t = T0
    for _ in range(3):
        poll(db, vehicle, car, t)
        t += timedelta(minutes=2)
    car.access, car.reason, car.access_ts = "UNAVAILABLE", "CAR_IN_USE", t - timedelta(seconds=30)
    for _ in range(10):
        out = poll(db, vehicle, car, t)
        assert out["new_trips"] == 0
        t += timedelta(minutes=2)
    car.access, car.reason, car.access_ts = "AVAILABLE", None, t - timedelta(seconds=30)
    for _ in range(10):
        out = poll(db, vehicle, car, t)
        assert out["new_trips"] == 0
        t += timedelta(minutes=2)
    assert len(_rows(db, vehicle)) == 3
    assert db.execute(select(M.RawTrip)).scalars().all() == []
    assert all(r.processed_at is not None for r in _rows(db, vehicle))  # ankaret följde med
    gaps = db.execute(select(M.ReviewItem)).scalars().all()
    assert [g for g in gaps if (g.data or {}).get("type") in ("collection_gap", "stale_data")] == []


def test_the_owners_drive_with_in_use_rows_gives_one_trip_with_the_right_distance(db, user, vehicle):
    """Tidslinjen: stillastående vid A, motorstart (och "används") 120,6 min före ankomsten,
    61 hämtningar utan ny mätare eller position, ankomst vid C med +194 km."""
    connect(db, user, vehicle)
    arrive = T0 + timedelta(hours=3)
    car = InUseCar(lat=PA[0], lon=PA[1], odometer=10000, odo_ts=arrive - timedelta(hours=20),
                   pos_ts=arrive - timedelta(hours=20), engine_ts=arrive - timedelta(hours=20),
                   access_ts=arrive - timedelta(hours=20))
    t = arrive - timedelta(minutes=140.4)
    while t < arrive - timedelta(minutes=121):
        poll(db, vehicle, car, t)
        t += timedelta(minutes=2)
    car.start(arrive - timedelta(minutes=120.6))
    while t < arrive:
        assert poll(db, vehicle, car, t)["new_trips"] == 0
        t += timedelta(minutes=2)
    car.odometer += 194
    car.odo_ts = arrive + timedelta(minutes=0.5)
    car.lat, car.lon, car.pos_ts = PC[0], PC[1], arrive
    car.engine, car.engine_ts = "STOPPED", arrive + timedelta(minutes=0.5)
    car.access, car.reason, car.access_ts = "AVAILABLE", None, arrive + timedelta(minutes=0.5)
    t = arrive + timedelta(minutes=1.7)
    created = 0
    for _ in range(15):
        created += poll(db, vehicle, car, t)["new_trips"]
        t += timedelta(minutes=2)
    raws = db.execute(select(M.RawTrip).where(M.RawTrip.voided_at.is_(None))).scalars().all()
    assert created == 1 and len(raws) == 1
    assert raws[0].distance_km == pytest.approx(194, abs=0.5)
    in_use = [r for r in _rows(db, vehicle) if r.car_in_use]
    assert len(in_use) == 1 and in_use[0].accessibility_reported_at == arrive - timedelta(minutes=120.6)
    assert in_use[0].seen_count >= 60


# ----------------------------------------------------------------------------- demobilen


def test_demo_car_reports_in_use_while_driving_and_available_when_parked(db, user, vehicle):
    connect(db, user, vehicle, tokens=False)
    car = demo.enable(db, user.id, now=T0)
    demo.simulate_trip(db, user.id, "commute", now=T0 + timedelta(hours=2))
    rows = _rows(db, car)
    assert any(r.car_in_use is True and r.accessibility_reason == "CAR_IN_USE" for r in rows)
    assert any(r.car_in_use is False and r.accessibility_status == "AVAILABLE" for r in rows)
    for r in rows:
        assert r.car_in_use is (r.engine_running is True)
    assert rows[-1].car_in_use is False


# ----------------------------------------------------------------------------- felsökningsfilen


def test_diagnostics_shows_the_fields_without_vin_or_positions(db, user, vehicle):
    connect(db, user, vehicle)
    vehicle.registration_number = "ABC123"
    car = InUseCar(lat=59.1234567, lon=15.7654321)
    now = datetime.now(UTC)
    poll(db, vehicle, car, now - timedelta(minutes=10))
    car.start(now - timedelta(minutes=9))
    poll(db, vehicle, car, now - timedelta(minutes=8))
    car.access, car.reason = "UNAVAILABLE", "Fri text med <b>html</b> och " + VIN
    poll(db, vehicle, car, now - timedelta(minutes=6))
    out = volvo_routes.build_diagnostics(db, vehicle, days=1, now=now)
    obs = out["observations"]
    assert [o["accessibility_status"] for o in obs] == ["AVAILABLE", "UNAVAILABLE", "UNAVAILABLE"]
    assert [o["car_in_use"] for o in obs] == [False, True, False]
    assert obs[1]["accessibility_reason"] == "CAR_IN_USE"
    assert obs[1]["accessibility_reported_at"] == (now - timedelta(minutes=9)).isoformat(timespec="seconds")
    assert obs[2]["accessibility_reason"] == "(annat värde)"
    dump = str(out)
    assert VIN not in dump and VIN[-6:] not in dump and "ABC123" not in dump
    assert "59.123" not in dump and "15.765" not in dump and "latitude" not in dump
    assert not re.search(r"(?<![:\d])\d{1,3}\.\d{4,}", dump)


# ----------------------------------------------------------------------------- migreringen

NEW = {"accessibility_status", "accessibility_reason", "accessibility_reported_at", "car_in_use"}


def _check_migration(url: str, engine) -> None:
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    assert NEW <= {c["name"] for c in inspect(engine).get_columns("vehicle_observations")}
    command.downgrade(cfg, "0005")
    assert not NEW & {c["name"] for c in inspect(engine).get_columns("vehicle_observations")}
    command.upgrade(cfg, "0006")
    assert NEW <= {c["name"] for c in inspect(engine).get_columns("vehicle_observations")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006"


def test_migration_up_and_down_on_sqlite(tmp_path):
    url = f"sqlite:///{tmp_path}/m.sqlite3"
    engine = create_engine(url)
    try:
        _check_migration(url, engine)
    finally:
        engine.dispose()


@pytest.mark.skipif(not (os.environ.get("DRIVELOG_TEST_DATABASE_URL") or "").startswith("postgresql"),
                    reason="kräver DRIVELOG_TEST_DATABASE_URL mot PostgreSQL")
def test_migration_up_and_down_on_postgresql():
    base = os.environ["DRIVELOG_TEST_DATABASE_URL"]
    schema = f"mig_{uuid.uuid4().hex[:10]}"  # eget schema: rör inte de andra testernas tabeller
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    target = make_url(base).update_query_dict({"options": f"-csearch_path={schema}"})
    url = target.render_as_string(hide_password=False)
    engine = create_engine(target)
    try:
        _check_migration(url, engine)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
