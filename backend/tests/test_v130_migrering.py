"""1.3.0: migreringen 0004 – en databas från 1.2.2/1.2.3 (0003) uppgraderas med ägarens
inställningar, resor och beslut i behåll.

* Nya kolumner: raw_trips.collection_gap, known_locations.forward_*, calendar_events.attendees,
  location_patterns.manual_purpose_mode. Modellerna matchar exakt.
* Varje befintlig användare får merge.by_category = false (dagens sammanslagning tills ägaren
  sparar), calendar.store_body_preview = true, automation.home_office_is_work = true och
  system.last_version. Övriga nycklar rörs inte; saknas sektionen skapas den.
* Ner och upp igen fungerar.
"""

from __future__ import annotations

import json

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select

import app.models as M
from alembic import command
from app import db as dbm
from app.db import Base
from app.models.enums import Category, ReviewStatus
from app.services import settings as settings_svc
from app.services.learning import patterns
from tests.conftest import TripFactory
from tests.test_enprocess_sqlite import alembic_config

NEW_COLUMNS = {
    "raw_trips": {"collection_gap"},
    "known_locations": {"forward_status", "forward_at", "forward_attempts", "forward_next_at"},
    "calendar_events": {"attendees"},
    "location_patterns": {"manual_purpose_mode"},
}


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _settings(engine) -> dict[tuple[str, str], dict]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT user_id, key, value FROM settings").all()
    return {(str(uid), key): (json.loads(value) if isinstance(value, str) else value) for uid, key, value in rows}


def test_upgrade_from_a_0003_database_keeps_everything(tmp_path):
    url = f"sqlite:///{tmp_path}/v122.sqlite3"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    dbm.set_engine(engine)
    try:
        s = dbm.get_sessionmaker()()
        owner = M.User(email="agare@example.com", display_name="Ägare", password_hash="x", is_admin=True)
        bare = M.User(email="annan@example.com", display_name="Annan", password_hash="x")
        s.add_all([owner, bare])
        s.flush()
        vehicle = M.Vehicle(owner_user_id=owner.id, name="Bilen", make="Volvo", model="V90")
        s.add(vehicle)
        s.flush()
        f = TripFactory(s, owner, vehicle)
        work = f.trip("Startgatan 1, Påhittby", "Kundvägen 2, Påhittby", category=Category.WORK,
                      purpose="Kundbesök Exempel Åkeri AB", km=36.0)
        f.skip(hours=2)
        private = f.trip("Kundvägen 2, Påhittby", "Startgatan 1, Påhittby", category=Category.PRIVATE, km=36.0)
        patterns.set_manual_rule(s, owner.id, origin_id=None, dest_id=work.end_location_id,
                                 category=Category.WORK, purpose="Kundbesök Exempel Åkeri AB")
        s.add(M.CalendarEvent(user_id=owner.id, calendar_id="ics", external_id="e1", subject="Möte",
                              start_at=work.started_at, end_at=work.ended_at, body_preview="Agenda"))
        settings_svc.update_section(s, owner.id, "merge", {"auto_max_minutes": 20, "ask_max_minutes": 45})
        settings_svc.update_section(s, owner.id, "calendar", {"store_body_preview": False, "sync_days_back": 30})
        settings_svc.update_section(s, owner.id, "automation", {"mode": "self_driving"})
        s.commit()
        ids = {"owner": str(owner.id).replace("-", ""), "bare": str(bare.id).replace("-", "")}
        work_id, private_id = work.id, private.id
        s.close()

        # Databasen som den såg ut i 1.2.2/1.2.3 (0003): nycklarna och kolumnerna finns inte.
        command.downgrade(cfg, "0003")
        for table, cols in NEW_COLUMNS.items():
            assert not (cols & _columns(engine, table)), table
        before = _settings(engine)
        assert "by_category" not in before[(ids["owner"], "merge")]
        assert before[(ids["owner"], "calendar")]["store_body_preview"] is False
        assert (ids["bare"], "merge") not in before

        command.upgrade(cfg, "head")
        for table, cols in NEW_COLUMNS.items():
            assert cols <= _columns(engine, table), table
        after = _settings(engine)
        assert after[(ids["owner"], "merge")] == {"auto_max_minutes": 20, "ask_max_minutes": 45, "by_category": False}
        assert after[(ids["owner"], "calendar")] == {"store_body_preview": True, "sync_days_back": 30}
        assert after[(ids["owner"], "automation")]["mode"] == "self_driving"
        assert after[(ids["owner"], "automation")]["home_office_is_work"] is True
        assert after[(ids["owner"], "system")] == {"last_version": "0003"}
        assert after[(ids["bare"], "merge")] == {"by_category": False}
        assert after[(ids["bare"], "calendar")] == {"store_body_preview": True}

        s = dbm.get_sessionmaker()()
        cfg_merge = settings_svc.get_section(s, owner.id, "merge")
        assert cfg_merge["by_category"] is False and cfg_merge["auto_max_minutes"] == 20
        trips = {t.id: t for t in s.execute(select(M.LogicalTrip)).scalars()}
        assert (trips[work_id].category, trips[work_id].review_status, trips[work_id].purpose) == (
            Category.WORK, ReviewStatus.APPROVED, "Kundbesök Exempel Åkeri AB")
        assert trips[private_id].category == Category.PRIVATE
        rule = s.execute(select(M.LocationPattern).where(M.LocationPattern.is_manual_rule.is_(True))).scalar_one()
        assert rule.manual_purpose == "Kundbesök Exempel Åkeri AB" and rule.manual_purpose_mode is None
        event = s.execute(select(M.CalendarEvent)).scalar_one()
        assert event.body_preview == "Agenda" and event.attendees is None
        loc = s.get(M.KnownLocation, trips[work_id].end_location_id)
        assert loc.forward_attempts == 0 and loc.forward_status is None
        s.close()

        # Ner och upp igen.
        command.downgrade(cfg, "0003")
        downgraded = _settings(engine)
        assert "by_category" not in downgraded[(ids["owner"], "merge")]
        assert (ids["bare"], "merge") not in downgraded  # raden blev tom och togs bort
        assert (ids["owner"], "system") not in downgraded
        assert "home_office_is_work" not in downgraded[(ids["owner"], "automation")]
        assert downgraded[(ids["owner"], "calendar")]["store_body_preview"] is True  # lämnas (ofarligt)
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        assert diff == []
    finally:
        dbm.dispose_engine()


def test_new_install_has_no_users_to_seed(tmp_path):
    url = f"sqlite:///{tmp_path}/ny.sqlite3"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM settings").scalar() == 0
    engine.dispose()


@pytest.mark.new_defaults
def test_new_user_gets_category_rules(db, user):
    cfg = settings_svc.get_section(db, user.id, "merge")
    assert cfg["by_category"] is True and cfg["category_rules_since"] == ""
