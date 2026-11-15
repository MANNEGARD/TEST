"""Regressionstester för granskningen av Home Assistant-tillägget (SAK-S1/SUP-F2 och SAK-S2)."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from alembic import command
from app import db as dbm
from app.api.deps import client_ip
from app.config import reset_config_cache
from app.main import create_app
from tests import test_enprocess_sqlite as enprocess
from tests.conftest import TEST_PASSWORD
from tests.test_enprocess_sqlite import API, https_client

NEW_ADMIN = {"email": "agare@example.com", "password": TEST_PASSWORD}


# ----------------------------------------------------------------------------- DRIVELOG_WEB_SETUP


@pytest.fixture()
def single_process(tmp_path, monkeypatch):
    """Tilläggets kontrakt mot en migrerad SQLite-fil (som i test_enprocess_sqlite)."""
    url = enprocess.contract_env(monkeypatch, tmp_path / "data" / "app")
    command.upgrade(enprocess.alembic_config(url), "head")
    yield url
    dbm.dispose_engine()
    reset_config_cache()


@pytest.fixture()
def web_setup_off(monkeypatch):
    monkeypatch.setenv("DRIVELOG_WEB_SETUP", "false")
    reset_config_cache()
    yield
    reset_config_cache()


def test_web_setup_disabled_rejects_first_account_but_status_works(engine, web_setup_off):
    with TestClient(create_app()) as c:
        assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": True}
        r = c.post(f"{API}/auth/setup", json=NEW_ADMIN)
        assert r.status_code == 403
        body = r.json()
        assert "setup_disabled" in r.text
        assert "inställningarna" in str(body)
        # Fortfarande inget konto.
        assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": True}


def test_web_setup_disabled_also_when_accounts_exist(engine, db, user, web_setup_off):
    db.commit()
    with TestClient(create_app()) as c:
        r = c.post(f"{API}/auth/setup", json=NEW_ADMIN)
        assert r.status_code == 403 and "setup_disabled" in r.text
        assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": False}


def test_web_setup_is_on_by_default(engine, monkeypatch):
    monkeypatch.delenv("DRIVELOG_WEB_SETUP", raising=False)
    reset_config_cache()
    with TestClient(create_app()) as c:
        assert c.post(f"{API}/auth/setup", json=NEW_ADMIN).status_code == 200


def test_bootstrap_warns_when_configured_admin_is_not_among_existing_accounts(single_process, monkeypatch, caplog):
    monkeypatch.setenv("DRIVELOG_WEB_SETUP", "true")
    reset_config_cache()
    with https_client() as c:  # "någon annan" hinner skapa första kontot
        assert c.post(f"{API}/auth/setup", json={"email": "inkraktare@example.com", "password": TEST_PASSWORD}).status_code == 200
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_EMAIL", "Agare@Example.com")
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_PASSWORD", TEST_PASSWORD)
    reset_config_cache()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="drivelog"), https_client() as c:
        assert c.get(f"{API}/health/ready").status_code == 200
    assert "agare@example.com" in caplog.text and "inget av dem har den adressen" in caplog.text
    assert TEST_PASSWORD not in caplog.text


def test_bootstrap_is_quiet_when_configured_admin_exists(single_process, monkeypatch, caplog):
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_EMAIL", "agare@example.com")
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_PASSWORD", TEST_PASSWORD)
    reset_config_cache()
    with https_client():
        pass
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="drivelog"), https_client():
        pass
    assert "inget av dem har den adressen" not in caplog.text


# ----------------------------------------------------------------------------- client_ip och proxyn


def _request(headers: dict[str, str], client: tuple[str, int] | None) -> StarletteRequest:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return StarletteRequest({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": client})


@pytest.mark.parametrize(("allow", "peer", "xff", "expected"), [
    # Osatt/"*" (Compose): rubriken från proxyn gäller, som tidigare.
    (None, ("192.0.2.10", 1), "10.9.9.9, 203.0.113.7", "203.0.113.7"),
    ("*", ("192.0.2.10", 1), "203.0.113.7", "203.0.113.7"),
    # Tillägget: bara 127.0.0.1 (Caddy) är betrodd.
    ("127.0.0.1", ("127.0.0.1", 1), "203.0.113.7", "203.0.113.7"),
    ("127.0.0.1", ("172.30.33.5", 1), "203.0.113.7", "172.30.33.5"),  # annan container, förfalskat
    # uvicorn har redan skrivit om klienten (betrodd proxy) – adressen används som den är.
    ("127.0.0.1", ("203.0.113.7", 0), "203.0.113.7", "203.0.113.7"),
    # Nät och listor.
    ("10.0.0.0/8, ::1", ("10.1.2.3", 1), "198.51.100.4", "198.51.100.4"),
    ("10.0.0.0/8, ::1", ("::1", 1), "198.51.100.4", "198.51.100.4"),
    ("10.0.0.0/8, ::1", ("11.1.2.3", 1), "198.51.100.4", "11.1.2.3"),
    ("127.0.0.1", ("testclient", 1), "198.51.100.4", "testclient"),
])
def test_client_ip_trusts_forwarded_for_only_from_trusted_proxy(monkeypatch, allow, peer, xff, expected):
    if allow is None:
        monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    else:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", allow)
    assert client_ip(_request({"X-Forwarded-For": xff}, peer)) == expected
    assert client_ip(_request({}, peer)) == peer[0]


def _behind_uvicorn(peer: str) -> object:
    """Appen bakom uvicorns ProxyHeadersMiddleware (--forwarded-allow-ips=127.0.0.1), anropad från ``peer``."""
    inner = FastAPI()

    @inner.get("/ip")
    def ip(request: Request) -> dict:
        return {"ip": client_ip(request)}

    proxied = ProxyHeadersMiddleware(inner, trusted_hosts="127.0.0.1")

    async def app(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (peer, 40000)}
        await proxied(scope, receive, send)

    return app


@pytest.mark.parametrize(("peer", "expected"), [
    ("127.0.0.1", "203.0.113.7"),  # via Caddy: besökarens adress
    ("172.30.33.5", "172.30.33.5"),  # direkt mot port 8000: containerns riktiga adress
])
def test_client_ip_with_uvicorn_proxy_headers(monkeypatch, peer, expected):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    with TestClient(_behind_uvicorn(peer)) as c:
        assert c.get("/ip", headers={"X-Forwarded-For": "203.0.113.7"}).json() == {"ip": expected}


def test_forged_forwarded_for_does_not_bypass_login_lockout(anon_client, db, monkeypatch):
    # TestClient ansluter som "testclient" – inte en betrodd proxy i tilläggets inställning.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    statuses = []
    for i in range(15):
        r = anon_client.post(
            f"{API}/auth/login",
            json={"email": f"okand{i}@example.com", "password": "fel-losenord-123"},
            headers={"X-Forwarded-For": f"10.0.{i}.1"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses, statuses
