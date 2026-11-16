"""Regressionstester för slutgranskningens fynd i backend (REG-2, B1/REG-3, D2, B2, A4, B4, D4)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.logging_setup import RedactingFilter, install_redaction, redact
from app.models.enums import Category, LocationType, ReviewKind, ReviewState, ReviewStatus, TimeQuality
from app.services import settings as settings_svc
from app.services.calendar import matching
from app.services.classify import purpose
from app.services.importer import service as imp
from app.services.learning import patterns
from app.services.trips import merge
from app.services.trips import segments as seg
from tests.test_fix_data import DATE_ONLY_CSV
from tests.test_fix_klassificering import (
    CUSTOMER,
    HOME,
    OFFICE,
    UNKNOWN,
    FakeProvider,
    ai_answer,
    classify,
    context,
    event,
    setup_places,
)

A, B, C = "A-gatan 1, Stad", "B-gatan 2, Stad", "C-gatan 3, Stad"


def _run_merge_rules(db, user, vehicle, actor):
    cfg = settings_svc.get_section(db, user.id, "merge")
    return merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor)


def _open_merge_items(db):
    return db.execute(
        select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.MERGE, M.ReviewItem.status == ReviewState.OPEN)
    ).scalars().all()


# ----------------------------------------------------------------------------- REG-2


def test_reg2_date_only_import_asks_no_merge_question(db, user, vehicle, actor):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("egen.csv", DATE_ONLY_CSV.encode())],
                               actor=actor)
    imp.commit(db, batch, actor=actor)
    _run_merge_rules(db, user, vehicle, actor)
    assert _open_merge_items(db) == []
    assert len(seg.active_trips(db, vehicle.id)) == 2
    # Manuell sammanslagning går fortfarande.
    merged = merge.merge_trips(db, seg.active_trips(db, vehicle.id), trigger="manual", actor=actor)
    assert len(seg.active_trips(db, vehicle.id)) == 1 and merged.distance_km == 40


def test_reg2_day_level_start_bounds_after_exact_end_ask_nothing(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=10, start_known=False)
    day = t1.ended_at.replace(hour=0, minute=0)
    raw2.start_earliest, raw2.start_latest = day - timedelta(hours=2), day + timedelta(hours=22)
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    result = _run_merge_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0 and _open_merge_items(db) == []


def test_reg2_bounded_previous_end_gives_unknown_gap(trips):
    r1 = trips.raw(A, B, km=10)
    r2 = trips.raw(B, C, km=10, gap_minutes=5)
    r1.end_time_quality = TimeQuality.BOUNDED
    assert not merge.stop_gap(r1, r2).known


def test_reg2_narrow_volvo_interval_still_asks(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=30, start_known=False)
    raw2.start_earliest, raw2.start_latest = t1.ended_at + timedelta(minutes=20), t1.ended_at + timedelta(minutes=45)
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    _run_merge_rules(db, user, vehicle, actor)
    (q,) = _open_merge_items(db)
    assert "mellan 20 och 45 minuter" in q.title


# ----------------------------------------------------------------------------- B1 + REG-3


OREBRO_OFFICE = "Kontorsvägen 1, Örebro"


@pytest.mark.parametrize(
    "text",
    [
        "Intervju hos kund i Storstad",
        "Rekrytering och kundbesök i Storstad",
        "Styrelsemöte på Storstadskontoret",
        "Utbildning hos Exempelbolaget",
        "Personalmöte på Storstadskontoret",
    ],
)
def test_b1_invented_errand_types_are_rejected(db, user, trips, text):
    setup_places(trips)
    t = trips.trip(OFFICE, CUSTOMER)
    ok, missing = purpose.validate_generated(text, context(db, user, t))
    assert not ok and missing


@pytest.mark.parametrize(
    "text",
    ["Styrelsemöte på Örebrokontoret", "Intervju hos kund i Örebro", "Rekrytering och kundbesök i Örebro"],
)
def test_b1_reviewer_examples_are_rejected(db, user, trips, text):
    setup_places(trips)
    trips.location(OREBRO_OFFICE, name="Örebrokontoret", type=LocationType.OFFICE)
    t = trips.trip(HOME, OREBRO_OFFICE)
    ok, missing = purpose.validate_generated(text, context(db, user, t))
    assert not ok and missing


def test_b1_meeting_at_known_office_is_supported(db, user, trips):
    setup_places(trips)
    trips.location(OREBRO_OFFICE, name="Örebrokontoret", type=LocationType.OFFICE)
    t = trips.trip(HOME, OREBRO_OFFICE)
    ok, missing = purpose.validate_generated("Möte på Örebrokontoret", context(db, user, t))
    assert ok, missing


@pytest.mark.parametrize(
    "text",
    [
        "Projektmöte hos Exempelbolaget",
        "Avstämningsmöte hos Exempelbolaget",
        "Avstämningsmötet hos Exempelbolaget",
        "Säljmöte hos Exempelbolaget",
        "Möte med leverantör",
        "Installation hos Exempelbolaget",
        "Felsökning hos kund",
        "Montage hos Exempelbolaget",
        "Kundbesök hos Exempelbolaget",
    ],
)
def test_reg3_compounds_of_common_words_are_accepted(db, user, trips, text):
    setup_places(trips)
    t = trips.trip(OFFICE, CUSTOMER)
    ok, missing = purpose.validate_generated(text, context(db, user, t))
    assert ok, missing


def test_b1_errand_type_supported_by_calendar_booking(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, OFFICE, km=30, minutes=30)
    event(db, user, "Intervju", t.ended_at + timedelta(minutes=10), location_name="Storstadskontoret")
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    ok, missing = purpose.validate_generated("Intervju på Storstadskontoret", context(db, user, t))
    assert ok, missing
    t2 = trips.trip(OFFICE, HOME, km=30, minutes=30)
    t2.started_at = t.ended_at + timedelta(days=3)
    ok, _ = purpose.validate_generated("Intervju på Storstadskontoret", context(db, user, t2))
    assert not ok


def test_b1_errand_type_supported_by_confirmed_history(db, user, trips):
    setup_places(trips)
    ctx = context(db, user, trips.trip(HOME, UNKNOWN))
    assert not purpose.validate_generated("Kundbesök i Annanstad", ctx)[0]  # ingen grund för kundärende
    for _ in range(3):
        trips.trip(HOME, UNKNOWN, category=Category.WORK, review_status=ReviewStatus.APPROVED,
                   purpose="Kundbesök Annanstad")
        trips.skip(hours=20)
    patterns.rebuild(db, user.id)
    t = trips.trip(HOME, UNKNOWN)
    ok, missing = purpose.validate_generated("Kundbesök i Annanstad", context(db, user, t))
    assert ok, missing


def test_b1_engine_keeps_unsupported_type_as_proposal_to_confirm(db, user, trips):
    """1.3.0 (6.C.3): en ärendetyp utan stöd kastas inte längre – förslaget behålls som
    "förslag att bekräfta" (supported False) och godkänns aldrig av sig självt."""
    setup_places(trips)
    fake = FakeProvider(ai_answer("work", "Intervju hos kund i Storstad"))
    t = trips.trip(OFFICE, CUSTOMER)
    a = classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    assert t.purpose == "Intervju hos kund i Storstad." and t.purpose_source == "ai" and not t.purpose_confirmed
    assert not any(s.kind == "ai_purpose_rejected" for s in a.signals)
    current = db.execute(select(M.TripPurpose).where(M.TripPurpose.logical_trip_id == t.id,
                                                     M.TripPurpose.is_current.is_(True))).scalar_one()
    assert current.based_on["supported"] is False and any("ärendetyp" in m for m in current.based_on["unsupported"])
    assert current.rationale.startswith("Förslag att bekräfta – ")


def test_reg3_engine_accepts_supported_compound_purpose(db, user, trips):
    setup_places(trips)
    fake = FakeProvider(ai_answer("work", "Projektmöte hos Exempelbolaget"))
    t = trips.trip(OFFICE, CUSTOMER)
    classify(db, user, t, provider=fake)
    assert t.purpose == "Projektmöte hos Exempelbolaget." and t.purpose_source == "ai"
    assert t.purpose_confirmed is False


# ----------------------------------------------------------------------------- D2


SECRET_LINE = (
    "Anrop med access_token=abcdef1234567890SECRET och Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.hemligt "
    "för bil YV1ZWA8UDL1234567"
)


def _assert_masked(text: str) -> None:
    assert "abcdef1234567890SECRET" not in text
    assert "eyJhbGciOiJIUzI1NiJ9" not in text
    assert "YV1ZWA8UDL1234567" not in text and "4567" in text


def test_d2_redact_masks_tokens_bearer_and_vin():
    _assert_masked(redact(SECRET_LINE))
    assert redact("Resa 2026-06-01 12 km") == "Resa 2026-06-01 12 km"


def test_d2_celery_logging_setup_installs_redaction(tmp_path, monkeypatch):
    from app.worker.celery_app import celery

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    task_logger = logging.getLogger("celery.task")
    saved_task_handlers = task_logger.handlers[:]
    monkeypatch.setattr(celery.log, "already_setup", False)
    monkeypatch.delenv("CELERY_LOG_FILE", raising=False)
    monkeypatch.delenv("CELERY_LOG_LEVEL", raising=False)
    logfile = tmp_path / "worker.log"
    root.handlers = []
    try:
        celery.log.setup(loglevel="INFO", logfile=str(logfile))
        assert root.handlers and all(
            any(isinstance(f, RedactingFilter) for f in h.filters) for h in root.handlers
        )
        logging.getLogger("app.worker.tasks").warning(SECRET_LINE)
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers:
            if h not in saved_handlers:
                h.close()
        root.handlers, root.level = saved_handlers, saved_level
        task_logger.handlers = saved_task_handlers
        logging.captureWarnings(False)
    content = logfile.read_text(encoding="utf-8")
    assert "Anrop med" in content
    _assert_masked(content)


def test_d2_uvicorn_access_handlers_get_filter():
    access = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler()
    access.addHandler(handler)
    try:
        install_redaction()
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    finally:
        access.removeHandler(handler)


# ----------------------------------------------------------------------------- B2



def _commute_trip(db, user, trips):
    p = setup_places(trips)
    db.add(M.WorkplacePeriod(user_id=user.id, kind="location", location_id=p["office"].id))
    db.flush()
    return trips.trip(HOME, OFFICE)


@pytest.mark.parametrize(("require", "expected"), [(True, ReviewStatus.PROPOSED), (False, ReviewStatus.AUTO)])
def test_b2_require_approval_when_uncertain_has_effect(db, user, trips, require, expected):
    settings_svc.update_section(db, user.id, "ai", {"auto_threshold": 0.75})
    settings_svc.update_section(db, user.id, "tax", {"require_approval_when_uncertain": require})
    t = _commute_trip(db, user, trips)
    a = classify(db, user, t)
    assert a.category == Category.PRIVATE and t.tax_note
    assert a.review_status == expected


def test_b2_car_benefit_does_not_break_classification(db, user, trips):
    setup_places(trips)
    settings_svc.update_section(db, user.id, "tax", {"car_benefit": True})
    fake = FakeProvider(ai_answer("work", "Kundbesök hos Exempelbolaget"))
    t = trips.trip(OFFICE, CUSTOMER)
    a = classify(db, user, t, provider=fake)
    assert a.category == Category.WORK
    assert fake.documents[0]["tax"]["car_benefit"] is True


def _ask_always_rule(client, trips, db):
    places_ = setup_places(trips)
    db.commit()
    resp = client.post("/api/v1/learning/rules", json={
        "destination_location_id": str(places_["customer"].id), "category": "undecided",
        "purpose": "Kundbesök hos Exempelbolaget",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["manual_category"] == "undecided" and resp.json()["is_manual_rule"] is True
    return places_


def test_b2_ask_always_rule_is_never_auto_classified(client, db, user, trips):
    setup_places(trips)
    t_before = trips.trip(OFFICE, CUSTOMER)
    classify(db, user, t_before)
    assert t_before.category == Category.WORK  # känd kund → klassas automatiskt utan regel
    db.commit()
    _ask_always_rule(client, trips, db)
    db.expire_all()
    before = seg.load_trip(db, t_before.id)
    assert before.category == Category.UNDECIDED and before.review_status == ReviewStatus.NEEDS_REVIEW

    # Historik, mönster och AI kan inte heller avgöra resan.
    for _ in range(4):
        trips.trip(OFFICE, CUSTOMER, category=Category.WORK, review_status=ReviewStatus.APPROVED, purpose="Kundbesök")
        trips.skip(hours=20)
    patterns.rebuild(db, user.id)
    fake = FakeProvider(ai_answer("work", "Kundbesök hos Exempelbolaget", conf=0.99))
    t = trips.trip(OFFICE, CUSTOMER)
    a = classify(db, user, t, provider=fake)
    assert fake.documents == []
    assert a.category == Category.UNDECIDED and t.category == Category.UNDECIDED
    assert t.review_status == ReviewStatus.NEEDS_REVIEW and a.suggested_category is None
    assert any(s.kind == "manual_review_rule" for s in a.signals)
    assert t.purpose_confirmed is False  # regelns syfte är bara ett förslag


def test_b2_pattern_update_accepts_undecided(client, db, user, trips):
    places_ = setup_places(trips)
    db.commit()
    resp = client.post("/api/v1/learning/rules", json={
        "destination_location_id": str(places_["customer"].id), "category": "work",
    })
    pid = resp.json()["id"]
    resp = client.patch(f"/api/v1/learning/patterns/{pid}", json={"manual_category": "undecided"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["manual_category"] == "undecided"


# ----------------------------------------------------------------------------- A4


def test_a4_trip_list_includes_start_interval_for_unknown_start(client, db, user, trips):
    known = trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=30, start_known=False)
    lo, hi = known.ended_at + timedelta(minutes=20), known.ended_at + timedelta(minutes=45)
    raw2.start_earliest, raw2.start_latest = lo, hi
    seg.set_raw_sort_time(raw2)
    unknown = seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    db.commit()
    resp = client.get("/api/v1/trips", params={"page_size": 50})
    assert resp.status_code == 200, resp.text
    items = {i["id"]: i for i in resp.json()["items"]}
    item = items[str(unknown.id)]
    assert datetime.fromisoformat(item["start_earliest"]) == lo
    assert datetime.fromisoformat(item["start_latest"]) == hi
    assert items[str(known.id)]["start_earliest"] is None and items[str(known.id)]["start_latest"] is None


# ----------------------------------------------------------------------------- B4


def test_b4_overview_uses_ha_heartbeat_rule(client, db, user):
    from app.db import utcnow
    from app.models.enums import IntegrationProvider, IntegrationStatus
    from app.services import integrations

    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull"})
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.HOME_ASSISTANT)
    conn.status = IntegrationStatus.CONNECTED
    conn.config = {"heartbeat": {"received_at": (utcnow() - timedelta(minutes=30)).isoformat()}}
    db.commit()
    ha = client.get("/api/v1/integrations/home-assistant").json()
    overview = client.get("/api/v1/integrations").json()["home_assistant"]
    assert ha["status"] == "error"
    assert overview["status"] == "error" and overview["is_connected"] is False and overview["last_error"]

    conn.config = {"heartbeat": {"received_at": utcnow().isoformat()}}
    db.commit()
    overview = client.get("/api/v1/integrations").json()["home_assistant"]
    assert overview["status"] == "connected" and overview["is_connected"] is True


# ----------------------------------------------------------------------------- D4


def test_d4_env_timezone_is_default_when_user_has_not_chosen(db, user, monkeypatch):
    from app.config import reset_config_cache

    assert settings_svc.get_section(db, user.id, "general")["timezone"] == "Europe/Stockholm"
    monkeypatch.setenv("DRIVELOG_TIMEZONE", "Europe/Helsinki")
    reset_config_cache()
    assert settings_svc.user_timezone(db, user.id).key == "Europe/Helsinki"
    assert settings_svc.defaults()["general"]["timezone"] == "Europe/Helsinki"
    settings_svc.update_section(db, user.id, "general", {"timezone": "Europe/Oslo"})
    assert settings_svc.user_timezone(db, user.id).key == "Europe/Oslo"  # användarens val går före
    monkeypatch.setenv("DRIVELOG_TIMEZONE", "Inte/EnZon")
    reset_config_cache()
    assert settings_svc.default_timezone() == "Europe/Stockholm"  # ogiltigt värde ignoreras
