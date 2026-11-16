"""Gemensamma testfixturer.

Testerna körs som standard mot SQLite i minnet. Sätt DRIVELOG_TEST_DATABASE_URL
(t.ex. postgresql+psycopg://…) för att köra samma svit mot PostgreSQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

os.environ.setdefault("DRIVELOG_ENVIRONMENT", "test")
os.environ.setdefault("DRIVELOG_DATABASE_URL", "sqlite://")
os.environ.setdefault("DRIVELOG_SECRET_KEY", "test-secret-key-test-secret-key-0123456789")
os.environ.setdefault("DRIVELOG_BASE_URL", "http://testserver")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models as M  # noqa: E402
from app import db as dbm  # noqa: E402
from app import jobs  # noqa: E402
from app.config import get_config, reset_config_cache  # noqa: E402
from app.db import Base  # noqa: E402
from app.models.enums import (  # noqa: E402
    Category,
    DecisionSource,
    DistanceQuality,
    LocationType,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.security import passwords, ratelimit  # noqa: E402
from app.services import audit  # noqa: E402
from app.services import settings as settings_svc  # noqa: E402
from app.services.calendar import ics_fetch  # noqa: E402
from app.services.places import geocode  # noqa: E402
from app.services.places import registry as places  # noqa: E402
from app.services.trips import segments as seg  # noqa: E402

TEST_PASSWORD = "korrekt-häst-batteri-1"


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "new_defaults: kör testet med 1.1.0:s riktiga standardvärden (inte legacy_defaults)"
    )


# Standardvärden från före 1.1.0, som de befintliga testerna bygger på.
LEGACY_DEFAULTS: dict[tuple[str, str], Any] = {
    ("automation", "mode"): "manual",
    ("privacy", "geocoding_enabled"): False,
    ("notifications", "hide_locations_on_lock_screen"): True,
    ("notifications", "reminder_after_hours"): 20,
    ("notifications", "expire_after_hours"): 72,
}


@pytest.fixture(autouse=True)
def legacy_defaults(request, monkeypatch) -> None:
    """Befintliga tester körs med standardvärdena från före 1.1.0 (manuellt läge, ingen
    geokodning). ``@pytest.mark.new_defaults`` ger de riktiga standardvärdena."""
    if request.node.get_closest_marker("new_defaults"):
        return
    for (section, key), value in LEGACY_DEFAULTS.items():
        monkeypatch.setitem(settings_svc.DEFAULTS[section], key, value)


class OfflineTransport(httpx.BaseTransport):
    """Geokodarens transport i testerna: inget nätverk, varje anrop misslyckas som offline."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ConnectError("Testerna är offline", request=request)


@pytest.fixture(autouse=True)
def offline_geocoder(monkeypatch) -> OfflineTransport:
    transport = OfflineTransport()
    monkeypatch.setattr(geocode, "_default_transport", transport)
    return transport


@pytest.fixture(autouse=True)
def offline_calendar_link(monkeypatch) -> OfflineTransport:
    """Kalenderlänkens transport i testerna: inget nätverk (samma som för geokodaren)."""
    transport = OfflineTransport()
    monkeypatch.setattr(ics_fetch, "_default_transport", transport)
    return transport


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DRIVELOG_DATA_DIR", str(tmp_path / "data"))
    reset_config_cache()
    ratelimit.reset()
    yield
    reset_config_cache()


@pytest.fixture()
def engine() -> Iterator[Any]:
    url = os.environ.get("DRIVELOG_TEST_DATABASE_URL")
    if url:
        eng = create_engine(url, future=True)
        Base.metadata.drop_all(eng)
    else:
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    dbm.set_engine(eng)
    yield eng
    if url:
        Base.metadata.drop_all(eng)
    dbm.dispose_engine()


@pytest.fixture()
def db(engine) -> Iterator[Session]:
    session = dbm.get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def user(db: Session) -> M.User:
    u = M.User(
        email="forare@example.com",
        display_name="Test Förare",
        password_hash=passwords.hash_password(TEST_PASSWORD),
        is_admin=True,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def vehicle(db: Session, user: M.User) -> M.Vehicle:
    v = M.Vehicle(
        owner_user_id=user.id, name="Testbil", make="Volvo", model="V90", model_year=2021,
        registration_number="ABC123", is_default=True,
    )
    db.add(v)
    db.flush()
    return v


@pytest.fixture()
def actor(user: M.User) -> audit.Actor:
    return audit.user_actor(user.id)


@pytest.fixture()
def complete_scope(db: Session, user: M.User) -> None:
    """Kör testet som fullständig körjournal (t.ex. förmånsbil).

    Standardomfånget är ``business_only`` (endast tjänsteresor redovisas, luckor är privat
    körning). Tester som prövar avstämning mot mätarställningen begär den här fixturen.
    I API-tester ska den stå före ``client`` så att inställningen sparas med användaren.
    """
    settings_svc.update_section(db, user.id, "general", {"journal_scope": settings_svc.SCOPE_COMPLETE})
    db.flush()


@pytest.fixture()
def job_queue() -> Iterator[list]:
    """Fångar köade bakgrundsjobb i stället för att köra dem."""
    queue = jobs.capture_for_tests(True)
    yield queue
    jobs.capture_for_tests(False)


@pytest.fixture()
def client(engine, db: Session, user: M.User, vehicle: M.Vehicle):
    """Inloggad TestClient som automatiskt skickar CSRF-token."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    db.commit()
    app = create_app()
    with TestClient(app) as c:
        resp = c.post("/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text
        c.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
        yield c


@pytest.fixture()
def anon_client(engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ----------------------------------------------------------------------------- fabriker

BASE_TIME = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)  # en måndag


class TripFactory:
    """Bygger resor direkt i databasen för regeltester."""

    def __init__(self, db: Session, user: M.User, vehicle: M.Vehicle):
        self.db, self.user, self.vehicle = db, user, vehicle
        self._odo = 10000.0
        self._clock = BASE_TIME

    def location(
        self, address: str, *, name: str | None = None, type: str = LocationType.UNKNOWN, confirmed: bool = True,
        company: str | None = None, lat: float | None = None, lon: float | None = None,
    ) -> M.KnownLocation:
        loc = places.resolve_address(self.db, self.user.id, address, created_from="user")
        assert loc is not None
        loc.name = name or address
        loc.type = type
        loc.confirmed = confirmed
        loc.company = company
        loc.latitude, loc.longitude = lat, lon
        self.db.flush()
        return loc

    def raw(
        self,
        start: str = "A-gatan 1, Stad",
        end: str = "B-gatan 2, Stad",
        *,
        km: float = 10.0,
        minutes: int = 20,
        gap_minutes: float | None = None,
        start_at: datetime | None = None,
        odometer_start: float | None = None,
        source: str = TripSource.VOLVO_IMPORT,
        notes: str | None = None,
        start_known: bool = True,
        **extra: Any,
    ) -> M.RawTrip:
        if start_at is None:
            start_at = self._clock + timedelta(minutes=gap_minutes or 0) if gap_minutes is not None else self._clock
        end_at = start_at + timedelta(minutes=minutes)
        odo_s = self._odo if odometer_start is None else odometer_start
        odo_e = odo_s + round(km)
        s_loc = places.resolve_address(self.db, self.user.id, start, created_from="import")
        e_loc = places.resolve_address(self.db, self.user.id, end, created_from="import")
        raw = M.RawTrip(
            vehicle_id=self.vehicle.id,
            source=source,
            source_key=uuid.uuid4().hex,
            started_at=start_at if start_known else None,
            ended_at=end_at,
            start_time_quality=TimeQuality.EXACT if start_known else TimeQuality.BOUNDED,
            end_time_quality=TimeQuality.EXACT,
            odometer_start=odo_s,
            odometer_end=odo_e,
            distance_km=km,
            distance_quality=DistanceQuality.MEASURED,
            duration_seconds=minutes * 60,
            start_address=start,
            end_address=end,
            start_location_id=s_loc.id if s_loc else None,
            end_location_id=e_loc.id if e_loc else None,
            fuel_liters=round(km * 0.07, 2),
            fuel_quality=ValueQuality.VERIFIED,
            energy_kwh=round(km * 0.05, 2),
            energy_quality=ValueQuality.VERIFIED,
            original_notes=notes,
            **extra,
        )
        seg.set_raw_sort_time(raw)
        self.db.add(raw)
        self.db.flush()
        self._odo = odo_e
        self._clock = end_at
        return raw

    def trip(
        self,
        start: str = "A-gatan 1, Stad",
        end: str = "B-gatan 2, Stad",
        *,
        category: str = Category.UNDECIDED,
        review_status: str | None = None,
        purpose: str | None = None,
        purpose_confirmed: bool | None = None,
        category_source: str = DecisionSource.SYSTEM,
        **raw_kwargs: Any,
    ) -> M.LogicalTrip:
        raw = self.raw(start, end, notes=purpose, **raw_kwargs)
        if review_status is None:
            review_status = ReviewStatus.NEEDS_REVIEW if category == Category.UNDECIDED else ReviewStatus.APPROVED
        trip = seg.create_logical_for_raw(
            self.db,
            raw,
            category=category,
            category_source=category_source,
            purpose=purpose,
            purpose_source=category_source if purpose else None,
            purpose_confirmed=bool(purpose) if purpose_confirmed is None else purpose_confirmed,
            review_status=review_status,
        )
        return trip

    def skip(self, **delta: Any) -> None:
        """Flyttar fram klockan (och ev. mätarställningen) inför nästa resa."""
        km = delta.pop("km", 0)
        self._clock += timedelta(**delta)
        self._odo += km


@pytest.fixture()
def trips(db: Session, user: M.User, vehicle: M.Vehicle) -> TripFactory:
    return TripFactory(db, user, vehicle)


def reload_trip(db: Session, trip_id: uuid.UUID) -> M.LogicalTrip:
    db.expire_all()
    trip = seg.load_trip(db, trip_id)
    assert trip is not None
    return trip


__all__ = ["BASE_TIME", "TEST_PASSWORD", "TripFactory", "get_config", "reload_trip"]
