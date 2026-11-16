"""1.3.0 – ägarens beslut B, H och I: resor hem ↔ arbetsgivarens kontor (regel K) och
kvällsärenden under en tjänsteresa (tilläggskontraktet 3.G)."""

from __future__ import annotations

import pytest

import app.models as M
from app.models.enums import ApprovalSource, Category, ReviewStatus
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.learning import patterns
from tests.conftest import reload_trip
from tests.v130_ai_stod import (
    CUSTOMER,
    HBG_OFFICE,
    HOME,
    HOTEL,
    JKPG_OFFICE,
    OFFICE,
    classify,
    decide,
    event,
    live,
    local,
    match,
    places,
    run,
    self_driving,
)

TUE = (2026, 9, 15)
SUN = (2026, 9, 20)
NEAR_HOTEL = "Krogvägen 2, 553 22 Jönköping"


@pytest.fixture()
def k(db, user, trips):
    self_driving(db, user)
    settings_svc.update_section(db, user.id, "automation", {"home_office_is_work": True})
    return places(trips)


def test_home_to_any_office_is_approved_as_work(db, user, vehicle, trips, k):
    a = live(trips, HOME, OFFICE, km=12, start_at=local(*TUE, 7, 30))
    b = live(trips, HOME, JKPG_OFFICE, km=150, minutes=100, start_at=local(*TUE, 9, 0))
    c = live(trips, JKPG_OFFICE, HOME, km=150, minutes=100, start_at=local(*TUE, 16, 0))
    for t, purpose in ((a, "Resa till kontoret i Exempelstad"), (b, "Resa till kontoret i Jönköping"),
                       (c, "Hemresa från kontoret i Jönköping")):
        d = decide(db, user, t)
        assert (d.outcome, d.category, d.source) == ("approve", Category.WORK, ApprovalSource.HOME_OFFICE), t
        assert d.purpose == purpose and d.reason.startswith("Hem ↔ kontor i ")
    run(db, user, vehicle)
    t = reload_trip(db, b.id)
    assert (t.review_status, t.approval_source, t.purpose_confirmed) == (ReviewStatus.APPROVED, "home_office", True)


def test_sunday_evening_to_office_city_is_approved(db, user, vehicle, trips, k):
    t = live(trips, HOME, HBG_OFFICE, km=250, minutes=160, start_at=local(*SUN, 19, 0))
    d = decide(db, user, t)
    assert d.source == ApprovalSource.HOME_OFFICE and d.purpose == "Resa till kontoret i Helsingborg"


def test_customer_and_hotel_are_not_offices(db, user, vehicle, trips, k):
    for dest in (CUSTOMER, HOTEL):
        t = live(trips, HOME, dest, km=80, start_at=local(*TUE, 8, 0))
        assert decide(db, user, t).source != ApprovalSource.HOME_OFFICE


def test_hard_block_and_calendar_conflict_stop_k(db, user, vehicle, trips, k):
    t = live(trips, HOME, OFFICE, km=12, start_at=local(*TUE, 7, 30))
    raw = t.segments[0].raw_trip
    raw.quality_flags = ["collection_gap"]
    t.quality_flags = ["collection_gap"]
    db.flush()
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.reason == autopilot.MULTI_TRIP_TEXT
    t2 = live(trips, HOME, OFFICE, km=12, start_at=local(2026, 9, 16, 7, 30))
    event(db, user, "Workshop Karlstad", t2.ended_at, minutes=120, location_city="Karlstad",
          location_name="Konferenshuset Karlstad")
    rows = match(db, user, t2)
    assert any(m.is_conflict for m in rows)
    d2 = decide(db, user, t2)
    assert d2.outcome == "ask" and d2.source is None


def test_own_private_rule_goes_first(db, user, vehicle, trips, k):
    patterns.set_manual_rule(db, user.id, origin_id=k["home"].id, dest_id=k["office"].id,
                             category=Category.PRIVATE, purpose=None)
    db.flush()
    t = live(trips, HOME, OFFICE, km=12, start_at=local(*TUE, 7, 30))
    d = decide(db, user, t)
    assert (d.source, d.category) == (ApprovalSource.RULE, Category.PRIVATE)


def test_setting_off_keeps_commute_rule(db, user, vehicle, trips, k):
    settings_svc.update_section(db, user.id, "automation", {"home_office_is_work": False})
    t = live(trips, HOME, OFFICE, km=12, start_at=local(*TUE, 7, 30))
    d = decide(db, user, t)
    assert (d.source, d.category, d.reason) == (ApprovalSource.COMMUTE, Category.PRIVATE, "Resa till arbetet")


def test_manual_mode_gives_work_proposal_with_signal(db, user, vehicle, trips, k):
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    t = live(trips, HOME, OFFICE, km=12, start_at=local(*TUE, 7, 30))
    a = classify(db, user, t)
    assert any(s.kind == "home_office_work" and s.direction == Category.WORK for s in a.signals)
    assert not any(s.kind == "commute" for s in a.signals) and a.tax_note is None
    t = reload_trip(db, t.id)
    assert t.category == Category.WORK and t.review_status != ReviewStatus.APPROVED


def test_evening_errand_from_hotel_asks(db, user, vehicle, trips, k):
    t = live(trips, HOTEL, NEAR_HOTEL, km=3, minutes=8, start_at=local(*TUE, 20, 10))
    d = decide(db, user, t)
    assert (d.outcome, d.reason, d.candidates) == ("ask", autopilot.EVENING_TEXT, ())
    # Mitt på dagen gäller den vanliga hanteringen.
    t2 = live(trips, HOTEL, NEAR_HOTEL, km=3, minutes=8, start_at=local(2026, 9, 16, 10, 10))
    assert decide(db, user, t2).reason != autopilot.EVENING_TEXT


def test_evening_trip_from_hotel_to_office_is_not_an_errand(db, user, vehicle, trips, k):
    t = live(trips, HOTEL, JKPG_OFFICE, km=3, minutes=8, start_at=local(*TUE, 19, 0))
    assert decide(db, user, t).reason != autopilot.EVENING_TEXT


@pytest.mark.new_defaults
def test_default_off_for_new_installations():
    assert settings_svc.DEFAULTS["automation"]["home_office_is_work"] is False


def test_month_close_note_uses_label(db, user, vehicle, trips, k):
    from app.services.export import month_close

    assert month_close.SOURCE_LABELS[ApprovalSource.HOME_OFFICE] == "Hem ↔ kontor"
    assert M.LogicalTrip  # import används
