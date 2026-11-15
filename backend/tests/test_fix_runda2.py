"""Småfel i API:t som webbappens vyer rapporterat (runda 2)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

import app.models as M
from app.db import utcnow
from app.models.enums import (
    Category,
    LogicalState,
    NotificationKind,
    NotificationStatus,
    ReviewKind,
    ReviewState,
    ReviewStatus,
)
from app.services import settings as settings_svc
from app.services.ha import notifier
from app.services.importer import service as imp
from app.services.stats import dashboard
from app.services.trips import pipeline
from tests.conftest import reload_trip
from tests.fixtures import volvo_exports as fx
from tests.test_ha_support import HOME, OREBRO, setup_pull

API = "/api/v1"


def _open_items(db):
    return db.execute(
        select(M.ReviewItem).where(M.ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)))
    ).scalars().all()


def _upload(client, *files: tuple[str, bytes]):
    r = client.post(f"{API}/imports/preview",
                    files=[("files", (name, data, "application/octet-stream")) for name, data in files])
    assert r.status_code == 201, r.text
    return r.json()


# ----------------------------------------------------------------------------- 1. ångra import


def test_import_undo_leaves_no_open_items_for_voided_trips(client, db):
    batch = _upload(client, ("juni.xlsx", fx.build_v2(fx.month_rows(2026, 6, 180000))))
    r = client.post(f"{API}/imports/{batch['id']}/commit", json={"apply_merge_rules": False})
    assert r.status_code == 200 and r.json()["merge_questions"] > 0
    db.expire_all()
    assert any(i.kind == ReviewKind.MERGE for i in _open_items(db))
    assert client.post(f"{API}/imports/{batch['id']}/undo").status_code == 200
    db.expire_all()
    for item in _open_items(db):
        if item.logical_trip_id is not None:
            assert db.get(M.LogicalTrip, item.logical_trip_id).state == LogicalState.ACTIVE
        for key in ("first_raw_trip_id", "second_raw_trip_id"):
            if (item.data or {}).get(key):
                raw = db.get(M.RawTrip, uuid.UUID(item.data[key]))
                assert raw is not None and raw.voided_at is None
    assert not [i for i in _open_items(db) if i.kind == ReviewKind.MERGE]


# ----------------------------------------------------------------------------- 2. sammanställning


def test_summary_notes_use_singular_and_plural():
    one = " ".join(imp._summary_notes(1, 12, False, 1, scope=settings_svc.SCOPE_COMPLETE))
    assert "1 lucka på" in one and "1 resa har" in one
    assert "1 luckor" not in one and "1 resor" not in one
    many = " ".join(imp._summary_notes(3, 40, False, 3, scope=settings_svc.SCOPE_COMPLETE))
    assert "3 luckor på" in many and "3 resor har" in many


def test_import_files_report_stored_and_locations_new(client, db, user):
    rows = fx.month_rows(2026, 6, 180000)
    batch = _upload(client, ("juni.xlsx", fx.build_v2(rows)))
    assert [f["stored"] for f in batch["summary"]["files"]] == [True]
    # En av adresserna finns redan som plats → den räknas inte som ny.
    from app.services.places import registry as places

    places.resolve_address(db, user.id, fx.HOME, created_from="user")
    db.commit()
    result = client.post(f"{API}/imports/{batch['id']}/commit", json={}).json()
    assert result["files"][0]["stored"] is True
    assert 0 <= result["locations_new"] == result["locations_created"] - 1

    listed = client.get(f"{API}/imports").json()["items"][0]
    assert listed["summary"]["files"][0]["stored"] is True
    assert listed["summary"]["locations_new"] == result["locations_new"]

    assert client.delete(f"{API}/imports/{batch['id']}/files").json()["removed"] == 1
    detail = client.get(f"{API}/imports/{batch['id']}").json()
    assert detail["summary"]["files"][0]["stored"] is False


def test_stored_is_false_when_file_missing_on_disk(client, db):
    batch = _upload(client, ("juni.xlsx", fx.build_v2(fx.month_rows(2026, 6, 180000))))
    f = db.execute(select(M.ImportedFile)).scalars().one()
    Path(f.stored_path).unlink()
    detail = client.get(f"{API}/imports/{batch['id']}").json()
    assert detail["summary"]["files"][0]["stored"] is False


# ----------------------------------------------------------------------------- 3. inställningar


def test_settings_bounds_for_calendar_privacy_and_notifications(client):
    cases = [
        ("calendar", {"arrival_window_before_minutes": 24 * 60 + 1}),
        ("calendar", {"arrival_window_after_minutes": -1}),
        ("calendar", {"sync_days_back": 3651}),
        ("calendar", {"sync_days_forward": 366}),
        ("privacy", {"observation_retention_days": -5}),
        ("notifications", {"snooze_minutes": 0}),
        ("notifications", {"digest_threshold": 0}),
    ]
    for section, values in cases:
        r = client.patch(f"{API}/settings/{section}", json=values)
        assert r.status_code == 400, (section, values, r.text)
        assert "måste ligga mellan" in r.json()["detail"]
    ok = client.patch(f"{API}/settings/calendar", json={"sync_days_back": 3650, "arrival_window_after_minutes": 0})
    assert ok.status_code == 200, ok.text


def test_settings_validate_abbreviations_and_urls(client):
    bad = [
        ("abbreviations", {"map": {"": "Jönköping"}}),
        ("abbreviations", {"map": {"JKPG": "  "}}),
        ("abbreviations", {"map": {"JKPG": 5}}),
        ("abbreviations", {"map": {"X" * 21: "Ort"}}),
        ("abbreviations", {"map": {"JKPG": "x" * 121}}),
        ("abbreviations", {"map": {f"K{i}": "Ort" for i in range(201)}}),
        ("privacy", {"map_tiles_url": "javascript:alert(1)"}),
        ("privacy", {"geocoder_url": "ftp://example.com"}),
        ("privacy", {"geocoder_url": "nominatim"}),
    ]
    for section, values in bad:
        r = client.patch(f"{API}/settings/{section}", json=values)
        assert r.status_code == 400, (section, values, r.text)
    assert client.patch(f"{API}/settings/abbreviations", json={"map": {"SKV": "Skatteverket"}}).status_code == 200
    assert client.patch(f"{API}/settings/privacy", json={
        "map_tiles_url": "https://tile.example.com/{z}/{x}/{y}.png", "geocoder_url": ""}).status_code == 200


# ----------------------------------------------------------------------------- 4. platser


def test_patch_location_null_clears_notes_and_company(client, trips, db):
    loc = trips.location("Kundgatan 1, Stad", name="Kunden", type="customer", company="AB Kund")
    loc.notes = "Parkera på baksidan"
    db.commit()
    r = client.patch(f"{API}/locations/{loc.id}", json={"notes": None, "company": None})
    assert r.status_code == 200, r.text
    db.expire_all()
    loc = db.get(M.KnownLocation, loc.id)
    assert loc.notes is None and loc.company is None
    r = client.patch(f"{API}/locations/{loc.id}", json={"name": None, "type": None, "radius_m": None})
    assert r.status_code == 200
    db.expire_all()
    loc = db.get(M.KnownLocation, loc.id)
    assert loc.name == "Kunden" and loc.type == "customer"


# ----------------------------------------------------------------------------- 5. mönster


def test_learning_patterns_hide_demo_locations(client, trips, db, user):
    real = trips.location("Riktiga vägen 1, Stad", name="Riktig")
    demo = trips.location("Demovägen 1, Stad", name="Demo")
    demo.is_demo = True
    for i, loc in enumerate((real, demo)):
        db.add(M.LocationPattern(owner_user_id=user.id, pattern_type="destination", pattern_key=f"dest:{i}",
                                 destination_location_id=loc.id, confirmed_work=3))
    db.add(M.LocationPattern(owner_user_id=user.id, pattern_type="route", pattern_key="route:demo",
                             origin_location_id=demo.id, destination_location_id=real.id, confirmed_work=3))
    db.commit()
    names = {(p["origin_name"], p["destination_name"]) for p in client.get(f"{API}/learning/patterns").json()}
    assert names == {(None, "Riktig")}
    all_names = client.get(f"{API}/learning/patterns?include_demo=true").json()
    assert len(all_names) == 3


# ----------------------------------------------------------------------------- 6. snabbvy


def test_notification_detail_has_safe_labels_and_time_quality(client, db, user, vehicle, trips):
    now = utcnow()
    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    trips.location(HOME, name="Hemmet", type="home")
    trip = trips.trip(HOME, OREBRO, km=42.0, minutes=30, start_at=now - timedelta(hours=1))
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                          trip_items=[(trip, [ReviewKind.CATEGORY])], now=now)
    db.commit()
    t = client.get(f"{API}/notifications/{req.public_id}").json()["trip"]
    assert t["start_label"] == "Hemmet" and "Hemvägen" not in t["start_label"]
    assert t["end_label"] == "Örebro"
    assert t["start_time_quality"] == "exact" and t["end_time_quality"] == "exact"
    assert t["start_address"] == HOME  # befintliga fält orörda


# ----------------------------------------------------------------------------- 7. syftesfrågor


def test_purpose_question_closed_when_web_sets_purpose(client, db, user, vehicle, trips):
    now = utcnow()
    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    trip = trips.trip(HOME, OREBRO, km=42.0, minutes=30, start_at=now - timedelta(hours=1),
                      category=Category.WORK, review_status=ReviewStatus.AUTO)
    pipeline.sync_trip_review_items(db, trip)
    cfg = settings_svc.get_section(db, user.id, "notifications")
    req = notifier.create_purpose_question(db, user_id=user.id, trip=trip, cfg=cfg, now=now)
    db.commit()
    assert req is not None
    assert any(i.kind == ReviewKind.PURPOSE for i in _open_items(db))

    r = client.patch(f"{API}/trips/{trip.id}", json={"purpose": "Kundmöte"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert reload_trip(db, trip.id).purpose_confirmed is True
    assert db.get(M.NotificationRequest, req.id).status == NotificationStatus.CANCELLED
    assert not [i for i in _open_items(db) if i.kind == ReviewKind.PURPOSE]


def test_web_work_answer_then_purpose_leaves_nothing_open(client, db, user, vehicle, trips):
    now = utcnow()
    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    trip = trips.trip(HOME, OREBRO, km=42.0, minutes=30, start_at=now - timedelta(hours=1))
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                          trip_items=[(trip, [ReviewKind.CATEGORY])], now=now)
    db.commit()
    r = client.post(f"{API}/notifications/{req.public_id}/respond", json={"action": "WORK"})
    assert r.json()["outcome"] == "applied" and r.json().get("follow_up") is None
    cfg = settings_svc.get_section(db, user.id, "notifications")
    purpose_req = notifier.create_purpose_question(db, user_id=user.id, trip=reload_trip(db, trip.id), cfg=cfg,
                                                   now=now)
    db.commit()
    assert client.patch(f"{API}/trips/{trip.id}", json={"purpose": "Leverans"}).status_code == 200
    db.expire_all()
    open_reqs = db.execute(select(M.NotificationRequest).where(
        M.NotificationRequest.logical_trip_id == trip.id,
        M.NotificationRequest.kind == NotificationKind.PURPOSE,
        M.NotificationRequest.status.in_((NotificationStatus.PENDING, NotificationStatus.SENT)),
    )).scalars().all()
    assert open_reqs == []
    assert purpose_req is None or db.get(M.NotificationRequest, purpose_req.id).status == NotificationStatus.CANCELLED
    assert not [i for i in _open_items(db) if i.logical_trip_id == trip.id]


# ----------------------------------------------------------------------------- 8. fordonsläge


def test_vehicle_status_never_matches_demo_location_for_real_vehicle(db, user, vehicle, trips):
    demo = trips.location("Demovägen 1, Stad", name="Demoplats", lat=59.3, lon=18.0)
    demo.is_demo = True
    now = utcnow()
    db.add(M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=now, last_seen_at=now,
                                latitude=59.3, longitude=18.0, position_reported_at=now, content_hash="t"))
    db.flush()
    assert dashboard.vehicle_status(db, vehicle, user.id)["position"]["place"] is None
    demo.is_demo = False
    db.flush()
    assert dashboard.vehicle_status(db, vehicle, user.id)["position"]["place"]["name"] == "Demoplats"
