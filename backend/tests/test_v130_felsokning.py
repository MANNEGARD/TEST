"""DEL 7 (1.3.0): felsökningsfilen under Integrationer → Volvo.

Bilens avläsningar, hämtningarnas utfall och de resor som byggdes av dem – UTAN VIN,
registreringsnummer, positioner, adresser, platsnamn, rådata, feltexter och nycklar.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import app.models as M
from app.models.enums import IntegrationProvider, IntegrationStatus, TripSource, ValueQuality
from app.services import integrations
from app.services.trips import segments as seg
from app.services.volvo import demo

VIN = "YV1ZZZ12345678901"
REG = "ABC123"
ADDRESS = "Hemlighetsgatan 17, 123 45 Exempelby"
PLACE = "Exempel Åkeri AB"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.hemlig-token"
COORD_RE = re.compile(r"(?<![:\d])\d{1,3}\.\d{4,}")  # tidpunkternas sekunder räknas inte


def setup_car(db, user, vehicle) -> None:
    now = datetime.now(UTC)
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.VOLVO)
    conn.status = IntegrationStatus.CONNECTED
    integrations.set_secrets(conn, {"access_token": TOKEN})
    vehicle.vin, vehicle.registration_number = VIN, REG
    vehicle.volvo_connection_id = conn.id
    vehicle.model, vehicle.fuel_type = "XC60 Recharge", "PETROL/ELECTRIC"
    db.flush()
    run = integrations.start_run(db, conn, "poll")
    integrations.finish_run(run, "error", stats={"observation": "new", "new_trips": 1},
                            error=f"Fel för {VIN} vid {ADDRESS}", http_status=503)
    obs = []
    for i, minutes in enumerate((-40, -5, 30)):
        at = now - timedelta(hours=2) + timedelta(minutes=minutes)
        o = M.VehicleObservation(
            vehicle_id=vehicle.id, fetched_at=at, last_seen_at=at, seen_count=2, odometer_km=52340.0 + i * 15,
            odometer_reported_at=at, latitude=57.7826123, longitude=14.1618456, heading=91.5,
            position_reported_at=at, fuel_liters=40.25 - i, battery_percent=80.0 - i * 5, avg_fuel_consumption=5.4,
            trip_meter_auto_km=float(i * 15), avg_speed_auto=48.0, charging_status="IDLE", engine_running=False,
            content_hash=f"h{i}", raw={"vin": VIN, "address": ADDRESS, "registration": REG},
            errors={"location": f"Kunde inte läsa {ADDRESS}", "Fri text med mellanslag": "x"},
        )
        db.add(o)
        obs.append(o)
    db.flush()
    loc = M.KnownLocation(owner_user_id=user.id, name=PLACE, address=ADDRESS, latitude=57.7826123,
                          longitude=14.1618456, confirmed=True, type="customer")
    db.add(loc)
    db.flush()
    raw = M.RawTrip(
        vehicle_id=vehicle.id, source=TripSource.VOLVO_API, source_key="k1", started_at=obs[1].fetched_at,
        ended_at=obs[2].fetched_at, odometer_start=52355.0, odometer_end=52370.0, distance_km=15.0,
        start_address=ADDRESS, end_address=f"{PLACE}, {ADDRESS}", start_lat=57.7826123, start_lon=14.1618456,
        end_lat=57.7926123, end_lon=14.1718456, end_location_id=loc.id, fuel_liters=0.81,
        fuel_quality=ValueQuality.ESTIMATED, quality_flags=["reconstructed", "end_time_uncertain"],
        observation_ids=[str(o.id) for o in obs[1:]], original_title=PLACE, original_notes=ADDRESS,
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    seg.create_logical_for_raw(db, raw)
    db.commit()


def test_file_has_readings_runs_and_trips_without_identifying_data(client, db, user, vehicle):
    setup_car(db, user, vehicle)
    resp = client.get("/api/v1/volvo/diagnostics?days=14")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert re.fullmatch(r'attachment; filename="drivelog-felsokning-\d{4}-\d{2}-\d{2}\.json"',
                        resp.headers["content-disposition"])
    assert resp.headers["cache-control"] == "no-store"
    text = resp.text
    for secret in (VIN, VIN[-6:], REG, "Hemlighetsgatan", "Exempelby", PLACE, "Åkeri", TOKEN, "hemlig-token",
                   "57.78", "14.16", "latitude", "longitude", "heading", '"raw"', "Kunde inte läsa", "Fri text",
                   str(vehicle.id), str(user.id)):
        assert secret not in text, secret
    assert not COORD_RE.search(text)

    data = json.loads(text)
    assert set(data) == {"generated_at", "app_version", "days", "vehicle", "observations", "sync_runs", "trips",
                         "internal_errors", "months"}  # 1.4.1; "months" 1.5.0
    assert data["months"] == []  # ingen rad i månadsskiftet
    assert data["internal_errors"] == []
    assert data["days"] == 14 and data["vehicle"] == {"model": "XC60 Recharge", "fuel_type": "PETROL/ELECTRIC",
                                                       "demo": False}
    assert [o["id"] for o in data["observations"]] == ["obs-1", "obs-2", "obs-3"]
    first = data["observations"][0]
    assert first["odometer_km"] == 52340.0 and first["fuel_liters"] == 40.25 and first["trip_meter_auto_km"] == 0.0
    assert first["errors_keys"] == ["location"]
    assert data["sync_runs"] == [{
        "started_at": data["sync_runs"][0]["started_at"], "finished_at": data["sync_runs"][0]["finished_at"],
        "status": "error", "error_code": "http_503", "observation": "new", "new_trips": 1,
    }]
    (trip,) = data["trips"]
    assert trip["id"] == "trip-1" and trip["observations"] == ["obs-2", "obs-3"]
    assert trip["distance_km"] == 15.0 and trip["fuel_quality"] == "estimated" and trip["fuel_liters"] == 0.81
    assert trip["quality_flags"] == ["end_time_uncertain", "reconstructed"]
    # 1.5.0: skyddet och skälen (känd starttid och bensin: inga skäl; elen saknas utan kapacitet).
    assert trip["protected_by_month"] is False
    assert (trip["start_unknown_reason"], trip["start_unknown_steps"], trip["fuel_unknown_reason"]) == (None, [], None)
    assert trip["energy_unknown_reason"] == "no_capacity"


def test_days_are_limited_to_one_to_31(client, db, user, vehicle):
    setup_car(db, user, vehicle)
    assert client.get("/api/v1/volvo/diagnostics?days=0").status_code == 422
    assert client.get("/api/v1/volvo/diagnostics?days=32").status_code == 422
    old = client.get("/api/v1/volvo/diagnostics?days=1").json()
    assert len(old["observations"]) == 3  # alla är från de senaste två timmarna


def test_demo_car_gives_the_same_file_with_demo_data(client, db, user):
    demo.enable(db, user.id)
    demo.simulate_trip(db, user.id, "commute")
    db.commit()
    data = client.get("/api/v1/volvo/diagnostics").json()
    assert data["vehicle"]["demo"] is True and data["observations"] and data["trips"]
    assert demo.DEMO_VIN not in json.dumps(data) and "DEMO123" not in json.dumps(data)
    assert not COORD_RE.search(json.dumps(data))


def test_file_requires_login(anon_client):
    assert anon_client.get("/api/v1/volvo/diagnostics").status_code == 401
