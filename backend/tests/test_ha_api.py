"""API-routes för Home Assistant: maskin-API (Bearer-token) och webbkonfiguration (session)."""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.db import utcnow
from app.models.enums import Category, IntegrationStatus, ReviewKind
from app.security import sessions
from app.services import settings as settings_svc
from app.services.ha import actions as A
from app.services.ha import notifier
from tests.conftest import reload_trip
from tests.test_ha_support import (  # noqa: F401
    HA_TOKEN,
    HA_URL,
    HOME,
    OREBRO,
    SERVICE,
    fake_ha,
    setup_pull,
    setup_push,
)

API = "/api/v1"


@pytest.fixture()
def ha_token(client) -> str:
    resp = client.post(f"{API}/auth/tokens", json={"name": "Home Assistant", "scopes": ["ha"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def live_question(db, user, vehicle, trips, *, mode: str = "pull") -> M.NotificationRequest:
    """En nyss avslutad resa med en fråga som är mogen för leverans."""
    now = utcnow()
    setup = setup_pull if mode == "pull" else setup_push
    setup(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    trips.location(HOME, name="Hemmet", type="home")
    trip = trips.trip(HOME, OREBRO, km=42.0, minutes=30, start_at=now - timedelta(hours=1))
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                          trip_items=[(trip, [ReviewKind.CATEGORY])], now=now)
    db.commit()
    return req


# ----------------------------------------------------------------------------- åtkomst


def test_machine_endpoints_require_token(anon_client):
    for method, path in (("get", "/ha/outbox"), ("get", "/ha/summary"), ("post", "/ha/heartbeat"),
                         ("post", "/ha/outbox/ack"), ("post", "/ha/callback")):
        resp = getattr(anon_client, method)(f"{API}{path}", **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 401, path


def test_invalid_token_and_wrong_scope_are_rejected(anon_client, db, user):
    db.commit()
    resp = anon_client.get(f"{API}/ha/summary", headers=bearer("dl_ogiltig"))
    assert resp.status_code == 401
    secret, _ = sessions.create_api_token(db, user, "Annat", ["export"])
    db.commit()
    resp = anon_client.get(f"{API}/ha/summary", headers=bearer(secret))
    assert resp.status_code == 403 and resp.json()["code"] == "insufficient_scope"


def test_only_ha_scope_can_be_created(client):
    resp = client.post(f"{API}/auth/tokens", json={"name": "x", "scopes": ["admin"]})
    assert resp.status_code == 400


def test_token_cannot_use_web_configuration(anon_client, ha_token):
    resp = anon_client.get(f"{API}/integrations/home-assistant", headers=bearer(ha_token))
    assert resp.status_code == 403 and resp.json()["code"] == "session_required"
    resp = anon_client.get(f"{API}/notifications", headers=bearer(ha_token))
    assert resp.status_code == 403


def test_revoked_token_is_rejected(client, anon_client, ha_token):
    token_id = client.get(f"{API}/auth/tokens").json()[0]["id"]
    assert client.delete(f"{API}/auth/tokens/{token_id}").status_code == 204
    assert anon_client.get(f"{API}/ha/summary", headers=bearer(ha_token)).status_code == 401


def test_whoami_with_token(anon_client, ha_token):
    resp = anon_client.get(f"{API}/auth/whoami", headers=bearer(ha_token))
    assert resp.status_code == 200 and resp.json()["scopes"] == ["ha"]


# ----------------------------------------------------------------------------- pull-flödet


def test_pull_flow_heartbeat_outbox_ack_callback(client, anon_client, ha_token, db, user, vehicle, trips):
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull"})
    db.commit()
    hb = anon_client.post(f"{API}/ha/heartbeat", headers=bearer(ha_token), json={
        "ha_version": "2026.11.1", "integration_version": "1.0.0",
        "notify_services": [f"notify.{SERVICE}", "notify.persistent_notification", "mobile_app_ipad"],
        "notify_service": f"notify.{SERVICE}", "queue_length": 0,
    })
    assert hb.status_code == 200, hb.text
    body = hb.json()
    assert body["delivery_mode"] == "pull" and body["status"] == "connected" and body["notify_service"] == SERVICE
    status = client.get(f"{API}/integrations/home-assistant").json()
    assert status["status"] == "connected"
    assert status["heartbeat"]["notify_services"] == ["mobile_app_ipad", SERVICE]
    assert status["heartbeat"]["ha_version"] == "2026.11.1"

    req = live_question(db, user, vehicle, trips)
    out = anon_client.get(f"{API}/ha/outbox", headers=bearer(ha_token))
    assert out.status_code == 200, out.text
    (item,) = out.json()["items"]
    assert item["id"] == req.public_id and item["type"] == "notify" and item["service"] == SERVICE
    actions = item["data"]["data"]["actions"]
    work = next(a for a in actions if a["title"] == "Arbete")
    assert work["action"] == f"DRIVELOG_WORK_{req.public_id}"

    ack = anon_client.post(f"{API}/ha/outbox/ack", headers=bearer(ha_token), json={"delivered": [req.public_id]})
    assert ack.status_code == 200 and ack.json() == {"acknowledged": 1, "failed": 0}
    assert anon_client.get(f"{API}/ha/outbox", headers=bearer(ha_token)).json()["items"] == []

    cb = anon_client.post(f"{API}/ha/callback", headers=bearer(ha_token), json={
        "action": work["action"], "event_id": "ha-event-0001",
    })
    assert cb.status_code == 200, cb.text
    assert cb.json()["outcome"] == "applied"
    assert reload_trip(db, req.logical_trip_id).category == Category.WORK
    # Samma händelse igen (omförsök från HA:s svarskö) → dubblett.
    cb2 = anon_client.post(f"{API}/ha/callback", headers=bearer(ha_token), json={
        "action": work["action"], "event_id": "ha-event-0001",
    })
    assert cb2.json()["outcome"] == "duplicate"


def test_callback_text_reply_and_invalid(anon_client, ha_token, db, user, vehicle, trips):
    req = live_question(db, user, vehicle, trips)
    cb = anon_client.post(f"{API}/ha/callback", headers=bearer(ha_token), json={
        "public_id": req.public_id, "action": "OKÄND", "event_id": "ha-event-0002",
    })
    assert cb.status_code == 200 and cb.json()["outcome"] == "invalid"
    bad = anon_client.post(f"{API}/ha/callback", headers=bearer(ha_token), json={"action": "WORK", "event_id": "x"})
    assert bad.status_code == 422


def test_summary_endpoint(anon_client, ha_token, db, user, vehicle, trips):
    from zoneinfo import ZoneInfo

    now = utcnow()
    # Resan läggs alltid i innevarande månad räknat i svensk tid (som summeringen räknar),
    # även de första timmarna i månaden och när det redan är ny månad i Sverige men inte i UTC.
    local = now.astimezone(ZoneInfo("Europe/Stockholm"))
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
    trips.location(HOME, name="Hemmet", type="home")
    trips.trip(HOME, OREBRO, km=42.0, start_at=max(now - timedelta(hours=2), month_start), minutes=1,
               category=Category.WORK)
    db.commit()
    resp = anon_client.get(f"{API}/ha/summary", headers=bearer(ha_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert {"review_count", "pending_questions", "work_km_month", "private_km_month", "last_sync_at",
            "last_trip"} <= data.keys()
    assert data["work_km_month"] == 42.0
    assert data["last_trip"]["route"] == "Hemmet → Örebro"
    assert "Drottninggatan" not in resp.text and "Hemvägen" not in resp.text


# ----------------------------------------------------------------------------- webbkonfiguration


def test_configure_push_never_returns_token(client, db, user, fake_ha):
    resp = client.put(f"{API}/integrations/home-assistant", json={
        "base_url": HA_URL + "/", "token": HA_TOKEN, "notify_service": f"notify.{SERVICE}", "delivery_mode": "push",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "configured" and body["has_token"] is True and body["base_url"] == HA_URL
    assert HA_TOKEN not in resp.text
    assert HA_TOKEN not in client.get(f"{API}/integrations/home-assistant").text
    conn = db.execute(select(M.IntegrationConnection).where(
        M.IntegrationConnection.provider == "home_assistant")).scalar_one()
    assert HA_TOKEN not in str(conn.config) and conn.secrets_encrypted and HA_TOKEN not in conn.secrets_encrypted
    # Granskningsloggen innehåller aldrig token.
    for row in db.execute(select(M.AuditEvent)).scalars():
        assert HA_TOKEN not in str(row.after or "")

    test = client.post(f"{API}/integrations/home-assistant/test")
    assert test.status_code == 200 and test.json()["ok"] is True, test.text
    assert test.json()["devices"] == ["mobile_app_ipad", SERVICE]
    assert client.get(f"{API}/integrations/home-assistant").json()["status"] == "connected"

    devices = client.get(f"{API}/integrations/home-assistant/devices").json()
    assert devices["selected"] == SERVICE and SERVICE in devices["devices"]

    sent = client.post(f"{API}/integrations/home-assistant/test-notification")
    assert sent.status_code == 200 and sent.json()["delivered"] is True, sent.text
    (payload,) = fake_ha.sent
    assert payload["data"]["actions"][0]["action"] == f"DRIVELOG_TESTOK_{sent.json()['public_id']}"


def test_configure_rejects_bad_values(client):
    assert client.put(f"{API}/integrations/home-assistant", json={"base_url": "ftp://x"}).status_code == 400
    assert client.put(f"{API}/integrations/home-assistant", json={"notify_service": "notify.tv"}).status_code == 400
    assert client.put(f"{API}/integrations/home-assistant", json={"delivery_mode": "smoke"}).status_code == 422


def test_connection_test_when_ha_offline(client, fake_ha):
    client.put(f"{API}/integrations/home-assistant", json={"base_url": HA_URL, "token": HA_TOKEN,
                                                           "notify_service": SERVICE})
    fake_ha.offline = True
    resp = client.post(f"{API}/integrations/home-assistant/test")
    assert resp.status_code == 200 and resp.json()["ok"] is False
    assert resp.json()["status"] != "connected"
    fake_ha.offline = False
    assert client.post(f"{API}/integrations/home-assistant/test").json()["ok"] is True


def test_pull_test_without_heartbeat_fails(client):
    client.put(f"{API}/integrations/home-assistant", json={"delivery_mode": "pull"})
    resp = client.post(f"{API}/integrations/home-assistant/test")
    assert resp.status_code == 200 and resp.json()["ok"] is False


def test_test_notification_requires_channel(client):
    resp = client.post(f"{API}/integrations/home-assistant/test-notification")
    assert resp.status_code == 409


def test_demo_mode_via_api(client, fake_ha):
    resp = client.post(f"{API}/integrations/home-assistant/demo/enable")
    assert resp.status_code == 200 and resp.json()["status"] == "demo" and resp.json()["demo"] is True
    inbox = client.get(f"{API}/notifications/demo-inbox").json()
    assert inbox and inbox[0]["kind"] == "test"
    pid = inbox[0]["public_id"]
    r = client.post(f"{API}/notifications/{pid}/respond", json={"action": "TESTOK", "as_phone": True})
    assert r.status_code == 200 and r.json()["outcome"] == "applied"
    test = client.post(f"{API}/integrations/home-assistant/test").json()
    assert test["ok"] is False and test["status"] == "demo"
    assert client.put(f"{API}/integrations/home-assistant", json={"base_url": HA_URL}).status_code == 409
    resp = client.post(f"{API}/integrations/home-assistant/demo/disable")
    assert resp.json()["status"] == "not_configured"
    assert fake_ha.calls == []


def test_disconnect_removes_secrets(client, db, fake_ha):
    client.put(f"{API}/integrations/home-assistant", json={"base_url": HA_URL, "token": HA_TOKEN,
                                                           "notify_service": SERVICE})
    resp = client.post(f"{API}/integrations/home-assistant/disconnect")
    assert resp.status_code == 200 and resp.json()["status"] == "not_configured" and resp.json()["has_token"] is False
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection).where(
        M.IntegrationConnection.provider == "home_assistant")).scalar_one()
    assert conn.secrets_encrypted is None


def test_mutations_require_csrf(client):
    client.headers.pop("X-CSRF-Token")
    assert client.put(f"{API}/integrations/home-assistant", json={"base_url": HA_URL}).status_code == 403


def test_action_ids_are_short_enough():
    assert len(A.action_id(A.PURPOSE_TEXT, "0123456789abcdef")) <= 80
    assert IntegrationStatus.DEMO != IntegrationStatus.CONNECTED


def test_pull_stale_heartbeat_then_reconnect(client, anon_client, ha_token, db, user):
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull"})
    db.commit()
    hb = {"ha_version": "2026.11.1", "integration_version": "1.0.0", "notify_services": [SERVICE]}
    assert anon_client.post(f"{API}/ha/heartbeat", headers=bearer(ha_token), json=hb).json()["status"] == "connected"
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection).where(
        M.IntegrationConnection.provider == "home_assistant")).scalar_one()
    old = (utcnow() - timedelta(minutes=30)).isoformat()
    conn.config = {**conn.config, "heartbeat": {**conn.config["heartbeat"], "received_at": old}}
    db.commit()
    status = client.get(f"{API}/integrations/home-assistant").json()
    assert status["status"] == "error" and status["heartbeat"]["fresh"] is False and status["last_error"]
    # Integrationen återansluter.
    assert anon_client.post(f"{API}/ha/heartbeat", headers=bearer(ha_token), json=hb).status_code == 200
    status = client.get(f"{API}/integrations/home-assistant").json()
    assert status["status"] == "connected" and status["heartbeat"]["fresh"] is True
