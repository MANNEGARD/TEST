"""API-routes för Volvo-integrationen (inloggad TestClient)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import IntegrationStatus
from app.services.volvo import client as vc
from tests.test_volvo_support import VIN, Recorder

API = "/api/v1/integrations/volvo"
SECRETS = ("klienthemlighet-xyz", "vcc-nyckel-abc", "AT-ny-111111111111", "RT-ny-22222222222", "portal-testtoken-123456789")


@pytest.fixture()
def rec(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(vc, "HTTP_TRANSPORT", recorder.transport)
    monkeypatch.setattr(vc, "_sleep", lambda s: None)
    vc._limiters.clear()
    return recorder


def assert_no_secrets(resp) -> None:
    for s in SECRETS + (VIN,):
        assert s not in resp.text, s


def save_credentials(client) -> None:
    resp = client.put(f"{API}/credentials", json={
        "client_id": "vid-dc-test-client", "client_secret": "klienthemlighet-xyz", "vcc_api_key": "vcc-nyckel-abc",
    })
    assert resp.status_code == 200, resp.text
    assert_no_secrets(resp)


def connect_via_callback(client) -> None:
    save_credentials(client)
    url = client.post(f"{API}/connect").json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    resp = client.get(f"{API}/callback", params={"code": "auth-kod", "state": state}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://testserver/integrations?volvo=connected"


def test_status_when_not_configured(client):
    resp = client.get(f"{API}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == IntegrationStatus.NOT_CONFIGURED and body["is_connected"] is False
    assert body["redirect_uri"] == "http://testserver/api/v1/integrations/volvo/callback"
    assert body["quota"]["documented_limit_per_api"] == 10000
    assert {e["name"] for e in body["endpoints"]} >= {"odometer", "location", "engine_status"}


def test_status_requires_login(anon_client):
    assert anon_client.get(f"{API}/status").status_code == 401


def test_credentials_are_never_returned(client, db):
    save_credentials(client)
    body = client.get(f"{API}/status").json()
    assert body["status"] == IntegrationStatus.CONFIGURED
    assert body["has_secrets"] == {"client_secret": True, "vcc_api_key": True}
    assert body["config"]["client_id"] == "vid-dc-test-client"
    conn = db.execute(select(M.IntegrationConnection)).scalar_one()
    assert "klienthemlighet-xyz" not in (conn.secrets_encrypted or "")
    event = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "volvo.credentials_saved")).scalar_one()
    assert "klienthemlighet" not in str(event.after)


def test_credentials_validation(client):
    resp = client.put(f"{API}/credentials", json={"client_id": "x"})
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_credentials"


def test_connect_requires_credentials(client):
    resp = client.post(f"{API}/connect")
    assert resp.status_code == 400 and resp.json()["code"] == "not_configured"


def test_connect_returns_authorize_url(client):
    save_credentials(client)
    resp = client.post(f"{API}/connect")
    assert resp.status_code == 200
    q = parse_qs(urlparse(resp.json()["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/integrations/volvo/callback")


def test_callback_success_lists_vehicles_and_hides_secrets(client, rec, db):
    connect_via_callback(client)
    status = client.get(f"{API}/status")
    body = status.json()
    assert body["status"] == IntegrationStatus.CONNECTED and body["is_connected"] is True
    assert body["has_secrets"]["refresh_token"] is True and body["reauth_due_at"]
    assert_no_secrets(status)
    vehicles = client.get(f"{API}/vehicles")
    assert vehicles.status_code == 200
    (car,) = vehicles.json()["vehicles"]
    assert car["vin_masked"].endswith(VIN[-4:]) and car["model"] == "V90 Recharge"
    assert_no_secrets(vehicles)
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "volvo.connected")).scalar_one()


@pytest.mark.parametrize("state", ["påhittat", ""])
def test_callback_with_wrong_state_redirects_with_error(client, rec, state):
    save_credentials(client)
    resp = client.get(f"{API}/callback", params={"code": "kod", "state": state}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://testserver/integrations?volvo=error&reason=invalid_state"
    assert rec.calls == []


def test_callback_with_reused_state_is_rejected(client, rec):
    save_credentials(client)
    url = client.post(f"{API}/connect").json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    first = client.get(f"{API}/callback", params={"code": "kod", "state": state}, follow_redirects=False)
    assert first.headers["location"].endswith("volvo=connected")
    again = client.get(f"{API}/callback", params={"code": "kod", "state": state}, follow_redirects=False)
    assert again.status_code == 302 and again.headers["location"].endswith("reason=invalid_state")


def test_callback_when_user_denies(client, rec, db):
    save_credentials(client)
    url = client.post(f"{API}/connect").json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    resp = client.get(f"{API}/callback", params={"error": "access_denied", "state": state}, follow_redirects=False)
    assert resp.headers["location"].endswith("volvo=error&reason=access_denied")
    assert db.execute(select(M.OAuthState)).first() is None  # state förbrukat


def test_callback_token_error_redirects(client, monkeypatch):
    save_credentials(client)
    monkeypatch.setattr(vc, "HTTP_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_client"})))
    url = client.post(f"{API}/connect").json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    resp = client.get(f"{API}/callback", params={"code": "kod", "state": state}, follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"].endswith("reason=invalid_client")


def test_callback_without_session_redirects(anon_client):
    resp = anon_client.get(f"{API}/callback", params={"code": "kod", "state": "x"}, follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"].endswith("reason=not_logged_in")


def test_select_vehicle_sync_and_capabilities(client, rec, vehicle, db):
    connect_via_callback(client)
    ref = client.get(f"{API}/vehicles").json()["vehicles"][0]["ref"]
    resp = client.post(f"{API}/select-vehicle", json={"ref": ref, "vehicle_id": str(vehicle.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (linked,) = body["vehicles"]
    assert linked["id"] == str(vehicle.id) and linked["capabilities"]["odometer"]["state"] == "supported"
    assert_no_secrets(resp)
    sync = client.post(f"{API}/sync", json={})
    assert sync.status_code == 200 and sync.json()["result"]["status"] in ("ok", "partial")
    assert_no_secrets(sync)
    status = client.get(f"{API}/status").json()
    assert status["last_run"]["kind"] == "poll" and status["quota"]["calls"]["connected-vehicle"] > 0


def test_sync_without_vehicle_is_bad_request(client, rec):
    connect_via_callback(client)
    resp = client.post(f"{API}/sync", json={})
    assert resp.status_code == 400 and resp.json()["code"] == "no_vehicle"


def test_test_token_mode(client, rec):
    resp = client.post(f"{API}/test-token", json={"access_token": "portal-testtoken-123456789",
                                                   "vcc_api_key": "vcc-nyckel-abc"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["test_token_mode"] is True and body["test_token_notice"]
    assert body["status"] == IntegrationStatus.CONNECTED
    assert_no_secrets(resp)
    assert rec.token_calls() == []


def test_test_token_rejected_by_volvo(client, monkeypatch):
    monkeypatch.setattr(vc, "HTTP_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    resp = client.post(f"{API}/test-token", json={"access_token": "portal-testtoken-123456789",
                                                   "vcc_api_key": "vcc-nyckel-abc"})
    assert resp.status_code == 409 and resp.json()["code"] == "needs_reauth"
    assert client.get(f"{API}/status").json()["status"] == IntegrationStatus.NEEDS_REAUTH


def test_disconnect_deletes_secrets(client, rec, db):
    connect_via_callback(client)
    resp = client.post(f"{API}/disconnect")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == IntegrationStatus.NOT_CONFIGURED and body["has_secrets"] == {}
    conn = db.execute(select(M.IntegrationConnection)).scalar_one()
    db.refresh(conn)
    assert conn.secrets_encrypted is None
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "volvo.disconnected")).scalar_one()


def test_demo_endpoints(client, vehicle, db):
    resp = client.post(f"{API}/demo/enable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == IntegrationStatus.DEMO and body["is_connected"] is False
    assert body["demo"]["enabled"] is True and body["demo"]["vehicle"]["is_demo"] is True
    sim = client.post(f"{API}/demo/simulate", json={"scenario": "commute"})
    assert sim.status_code == 200 and sim.json()["new_trip_ids"]
    assert client.post(f"{API}/demo/simulate", json={"scenario": "okänt"}).status_code == 400
    sync = client.post(f"{API}/sync", json={})
    assert sync.status_code == 200
    off = client.post(f"{API}/demo/disable")
    assert off.status_code == 200 and off.json()["status"] == IntegrationStatus.NOT_CONFIGURED
    assert off.json()["removed"]["trips"] >= 1
    assert client.post(f"{API}/demo/simulate", json={}).status_code == 409
    assert db.execute(select(M.Vehicle).where(M.Vehicle.is_demo.is_(True))).first() is None


def test_mutations_require_csrf(client):
    token = client.headers.pop("X-CSRF-Token")
    try:
        assert client.post(f"{API}/demo/enable").status_code == 403
    finally:
        client.headers["X-CSRF-Token"] = token
