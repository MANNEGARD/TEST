"""Regressionstester för granskningsfynd om säkerhet (säke-*, inte-6, api-*)."""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category
from app.security import passwords
from tests.conftest import TEST_PASSWORD

API = "/api/v1"

# Routes som uttryckligen godtar en Home Assistant-token. Allt annat ska ge 403.
HA_TOKEN_PATHS = {
    f"{API}/auth/whoami",
    f"{API}/ha/callback",
    f"{API}/ha/outbox",
    f"{API}/ha/outbox/ack",
    f"{API}/ha/heartbeat",
    f"{API}/ha/summary",
}
PUBLIC_PATHS = {
    f"{API}/health",
    f"{API}/health/ready",
    f"{API}/auth/setup-status",
    f"{API}/auth/setup",
    f"{API}/auth/login",
    f"{API}/openapi.json",
    f"{API}/docs",
}


@pytest.fixture()
def ha_headers(client) -> dict[str, str]:
    resp = client.post(f"{API}/auth/tokens", json={"name": "Home Assistant", "scopes": ["ha"]})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ----------------------------------------------------------------------------- säke-1 / inte-6


def test_ha_token_is_rejected_outside_allowed_routes(client, anon_client, ha_headers):
    """Systematiskt: en giltig ha-token ger 403 på varje route utanför den tillåtna listan."""
    spec = anon_client.get(f"{API}/openapi.json").json()
    checked = 0
    for path, methods in spec["paths"].items():
        if path in PUBLIC_PATHS or path in HA_TOKEN_PATHS:
            continue
        url = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)
        for method in methods:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            resp = anon_client.request(method.upper(), url, json={}, headers=ha_headers, follow_redirects=False)
            if path.startswith(f"{API}/integrations/") and path.endswith("/callback"):
                assert resp.status_code == 302 and "reason=session_required" in resp.headers["location"], path
            else:
                assert resp.status_code == 403, f"{method.upper()} {path} svarade {resp.status_code} med ha-token"
                assert resp.json()["code"] in ("insufficient_scope", "session_required"), path
            checked += 1
    assert checked > 150


def test_ha_token_reaches_allowed_routes(anon_client, ha_headers):
    assert anon_client.get(f"{API}/auth/whoami", headers=ha_headers).json()["auth"] == "token"
    assert anon_client.get(f"{API}/ha/summary", headers=ha_headers).status_code == 200
    assert anon_client.get(f"{API}/ha/outbox", headers=ha_headers).status_code == 200


def test_ha_token_cannot_change_trip(client, anon_client, ha_headers, db, trips):
    trip = trips.trip(category=Category.WORK, purpose="Kundmöte", km=42.0)
    db.commit()
    resp = anon_client.patch(f"{API}/trips/{trip.id}", headers=ha_headers,
                             json={"category": "private", "purpose": "ändrad via HA-token"})
    assert resp.status_code == 403 and resp.json()["code"] == "insufficient_scope"
    assert anon_client.get(f"{API}/trips", headers=ha_headers).status_code == 403
    db.expire_all()
    assert db.get(M.LogicalTrip, trip.id).category == Category.WORK


def test_session_still_reaches_journal(client):
    assert client.get(f"{API}/trips").status_code == 200
    assert client.get(f"{API}/auth/whoami").json()["auth"] == "session"


# ----------------------------------------------------------------------------- säke-6


def test_patch_trip_rejects_foreign_or_unknown_driver(client, db, trips):
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.flush()
    foreign = M.Driver(owner_user_id=other.id, name="Främmande")
    db.add(foreign)
    trip = trips.trip()
    db.commit()
    for driver_id in (str(foreign.id), str(uuid.uuid4())):
        resp = client.patch(f"{API}/trips/{trip.id}", json={"driver_id": driver_id})
        assert resp.status_code == 404 and resp.json()["code"] == "not_found", resp.text
    db.expire_all()
    assert db.get(M.LogicalTrip, trip.id).driver_id is None


def test_patch_trip_accepts_own_driver_and_clearing(client, db, user, trips):
    own = M.Driver(owner_user_id=user.id, name="Jag")
    db.add(own)
    trip = trips.trip()
    db.commit()
    assert client.patch(f"{API}/trips/{trip.id}", json={"driver_id": str(own.id)}).status_code == 200
    assert client.patch(f"{API}/trips/{trip.id}", json={"driver_id": None}).status_code == 200


# ----------------------------------------------------------------------------- säke-3


def test_rejected_login_attempts_do_not_extend_lockout(anon_client, db, user):
    db.commit()
    c = anon_client
    for _ in range(8):
        assert c.post(f"{API}/auth/login", json={"email": "ingen@example.com", "password": "fel"}).status_code == 401
    # Gränsen nådd från den här adressen; fler försök registreras inte.
    for _ in range(5):
        assert c.post(f"{API}/auth/login", json={"email": user.email, "password": "fel"}).status_code == 429
    # Kontot har inga registrerade misslyckanden: från en annan adress går rätt lösenord igenom.
    ok = c.post(f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD},
                headers={"X-Forwarded-For": "10.0.0.2"})
    assert ok.status_code == 200, ok.text


def test_successful_logins_do_not_count_against_account(anon_client, db, user):
    db.commit()
    for i in range(12):
        resp = anon_client.post(f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD},
                                headers={"X-Forwarded-For": f"10.0.1.{i}"})
        assert resp.status_code == 200, (i, resp.text)


# ----------------------------------------------------------------------------- säke-4


def test_unknown_account_costs_one_verification(anon_client, db, user, monkeypatch):
    db.commit()
    calls = {"hash": 0, "verify": 0}
    real_hash, real_verify = passwords.hash_password, passwords.verify_password

    def counting_hash(pw):
        calls["hash"] += 1
        return real_hash(pw)

    def counting_verify(stored, pw):
        calls["verify"] += 1
        return real_verify(stored, pw)

    monkeypatch.setattr(passwords, "hash_password", counting_hash)
    monkeypatch.setattr(passwords, "verify_password", counting_verify)
    anon_client.post(f"{API}/auth/login", json={"email": "ingen@example.com", "password": "fel"})  # värmer upp
    calls.update(hash=0, verify=0)
    anon_client.post(f"{API}/auth/login", json={"email": "ingen2@example.com", "password": "fel"})
    assert calls == {"hash": 0, "verify": 1}


# ----------------------------------------------------------------------------- api-7


def test_revoked_token_can_be_deleted(client, db):
    tid = client.post(f"{API}/auth/tokens", json={"name": "HA", "scopes": ["ha"]}).json()["id"]
    assert client.delete(f"{API}/auth/tokens/{tid}").status_code == 204
    listed = client.get(f"{API}/auth/tokens").json()
    assert listed[0]["revoked_at"] is not None  # första anropet återkallar
    assert client.delete(f"{API}/auth/tokens/{tid}").status_code == 204
    assert client.get(f"{API}/auth/tokens").json() == []  # andra anropet tar bort raden
    assert client.delete(f"{API}/auth/tokens/{tid}").status_code == 404
    actions = db.execute(select(M.AuditEvent.action).where(M.AuditEvent.entity_type == "api_token")).scalars().all()
    assert "auth.token_deleted" in actions


# ----------------------------------------------------------------------------- api-3


def test_ai_test_returns_verified_model(client, db, user, monkeypatch):
    from app.services.classify import llm

    class FakeProvider:
        def ping(self):
            return "leverantorens-modell"

    monkeypatch.setattr(llm, "build_provider", lambda *a, **k: FakeProvider())
    r = client.put(f"{API}/integrations/ai", json={"provider": "anthropic", "api_key": "sk-test", "model": "vald-modell"})
    assert r.status_code == 200, r.text
    body = client.post(f"{API}/integrations/ai/test").json()
    assert body["ok"] is True and body["verified_model"] == "leverantorens-modell" and body["model"] == "vald-modell"
