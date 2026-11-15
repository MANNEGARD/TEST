"""Export-API: förhandskontroll, skapa, lista, hämta (filnamn/ägare) och radera."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

import app.models as M
from app.models.enums import Category
from app.security import passwords
from app.services import audit
from app.services.export import service as export_svc
from app.services.stats import common
from tests.conftest import TEST_PASSWORD

TZ = ZoneInfo("Europe/Stockholm")
PERIOD = {"from": "2026-09-01", "to": "2026-09-30"}


def seed(db, trips, vehicle):
    start = common.local_midnight(datetime(2026, 9, 1).date(), TZ)
    end = common.local_midnight(datetime(2026, 10, 1).date(), TZ)
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=start, odometer_km=10000, source="year_start"))
    t1 = trips.trip(category=Category.WORK, purpose="Kundbesök", km=12.4)
    t2 = trips.trip(category=Category.PRIVATE, km=7.6, gap_minutes=60)
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=end, odometer_km=trips._odo, source="manual"))
    db.commit()
    return t1, t2


def test_precheck(complete_scope, client, db, vehicle, trips):
    seed(db, trips, vehicle)
    r = client.get("/api/v1/exports/checks", params=PERIOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_complete"] and body["errors"] == 0 and body["status_label"] == "Fullständig"
    r = client.get("/api/v1/exports/checks", params={"from": "2026-10-01", "to": "2026-10-31"})
    body = r.json()
    assert not body["is_complete"] and body["status_label"] == "OFULLSTÄNDIG"
    assert {c["code"] for c in body["checks"]} >= {"no_trips", "incomplete_period"}
    assert client.get("/api/v1/exports/checks").status_code == 400
    assert client.get("/api/v1/exports/checks", params={"from": "2026-10-01", "to": "2026-09-01"}).status_code == 400


def test_create_list_download_delete(client, db, vehicle, trips):
    seed(db, trips, vehicle)
    r = client.post("/api/v1/exports", json={**PERIOD, "layout": "detailed"})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "done" and job["is_complete"]
    assert job["filename"] == "Körjournal_ABC123_2026-09.xlsx"
    assert job["summary"]["total_km"] == 20.0 and job["summary"]["odometer_start"] == 10000
    assert "file_path" not in job

    assert [j["id"] for j in client.get("/api/v1/exports").json()] == [job["id"]]
    assert client.get(f"/api/v1/exports/{job['id']}").json()["sha256"] == job["sha256"]

    r = client.get(f"/api/v1/exports/{job['id']}/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert 'filename="Korjournal_ABC123_2026-09.xlsx"' in cd
    assert "filename*=UTF-8''K%C3%B6rjournal_ABC123_2026-09.xlsx" in cd
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[0] == "Översikt"

    path = Path(db.get(M.ExportJob, uuid.UUID(job["id"])).file_path)
    assert path.is_file()
    assert client.delete(f"/api/v1/exports/{job['id']}").status_code == 204
    assert not path.exists()
    assert client.get(f"/api/v1/exports/{job['id']}").status_code == 404
    assert client.get(f"/api/v1/exports/{job['id']}/download").status_code == 404
    assert client.get("/api/v1/exports").json() == []


def test_create_validation_and_selection(complete_scope, client, db, vehicle, trips):
    t1, _ = seed(db, trips, vehicle)
    assert client.post("/api/v1/exports", json={}).status_code == 422
    assert client.post("/api/v1/exports", json={"from": "2026-09-30", "to": "2026-09-01"}).status_code == 422
    assert client.post("/api/v1/exports", json={"trip_ids": []}).status_code == 422
    r = client.post("/api/v1/exports", json={"trip_ids": [str(t1.id)]})
    assert r.status_code == 201 and r.json()["filename"] == "Körjournal_ABC123_urval.xlsx"
    assert not r.json()["is_complete"] and r.json()["summary"]["trips"] == 1
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post("/api/v1/exports", json={"trip_ids": [missing]})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_other_users_export_is_not_accessible(client, db, vehicle, trips):
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.flush()
    car = M.Vehicle(owner_user_id=other.id, name="Annans bil", registration_number="XYZ999", is_default=True)
    db.add(car)
    db.flush()
    job = export_svc.create_export(
        db, user_id=other.id, vehicle=car, start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC),
        actor=audit.user_actor(other.id), tz=TZ,
    )
    db.commit()
    assert client.get(f"/api/v1/exports/{job.id}").status_code == 404
    assert client.get(f"/api/v1/exports/{job.id}/download").status_code == 404
    assert client.delete(f"/api/v1/exports/{job.id}").status_code == 404
    assert client.get("/api/v1/exports").json() == []
    assert client.post("/api/v1/exports", json={**PERIOD, "vehicle_id": str(car.id)}).status_code == 404
    assert client.get("/api/v1/exports/checks", params={**PERIOD, "vehicle_id": str(car.id)}).status_code == 404
    assert Path(job.file_path).is_file()  # den andra användarens fil är orörd


def test_requires_login(anon_client):
    assert anon_client.get("/api/v1/exports").status_code == 401
    assert anon_client.get("/api/v1/dashboard/summary").status_code == 401
