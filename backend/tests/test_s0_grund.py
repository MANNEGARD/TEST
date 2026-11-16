"""Grunden för 1.1.0 (självgående körjournal): migreringen, inställningarna, texttvätten,
stubbarna, API-schemana och ordningen i tick."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select

import app.models as M
from alembic import command
from app.db import Base
from app.models.enums import (
    AUTO_APPROVAL_SOURCES,
    ApprovalSource,
    Category,
    NotificationKind,
    TimeQuality,
    TripSource,
)
from app.services import privacy
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.export import links, month_close
from app.services.ha import alerts, notices, notifier
from app.services.places import geocode
from app.services.text_safety import MAX_TEXT, sanitize_text
from app.services.trips import start_time
from tests.conftest import TEST_PASSWORD
from tests.test_enprocess_sqlite import alembic_config

API = "/api/v1"

NEW_COLUMNS = {
    "users": {"link_epoch"},
    "vehicle_observations": {"avg_speed_auto", "statistics_reported_at"},
    "raw_trips": {"start_estimate_basis"},
    "logical_trips": {"start_time_override", "approval_source", "approval_basis", "auto_approved_at"},
    "known_locations": {"geocode_status", "geocoded_at", "osm_class", "place_kind", "assumption"},
    "export_jobs": {"origin", "superseded_by_id", "rows_fingerprint", "link_downloads"},
}
NEW_TABLES = {"geocode_cache", "month_closes"}


# ----------------------------------------------------------------------------- migreringen


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migration_0002_up_down_up_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path}/mig.sqlite3"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    with engine.begin() as conn:  # en rad från före uppgraderingen får standardvärden
        conn.exec_driver_sql(
            "INSERT INTO users (id, email, display_name, password_hash, is_active, is_admin, created_at) "
            "VALUES ('00000000000000000000000000000001', 'a@example.com', '', 'x', 1, 0, '2026-01-01 00:00:00')"
        )
    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert NEW_TABLES <= set(insp.get_table_names())
    for table, cols in NEW_COLUMNS.items():
        assert cols <= _columns(engine, table), table
    uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("geocode_cache")}
    assert uniques["uq_geocode_cell"] == ["owner_user_id", "provider_host", "lat_key", "lon_key"]
    uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("month_closes")}
    assert uniques["uq_month_close_vehicle_month"] == ["vehicle_id", "month"]
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT link_epoch FROM users").scalar() == 0
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []

    command.downgrade(cfg, "0001")
    insp = inspect(engine)
    assert not (NEW_TABLES & set(insp.get_table_names()))
    for table, cols in NEW_COLUMNS.items():
        assert not (cols & _columns(engine, table)), table
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT email FROM users").scalar() == "a@example.com"

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []
    engine.dispose()


def test_migration_file_names_revision():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0002_sjalvgaende.py"
    text = path.read_text(encoding="utf-8")
    assert "revision: str = '0002'" in text and "down_revision: str | None = '0001'" in text


def test_enums():
    assert {NotificationKind.ASSUMED, NotificationKind.COMPANY, NotificationKind.ALERT, NotificationKind.MONTHLY} == {
        "assumed", "company", "alert", "monthly"}
    assert {s.value for s in ApprovalSource} == {
        "user", "bulk", "rule", "history", "return_trip", "commute", "private_place", "assumed"}
    assert AUTO_APPROVAL_SOURCES == {"rule", "history", "return_trip", "commute", "private_place", "assumed"}


# ----------------------------------------------------------------------------- inställningar


@pytest.mark.new_defaults
def test_new_defaults():
    d = settings_svc.DEFAULTS
    assert d["automation"] == {
        "mode": "self_driving", "active_since": "",
        "office_hours": {"days": [0, 1, 2, 3, 4], "start": "07:00", "end": "18:00"},
        "assumption_veto_hours": 24,
    }
    assert d["privacy"]["geocoding_enabled"] is True
    assert d["privacy"]["geocoder_url"] == "https://nominatim.openstreetmap.org"
    assert d["notifications"]["hide_locations_on_lock_screen"] is False
    assert d["notifications"]["reminder_after_hours"] == 24
    assert d["notifications"]["expire_after_hours"] == 48
    assert start_time.K1_ENABLED is False


def test_legacy_defaults_for_existing_tests(db, user):
    assert settings_svc.automation_mode(db, user.id) == "manual"
    assert settings_svc.get_section(db, user.id, "privacy")["geocoding_enabled"] is False
    n = settings_svc.get_section(db, user.id, "notifications")
    assert (n["hide_locations_on_lock_screen"], n["reminder_after_hours"], n["expire_after_hours"]) == (True, 20, 72)


@pytest.mark.new_defaults
def test_saved_settings_are_kept_on_upgrade(db, user):
    # En inställning som ägaren redan satt rörs inte av de nya standardvärdena.
    db.add(M.Setting(user_id=user.id, key="notifications", value={"hide_locations_on_lock_screen": True}))
    db.flush()
    n = settings_svc.get_section(db, user.id, "notifications")
    assert n["hide_locations_on_lock_screen"] is True and n["expire_after_hours"] == 48


@pytest.mark.parametrize("values", [
    {"mode": "auto"},
    {"mode": ""},
    {"assumption_veto_hours": -1},
    {"assumption_veto_hours": 169},
    {"assumption_veto_hours": 1.5},
    {"assumption_veto_hours": True},
    {"office_hours": {"days": [0, 1], "start": "18:00", "end": "07:00"}},
    {"office_hours": {"days": [0, 1], "start": "07:00", "end": "07:00"}},
    {"office_hours": {"days": [7], "start": "07:00", "end": "18:00"}},
    {"office_hours": {"days": [], "start": "07:00", "end": "18:00"}},
    {"office_hours": {"days": [1, 1], "start": "07:00", "end": "18:00"}},
    {"office_hours": {"days": [1], "start": "7", "end": "18:00"}},
    {"office_hours": {"days": [1], "start": "07:00"}},
    {"office_hours": "mån–fre"},
    {"active_since": "igår"},
])
def test_automation_bounds(db, user, values):
    with pytest.raises(settings_svc.SettingsError):
        settings_svc.update_section(db, user.id, "automation", values, allow_read_only=True)


def test_automation_valid_values(db, user):
    out = settings_svc.update_section(db, user.id, "automation", {
        "mode": "self_driving", "assumption_veto_hours": 0,
        "office_hours": {"days": [0, 1, 2, 3, 4, 5], "start": "06:30", "end": "19:00"},
    })
    assert out["mode"] == "self_driving" and out["assumption_veto_hours"] == 0
    assert out["office_hours"]["days"] == [0, 1, 2, 3, 4, 5]
    assert settings_svc.update_section(db, user.id, "automation", {"assumption_veto_hours": 168})[
        "assumption_veto_hours"] == 168


@pytest.mark.parametrize("url", [
    "http://nominatim.example.org",
    "ftp://nominatim.example.org",
    "https://user:pw@nominatim.example.org",
    "https://nominatim.example.org/?key=1",
    "https://nominatim.example.org/#x",
    "https://nominatim.example.org:8443",
    "https://localhost",
    "https://geo.localhost",
    "https://127.0.0.1",
    "https://10.1.2.3",
    "https://100.64.0.1",
    "https://169.254.169.254",
    "https://0.0.0.0",
    "https://[::1]",
    "https://[::ffff:127.0.0.1]",
    "https://192.0.2.10",  # dokumentationsnät, inte globalt
    "https://" + "a" * 500 + ".example.org",
    "nominatim",
])
def test_geocoder_url_rejected(db, user, url):
    with pytest.raises(settings_svc.SettingsError):
        settings_svc.update_section(db, user.id, "privacy", {"geocoder_url": url})
    with pytest.raises(ValueError):
        geocode.validate_runtime_url(url)


def test_geocoder_url_accepted(db, user):
    out = settings_svc.update_section(db, user.id, "privacy", {"geocoder_url": "https://geo.example.org/nominatim/"})
    assert out["geocoder_url"] == "https://geo.example.org/nominatim/"
    assert geocode.validate_runtime_url("https://geo.example.org/nominatim/") == "https://geo.example.org/nominatim"
    assert geocode.validate_runtime_url("https://nominatim.openstreetmap.org:443") == "https://nominatim.openstreetmap.org:443"
    # Tom adress = ingen geokodning (som förut).
    assert settings_svc.update_section(db, user.id, "privacy", {"geocoder_url": ""})["geocoder_url"] == ""


def test_active_since_is_read_only(db, user):
    with pytest.raises(settings_svc.SettingsError, match="kan inte ändras"):
        settings_svc.update_section(db, user.id, "automation", {"active_since": "2026-01-01T00:00:00+00:00"})


def test_active_since_read_only_via_api(client):
    r = client.patch(f"{API}/settings/automation", json={"active_since": "2020-01-01T00:00:00+00:00"})
    assert r.status_code == 400 and "kan inte ändras" in r.json()["detail"]
    r = client.patch(f"{API}/settings/automation", json={"mode": "self_driving", "active_since": ""})
    assert r.status_code == 400


def test_switching_to_self_driving_sets_active_since(client, db, user):
    before = datetime.now(UTC) - timedelta(seconds=2)
    r = client.patch(f"{API}/settings/automation", json={"mode": "self_driving"})
    assert r.status_code == 200, r.text
    since = datetime.fromisoformat(r.json()["active_since"])
    assert since >= before
    # Läget sparat igen ändrar inte tidpunkten.
    r = client.patch(f"{API}/settings/automation", json={"mode": "self_driving", "assumption_veto_hours": 72})
    assert r.json()["active_since"] == since.isoformat(timespec="seconds")
    # Tillbaka till manuellt och sedan självgående igen: en ny tidpunkt, så att resorna
    # från den manuella tiden aldrig rörs av automatiken.
    assert client.patch(f"{API}/settings/automation", json={"mode": "manual"}).status_code == 200
    db.expire_all()
    with_new = settings_svc.get_section(db, user.id, "automation")
    assert with_new["active_since"] == since.isoformat(timespec="seconds")
    settings_svc.update_section(db, user.id, "automation",
                                {"active_since": "2020-01-01T00:00:00+00:00"}, allow_read_only=True)
    db.commit()
    r = client.patch(f"{API}/settings/automation", json={"mode": "self_driving"})
    assert datetime.fromisoformat(r.json()["active_since"]) >= before
    settings = client.get(f"{API}/settings").json()
    assert "automation" in settings["settings"] and "automation" in settings["defaults"]


@pytest.mark.new_defaults
def test_ensure_active_since(db, user):
    now = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    assert autopilot.is_active(db, user.id) is False
    autopilot.ensure_active_since(db, now=now)
    assert settings_svc.active_since(db, user.id) == now
    assert autopilot.is_active(db, user.id) is True
    # En redan satt tidpunkt ändras aldrig.
    autopilot.ensure_active_since(db, user.id, now=now + timedelta(days=3))
    assert settings_svc.active_since(db, user.id) == now
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "settings.automation_active_since")).scalars().all()


def test_ensure_active_since_not_in_manual_mode(db, user):
    autopilot.ensure_active_since(db, user.id, now=datetime(2026, 11, 1, tzinfo=UTC))
    assert settings_svc.active_since(db, user.id) is None
    assert autopilot.is_active(db, user.id) is False


# ----------------------------------------------------------------------------- texttvätt


def test_sanitize_text_removes_bidi_and_zero_width():
    assert sanitize_text("Kund‮besök​ Acme⁦ AB⁩") == "Kundbesök Acme AB"
    assert sanitize_text("﻿Stor‍gatan­ 5") == "Storgatan 5"
    assert sanitize_text("rad1 rad2 rad3") == "rad1rad2rad3"
    assert sanitize_text("  flera \t\n  blanksteg\r\n ") == "flera blanksteg"
    assert sanitize_text("noll\x00tecken\x1b[31m") == "nolltecken[31m"


def test_sanitize_text_nfc_limit_and_empty():
    decomposed = "Jönköping"
    assert sanitize_text(decomposed) == "Jönköping" and len(sanitize_text(decomposed)) == 9
    assert sanitize_text("x" * 500) == "x" * MAX_TEXT
    assert sanitize_text("abc def", limit=4) == "abc"
    assert sanitize_text(None) is None
    assert sanitize_text("") is None
    assert sanitize_text(" ​‮\n ") is None


# ----------------------------------------------------------------------------- stubbarna


def test_stubs_are_neutral(db, user, vehicle, trips, actor):
    trip = trips.trip(source=TripSource.VOLVO_API, start_known=False)
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    d = autopilot.decide(db, trip, user_id=user.id, ctx=None, rules=None, cfg=None)
    assert d == autopilot.AutoDecision("skip") and d.outcome == "skip" and d.candidates == ()
    assert autopilot.apply(db, trip, d, actor=actor) is False
    # S1c: eligible är byggd – live-resan omfattas när den skapats efter active_since.
    assert autopilot.eligible(trip, active_since=None) is False
    assert autopilot.run_vehicle(db, user_id=user.id, vehicle=vehicle, actor=actor) == {}
    assert autopilot.reconsider(db, user_id=user.id, trip_ids=[trip.id], actor=actor) is None
    assert autopilot.purpose_candidates(db, trip, user_id=user.id) == []
    assert autopilot.can_remember(db, trip, "Kundbesök Acme") is False
    with pytest.raises(ValueError):
        autopilot.remember_always(db, user_id=user.id, trip=trip, category=Category.WORK, purpose="x", actor=actor)
    assert autopilot.on_owner_decision(db, trip, before={}, actor=actor) is None
    # S1d: antagandet är byggt – en resa som inte är antagen kan inte bekräftas, och ett
    # tomt företagsnamn avvisas innan något ändras.
    with pytest.raises(ValueError):
        autopilot.confirm_assumption(db, trip, actor=actor)
    with pytest.raises(ValueError):
        autopilot.set_company(db, trip, "  ", actor=actor)
    assert autopilot.classify_place("building", "warehouse") == "work"

    est = start_time.estimate(earliest=now - timedelta(hours=2), latest=now - timedelta(minutes=30),
                              start_odometer=100.0, ended_at=now, distance_km=20.0, steps=[],
                              end=start_time.ObsView(now, 120.0, now, False, now, None, None, None))
    assert (est.at, est.method, est.margin_min) == (None, "none", None)
    assert est.earliest == now - timedelta(hours=2) and est.latest == now - timedelta(minutes=30)

    assert geocode.place_for_position(db, user_id=user.id, lat=57.78, lon=14.16, demo=False) is None
    assert db.execute(select(M.KnownLocation).where(M.KnownLocation.created_from == "api")).first() is None
    assert geocode.run_pending(now) == {}
    # S1a: reverse är byggd; med testernas offlinetransport blir anropet ett GeocodeError.
    with pytest.raises(geocode.GeocodeError):
        geocode.reverse(provider_url="https://geo.example.org", lat_key=577800, lon_key=141600)
    assert "SMGD-DriveLog/" in geocode.USER_AGENT and "@" not in geocode.USER_AGENT

    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1), now=now)
    assert (status.month, status.state, status.blockers, status.notes, status.export_id) == ("2026-10", "open", [], [], None)
    assert month_close.run_due(db, now) == {}
    assert month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1), now=now).state == "open"
    assert db.execute(select(M.ExportJob)).first() is None and db.execute(select(M.MonthClose)).first() is None

    job = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, status="done", sha256="0" * 64)
    db.add(job)
    db.flush()
    assert links.make_token(job, user, now) is None and links.make_url(job, user, now) is None
    with pytest.raises(links.LinkInvalid):
        links.verify(db, "x.1.y", now)

    assert notices.queue_assumed(db, user_id=user.id, trip=trip, now=now) is None
    assert notices.queue_month_waiting(db, user_id=user.id, status=status, now=now) is None
    assert notices.queue_month_ready(db, user_id=user.id, status=status, export=job, now=now) is None
    assert notices.queue_alert(db, user_id=user.id, code="vehicle_silent", episode="2026-11-01",
                               title="t", message="m", now=now) is None
    assert notices.resolve_alert(db, user_id=user.id, code="vehicle_silent") == 0
    assert alerts.run(db, now) == {}
    assert notifier.ensure_open_questions(db, now) == {}
    assert db.execute(select(M.NotificationRequest)).first() is None


def test_revoke_all_bumps_link_epoch(db, user):
    assert user.link_epoch == 0
    links.revoke_all(db, user)
    links.revoke_all(db, user)
    assert user.link_epoch == 2


def test_offline_geocoder_transport(offline_geocoder):
    import httpx

    assert geocode.default_transport() is offline_geocoder
    with httpx.Client(transport=geocode.default_transport()) as c, pytest.raises(httpx.ConnectError):
        c.get("https://nominatim.openstreetmap.org/reverse")
    assert len(offline_geocoder.requests) == 1


# ----------------------------------------------------------------------------- API


def test_trip_out_new_fields_and_start_time_override(client, db, trips):
    trip = trips.trip(source=TripSource.VOLVO_API, start_known=False)
    db.commit()
    out = client.get(f"{API}/trips/{trip.id}").json()["trip"]
    assert out["start_time_source"] == "unknown" and out["start_time_text"] == ""
    assert out["start_estimate_basis"] is None
    assert out["approval_source"] is None and out["approval_reason"] is None

    end = datetime.fromisoformat(out["ended_at"])
    too_early = (end - timedelta(hours=24, minutes=1)).isoformat()
    r = client.patch(f"{API}/trips/{trip.id}", json={"start_time": too_early})
    assert r.status_code == 400 and r.json()["code"] == "start_time_out_of_range"
    r = client.patch(f"{API}/trips/{trip.id}", json={"start_time": (end + timedelta(minutes=1)).isoformat()})
    assert r.status_code == 400
    r = client.patch(f"{API}/trips/{trip.id}", json={"start_time": "2026-09-14T06:00:00"})
    assert r.status_code == 400  # utan tidszon

    ok = (end - timedelta(minutes=25)).isoformat()
    r = client.patch(f"{API}/trips/{trip.id}", json={"start_time": ok})
    assert r.status_code == 200, r.text
    assert r.json()["start_time_source"] == "user"
    db.expire_all()
    saved = db.get(M.LogicalTrip, trip.id)
    assert saved.start_time_override == end - timedelta(minutes=25)
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.start_time_override")).first()
    r = client.patch(f"{API}/trips/{trip.id}", json={"start_time": None})
    assert r.status_code == 200 and r.json()["start_time_source"] == "unknown"


def test_trip_out_sources_text_and_approval(client, db, trips):
    exact = trips.trip(category=Category.WORK, purpose="Kundbesök Acme")
    estimated = trips.trip(source=TripSource.VOLVO_API)
    raw = estimated.segments[0].raw_trip
    raw.start_time_quality = TimeQuality.ESTIMATED
    raw.start_estimate_basis = {"method": "window", "margin_min": 4.0}
    estimated.start_time_quality = TimeQuality.ESTIMATED
    estimated.approval_source = ApprovalSource.ASSUMED
    estimated.approval_basis = {"reason": "Antaget kundbesök", "veto_until": "2026-09-15T08:00:00+00:00"}
    db.commit()
    items = {i["id"]: i for i in client.get(f"{API}/trips").json()["items"]}
    e = items[str(exact.id)]
    assert e["start_time_source"] == "car" and e["start_time_text"] == "08:00"  # 06:00 UTC = 08:00 i Stockholm
    s = items[str(estimated.id)]
    assert s["start_time_source"] == "estimated" and s["start_time_text"].startswith("≈ ")
    assert s["start_estimate_basis"] == {"method": "window", "margin_min": 4.0}
    assert s["approval_source"] == "assumed" and s["approval_reason"] == "Antaget kundbesök"


def test_export_out_new_fields(client, db, user, vehicle):
    job = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, status="done", filename="x.xlsx")
    db.add(job)
    db.commit()
    out = next(e for e in client.get(f"{API}/exports").json() if e["id"] == str(job.id))
    assert out["origin"] == "manual" and out["superseded_by_id"] is None and out["link_downloads"] == 0


def test_month_close_routes(client, db, user, vehicle):
    r = client.get(f"{API}/month-close")
    assert r.status_code == 200, r.text
    months = r.json()["months"]
    assert len(months) == 2 and all(m["vehicle_id"] == str(vehicle.id) for m in months)
    assert months[0]["month"] > months[1]["month"] and months[0]["review_rows"] == []
    assert client.get(f"{API}/month-close", params={"vehicle_id": str(uuid.uuid4())}).status_code == 404

    assert client.post(f"{API}/month-close/2026-13/run").status_code == 400
    assert client.post(f"{API}/month-close/26-10/run").status_code == 400
    assert client.post(f"{API}/month-close/2999-01/run").status_code == 400
    r = client.post(f"{API}/month-close/2026-10/run", params={"force": "true"})
    assert r.status_code == 200 and r.json()["month"] == "2026-10"
    # CSRF krävs.
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(f"{API}/month-close/2026-10/run").status_code == 403
    assert client.post(f"{API}/month-close/links/revoke").status_code == 403
    client.headers["X-CSRF-Token"] = token

    assert client.post(f"{API}/month-close/links/revoke").status_code == 204
    db.expire_all()
    assert db.get(M.User, user.id).link_epoch == 1


def test_month_close_review_rows(client, db, trips):
    from app.services.stats import common

    trip = trips.trip(category=Category.WORK, purpose="Kundbesök Acme", start_at=common.utcnow() - timedelta(minutes=90))
    trip.approval_source = ApprovalSource.RULE
    trip.approval_basis = {"reason": "Din regel för Acme"}
    trips.trip(category=Category.WORK, purpose="Ägarens eget beslut", start_at=common.utcnow() - timedelta(minutes=60))
    db.commit()
    rows = [r for m in client.get(f"{API}/month-close").json()["months"] for r in m["review_rows"]]
    assert len(rows) == 1
    assert rows[0]["trip_id"] == str(trip.id) and rows[0]["approval_source"] == "rule"
    assert rows[0]["reason"] == "Din regel för Acme" and "→" in rows[0]["route"]


def test_public_link_route_without_login(anon_client):
    r = anon_client.get(f"{API}/public/exports/{'a' * 32}.1700000000.sig")
    assert r.status_code == 404
    assert anon_client.get(f"{API}/public/exports/{'x' * 400}").status_code == 404


def test_public_link_route_serves_file_and_counts(anon_client, db, user, vehicle, monkeypatch):
    from app.config import get_config

    exports_dir = get_config().exports_dir
    exports_dir.mkdir(parents=True, exist_ok=True)
    path = exports_dir / "underlag.xlsx"
    path.write_bytes(b"PK-test")
    job = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, status="done", filename="Körjournal 2026-10.xlsx",
                      file_path=str(path), sha256="0" * 64)
    db.add(job)
    db.commit()
    token = f"{job.id.hex}.1900000000.signatur"
    monkeypatch.setattr(links, "verify", lambda db_, tok, now: db_.get(M.ExportJob, job.id) if tok == token
                        else (_ for _ in ()).throw(links.LinkExpired()))
    r = anon_client.get(f"{API}/public/exports/{token}")
    assert r.status_code == 200 and r.content == b"PK-test"
    assert r.headers["cache-control"] == "no-store" and r.headers["x-robots-tag"] == "noindex"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert anon_client.get(f"{API}/public/exports/annan.1.x").status_code == 410
    db.expire_all()
    assert db.get(M.ExportJob, job.id).link_downloads == 1
    ev = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "export.link_download")).scalar_one()
    assert token not in str(ev.after) and ev.user_id == user.id
    path.unlink()
    assert anon_client.get(f"{API}/public/exports/{token}").status_code == 404  # filen finns inte längre


def test_public_link_rate_limit(anon_client):
    codes = [anon_client.get(f"{API}/public/exports/x.1.y").status_code for _ in range(31)]
    assert codes[:30] == [404] * 30 and codes[30] == 429


def test_password_change_bumps_link_epoch(client, db, user):
    r = client.post(f"{API}/auth/change-password",
                    json={"current_password": TEST_PASSWORD, "new_password": "ett-helt-annat-lösenord-7"})
    assert r.status_code == 204, r.text
    db.expire_all()
    assert db.get(M.User, user.id).link_epoch == 1


def test_openapi_has_new_schemas(anon_client):
    spec = anon_client.get("/api/v1/openapi.json")
    if spec.status_code != 200:
        spec = anon_client.get("/openapi.json")
    schemas = spec.json()["components"]["schemas"]
    assert "always" in schemas["RespondIn"]["properties"]
    assert {"origin", "superseded_by_id", "link_downloads"} <= set(schemas["ExportOut"]["properties"])
    assert {"start_time_source", "start_time_text", "start_estimate_basis", "approval_source", "approval_reason"} <= set(
        schemas["TripOut"]["properties"])
    assert "start_time" in schemas["TripUpdate"]["properties"]
    assert "MonthCloseOut" in schemas and "MonthStatusOut" in schemas


# ----------------------------------------------------------------------------- tick och sekretess


def test_tick_order(monkeypatch, engine):
    from app.services.trips import review
    from app.services.volvo import collector
    from app.services.volvo import demo as volvo_demo
    from app.worker import tasks

    calls: list[str] = []

    def rec(name):
        def fn(*args, **kwargs):
            calls.append(name)
            return {}
        return fn

    monkeypatch.setattr(autopilot, "ensure_active_since", rec("ensure_active_since"))
    monkeypatch.setattr(collector, "poll_due", rec("poll_due"))
    monkeypatch.setattr(volvo_demo, "tick", rec("demo.tick"))
    monkeypatch.setattr(review, "wake_snoozed", rec("wake_snoozed"))
    monkeypatch.setattr(geocode, "run_pending", rec("geocode.run_pending"))
    monkeypatch.setattr(notifier, "ensure_open_questions", rec("ensure_open_questions"))
    monkeypatch.setattr(notifier, "deliver_due", rec("deliver_due"))
    monkeypatch.setattr(notifier, "run_scheduled", rec("run_scheduled"))
    monkeypatch.setattr(alerts, "run", rec("alerts.run"))
    monkeypatch.setattr(month_close, "run_due", rec("month_close.run_due"))
    tasks.tick()
    assert calls == ["ensure_active_since", "poll_due", "demo.tick", "wake_snoozed", "geocode.run_pending",
                     "ensure_open_questions", "deliver_due", "run_scheduled", "alerts.run", "month_close.run_due"]


def test_tick_step_failure_does_not_stop_others(monkeypatch, engine):
    from app.worker import tasks

    def boom(*_a, **_k):
        raise RuntimeError("fel")

    monkeypatch.setattr(autopilot, "ensure_active_since", boom)
    seen = []
    monkeypatch.setattr(month_close, "run_due", lambda db: seen.append(True) or {})
    out = tasks.tick()
    assert out["active_since"] == {"error": True} and seen == [True]


def test_privacy_deletes_geocode_cache_and_month_closes(db, user, vehicle, actor):
    db.add(M.GeocodeCache(owner_user_id=user.id, provider_host="nominatim.openstreetmap.org",
                          lat_key=577800, lon_key=141600, status="ok", result={"label": "Exempelgatan 1"}))
    db.add(M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-10", state="waiting"))
    db.flush()
    removed = privacy.delete_history(db, user_id=user.id, scope="trips", vehicle=vehicle, actor=actor)
    assert removed["geocode_cache"] == 1 and removed["month_closes"] == 1
    db.add(M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-11", state="exported"))
    db.flush()
    removed = privacy.delete_history(db, user_id=user.id, scope="exports", vehicle=None, actor=actor)
    assert removed["month_closes"] == 1
    assert db.execute(select(M.GeocodeCache)).first() is None and db.execute(select(M.MonthClose)).first() is None
