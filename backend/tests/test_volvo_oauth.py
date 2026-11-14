"""Volvo ID: OAuth2 authorization code + PKCE, tokenförnyelse och testtoken-läge."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import IntegrationStatus
from app.services import integrations
from app.services.volvo import collector, oauth
from app.services.volvo import constants as C
from app.services.volvo.client import VolvoAuthError, VolvoError
from tests.test_volvo_support import VIN, Recorder, connect


def configured(db, user, **kw):
    conn = oauth.connection(db, user.id)
    oauth.save_credentials(
        db, conn, client_id="vid-dc-test-client", client_secret="klienthemlighet-xyz", vcc_api_key="vcc-nyckel-abc", **kw
    )
    return conn


def start(db, user, conn):
    url = oauth.start_authorization(db, conn, user.id)
    return url, {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_credentials_are_stored_encrypted(db, user):
    conn = configured(db, user)
    assert conn.status == IntegrationStatus.CONFIGURED
    assert "klienthemlighet-xyz" not in (conn.secrets_encrypted or "")
    assert "vcc-nyckel-abc" not in conn.secrets_encrypted
    assert integrations.secrets(conn)["client_secret"] == "klienthemlighet-xyz"
    view = integrations.public_view(conn, "volvo")
    assert "klienthemlighet-xyz" not in str(view) and view["has_secrets"]["vcc_api_key"] is True


def test_credentials_require_secret_and_api_key(db, user):
    conn = oauth.connection(db, user.id)
    with pytest.raises(VolvoError):
        oauth.save_credentials(db, conn, client_id="x", client_secret=None, vcc_api_key=None)


def test_authorize_url_uses_pkce_s256_and_redirect_uri(db, user):
    conn = configured(db, user)
    url, q = start(db, user, conn)
    assert url.startswith("https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/auth?")
    assert q["response_type"] == "code" and q["client_id"] == "vid-dc-test-client"
    assert q["redirect_uri"] == "http://testserver/api/v1/integrations/volvo/callback"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert set(q["scope"].split()) == set(C.DEFAULT_SCOPES)
    assert "conve:engine_status" in q["scope"] and "location:read" in q["scope"]
    row = db.execute(select(M.OAuthState)).scalar_one()
    assert row.state_hash != q["state"] and q["state"] not in (row.verifier_encrypted or "")
    assert row.expires_at - row.created_at == timedelta(minutes=10)


def test_legacy_auth_server_can_be_selected(db, user):
    conn = configured(db, user, auth_server="legacy")
    url, _ = start(db, user, conn)
    assert url.startswith("https://volvoid.eu.volvocars.com/as/authorization.oauth2?")


def test_code_exchange_uses_basic_auth_and_pkce_and_stores_tokens_encrypted(db, user):
    conn = configured(db, user)
    _, q = start(db, user, conn)
    rec = Recorder()
    conn = oauth.complete_authorization(db, user.id, state=q["state"], code="auth-kod", transport=rec.transport)
    (req,) = rec.token_calls()
    assert str(req.url) == C.AUTH_SERVERS["new"]["token"]
    expected = base64.b64encode(b"vid-dc-test-client:klienthemlighet-xyz").decode()
    assert req.headers["authorization"] == f"Basic {expected}"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-kod"
    assert form["redirect_uri"] == oauth.redirect_uri()
    verifier = form["code_verifier"]
    assert 43 <= len(verifier) <= 128
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert digest == q["code_challenge"]
    assert conn.status == IntegrationStatus.CONNECTED
    assert "AT-ny" not in (conn.secrets_encrypted or "") and "RT-ny" not in conn.secrets_encrypted
    sec = integrations.secrets(conn)
    assert sec["access_token"] == "AT-ny-111111111111" and sec["refresh_token"] == "RT-ny-22222222222"
    assert conn.token_expires_at > datetime.now(UTC) + timedelta(minutes=55)
    assert oauth.reauth_due_at(conn) is not None


def test_state_cannot_be_reused(db, user):
    conn = configured(db, user)
    _, q = start(db, user, conn)
    rec = Recorder()
    oauth.complete_authorization(db, user.id, state=q["state"], code="kod", transport=rec.transport)
    with pytest.raises(VolvoError) as err:
        oauth.complete_authorization(db, user.id, state=q["state"], code="kod", transport=rec.transport)
    assert err.value.code == "invalid_state"
    assert len(rec.token_calls()) == 1


def test_wrong_and_missing_state_are_rejected_without_token_call(db, user):
    configured(db, user)
    rec = Recorder()
    for state in ("påhittat-state", None):
        with pytest.raises(VolvoError) as err:
            oauth.complete_authorization(db, user.id, state=state, code="kod", transport=rec.transport)
        assert err.value.code == "invalid_state"
    assert rec.calls == []


def test_expired_state_is_consumed_and_rejected(db, user):
    conn = configured(db, user)
    _, q = start(db, user, conn)
    row = db.execute(select(M.OAuthState)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(VolvoError) as err:
        oauth.complete_authorization(db, user.id, state=q["state"], code="kod", transport=Recorder().transport)
    assert err.value.code == "state_expired"
    assert db.execute(select(M.OAuthState)).first() is None


def test_refresh_rotates_refresh_token(db, user, vehicle):
    conn = connect(db, user, vehicle)
    conn.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    rec = Recorder(token_body={"access_token": "AT-2-xxxxxxxxxxxx", "refresh_token": "RT-2-xxxxxxxxxxxx", "expires_in": 300})
    token = oauth.get_access_token(db, conn, transport=rec.transport)
    assert token == "AT-2-xxxxxxxxxxxx"
    (req,) = rec.token_calls()
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form == {"grant_type": "refresh_token", "refresh_token": "RT-gammal-0000000000"}
    sec = integrations.secrets(conn)
    assert sec["refresh_token"] == "RT-2-xxxxxxxxxxxx"  # roterad – den gamla är ogiltig
    assert conn.token_expires_at <= datetime.now(UTC) + timedelta(seconds=301)  # expires_in respekteras


def test_valid_token_is_not_refreshed(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder()
    assert oauth.get_access_token(db, conn, transport=rec.transport) == "AT-gammal-0000000000"
    assert rec.calls == []


def test_invalid_grant_requires_reauthentication(db, user, vehicle):
    conn = connect(db, user, vehicle)
    conn.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(VolvoAuthError):
        oauth.get_access_token(db, conn, transport=transport)
    assert conn.status == IntegrationStatus.NEEDS_REAUTH
    sec = integrations.secrets(conn)
    assert "refresh_token" not in sec and "access_token" not in sec
    assert sec["client_secret"]  # klientuppgifterna finns kvar för ny inloggning


def test_invalid_grant_during_poll_gives_needs_reauth_status(db, user, vehicle):
    conn = connect(db, user, vehicle)
    conn.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    result = collector.poll_vehicle(db, vehicle, transport=transport)
    assert result["status"] == "needs_reauth"
    assert conn.status == IntegrationStatus.NEEDS_REAUTH
    run = db.execute(select(M.IntegrationSyncRun)).scalar_one()
    assert run.status == "error"


def test_401_triggers_one_refresh_and_one_retry(db, user, vehicle):
    conn = connect(db, user, vehicle)

    def odometer(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer AT-gammal-0000000000":
            return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
        return httpx.Response(200, json={"data": {"odometer": {"value": 1500, "unit": "km", "timestamp": "2026-10-05T06:00:00Z"}}})

    rec = Recorder(overrides={"odometer": odometer})
    client = collector.build_client(db, conn, transport=rec.transport)
    result = client.fetch("odometer", VIN)
    assert result.ok and result.data["data"]["odometer"]["value"] == 1500
    assert len(rec.token_calls()) == 1
    assert [c.headers["authorization"] for c in rec.api_calls()] == ["Bearer AT-gammal-0000000000", "Bearer AT-ny-111111111111"]
    assert integrations.secrets(conn)["refresh_token"] == "RT-ny-22222222222"


def test_persistent_401_raises_auth_error_after_single_retry(db, user, vehicle):
    conn = connect(db, user, vehicle)
    rec = Recorder(overrides={"odometer": httpx.Response(401, json={})})
    client = collector.build_client(db, conn, transport=rec.transport)
    with pytest.raises(VolvoAuthError):
        client.fetch("odometer", VIN)
    assert len(rec.api_calls()) == 2 and len(rec.token_calls()) == 1


def test_test_token_mode_has_no_refresh_and_is_marked(db, user, vehicle):
    conn = oauth.connection(db, user.id)
    oauth.set_test_token(db, conn, access_token="portal-testtoken-123456789", vcc_api_key="vcc-nyckel-abc")
    assert oauth.is_test_token_mode(conn) and conn.status == IntegrationStatus.CONFIGURED
    assert "portal-testtoken" not in (conn.secrets_encrypted or "")
    rec = Recorder()
    collector.discover_vehicles(db, conn, transport=rec.transport)
    assert conn.status == IntegrationStatus.CONNECTED  # verifierad med ett riktigt anrop
    assert rec.api_calls()[0].headers["authorization"] == "Bearer portal-testtoken-123456789"
    assert rec.api_calls()[0].headers["vcc-api-key"] == "vcc-nyckel-abc"
    assert rec.token_calls() == []
    # Utgången testtoken → ingen förnyelse, ny token eller inloggning krävs.
    expired = Recorder(overrides={"vehicles": httpx.Response(401, json={})})
    with pytest.raises(VolvoAuthError):
        collector.discover_vehicles(db, conn, transport=expired.transport)
    assert expired.token_calls() == [] and conn.status == IntegrationStatus.NEEDS_REAUTH


def test_disconnect_removes_all_secrets(db, user, vehicle):
    conn = connect(db, user, vehicle)
    oauth.disconnect(db, conn)
    assert conn.secrets_encrypted is None and integrations.secrets(conn) == {}
    assert conn.status == IntegrationStatus.NOT_CONFIGURED
    assert vehicle.volvo_connection_id is None
    assert not oauth.has_real_credentials(conn)
