"""Databasmotor, sessioner och gemensamma kolumntyper."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.config import get_config

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# JSONB på PostgreSQL (stöder likhet, DISTINCT och index); vanlig JSON på SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """Tidsstämpel som alltid lagras i UTC och alltid returneras tidszonsmedveten.

    SQLite saknar tidszonsstöd; därför normaliseras värdet i båda riktningar så att
    koden beter sig identiskt på SQLite (test) och PostgreSQL (drift).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naiva datetime-värden får inte lagras; ange tidszon (UTC).")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


# Hur länge en anslutning väntar på SQLite-låset innan den ger upp ("database is locked").
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _is_sqlite_memory(url: str) -> bool:
    return ":memory:" in url or url.rstrip("/") in ("sqlite:", "sqlite+pysqlite:") or "mode=memory" in url


def _build_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"future": True}
    sqlite = url.startswith("sqlite")
    memory = sqlite and _is_sqlite_memory(url)
    if sqlite:
        # check_same_thread=False: uvicorns trådpool och schemaläggartråden delar poolen
        # (varje anslutning används av en tråd i taget). timeout = väntetid på skrivlåset.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
        if memory:
            kwargs["poolclass"] = StaticPool
        else:
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 10
            kwargs["pool_timeout"] = 60
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 10
    engine = create_engine(url, **kwargs)

    if sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
            if not memory:
                # WAL: läsare blockerar inte skrivare (och tvärtom); NORMAL är säkert med WAL
                # och skonsamt mot SD-kort.
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def sqlite_file_path(url: str) -> Path | None:
    """Sökvägen till databasfilen för en filbaserad SQLite-URL, annars None."""
    if not url.startswith("sqlite") or _is_sqlite_memory(url):
        return None
    from sqlalchemy.engine import make_url

    database = make_url(url).database
    return Path(database) if database else None


def secure_sqlite_file(path: Path) -> None:
    """Databasfilen ska bara kunna läsas av appens användare (600). SQLite ger WAL- och
    shm-filerna samma rättigheter som databasfilen."""
    import os

    try:
        if not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        for f in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if f.exists() and f.stat().st_mode & 0o077:
                f.chmod(0o600)
    except OSError:  # t.ex. en fil som ägs av någon annan: SQLite rapporterar det själv
        pass


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        cfg = get_config()
        db_file = sqlite_file_path(cfg.database_url)
        if db_file is not None:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            db_file.parent.mkdir(parents=True, exist_ok=True)
            secure_sqlite_file(db_file)
        _engine = _build_engine(cfg.database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def set_engine(engine: Engine) -> None:
    """Används av tester för att injicera en egen motor."""
    global _engine, _SessionLocal
    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_db() -> Iterator[Session]:
    """FastAPI-beroende: en session per request, rollback vid fel."""
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaktionsscope för bakgrundsjobb och skript."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
