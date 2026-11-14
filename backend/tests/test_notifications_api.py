"""Notiser i webbappen: lista, snabbvy, svar och uppskjutning (session + CSRF)."""

from __future__ import annotations

from datetime import timedelta

from app.db import utcnow
from app.models.enums import Category, NotificationStatus, ReviewKind
from app.services.ha import notifier
from tests.conftest import reload_trip
from tests.test_ha_support import HOME, OREBRO, setup_pull

API = "/api/v1"


def live_question(db, user, vehicle, trips):
    now = utcnow()
    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    trips.location(HOME, name="Hemmet", type="home")
    trip = trips.trip(HOME, OREBRO, km=42.0, minutes=30, start_at=now - timedelta(hours=1))
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                          trip_items=[(trip, [ReviewKind.CATEGORY])], now=now)
    db.commit()
    return req


def test_notifications_list_detail_and_respond(client, db, user, vehicle, trips):
    req = live_question(db, user, vehicle, trips)
    lst = client.get(f"{API}/notifications").json()
    assert [n["public_id"] for n in lst] == [req.public_id]
    detail = client.get(f"{API}/notifications/{req.public_id}")
    assert detail.status_code == 200 and detail.json()["trip"]["id"] == str(req.logical_trip_id)
    assert client.get(f"{API}/notifications/finnsinte0000").status_code == 404
    r = client.post(f"{API}/notifications/{req.public_id}/respond", json={"action": "PRIVATE"})
    assert r.status_code == 200 and r.json()["outcome"] == "applied"
    assert reload_trip(db, req.logical_trip_id).category == Category.PRIVATE
    again = client.post(f"{API}/notifications/{req.public_id}/respond", json={"action": "WORK"})
    assert again.json()["outcome"] == "duplicate"
    assert [n["public_id"] for n in client.get(f"{API}/notifications?status=answered").json()] == [req.public_id]
    detail = client.get(f"{API}/notifications/{req.public_id}").json()
    assert detail["status"] == NotificationStatus.ANSWERED and detail["answer"]["source"] == "web"


def test_snooze_from_web(client, db, user, vehicle, trips):
    req = live_question(db, user, vehicle, trips)
    r = client.post(f"{API}/notifications/{req.public_id}/snooze", json={"minutes": 60})
    assert r.status_code == 200 and r.json()["outcome"] == "snoozed"


def test_notifications_require_login(anon_client):
    assert anon_client.get(f"{API}/notifications").status_code == 401


def test_web_answer_to_expired_question(client, db, user, vehicle, trips):
    req = live_question(db, user, vehicle, trips)
    req.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    detail = client.get(f"{API}/notifications/{req.public_id}").json()
    assert detail["expired"] is True and detail["answerable"] is True
    r = client.post(f"{API}/notifications/{req.public_id}/respond", json={"action": "WORK"})
    assert r.json()["outcome"] == "applied"
    assert reload_trip(db, req.logical_trip_id).category == Category.WORK
