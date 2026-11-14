"""Bränsle och laddning: sammanställning, täckningsgrad, tankningsförslag och API (CRUD)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, ValueQuality
from app.services import settings as settings_svc
from app.services.stats import common
from app.services.stats import energy as energy_stats
from app.services.trips import segments as seg
from tests.conftest import BASE_TIME, TEST_PASSWORD

TZ = ZoneInfo("Europe/Stockholm")
SEPT = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))


def set_quality(db, trip, *, fuel=None, fuel_q=ValueQuality.UNKNOWN, kwh=None, kwh_q=ValueQuality.UNKNOWN):
    raw = trip.segments[0].raw_trip
    raw.fuel_liters, raw.fuel_quality, raw.energy_kwh, raw.energy_quality = fuel, fuel_q, kwh, kwh_q
    seg.refresh_logical(db, trip)
    db.flush()


def obs(db, vehicle, at, fuel, km=None, avg=None):
    db.add(M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=at, last_seen_at=at, fuel_liters=fuel, odometer_km=km,
                                avg_fuel_consumption=avg, content_hash=f"{at.timestamp()}-{fuel}"))
    db.flush()


# ----------------------------------------------------------------------------- sammanställning


def test_summary_separates_verified_estimated_and_unknown(db, user, vehicle, trips):
    t1 = trips.trip(category=Category.WORK, purpose="Kundbesök", km=40.0)  # verifierad: 2,8 l, 2,0 kWh
    t2 = trips.trip(category=Category.WORK, purpose="Kundbesök", km=20.0, gap_minutes=60)
    t3 = trips.trip(category=Category.PRIVATE, km=40.0, gap_minutes=60)
    set_quality(db, t2, fuel=5.0, fuel_q=ValueQuality.ESTIMATED, kwh=4.0, kwh_q=ValueQuality.ESTIMATED)
    set_quality(db, t3)
    obs(db, vehicle, BASE_TIME, 30.0, avg=5.5)  # bilens snitt och tanknivå – inte förbrukning per resa
    settings_svc.update_section(db, user.id, "costs", {"fuel_price_per_liter": 20.0})
    db.add(M.FuelRecord(vehicle_id=vehicle.id, at=BASE_TIME + timedelta(hours=1), liters=40.0, total_cost=800.0))
    db.add(M.ChargingRecord(vehicle_id=vehicle.id, started_at=BASE_TIME + timedelta(hours=8), kwh=10.0, total_cost=25.0))
    db.flush()

    out = energy_stats.summary(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ)
    fuel = out["fuel"]
    assert out["total_km"] == 100.0
    assert fuel["verified"] == pytest.approx(2.8) and fuel["verified_km"] == 40.0
    assert fuel["estimated"] == pytest.approx(5.0) and fuel["estimated_km"] == 20.0
    assert fuel["unknown_km"] == 40.0
    assert fuel["coverage"] == pytest.approx(0.4)
    assert fuel["per_100km"] == pytest.approx(7.0)  # 2,8 l / 40 km – inte över 100 km och inte bilens 5,5
    assert out["electricity"]["per_100km"] == pytest.approx(5.0)
    assert out["vehicle_reported_average"]["fuel_l_per_100km"] == 5.5
    assert out["refuels"] == {"count": 1, "liters": 40.0, "manual": 1, "detected": 0, "cost": 800.0,
                              "average_price_per_liter": 20.0}
    assert out["charging"] == {"count": 1, "kwh": 10.0, "cost": 25.0}
    assert out["estimated_costs"]["fuel"] == pytest.approx(56.0)  # 2,8 l × 20 kr
    (month,) = out["monthly"]
    assert month["month"] == "2026-09" and month["fuel_coverage"] == pytest.approx(0.4) and month["refuels"] == 1
    del t1


def test_coverage_none_when_no_distance(db, user, vehicle):
    out = energy_stats.summary(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ)
    assert out["fuel"]["coverage"] is None and out["fuel"]["per_100km"] is None and out["monthly"] == []


def test_detect_refuels_is_idempotent_and_respects_manual(db, user, vehicle):
    t0 = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    obs(db, vehicle, t0, 12.0, km=10000)
    obs(db, vehicle, t0 + timedelta(hours=2), 55.0, km=10010)  # tankning ~43 l
    obs(db, vehicle, t0 + timedelta(hours=5), 50.0, km=10060)  # förbrukning, ingen tankning
    obs(db, vehicle, t0 + timedelta(days=2), 20.0, km=10500)
    obs(db, vehicle, t0 + timedelta(days=2, hours=3), 58.0, km=10510)  # andra tankningen – manuellt registrerad
    db.add(M.FuelRecord(vehicle_id=vehicle.id, at=t0 + timedelta(days=2, hours=1), liters=38.5, source="manual"))
    db.flush()

    created = energy_stats.detect_refuels(db, vehicle)
    assert len(created) == 1
    rec = created[0]
    assert rec.liters == 43.0 and rec.liters_quality == ValueQuality.ESTIMATED and rec.source == "detected"
    assert "uppskattning" in rec.note
    assert energy_stats.detect_refuels(db, vehicle) == []
    assert len(db.execute(select(M.FuelRecord)).scalars().all()) == 2


# ----------------------------------------------------------------------------- API


def test_fuel_crud_and_validation(client, db, vehicle):
    bad = [
        {"at": "2026-09-14T08:00:00+02:00", "liters": 0},
        {"at": "2026-09-14T08:00:00+02:00", "liters": 10, "total_cost": -1},
        {"at": "2026-09-14T08:00:00", "liters": 10},  # saknar tidszon
    ]
    for body in bad:
        r = client.post("/api/v1/energy/fuel", json=body)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "validation_error"

    r = client.post("/api/v1/energy/fuel", json={"at": "2026-09-14T08:00:00+02:00", "liters": 40, "total_cost": 790,
                                                 "station": "Macken", "full_tank": True})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["vehicle_id"] == str(vehicle.id) and rec["price_per_liter"] == 19.75 and rec["source"] == "manual"
    assert rec["at"].startswith("2026-09-14T06:00:00")

    r = client.get("/api/v1/energy/fuel")
    assert [x["id"] for x in r.json()] == [rec["id"]]
    r = client.patch(f"/api/v1/energy/fuel/{rec['id']}", json={"liters": 41.5, "note": "Kvitto"})
    assert r.status_code == 200 and r.json()["liters"] == 41.5 and r.json()["note"] == "Kvitto"
    assert client.patch(f"/api/v1/energy/fuel/{rec['id']}", json={"liters": None}).status_code == 400
    assert client.patch(f"/api/v1/energy/fuel/{rec['id']}", json={"liters": -3}).status_code == 422

    summary = client.get("/api/v1/energy/summary", params={"period": "custom", "from": "2026-09-01", "to": "2026-09-30"})
    assert summary.status_code == 200 and summary.json()["refuels"]["count"] == 1
    assert client.delete(f"/api/v1/energy/fuel/{rec['id']}").status_code == 204
    assert client.get(f"/api/v1/energy/fuel/{rec['id']}").status_code == 404


def test_charging_crud_and_validation(client):
    base = {"started_at": "2026-09-14T18:00:00+02:00", "ended_at": "2026-09-14T22:00:00+02:00"}
    assert client.post("/api/v1/energy/charging", json={**base, "kwh": 0}).status_code == 422
    assert client.post("/api/v1/energy/charging", json={**base, "kwh": 5, "total_cost": -2}).status_code == 422
    assert client.post("/api/v1/energy/charging",
                       json={**base, "kwh": 5, "ended_at": "2026-09-14T17:00:00+02:00"}).status_code == 422
    assert client.post("/api/v1/energy/charging", json={"started_at": "2026-09-14T18:00:00", "kwh": 5}).status_code == 422
    r = client.post("/api/v1/energy/charging", json={**base, "kwh": 11.2, "total_cost": 28.0, "location": "Hemma"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = client.patch(f"/api/v1/energy/charging/{cid}", json={"ended_at": "2026-09-14T17:00:00+02:00"})
    assert r.status_code == 400
    r = client.patch(f"/api/v1/energy/charging/{cid}", json={"kwh": 12.0})
    assert r.status_code == 200 and r.json()["kwh"] == 12.0
    assert len(client.get("/api/v1/energy/charging").json()) == 1
    assert client.delete(f"/api/v1/energy/charging/{cid}").status_code == 204
    assert client.get("/api/v1/energy/charging").json() == []


def test_other_users_records_are_not_accessible(client, db):
    from app.security import passwords

    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.flush()
    car = M.Vehicle(owner_user_id=other.id, name="Annans bil", registration_number="XYZ999", is_default=True)
    db.add(car)
    db.flush()
    rec = M.FuelRecord(vehicle_id=car.id, at=BASE_TIME, liters=30.0)
    ch = M.ChargingRecord(vehicle_id=car.id, started_at=BASE_TIME, kwh=5.0)
    db.add_all([rec, ch])
    db.commit()
    assert client.get(f"/api/v1/energy/fuel/{rec.id}").status_code == 404
    assert client.patch(f"/api/v1/energy/fuel/{rec.id}", json={"liters": 1}).status_code == 404
    assert client.delete(f"/api/v1/energy/fuel/{rec.id}").status_code == 404
    assert client.get(f"/api/v1/energy/charging/{ch.id}").status_code == 404
    assert client.get("/api/v1/energy/fuel", params={"vehicle_id": str(car.id)}).status_code == 404
    assert client.post("/api/v1/energy/fuel", json={"vehicle_id": str(car.id), "at": "2026-09-14T08:00:00+02:00",
                                                    "liters": 10}).status_code == 404


def test_detect_refuels_route(client, db, vehicle):
    t0 = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    obs(db, vehicle, t0, 10.0)
    obs(db, vehicle, t0 + timedelta(hours=1), 50.0)
    db.commit()
    r = client.post("/api/v1/energy/detect-refuels")
    assert r.status_code == 200 and r.json()["created"] == 1
    assert r.json()["items"][0]["liters_quality"] == "estimated"
    assert client.post("/api/v1/energy/detect-refuels").json()["created"] == 0
