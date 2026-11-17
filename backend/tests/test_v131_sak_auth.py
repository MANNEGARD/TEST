"""1.3.1, säkerhet: inloggningens spärrar (inloggning-2, -3, -5, -8)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import app.models as M
from app import db as dbm
from app.config import get_config, reset_config_cache
from app.db import Base
from app.main import create_app
from app.security import passwords, ratelimit
from tests.conftest import TEST_PASSWORD

API = "/api/v1"
DEVICE_COOKIE = "drivelog_device"


def _login(c: TestClient, email: str, password: str, ip: str):
    return c.post(f"{API}/auth/login", json={"email": email, "password": password}, headers={"X-Forwarded-For": ip})


# ----------------------------------------------------------------------------- inloggning-2


def test_account_attempt_is_counted_before_password_check(anon_client, db, user, monkeypatch):
    """Försöket mot kontot räknas innan lösenordet prövas (inte efter Argon2-verifieringen)."""
    db.commit()
    seen: list[int] = []
    real_verify = passwords.verify_password

    def spy(stored: str, password: str) -> bool:
        seen.append(len(ratelimit._memory.get(f"login:user:{user.email}", ())))
        return real_verify(stored, password)

    monkeypatch.setattr(passwords, "verify_password", spy)
    assert _login(anon_client, user.email, "fel-lösenord", "10.2.0.1").status_code == 401
    assert seen == [1]


@pytest.fixture()
def file_engine(tmp_path):
    """SQLite i en fil med vanlig anslutningspool, så att samtidiga anrop får egna anslutningar."""
    eng = create_engine(f"sqlite:///{tmp_path / 'par.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(eng)
    dbm.set_engine(eng)
    with dbm.get_sessionmaker()() as s:
        s.add(M.User(email="forare@example.com", display_name="Test", password_hash=passwords.hash_password(TEST_PASSWORD)))
        s.commit()
    yield eng
    dbm.dispose_engine()


def test_parallel_attempts_cannot_pass_account_lockout(file_engine, monkeypatch):
    """40 samtidiga försök från 5 adresser: högst gränsen (8) når lösenordsprövningen."""
    reached: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def slow_wrong(stored: str, password: str) -> bool:
        with lock:
            reached.append(1)
        time.sleep(0.1)
        return False

    monkeypatch.setattr(passwords, "verify_password", slow_wrong)
    statuses: list[int] = []
    with TestClient(create_app()) as c:

        def attempt(i: int) -> None:
            barrier.wait()
            statuses.append(_login(c, "forare@example.com", "fel", f"10.3.0.{i % 5}").status_code)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(statuses) == 40 and set(statuses) <= {401, 429}, statuses
    assert len(reached) <= get_config().login_rate_limit_attempts, len(reached)


def test_try_acquire_is_atomic_in_memory():
    barrier = threading.Barrier(30)
    results: list[bool] = []

    def worker() -> None:
        barrier.wait()
        results.append(ratelimit.try_acquire(["k:a", "k:b"], 8, 300)[0])

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 8
    assert len(ratelimit._memory["k:a"]) == len(ratelimit._memory["k:b"]) == 8


def test_try_acquire_rejected_attempt_is_not_recorded():
    for _ in range(8):
        assert ratelimit.try_acquire(["ip:x"], 8, 300)[0]
    # Nyckeln "user:y" är tom men "ip:x" är full: inget av dem får ett nytt försök registrerat.
    allowed, wait = ratelimit.try_acquire(["user:y", "ip:x"], 8, 300)
    assert not allowed and wait > 0
    assert len(ratelimit._memory.get("user:y", ())) == 0
    assert len(ratelimit._memory["ip:x"]) == 8


def test_argon2_concurrency_is_bounded(monkeypatch):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    class SlowHasher:
        def _work(self) -> None:
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1

        def verify(self, h: str, p: str) -> bool:
            self._work()
            return True

        def hash(self, p: str) -> str:
            self._work()
            return "$argon2id$x"

    monkeypatch.setattr(passwords, "_hasher", SlowHasher())
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        if i % 2:
            passwords.verify_password("h", "p")
        else:
            passwords.hash_password("p")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= active["max"] <= 4, active


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def local_redis(monkeypatch):
    """En lokal redis-server (bara 127.0.0.1) för att pröva Redis-vägen i spärren."""
    exe = shutil.which("redis-server")
    if exe is None:
        pytest.skip("redis-server saknas")
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [exe, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import redis

    client = redis.Redis(host="127.0.0.1", port=port)
    for _ in range(50):
        try:
            client.ping()
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("redis-server startade inte")
    monkeypatch.setattr(ratelimit, "_redis", lambda: client)
    yield client
    proc.kill()
    proc.wait()


def test_try_acquire_is_atomic_with_redis(local_redis):
    barrier = threading.Barrier(30)
    results: list[bool] = []

    def worker() -> None:
        barrier.wait()
        results.append(ratelimit.try_acquire(["r:a", "r:b"], 8, 300)[0])

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 8
    assert local_redis.zcard("drivelog:rl:r:a") == local_redis.zcard("drivelog:rl:r:b") == 8
    assert not ratelimit._memory.get("r:a")  # Redis användes, inte minnet
    # Ett avvisat försök registreras inte heller i Redis.
    assert not ratelimit.try_acquire(["r:c", "r:a"], 8, 300)[0]
    assert local_redis.zcard("drivelog:rl:r:c") == 0
    ratelimit.reset("r:a")
    assert local_redis.zcard("drivelog:rl:r:a") == 0


# ----------------------------------------------------------------------------- inloggning-3


def _device_cookie(resp) -> str:
    raw = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie" and v.startswith(DEVICE_COOKIE + "=")]
    assert len(raw) == 1, resp.headers
    return raw[0]


def test_login_sets_known_device_cookie_without_changing_body(anon_client, db, user):
    db.commit()
    resp = _login(anon_client, user.email, TEST_PASSWORD, "10.4.0.1")
    assert resp.status_code == 200
    assert set(resp.json()) == {"user", "csrf_token"}
    cookie = _device_cookie(resp).lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api/v1/auth/login" in cookie
    assert "secure" not in cookie  # testservern kör http
    # Sessionskakan sätts som förut.
    assert any(v.startswith(get_config().cookie_name + "=") for k, v in resp.headers.multi_items() if k == "set-cookie")


def test_device_cookie_is_secure_with_secure_cookies(anon_client, db, user, monkeypatch):
    db.commit()
    monkeypatch.setenv("DRIVELOG_COOKIE_SECURE", "true")
    reset_config_cache()
    resp = _login(anon_client, user.email, TEST_PASSWORD, "10.4.0.2")
    assert resp.status_code == 200
    assert "secure" in _device_cookie(resp).lower()


def _owner_device(engine, user) -> str:
    with TestClient(create_app()) as owner:
        resp = _login(owner, user.email, TEST_PASSWORD, "10.5.0.9")
        assert resp.status_code == 200
        return resp.cookies[DEVICE_COOKIE]


def _attack(engine, user) -> None:
    with TestClient(create_app()) as attacker:
        for _ in range(8):
            assert _login(attacker, user.email, "gissning", "10.5.0.66").status_code == 401


def test_known_device_is_not_locked_out_by_attacker(engine, db, user):
    db.commit()
    device = _owner_device(engine, user)
    _attack(engine, user)
    with TestClient(create_app()) as fresh:
        assert _login(fresh, user.email, TEST_PASSWORD, "10.5.0.7").status_code == 429  # okänd enhet: som förut
    with TestClient(create_app()) as owner:
        owner.cookies.set(DEVICE_COOKIE, device, path="/api/v1/auth/login")
        resp = _login(owner, user.email, TEST_PASSWORD, "10.5.0.7")
        assert resp.status_code == 200, resp.text


def test_forged_device_cookie_does_not_help(engine, db, user):
    db.commit()
    device = _owner_device(engine, user)
    _attack(engine, user)
    nonce, sig = device.split(".", 1)
    forged = f"{nonce}.{'0' * len(sig)}"
    with TestClient(create_app()) as c:
        c.cookies.set(DEVICE_COOKIE, forged, path="/api/v1/auth/login")
        assert _login(c, user.email, TEST_PASSWORD, "10.5.0.8").status_code == 429


def test_device_cookie_is_bound_to_account(engine, db, user):
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.commit()
    with TestClient(create_app()) as c:
        device = _login(c, other.email, TEST_PASSWORD, "10.5.1.1").cookies[DEVICE_COOKIE]
    _attack(engine, user)
    with TestClient(create_app()) as c:
        c.cookies.set(DEVICE_COOKIE, device, path="/api/v1/auth/login")
        assert _login(c, user.email, TEST_PASSWORD, "10.5.1.2").status_code == 429


def test_device_cookie_stops_working_after_password_change(engine, db, user):
    db.commit()
    device = _owner_device(engine, user)
    new_password = "nytt-långt-lösenord-9"
    with TestClient(create_app()) as c:
        resp = _login(c, user.email, TEST_PASSWORD, "10.5.2.1")
        c.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
        r = c.post(f"{API}/auth/change-password", json={"current_password": TEST_PASSWORD, "new_password": new_password},
                   headers={"X-Forwarded-For": "10.5.2.1"})
        assert r.status_code == 204
    with TestClient(create_app()) as attacker:
        for _ in range(8):
            assert _login(attacker, user.email, "gissning", "10.5.2.66").status_code == 401
    with TestClient(create_app()) as c:
        c.cookies.set(DEVICE_COOKIE, device, path="/api/v1/auth/login")
        assert _login(c, user.email, new_password, "10.5.2.2").status_code == 429


def test_known_device_still_has_its_own_limit(engine, db, user):
    db.commit()
    device = _owner_device(engine, user)
    with TestClient(create_app()) as c:
        c.cookies.set(DEVICE_COOKIE, device, path="/api/v1/auth/login")
        for i in range(8):
            assert _login(c, user.email, "gissning", f"10.5.3.{i}").status_code == 401
        assert _login(c, user.email, TEST_PASSWORD, "10.5.3.99").status_code == 429


# ----------------------------------------------------------------------------- inloggning-5


def _change(client, current: str, new: str = "nytt-långt-lösenord-1"):
    return client.post(f"{API}/auth/change-password", json={"current_password": current, "new_password": new})


def test_password_change_guessing_is_limited(client, db, user):
    for _ in range(8):
        assert _change(client, "fel-gissning").status_code == 400
    blocked = _change(client, "fel-gissning")
    assert blocked.status_code == 429 and blocked.json()["code"] == "rate_limited"
    assert _change(client, TEST_PASSWORD).status_code == 429
    rows = db.query(M.AuditEvent).filter(M.AuditEvent.action == "auth.password_change_failed").all()
    assert len(rows) == 8
    assert all("fel-gissning" not in repr(r.after) and "fel-gissning" not in repr(r.before) for r in rows)


def test_password_change_with_right_password_is_not_limited(client):
    for _ in range(3):
        assert _change(client, "fel-gissning").status_code == 400
    # Rätt nuvarande lösenord men för svagt nytt: räknas inte mot spärren.
    for _ in range(10):
        r = _change(client, TEST_PASSWORD, "kort")
        assert r.status_code == 400 and r.json()["code"] == "weak_password"
    assert _change(client, TEST_PASSWORD).status_code == 204
    assert not ratelimit._memory.get(next(iter(k for k in ratelimit._memory if k.startswith("pwchange:")), ""), None)


# ----------------------------------------------------------------------------- inloggning-8


def test_failed_login_does_not_store_typed_text_for_unknown_account(anon_client, db, user):
    db.commit()
    assert _login(anon_client, "Hemligt-Lösen-123", "x", "10.6.0.1").status_code == 401
    assert _login(anon_client, "Hemligt@Lösen-123", "x", "10.6.0.1").status_code == 401
    db.expire_all()
    rows = db.query(M.AuditEvent).filter(M.AuditEvent.action == "auth.login_failed").all()
    assert len(rows) == 2
    for r in rows:
        text = repr(r.after).lower()
        assert "hemligt" not in text and "lösen" not in text, r.after
        assert r.after == {"known_account": False}


def test_failed_login_keeps_address_for_existing_account(anon_client, db, user):
    db.commit()
    assert _login(anon_client, user.email, "fel", "10.6.0.2").status_code == 401
    db.expire_all()
    row = db.query(M.AuditEvent).filter(M.AuditEvent.action == "auth.login_failed").one()
    assert row.after["email"] == user.email
