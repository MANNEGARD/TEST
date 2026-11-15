"""Enprocessläget i drift (Home Assistant-tillägget): filbaserad SQLite, migrationer,
jobb i API-processen utan låsningar, ingen Redis, första kontot och säkerhetskopior.

Testerna här använder en riktig SQLite-FIL (WAL, flera anslutningar och trådar) – inte
minnesdatabasen med StaticPool som resten av sviten kör mot.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import stat
import threading
import time
import traceback
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import app.models as M
from alembic import command
from app import cli, jobs
from app import db as dbm
from app.config import AppConfig, reset_config_cache
from app.db import Base
from app.security import crypto, ratelimit
from app.services import backup
from tests.conftest import TEST_PASSWORD
from tests.fixtures import volvo_exports as fx

API = "/api/v1"
BACKEND = Path(__file__).resolve().parents[1]
MODE_LINE = "Enprocessläge: SQLite, jobb och schemaläggare i API-processen"


def alembic_config(url: str) -> Config:
    cfg = Config()  # ingen ini-fil: rör inte testkörningens loggning
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["database_url"] = url
    return cfg


def contract_env(monkeypatch, data: Path, *, environment: str = "production") -> str:
    """Miljön som Home Assistant-tilläggets startprogram sätter (se docs/arkitektur.md)."""
    url = f"sqlite:///{data}/drivelog.sqlite3"
    env = {
        "DRIVELOG_ENVIRONMENT": environment,
        "DRIVELOG_DATABASE_URL": url,
        "DRIVELOG_DATA_DIR": str(data),
        "DRIVELOG_JOBS_EAGER": "true",
        "DRIVELOG_REDIS_URL": "",
        "DRIVELOG_BASE_URL": "https://drivelog.example.org",
        "DRIVELOG_SECRET_KEY": "s" * 48,
        "DRIVELOG_ENCRYPTION_KEYS": crypto.generate_key(),
        "DRIVELOG_TIMEZONE": "Europe/Stockholm",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DRIVELOG_FIRST_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("DRIVELOG_FIRST_ADMIN_PASSWORD", raising=False)
    reset_config_cache()
    dbm.dispose_engine()
    return url


@pytest.fixture()
def no_redis(monkeypatch):
    """Varje försök att nå Redis fäller testet."""
    import redis

    def boom(*_a, **_k):
        raise AssertionError("Redis får inte anropas i enprocessläget")

    monkeypatch.setattr(redis.Redis, "from_url", boom)
    monkeypatch.setattr(ratelimit, "_redis_client", None)


@pytest.fixture()
def single_process(tmp_path, monkeypatch, no_redis):
    """Kontraktets miljö + en ny databasfil som har migrerats med alembic upgrade head."""
    data = tmp_path / "data"
    url = contract_env(monkeypatch, data)
    command.upgrade(alembic_config(url), "head")
    yield url
    dbm.dispose_engine()
    reset_config_cache()


def https_client(**kw) -> TestClient:
    from app.main import create_app

    # https: produktionens sessionskaka är Secure och skickas bara över https.
    return TestClient(create_app(), base_url="https://testserver", **kw)


# ----------------------------------------------------------------------------- SQLite-motorn


def test_file_sqlite_engine_uses_wal_busy_timeout_and_foreign_keys(single_process):
    engine = dbm.get_engine()
    results: list[tuple] = []

    def read() -> None:
        with engine.connect() as conn:
            results.append(tuple(conn.execute(text(f"PRAGMA {p}")).scalar() for p in
                                 ("journal_mode", "busy_timeout", "foreign_keys", "synchronous")))

    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    read()
    assert len(results) == 5
    for journal, busy, fks, sync in results:
        assert journal == "wal" and busy >= 10_000 and fks == 1 and sync == 1  # 1 = NORMAL
    assert engine.pool.__class__.__name__ == "QueuePool"
    db_file = dbm.sqlite_file_path(single_process)
    assert db_file is not None and stat.S_IMODE(db_file.stat().st_mode) == 0o600


def test_migration_on_new_file_matches_models_and_is_repeatable(single_process):
    url = single_process
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    command.upgrade(alembic_config(url), "head")  # andra körningen: ofarlig
    engine = dbm.get_engine()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [head]
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


# ----------------------------------------------------------------------------- konfiguration


def test_production_check_accepts_contract_and_keeps_compose_rules(single_process, monkeypatch):
    cfg = AppConfig()
    assert cfg.validate_for_production() == []
    assert cfg.single_process and not cfg.redis_enabled and cfg.is_sqlite
    assert cfg.mode_description() == MODE_LINE

    base = {"environment": "production", "secret_key": "s" * 48, "encryption_keys": crypto.generate_key(),
            "base_url": "https://drivelog.example.org"}
    compose = AppConfig(**base, database_url="postgresql://u:p@db/drivelog", redis_url="redis://redis:6379/0",
                        jobs_eager=False)
    assert compose.validate_for_production() == [] and not compose.single_process
    assert compose.mode_description().startswith("Compose-läge")
    assert any("REDIS" in p for p in AppConfig(**base, database_url="postgresql://u:p@db/x", redis_url="",
                                               jobs_eager=False).validate_for_production())
    assert any("SQLite" in p for p in AppConfig(**base, redis_url="redis://r", jobs_eager=False,
                                                database_url="sqlite:////data/x.sqlite3").validate_for_production())
    assert any("minnesdatabas" in p for p in AppConfig(**base, database_url="sqlite://", redis_url="",
                                                       jobs_eager=True).validate_for_production())


def test_production_startup_logs_mode_skips_init_schema_and_needs_no_redis(single_process, monkeypatch, caplog):
    import app.main as main

    def forbidden() -> None:
        raise AssertionError("init_schema får inte köras i produktion")

    monkeypatch.setattr(main, "init_schema", forbidden)
    caplog.set_level(logging.INFO)
    with https_client() as c:
        assert MODE_LINE in caplog.text
        ready = c.get(f"{API}/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["checks"] == {"database": "ok", "queue": "eager", "scheduler": "ok"}
        assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": True}
        sched = main._scheduler
        assert sched is not None and sched.running
        # Inloggningsspärren fungerar med minnet direkt (Redis-anrop skulle fälla testet).
        for _ in range(3):
            c.post(f"{API}/auth/login", json={"email": "ingen@example.com", "password": "fel-lösenord-1"})
        assert ratelimit._redis() is None
    assert not sched.running and main._scheduler is None  # tråden avslutades vid nedstängning


# ----------------------------------------------------------------------------- första kontot


def users(engine) -> list[str]:
    with engine.connect() as conn:
        return conn.execute(select(M.User.email)).scalars().all()


def test_first_admin_from_env_created_before_requests(single_process, monkeypatch):
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_EMAIL", "Agare@Example.com")
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_PASSWORD", TEST_PASSWORD)
    reset_config_cache()
    with https_client() as c:
        assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": False}
        assert c.post(f"{API}/auth/setup", json={"email": "x@example.com", "password": TEST_PASSWORD}).status_code == 409
        r = c.post(f"{API}/auth/login", json={"email": "agare@example.com", "password": TEST_PASSWORD})
        assert r.status_code == 200
    # Med ett konto på plats gör variablerna ingenting – inte heller med andra värden.
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_EMAIL", "annan@example.com")
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_PASSWORD", "kort")
    reset_config_cache()
    with https_client() as c:
        assert c.get(f"{API}/health/ready").status_code == 200
    assert users(dbm.get_engine()) == ["agare@example.com"]


@pytest.mark.parametrize(("email", "password", "message"), [
    ("agare@example.com", "kort-1", "DRIVELOG_FIRST_ADMIN_PASSWORD duger inte"),
    ("inte-en-adress", TEST_PASSWORD, "DRIVELOG_FIRST_ADMIN_EMAIL är inte en giltig"),
    ("agare@example.com", "", "Både DRIVELOG_FIRST_ADMIN_EMAIL och DRIVELOG_FIRST_ADMIN_PASSWORD"),
])
def test_invalid_first_admin_stops_startup_without_leaking_password(single_process, monkeypatch, caplog,
                                                                     email, password, message):
    from app.main import BootstrapError

    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_EMAIL", email)
    monkeypatch.setenv("DRIVELOG_FIRST_ADMIN_PASSWORD", password)
    reset_config_cache()
    with pytest.raises(BootstrapError), https_client():
        pass  # pragma: no cover - uppstarten ska avbrytas
    assert message in caplog.text and "API:t startas inte" in caplog.text
    if password:
        assert password not in caplog.text
    assert users(dbm.get_engine()) == []  # inget halvfärdigt konto


# ----------------------------------------------------------------------------- jobb i processen


def test_job_waits_until_callers_write_transaction_is_finished(single_process):
    engine = dbm.get_engine()
    with dbm.session_scope() as s:
        s.add(M.User(email="a@example.com", display_name="A", password_hash="x", is_admin=True))
    ran: list[str] = []

    @jobs.register("test.rename")
    def rename(label: str) -> None:
        with dbm.session_scope() as s:
            s.execute(M.User.__table__.update().values(display_name=label))
        ran.append(label)

    try:
        session = dbm.get_sessionmaker()()
        user = session.execute(select(M.User)).scalar_one()
        user.display_name = "Från anropet"
        session.flush()  # skrivlåset hålls nu av den här tråden
        start = time.monotonic()
        jobs.enqueue("test.rename", label="Från jobbet")
        assert ran == [] and time.monotonic() - start < 2  # ingen väntan på det egna låset
        session.commit()
        assert ran == ["Från jobbet"]
        session.close()
        with engine.connect() as conn:
            assert conn.execute(select(M.User.display_name)).scalar() == "Från jobbet"
        # Utan öppen skrivtransaktion körs jobbet direkt; även efter rollback körs uppskjutna jobb.
        jobs.enqueue("test.rename", label="Direkt")
        assert ran[-1] == "Direkt"
        session = dbm.get_sessionmaker()()
        session.execute(M.User.__table__.update().values(display_name="Rullas tillbaka"))
        jobs.enqueue("test.rename", label="Efter rollback")
        assert ran[-1] == "Direkt"
        session.close()
        assert ran[-1] == "Efter rollback"
    finally:
        jobs._registry.pop("test.rename", None)


class Ticker(threading.Thread):
    """Kör schemaläggarens jobb om och om igen från en egen tråd."""

    def __init__(self) -> None:
        super().__init__(name="test-ticker", daemon=True)
        self.stop = threading.Event()
        self.rounds = 0
        self.errors: list[str] = []

    def run(self) -> None:
        from app.worker import tasks

        while not self.stop.is_set():
            try:
                tasks.tick()
                if self.rounds % 5 == 0:
                    tasks.calendar_sync_due()
                if self.rounds % 20 == 0:
                    tasks.maintenance()
            except Exception:
                self.errors.append(traceback.format_exc())
            self.rounds += 1


def test_full_flow_on_sqlite_file_with_concurrent_scheduler(single_process, caplog, tmp_path):
    caplog.set_level(logging.WARNING)
    ticker = Ticker()
    with https_client() as c:
        ticker.start()
        try:
            r = c.post(f"{API}/auth/setup", json={"email": "agare@example.com", "password": TEST_PASSWORD})
            assert r.status_code == 200, r.text
            c.headers["X-CSRF-Token"] = r.json()["csrf_token"]

            files = [("files", (n, io.BytesIO(d), "application/octet-stream")) for n, d in fx.sample_files()]
            batch = c.post(f"{API}/imports/preview", files=files)
            assert batch.status_code == 201, batch.text
            result = c.post(f"{API}/imports/{batch.json()['id']}/commit", json={})
            assert result.status_code == 200, result.text
            assert result.json()["imported"] == 32

            review = c.get(f"{API}/review").json()
            q = next(i for i in review["items"] if i["kind"] == "merge")
            answered = c.post(f"{API}/merge-questions/{q['id']}/answer", json={"accept": True})
            assert answered.status_code == 200 and answered.json()["merged"] is True, answered.text

            trip = c.get(f"{API}/trips?page_size=1").json()["items"][0]
            patched = c.patch(f"{API}/trips/{trip['id']}", json={"category": "private"})
            assert patched.status_code == 200, patched.text  # köar classify.pending
            assert c.post(f"{API}/integrations/ai/classify-pending").status_code == 200

            export = c.post(f"{API}/exports", json={"from": "2026-06-01", "to": "2026-07-31"})
            assert export.status_code == 201, export.text

            assert c.post(f"{API}/integrations/volvo/demo/enable").status_code == 200
            assert c.post(f"{API}/integrations/home-assistant/demo/enable").status_code == 200
            for scenario in ("commute", "customer_visit", "round_trip"):
                sim = c.post(f"{API}/integrations/volvo/demo/simulate", json={"scenario": scenario})
                assert sim.status_code == 200 and sim.json()["new_trip_ids"], sim.text
            assert c.get(f"{API}/notifications").status_code == 200
            assert c.get(f"{API}/health/ready").json()["checks"]["scheduler"] == "ok"
        finally:
            ticker.stop.set()
            ticker.join(60)
    assert not ticker.is_alive(), "schemaläggartråden hängde sig"
    assert ticker.errors == []
    assert ticker.rounds >= 3
    assert "database is locked" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text

    # Säkerhetskopiering på applikationsnivå (app.cli backup/restore) fungerar med SQLite-filen.
    caplog.clear()
    assert cli.main(["backup"]) == 0
    zips = sorted((tmp_path / "data" / "backups").glob("drivelog-backup-*.zip"))
    assert len(zips) == 1 and stat.S_IMODE(zips[0].stat().st_mode) == 0o600
    engine = dbm.get_engine()
    with engine.connect() as conn:
        before = {t: conn.execute(select(func.count()).select_from(Base.metadata.tables[t])).scalar()
                  for t in ("users", "raw_trips", "logical_trips", "vehicles")}
    assert cli.main(["restore", str(zips[0])]) == 1  # vägrar skriva över utan --force
    assert cli.main(["restore", str(zips[0]), "--force"]) == 0
    with engine.connect() as conn:
        after = {t: conn.execute(select(func.count()).select_from(Base.metadata.tables[t])).scalar()
                 for t in before}
    assert after == before and before["raw_trips"] >= 32


# ----------------------------------------------------------------------------- filkopia av databasen


def test_sqlite_backup_is_consistent_while_writes_continue(single_process, tmp_path, capsys):
    engine = dbm.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE skrivtest (n INTEGER NOT NULL)"))
    stop = threading.Event()
    written = [0]

    def writer() -> None:
        while not stop.is_set():
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO skrivtest (n) VALUES (1)"))
            written[0] += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        while written[0] < 50:
            time.sleep(0.01)
        target = tmp_path / "kopior" / "drivelog-db.sqlite3"
        assert cli.main(["sqlite-backup", str(target)]) == 0
    finally:
        stop.set()
        t.join(10)
    assert capsys.readouterr().out.strip() == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".*.tmp"))
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # fristående fil
        assert copy.execute("SELECT count(*) FROM skrivtest").fetchone()[0] >= 50
        assert copy.execute("SELECT count(*) FROM alembic_version").fetchone()[0] == 1
    finally:
        copy.close()
    # Målet får inte vara själva databasen, och kommandot kräver SQLite.
    assert cli.main(["sqlite-backup", str(tmp_path / "data" / "drivelog.sqlite3")]) == 1
    with pytest.raises(backup.BackupError):
        backup.sqlite_file_backup(tmp_path / "x.sqlite3", database_url="postgresql+psycopg://u:p@h/db")

