"""Dashboard- och rapport-API via TestClient."""

from __future__ import annotations

import app.models as M
from app.models.enums import Category, ReviewKind
from app.security import passwords
from tests.conftest import TEST_PASSWORD

SEPT = {"period": "custom", "from": "2026-09-01", "to": "2026-09-30"}


def seed(db, trips):
    trips.trip("Kundvägen 1, Storstad", "Lagergatan 3, Storstad", category=Category.WORK, purpose="Kundbesök", km=12.4)
    trips.trip("Lagergatan 3, Storstad", "Hemvägen 2, Byn", category=Category.PRIVATE, km=7.6, gap_minutes=60)
    db.commit()


def test_dashboard_endpoints(client, db, trips):
    seed(db, trips)
    r = client.get("/api/v1/dashboard/summary", params=SEPT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["distance"]["total"] == {"km": 20.0, "mil": 2.0}
    assert body["distance"]["work"]["km"] == 12.4 and body["distance"]["private"]["km"] == 7.6
    assert body["period"]["timezone"] == "Europe/Stockholm" and body["period"]["key"] == "custom"
    assert body["period"]["start"].startswith("2026-08-31T22:00:00")

    r = client.get("/api/v1/dashboard/timeseries", params={**SEPT, "bucket": "week"})
    points = r.json()["points"]
    assert r.status_code == 200 and sum(p["total_km"] for p in points) == 20.0
    assert next(p for p in points if p["total_km"])["label"] == "v38 2026"
    assert client.get("/api/v1/dashboard/timeseries", params={**SEPT, "bucket": "hour"}).status_code == 422

    dest = client.get("/api/v1/dashboard/destinations", params=SEPT).json()["items"]
    assert {d["label"] for d in dest} == {"Lagergatan 3, Storstad", "Privat resa (Byn)"}
    rts = client.get("/api/v1/dashboard/routes", params=SEPT).json()["items"]
    assert sum(r["trips"] for r in rts) == 2

    monthly = client.get("/api/v1/dashboard/monthly", params={"year": 2026}).json()
    assert monthly["months"][8]["current"]["total_km"] == 20.0 and monthly["previous_year"] == 2025

    year = client.get("/api/v1/dashboard/summary", params={"period": "year", "year": 2026}).json()
    assert year["distance"]["total"]["km"] == 20.0 and year["period"]["label"] == "2026"
    empty = client.get("/api/v1/dashboard/summary", params={"period": "custom", "from": "2025-01-01", "to": "2025-01-31"})
    assert empty.json()["distance"]["total"]["km"] == 0.0


def test_invalid_periods(client):
    assert client.get("/api/v1/dashboard/summary", params={"period": "custom"}).status_code == 400
    r = client.get("/api/v1/dashboard/summary", params={"period": "custom", "from": "2026-09-30", "to": "2026-09-01"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_period"
    assert client.get("/api/v1/dashboard/summary", params={"period": "decade"}).status_code == 422


def test_reports_and_anomaly_scan(client, db, trips, vehicle):
    seed(db, trips)
    weekly = client.get("/api/v1/reports/weekly", params={"week_start": "2026-09-16"})
    assert weekly.status_code == 200 and weekly.json()["week"]["start"] == "2026-09-14"
    assert weekly.json()["distance"]["work"]["km"] == 12.4
    daily = client.get("/api/v1/reports/daily", params={"day": "2026-09-14"})
    assert daily.status_code == 200 and daily.json()["trip_count"] == 2

    raw = db.query(M.RawTrip).first()
    raw.odometer_end = raw.odometer_start + 60
    db.commit()
    r = client.post("/api/v1/dashboard/anomalies/scan")
    # Sträckan stämmer inte med mätaren, och nästa resa börjar "bakåt" i mätarställning.
    assert r.status_code == 200 and r.json()["created"] == {"odometer_mismatch": 1, "odometer_backwards": 1}
    assert client.post("/api/v1/dashboard/anomalies/scan").json()["created_total"] == 0
    db.expire_all()
    assert db.query(M.ReviewItem).filter(M.ReviewItem.kind == ReviewKind.ANOMALY).count() == 2


def test_other_users_vehicle_is_rejected(client, db):
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.flush()
    car = M.Vehicle(owner_user_id=other.id, name="Annans bil", is_default=True)
    db.add(car)
    db.commit()
    for url in ("/api/v1/dashboard/summary", "/api/v1/dashboard/timeseries", "/api/v1/reports/weekly",
                "/api/v1/energy/summary"):
        assert client.get(url, params={"vehicle_id": str(car.id)}).status_code == 404, url
    assert client.post("/api/v1/dashboard/anomalies/scan", params={"vehicle_id": str(car.id)}).status_code == 404
