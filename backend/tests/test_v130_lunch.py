"""1.3.0 DEL 4 – lunchregeln (byggkontraktet 7.A, tilläggskontraktet 3.H): tabellen i 7.A.2,
väntan på återresan, ägarens svar och egna regler, manuellt läge och inställningarna."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models as M
from app.models.enums import ApprovalSource, Category, LocationType, ReviewStatus
from app.services import settings as settings_svc
from app.services.classify import autopilot, lunch
from app.services.learning import patterns
from tests.conftest import reload_trip
from tests.v130_ai_stod import (
    CAFE,
    CUSTOMER,
    HBG_OFFICE,
    HOTEL,
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

MON = (2026, 9, 14)
SAT = (2026, 9, 19)
OTHER = "Annanväg 7, 111 44 Exempelstad"
FAR = "Långväg 1, 111 55 Exempelstad"


@pytest.fixture()
def setup(db, user, trips):
    settings_svc.update_section(db, user.id, "lunch", {"enabled": True})
    self_driving(db, user)
    locs = places(trips)
    cafe = trips.location(CAFE, name="Kafé Exempel", type=LocationType.UNKNOWN, confirmed=False)
    cafe.osm_class, cafe.place_kind = "amenity=cafe", "never"
    db.flush()
    locs["cafe"] = cafe
    return locs


def outbound(trips, *, day=MON, start=(11, 52), minutes=4, km=3.0, origin=OFFICE, dest=CAFE):
    return live(trips, origin, dest, km=km, minutes=minutes, start_at=local(*day, *start))


def back(trips, *, day=MON, start=(12, 21), minutes=4, km=3.0, origin=CAFE, dest=OFFICE):
    return live(trips, origin, dest, km=km, minutes=minutes, start_at=local(*day, *start))


def lunch_pair(trips, *, day=MON, origin=OFFICE):
    return outbound(trips, day=day, origin=origin), back(trips, day=day, dest=origin)


# ----------------------------------------------------------------------------- tabellen


def test_complete_pattern_makes_both_private_with_reason(db, user, vehicle, trips, setup):
    u, r = lunch_pair(trips)
    d = decide(db, user, u)
    assert (d.outcome, d.category, d.source, d.reason) == ("approve", Category.PRIVATE, ApprovalSource.LUNCH,
                                                           "Lunch (kafé)")
    assert d.related_trip_id == r.id
    run(db, user, vehicle)
    for t in (u, r):
        t = reload_trip(db, t.id)
        assert (t.review_status, t.category, t.approval_source) == (ReviewStatus.APPROVED, Category.PRIVATE,
                                                                    ApprovalSource.LUNCH)


def test_lunch_from_another_office_counts(db, user, vehicle, trips, setup):
    u, r = lunch_pair(trips, origin=HBG_OFFICE)
    assert decide(db, user, u).source == ApprovalSource.LUNCH


def test_lunch_from_hotel_is_never_lunch(db, user, vehicle, trips, setup):
    u, r = lunch_pair(trips, origin=HOTEL)
    assert decide(db, user, u).source != ApprovalSource.LUNCH
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_return_after_window_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips)
    back(trips, start=(13, 5))
    assert decide(db, user, u).source != ApprovalSource.LUNCH


def test_long_stop_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips, start=(11, 5))
    back(trips, start=(12, 5))  # 60 minuters stopp
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_unknown_stop_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips)
    r = live(trips, CAFE, OFFICE, km=3.0, minutes=4, start_at=local(*MON, 12, 21), start_known=False)
    assert r.started_at is None
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_confirmed_customer_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips, dest=CUSTOMER)
    back(trips, origin=CUSTOMER)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_calendar_booking_on_outbound_stops_lunch(db, user, vehicle, trips, setup):
    setup["cafe"].city = "Exempelstad"
    u = outbound(trips)
    back(trips)
    event(db, user, "Lunchmöte Exempelstad", local(*MON, 12, 0), minutes=20, location_city="Exempelstad")
    rows = match(db, user, u)
    assert any(m.is_selected for m in rows)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_saturday_and_holiday_are_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips, day=SAT)
    back(trips, day=SAT)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None
    h = outbound(trips, day=(2026, 12, 25))  # juldagen, en fredag
    back(trips, day=(2026, 12, 25))
    assert lunch.match(db, reload_trip(db, h.id), user_id=user.id) is None


def test_return_to_other_place_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips)
    back(trips, dest=OTHER)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_long_drive_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips, km=12.0)
    back(trips, km=12.0)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_no_confirmed_office_means_rule_never_applies(db, user, vehicle, trips, setup, client):
    for loc in db.query(M.KnownLocation).filter(M.KnownLocation.type.in_(("regular_workplace", "office"))):
        loc.confirmed = False
    db.commit()
    u, _r = lunch_pair(trips)
    assert lunch.office_ready(db, user.id) is False
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None
    db.commit()
    assert client.get("/api/v1/settings").json()["status"] == {"lunch_office_ready": False}


def test_status_office_ready(client, db, user, trips):
    places(trips)
    db.commit()
    assert client.get("/api/v1/settings").json()["status"]["lunch_office_ready"] is True


def test_awaiting_return_waits_without_question(db, user, vehicle, trips, setup):
    from app.services.ha import notifier

    u = outbound(trips)
    ended = reload_trip(db, u.id).ended_at
    d = decide(db, user, u, now=ended + timedelta(minutes=5))
    assert (d.outcome, d.reason) == ("wait", autopilot.LUNCH_WAIT_TEXT)
    assert notifier._waiting_for_lunch(db, reload_trip(db, u.id), user.id, ended + timedelta(minutes=5))
    # 11:56 + 45 + 10 + 10 = 13:01: därefter vanlig hantering.
    late = ended + timedelta(minutes=66)
    assert decide(db, user, u, now=late).outcome != "wait"
    assert not notifier._waiting_for_lunch(db, reload_trip(db, u.id), user.id, late)


def test_late_return_makes_both_lunch_and_cancels_question(db, user, vehicle, trips, setup):
    u = outbound(trips)
    db.add(M.NotificationRequest(user_id=user.id, kind="classify", public_id="lunchq1", logical_trip_id=u.id,
                                 vehicle_id=vehicle.id, title="Fråga", message="?", status="sent",
                                 dedupe_key=f"classify:{u.id}"))
    db.flush()
    back(trips)
    run(db, user, vehicle)
    assert reload_trip(db, u.id).approval_source == ApprovalSource.LUNCH
    req = db.query(M.NotificationRequest).filter_by(public_id="lunchq1").one()
    assert req.status == "cancelled"


def test_owner_said_work_on_outbound_so_return_is_not_lunch(db, user, vehicle, trips, setup):
    u = outbound(trips)
    t = reload_trip(db, u.id)
    t.category, t.review_status, t.approval_source = Category.WORK, ReviewStatus.APPROVED, ApprovalSource.USER
    t.purpose, t.purpose_confirmed = "Kundbesök Kafé Exempel", True
    db.flush()
    r = back(trips)
    assert lunch.match(db, reload_trip(db, r.id), user_id=user.id) is None


def test_own_rule_for_place_wins(db, user, vehicle, trips, setup):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=setup["cafe"].id, category=Category.WORK,
                             purpose="Leverans till kafét")
    db.flush()
    u, _r = lunch_pair(trips)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None
    d = decide(db, user, u)
    assert d.source == ApprovalSource.RULE and d.category == Category.WORK


def test_earlier_work_trip_to_place_stops_lunch(db, user, vehicle, trips, setup):
    trips.trip(OFFICE, CAFE, category=Category.WORK, purpose="Leverans", km=3, minutes=4,
               start_at=local(2026, 9, 10, 9, 0))
    u, _r = lunch_pair(trips)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_corrected_lunch_trip_stops_next_lunch(db, user, vehicle, trips, setup):
    u, r = lunch_pair(trips)
    run(db, user, vehicle)
    t = reload_trip(db, u.id)
    t.category, t.approval_source = Category.WORK, ApprovalSource.USER  # ägaren rättar till tjänst
    t.work_km, t.private_km = t.distance_km, 0.0
    db.flush()
    u2 = outbound(trips, day=(2026, 9, 15))
    back(trips, day=(2026, 9, 15))
    assert lunch.match(db, reload_trip(db, u2.id), user_id=user.id) is None


def test_map_says_work_place_stops_lunch(db, user, vehicle, trips, setup):
    setup["cafe"].place_kind = "work"
    db.flush()
    u, _r = lunch_pair(trips)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_clock_alone_is_never_lunch(db, user, vehicle, trips, setup):
    u = live(trips, CUSTOMER, FAR, km=3, minutes=4, start_at=local(*MON, 12, 0))
    live(trips, FAR, CUSTOMER, km=3, minutes=4, start_at=local(*MON, 12, 25))
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None


def test_lunch_trips_do_not_count_as_history(db, user, vehicle, trips, setup):
    u, r = lunch_pair(trips)
    run(db, user, vehicle)
    assert ApprovalSource.LUNCH in ApprovalSource and "lunch" in autopilot.AUTO_APPROVAL_SOURCES
    assert not autopilot._owner_confirmed(reload_trip(db, u.id))


# ----------------------------------------------------------------------------- manuellt läge och inställningar


def test_manual_mode_gives_private_proposal_with_signal(db, user, vehicle, trips, setup):
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    u, _r = lunch_pair(trips)
    a = classify(db, user, u)
    sig = next(s for s in a.signals if s.kind == "lunch_pattern")
    assert sig.direction == Category.PRIVATE and "kafé" in sig.label
    t = reload_trip(db, u.id)
    assert t.review_status != ReviewStatus.APPROVED and t.approval_source is None


def test_disabled_rule_does_nothing(db, user, vehicle, trips, setup):
    settings_svc.update_section(db, user.id, "lunch", {"enabled": False})
    u, _r = lunch_pair(trips)
    assert lunch.match(db, reload_trip(db, u.id), user_id=user.id) is None
    assert decide(db, user, u).source != ApprovalSource.LUNCH


def test_settings_validation_texts(db, user):
    with pytest.raises(settings_svc.SettingsError, match="fönstrets början måste ligga före slutet"):
        settings_svc.update_section(db, user.id, "lunch", {"window_start": "13:00", "window_end": "11:00"})
    with pytest.raises(settings_svc.SettingsError, match="kortaste stoppet får inte vara längre"):
        settings_svc.update_section(db, user.id, "lunch", {"min_stop_minutes": 50, "max_stop_minutes": 40})
    with pytest.raises(settings_svc.SettingsError, match="mellan"):
        settings_svc.update_section(db, user.id, "lunch", {"max_distance_km": 100})
    with pytest.raises(settings_svc.SettingsError, match="HH:MM"):
        settings_svc.update_section(db, user.id, "lunch", {"window_start": "11"})


@pytest.mark.new_defaults
def test_rule_is_on_by_default():
    assert settings_svc.DEFAULTS["lunch"] == {
        "enabled": True, "window_start": "11:00", "window_end": "13:00", "max_drive_minutes": 10,
        "max_distance_km": 8, "min_stop_minutes": 20, "max_stop_minutes": 45,
    }


def test_month_close_label():
    from app.services.export import month_close

    assert month_close.SOURCE_LABELS["lunch"] == "Lunch" and month_close.SOURCE_LABELS["home_office"] == "Hem ↔ kontor"
