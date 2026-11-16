"""S1c: självgående läge – regeltabellen (A.2), exemplen (A.4), sammanslagningen och
"Privat stopp" i underlaget (A.5). Platsantagandet (A) prövas i test_s1_antagande.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    ApprovalSource,
    Category,
    LocationType,
    LogicalState,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TripSource,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import autopilot, engine
from app.services.export import simple
from app.services.learning import feedback, patterns
from app.services.trips import journeys, merge, pipeline
from app.services.trips import segments as seg
from tests.conftest import reload_trip

ACTIVE_SINCE = "2020-01-01T00:00:00+00:00"
HOME = "Hemvägen 1, 111 11 Exempelstad"
OFFICE = "Kontorsgatan 2, 111 22 Exempelstad"
ACME = "Industrivägen 10, 222 33 Verkstadsby"
BETA = "Lagergatan 4, 333 44 Norrby"
HOTEL = "Hotellgatan 8, 444 55 Söderby"
SCHOOL = "Skolvägen 3, 111 33 Exempelstad"
FUEL = "Macken 1, 111 44 Exempelstad"
SHOP = "Butiksgatan 7, 111 55 Exempelstad"
MON = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)  # måndag 09:00 svensk tid
TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture()
def auto(db, user):
    """Självgående läge med en starttid långt före testernas resor."""
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    db.flush()


@pytest.fixture()
def locs(trips):
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "office": trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE),
        "acme": trips.location(ACME, name="Acme AB", type=LocationType.CUSTOMER, company="Acme AB"),
        "beta": trips.location(BETA, name="Beta AB", type=LocationType.CUSTOMER, company="Beta AB"),
        "hotel": trips.location(HOTEL, name="Hotellet", type=LocationType.HOTEL),
        "school": trips.location(SCHOOL, name="Förskolan", type=LocationType.UNKNOWN, confirmed=False),
        "fuel": trips.location(FUEL, name="Macken", type=LocationType.FUEL_STATION),
    }


def live(trips, start, end, **kw):
    """En resa från bilen som ingen har avgjort."""
    return trips.trip(start, end, source=TripSource.VOLVO_API, **kw)


def owner_work(trips, start, end, purpose="Service av maskin", **kw):
    """Ägarens egen godkända tjänsteresa (approval_source NULL = ägaren)."""
    return trips.trip(start, end, category=Category.WORK, purpose=purpose, source=TripSource.VOLVO_API, **kw)


def run(db, user, vehicle):
    out = autopilot.run_vehicle(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM)
    db.flush()
    return out


def decide(db, user, trip):
    return autopilot.decide(db, reload_trip(db, trip.id), user_id=user.id, ctx=None, rules=None, cfg=None)


def history(trips, start, end, *, n=3, km=20.0, purpose="Service av maskin", first=MON, category=Category.WORK):
    out = []
    for i in range(n):
        out.append(
            trips.trip(start, end, category=category, purpose=purpose if category == Category.WORK else None,
                       source=TripSource.VOLVO_API, km=km, start_at=first + timedelta(days=i))
        )
    return out


# ----------------------------------------------------------------------------- vem omfattas


def test_eligible_only_live_unapproved_trips_after_active_since(db, user, vehicle, trips, auto, locs):
    since = settings_svc.active_since(db, user.id)
    t_live = live(trips, HOME, ACME)
    t_import = trips.trip(HOME, ACME, source=TripSource.VOLVO_IMPORT)
    t_manual = trips.trip(HOME, ACME, source=TripSource.MANUAL)
    t_approved = owner_work(trips, HOME, ACME)
    assert autopilot.eligible(t_live, active_since=since)
    assert not autopilot.eligible(t_import, active_since=since)
    assert not autopilot.eligible(t_manual, active_since=since)
    assert not autopilot.eligible(t_approved, active_since=since)
    assert not autopilot.eligible(t_live, active_since=None)
    # Skapad före active_since (t.ex. före uppgraderingen): rörs aldrig.
    assert not autopilot.eligible(t_live, active_since=datetime.now(UTC) + timedelta(days=1))


def test_manual_mode_never_decides(db, user, vehicle, trips, locs):
    history(trips, HOME, ACME)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    assert run(db, user, vehicle) == {}
    t = reload_trip(db, t.id)
    assert t.review_status == ReviewStatus.NEEDS_REVIEW and t.approval_source is None


def test_mode_without_active_since_is_not_active(db, user, vehicle, trips, locs):
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    assert not autopilot.is_active(db, user.id)
    t = live(trips, HOME, SCHOOL)
    assert run(db, user, vehicle) == {}
    assert reload_trip(db, t.id).approval_source is None


# ----------------------------------------------------------------------------- P


def test_p_commute_is_private_with_note(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, OFFICE)
    assert run(db, user, vehicle) == {"approved": 1}
    t = reload_trip(db, t.id)
    assert t.category == Category.PRIVATE and t.review_status == ReviewStatus.APPROVED
    assert t.approval_source == ApprovalSource.COMMUTE
    assert t.approval_basis["reason"] == "Resa till arbetet"
    assert t.notes == "Resa mellan bostad och arbetsplats"
    assert t.category_source == "rule" and t.tax_note is None and t.auto_approved_at is not None
    cls = db.execute(
        select(M.TripClassification).where(M.TripClassification.logical_trip_id == t.id,
                                           M.TripClassification.is_current.is_(True))
    ).scalar_one()
    assert cls.signals["autopilot"] == ApprovalSource.COMMUTE
    assert db.execute(select(M.LearningFeedback)).first() is None  # ingen inlärning av automatiken
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.auto_approved")).first()


def test_p_commute_back_home_too(db, user, vehicle, trips, auto, locs):
    t = live(trips, OFFICE, HOME)
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source == ApprovalSource.COMMUTE


def test_p_commute_needs_business_only_and_commute_rule(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "tax", {"home_is_workplace": True})
    t1 = live(trips, HOME, OFFICE)
    run(db, user, vehicle)
    assert reload_trip(db, t1.id).approval_source is None
    settings_svc.update_section(db, user.id, "tax", {"home_is_workplace": False})
    settings_svc.update_section(db, user.id, "general", {"journal_scope": settings_svc.SCOPE_COMPLETE})
    trips.skip(hours=3)
    t2 = live(trips, HOME, OFFICE)
    run(db, user, vehicle)
    assert reload_trip(db, t2.id).approval_source is None


def test_p_private_place(db, user, vehicle, trips, auto, locs):
    trips.location(SHOP, name="Gymmet", type=LocationType.PRIVATE)
    t1 = live(trips, HOME, SHOP)
    trips.skip(hours=1)
    t2 = live(trips, SHOP, HOME)
    assert run(db, user, vehicle) == {"approved": 2}
    for t in (t1, t2):
        t = reload_trip(db, t.id)
        assert t.category == Category.PRIVATE and t.approval_source == ApprovalSource.PRIVATE_PLACE
        assert t.approval_basis["reason"] == "Privat plats"


# ----------------------------------------------------------------------------- R


def test_r_private_rule(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["school"].id, category=Category.PRIVATE,
                             purpose=None)
    t = live(trips, ACME, SCHOOL)
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert (t.category, t.approval_source) == (Category.PRIVATE, ApprovalSource.RULE)
    assert t.approval_basis["reason"] == "Din regel för Förskolan"


def test_r_work_rule_with_purpose(db, user, vehicle, trips, auto, locs):
    pat = patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                                   purpose="Service av maskin")
    t = live(trips, HOME, ACME)
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert (t.category, t.review_status, t.approval_source) == (Category.WORK, ReviewStatus.APPROVED, "rule")
    assert t.purpose == "Service av maskin" and t.purpose_confirmed and t.purpose_source == "pattern"
    assert t.approval_basis == {"pattern_id": str(pat.id), "related_trip_id": None,
                                "reason": "Din regel för Acme AB", "veto_until": None}
    assert t.category_source == "rule"


def test_r_route_rule_wins_over_destination_rule(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                             purpose="Service av maskin")
    patterns.set_manual_rule(db, user.id, origin_id=locs["beta"].id, dest_id=locs["acme"].id,
                             category=Category.PRIVATE, purpose=None)
    t = live(trips, BETA, ACME)
    run(db, user, vehicle)
    assert reload_trip(db, t.id).category == Category.PRIVATE


def test_r_work_rule_without_purpose_asks_for_purpose(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                             purpose=None)
    t = live(trips, HOME, ACME)
    assert decide(db, user, t).outcome == "ask_purpose"
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert (t.category, t.review_status, t.approval_source) == (Category.WORK, ReviewStatus.PROPOSED, None)


def test_r_work_rule_to_hotel_applies(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["hotel"].id, category=Category.WORK,
                             purpose="Övernattning under tjänsteresa")
    t = live(trips, ACME, HOTEL)
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source == ApprovalSource.RULE


def test_r_correction_turns_rule_into_ask_always(db, user, vehicle, trips, auto, locs):
    pat = patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                                   purpose="Service av maskin")
    t = live(trips, HOME, ACME)
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert t.approval_source == ApprovalSource.RULE
    t = feedback.apply_decision(db, t, user_id=user.id, actor=audit.user_actor(user.id), category=Category.PRIVATE)
    assert t._autopilot_notice == "Regeln för Acme AB frågar dig nu i stället."
    assert t.approval_source == ApprovalSource.USER and t.auto_approved_at is None
    db.refresh(pat)
    assert pat.is_manual_rule and pat.manual_category == Category.UNDECIDED
    trips.skip(days=1)
    t2 = live(trips, HOME, ACME)
    d = decide(db, user, t2)
    assert d.outcome == "ask" and d.reason == "Din regel: fråga alltid"


def test_confirming_an_automatic_trip_is_not_a_correction(db, user, vehicle, trips, auto, locs):
    pat = patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                                   purpose="Service av maskin")
    t = live(trips, HOME, ACME)
    run(db, user, vehicle)
    t = feedback.apply_decision(db, reload_trip(db, t.id), user_id=user.id, actor=audit.user_actor(user.id),
                                approve=True)
    assert t._autopilot_notice is None and t.approval_source == ApprovalSource.USER
    db.refresh(pat)
    assert pat.manual_category == Category.WORK


# ----------------------------------------------------------------------------- H


def test_h_three_owner_confirmed_trips(db, user, vehicle, trips, auto, locs):
    hist = history(trips, HOME, ACME)
    t = live(trips, HOME, ACME, km=21.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert (t.category, t.approval_source, t.purpose) == (Category.WORK, "history", "Service av maskin")
    assert t.approval_basis["reason"] == "Som de 3 senaste gångerna"
    assert t.category_source == "pattern"
    del hist


def test_h_private_history(db, user, vehicle, trips, auto, locs):
    history(trips, OFFICE, SCHOOL, category=Category.PRIVATE)
    t = live(trips, OFFICE, SCHOOL, km=40.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert (t.category, t.approval_source) == (Category.PRIVATE, ApprovalSource.HISTORY)


def test_h_needs_three(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME, n=2)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_h_bulk_approvals_do_not_count(db, user, vehicle, trips, auto, locs):
    hist = history(trips, HOME, ACME)
    hist[0].approval_source = ApprovalSource.BULK
    db.flush()
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_h_bulk_channel_marks_bulk(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, ACME)
    feedback.bulk_apply(db, [t], user_id=user.id, actor=audit.user_actor(user.id), category=Category.WORK,
                        purpose="Service av maskin", approve=True)
    assert reload_trip(db, t.id).approval_source == ApprovalSource.BULK


def test_h_counter_evidence_from_automatic_private_trip_stops_h(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME)
    counter = trips.trip(HOME, ACME, category=Category.PRIVATE, source=TripSource.VOLVO_API, km=20.0,
                         start_at=MON + timedelta(days=3, hours=6))
    counter.approval_source = ApprovalSource.RULE  # avgjord av automatiken, inte av ägaren
    db.flush()
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=4))
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_h_distance_outside_15_percent_stops_h(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME, km=20.0)
    t = live(trips, HOME, ACME, km=24.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_h_different_purposes_ask_with_previous_purposes(db, user, vehicle, trips, auto, locs):
    for i, p in enumerate(("Service av maskin", "Leverans av delar", "Möte med inköp")):
        trips.trip(HOME, ACME, category=Category.WORK, purpose=p, source=TripSource.VOLVO_API, km=20.0,
                   start_at=MON + timedelta(days=i))
    patterns.rebuild(db, user.id)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    d = decide(db, user, t)
    assert d.outcome == "ask" and len(d.candidates) == 2
    assert set(d.candidates) <= {"Service av maskin", "Leverans av delar", "Möte med inköp"}


def test_h_weekend_trip_against_weekday_history(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME)
    saturday = MON + timedelta(days=5)
    t = live(trips, HOME, ACME, km=20.0, start_at=saturday)
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_h_correction_to_destination_stops_h(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME)
    other = trips.trip(BETA, ACME, category=Category.WORK, purpose="Service av maskin", km=30.0,
                       start_at=MON + timedelta(days=3))
    feedback.apply_decision(db, other, user_id=user.id, actor=audit.user_actor(user.id), category=Category.PRIVATE)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=4))
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_h_trip_corrected_stops_h_for_route(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert t.approval_source == ApprovalSource.HISTORY
    feedback.apply_decision(db, t, user_id=user.id, actor=audit.user_actor(user.id), category=Category.PRIVATE)
    t2 = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=7))
    run(db, user, vehicle)
    assert reload_trip(db, t2.id).approval_source is None


def test_automatic_approvals_never_count_as_history(db, user, vehicle, trips, auto, locs):
    """Tre resor som godkänts av en regel ger ingen H när regeln tas bort, och inga mönster."""
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                             purpose="Service av maskin")
    for i in range(3):
        live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=i))
    run(db, user, vehicle)
    assert sum(1 for t in seg.active_trips(db, vehicle.id) if t.approval_source == ApprovalSource.RULE) == 3
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=None, purpose=None)
    patterns.rebuild(db, user.id)
    route, dest = patterns.get_patterns(db, user.id, locs["home"].id, locs["acme"].id)
    assert dest is None or (dest.confirmed_work == 0 and not dest.purposes)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


# ----------------------------------------------------------------------------- T


def _outbound(db, user, trips, *, km=20.0, at=MON):
    out = live(trips, HOME, ACME, km=km, start_at=at)
    return feedback.apply_decision(db, out, user_id=user.id, actor=audit.user_actor(user.id),
                                   category=Category.WORK, purpose="Kundbesök Acme AB")


def test_t_return_trip_after_approved_outbound(db, user, vehicle, trips, auto, locs):
    out = _outbound(db, user, trips)
    back = live(trips, ACME, HOME, km=21.0, start_at=MON + timedelta(hours=3))
    run(db, user, vehicle)
    back = reload_trip(db, back.id)
    assert (back.category, back.approval_source) == (Category.WORK, ApprovalSource.RETURN_TRIP)
    assert back.purpose == "Hemresa efter kundbesök Acme AB."
    assert back.approval_basis["related_trip_id"] == str(out.id)
    assert back.approval_basis["reason"] == "Hemresa efter kundbesök Acme AB"


def test_t_after_assumed_outbound_is_assumed(db, user, vehicle, trips, auto, locs):
    out = _outbound(db, user, trips)
    out.approval_source = ApprovalSource.ASSUMED
    out.approval_basis = {"pattern_id": None, "related_trip_id": None, "reason": "Antaget kundbesök",
                          "veto_until": "2026-09-15T09:00:00+00:00"}
    db.flush()
    back = live(trips, ACME, HOME, km=20.0, start_at=MON + timedelta(hours=3))
    run(db, user, vehicle)
    back = reload_trip(db, back.id)
    assert back.approval_source == ApprovalSource.ASSUMED
    assert back.approval_basis["veto_until"] == "2026-09-15T09:00:00+00:00"


@pytest.mark.parametrize(("km", "delay"), [(26.0, timedelta(hours=3)), (20.0, timedelta(days=1))])
def test_t_needs_same_day_and_similar_distance(db, user, vehicle, trips, auto, locs, km, delay):
    _outbound(db, user, trips)
    back = live(trips, ACME, HOME, km=km, start_at=MON + delay)
    run(db, user, vehicle)
    assert reload_trip(db, back.id).approval_source is None


def test_t_runs_after_journeys_and_before_review_items(db, user, vehicle, trips, auto, locs, monkeypatch):
    calls: list[str] = []
    real_detect, real_run, real_sync = journeys.detect, autopilot.run_vehicle, pipeline.sync_trip_review_items
    monkeypatch.setattr(journeys, "detect", lambda *a, **k: calls.append("journeys") or real_detect(*a, **k))
    monkeypatch.setattr(autopilot, "run_vehicle", lambda *a, **k: calls.append("autopilot") or real_run(*a, **k))
    monkeypatch.setattr(pipeline, "sync_trip_review_items",
                        lambda *a, **k: calls.append("sync") or real_sync(*a, **k))
    live(trips, HOME, ACME)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False,
                                 use_llm=False)
    assert calls.index("journeys") < calls.index("autopilot") < calls.index("sync")


def test_t_applies_when_outbound_is_answered_later(db, user, vehicle, trips, auto, locs):
    out = live(trips, HOME, ACME, km=20.0, start_at=MON)
    back = live(trips, ACME, HOME, km=20.0, start_at=MON + timedelta(hours=3))
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False,
                                 use_llm=False)
    assert reload_trip(db, back.id).review_status != ReviewStatus.APPROVED
    feedback.apply_decision(db, reload_trip(db, out.id), user_id=user.id, actor=audit.user_actor(user.id),
                            category=Category.WORK, purpose="Kundbesök Acme AB")
    back = reload_trip(db, back.id)
    assert back.approval_source == ApprovalSource.RETURN_TRIP
    assert back.review_status == ReviewStatus.APPROVED
    open_items = db.execute(
        select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == back.id, M.ReviewItem.status == ReviewState.OPEN)
    ).scalars().all()
    assert not [i for i in open_items if i.kind in (ReviewKind.CATEGORY, ReviewKind.PURPOSE)]


# ----------------------------------------------------------------------------- spärr 2


def test_spärr2_commute_never_work_even_with_history(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "general", {"journal_scope": settings_svc.SCOPE_COMPLETE})
    history(trips, HOME, OFFICE)
    t = live(trips, HOME, OFFICE, km=20.0, start_at=MON + timedelta(days=3))
    d = decide(db, user, t)
    assert d.outcome == "ask"
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert t.review_status != ReviewStatus.APPROVED and t.approval_source is None


def test_spärr2_office_to_home_is_never_t(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "general", {"journal_scope": settings_svc.SCOPE_COMPLETE})
    out = live(trips, ACME, OFFICE, km=20.0, start_at=MON)
    feedback.apply_decision(db, out, user_id=user.id, actor=audit.user_actor(user.id), category=Category.WORK,
                            purpose="Kundbesök Acme AB")
    t = live(trips, OFFICE, HOME, km=20.0, start_at=MON + timedelta(hours=3))
    assert decide(db, user, t).outcome == "ask"


def test_spärr2_private_calendar_booking_stops_work(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    ev = M.CalendarEvent(user_id=user.id, external_id="x1", subject="Tandläkare", sensitivity="private",
                         start_at=t.ended_at, end_at=t.ended_at + timedelta(hours=1))
    db.add(ev)
    db.flush()
    db.add(M.CalendarMatch(logical_trip_id=t.id, calendar_event_id=ev.id, relation="arrival", score=0.9,
                           time_score=0.9, geo_score=0.9, geo_basis="address", is_selected=True))
    db.flush()
    assert decide(db, user, t).outcome == "ask"
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


def test_spärr2_hotel_never_automatic_work_by_history(db, user, vehicle, trips, auto, locs):
    history(trips, ACME, HOTEL)
    t = live(trips, ACME, HOTEL, km=20.0, start_at=MON + timedelta(days=3))
    assert decide(db, user, t).outcome == "ask"


# ----------------------------------------------------------------------------- spärr 1


def test_spärr1_possible_missed_trips_asks_without_work_shortcut(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                             purpose="Service av maskin")
    history(trips, HOME, ACME)
    patterns.rebuild(db, user.id)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3), possible_missed_trips=True)
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.reason == "Kan vara flera körningar" and d.candidates == ()


def test_spärr1_missing_odometer(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["school"].id, category=Category.PRIVATE,
                             purpose=None)
    t = live(trips, ACME, SCHOOL)
    raw = seg.ordered_raws(t)[0]
    raw.odometer_end = None
    seg.refresh_logical(db, t)
    db.flush()
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.reason == "Mätarställning saknas"


def test_spärr1_mixed_trip(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["school"].id, category=Category.PRIVATE,
                             purpose=None)
    t = live(trips, ACME, SCHOOL, km=10.0)
    raw = seg.ordered_raws(t)[0]
    seg.allocate_raw_trip(db, t, raw.id, [{"distance_km": 6.0, "category": Category.WORK},
                                          {"distance_km": 4.0, "category": Category.PRIVATE}], "test")
    db.flush()
    t = reload_trip(db, t.id)
    assert t.has_mixed_categories
    assert decide(db, user, t).outcome == "ask"


def test_spärr1_ask_always_rule_beats_history(db, user, vehicle, trips, auto, locs):
    history(trips, HOME, ACME)
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.UNDECIDED,
                             purpose=None)
    t = live(trips, HOME, ACME, km=20.0, start_at=MON + timedelta(days=3))
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.reason == "Din regel: fråga alltid"


def test_spärr1_calendar_conflict():
    class Rules:
        calendar_conflict = True
        signals: list = []
        caps: list = []

    class Trip:
        quality_flags: list = []
        odometer_start, odometer_end = 1.0, 2.0
        has_mixed_categories = False

    assert autopilot._hard_block(Trip(), Rules()) == "Kalendern säger emot bilens uppgifter"


def test_wait_while_address_is_looked_up(db, user, vehicle, trips, auto, locs):
    loc = locs["school"]
    loc.geocode_status = "pending"
    loc.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db.flush()
    t = live(trips, ACME, SCHOOL)
    assert decide(db, user, t).outcome == "wait"
    loc.created_at = datetime.now(UTC) - timedelta(minutes=31)
    db.flush()
    assert decide(db, user, t).outcome == "ask"


# ----------------------------------------------------------------------------- AUTO blir PROPOSED


def _strong_customer_trip(db, user, trips):
    settings_svc.update_section(db, user.id, "ai", {"auto_threshold": 0.6, "review_threshold": 0.5})
    return live(trips, HOME, ACME)


def test_engine_auto_becomes_proposed_in_self_driving_mode(db, user, vehicle, trips, auto, locs):
    t = _strong_customer_trip(db, user, trips)
    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM, use_llm=False)
    assert a.review_status == ReviewStatus.PROPOSED
    assert reload_trip(db, t.id).review_status == ReviewStatus.PROPOSED


def test_engine_auto_unchanged_in_manual_mode(db, user, vehicle, trips, locs):
    t = _strong_customer_trip(db, user, trips)
    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM, use_llm=False)
    assert a.review_status == ReviewStatus.AUTO


def test_ask_turns_auto_into_proposed(db, user, vehicle, trips, auto, locs):
    t = live(trips, ACME, SCHOOL, category=Category.WORK, review_status=ReviewStatus.AUTO)
    assert autopilot.apply(db, t, autopilot.AutoDecision("ask"), actor=audit.SYSTEM)
    assert t.review_status == ReviewStatus.PROPOSED
    created = pipeline.sync_trip_review_items(db, t)
    assert ReviewKind.CATEGORY in created


def test_self_driving_pipeline_does_not_enqueue_live_questions(db, user, vehicle, trips, auto, locs, monkeypatch):
    from app.services.ha import notifier

    seen: list = []
    monkeypatch.setattr(notifier, "enqueue_for_changes", lambda db, **kw: seen.append(kw["trip_items"]))
    live(trips, ACME, SCHOOL)
    trips.trip(BETA, SCHOOL, source=TripSource.MANUAL, start_at=MON + timedelta(days=2))
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, use_llm=False)
    assert seen and all(t.source == TripSource.MANUAL for t, _ in seen[0])


# ----------------------------------------------------------------------------- sammanslagning


def _merge_cfg(db, user):
    return settings_svc.get_section(db, user.id, "merge")


def test_self_driving_does_not_merge_or_ask_at_ordinary_stop(db, user, vehicle, trips, auto, locs):
    live(trips, HOME, ACME, minutes=20)
    live(trips, ACME, BETA, minutes=20, gap_minutes=5)
    res = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=_merge_cfg(db, user), actor=audit.SYSTEM)
    assert res["auto_merged"] == 0 and res["questions"] == []
    assert len(seg.active_trips(db, vehicle.id)) == 2


def test_self_driving_merges_short_stop_at_confirmed_fuel_station(db, user, vehicle, trips, auto, locs):
    live(trips, HOME, FUEL, minutes=20)
    live(trips, FUEL, ACME, minutes=20, gap_minutes=8)
    res = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=_merge_cfg(db, user), actor=audit.SYSTEM)
    assert res["auto_merged"] == 1
    assert len(seg.active_trips(db, vehicle.id)) == 1


def test_stop_kind_ignores_map_suggestions(db, user, vehicle, trips, auto, locs):
    loc = trips.location(SHOP, name="Mack?", type=LocationType.UNKNOWN, confirmed=False)
    loc.suggested_type = LocationType.FUEL_STATION
    loc.osm_class = "amenity=fuel"
    loc.place_kind = "never"
    db.flush()
    assert merge.stop_kind(loc) == "other"
    live(trips, HOME, SHOP, minutes=20)
    live(trips, SHOP, ACME, minutes=20, gap_minutes=5)
    res = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=_merge_cfg(db, user), actor=audit.SYSTEM)
    assert res["auto_merged"] == 0


@pytest.mark.parametrize("stop", [HOME, "private"])
def test_stop_at_home_or_private_place_never_merges_in_any_mode(db, user, vehicle, trips, locs, stop):
    if stop == "private":
        trips.location(SHOP, name="Gymmet", type=LocationType.PRIVATE)
        stop = SHOP
    live(trips, ACME, stop, minutes=20)
    live(trips, stop, BETA, minutes=20, gap_minutes=5)
    res = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=_merge_cfg(db, user), actor=audit.SYSTEM)
    assert res["auto_merged"] == 0 and res["questions"] == []
    assert len(seg.active_trips(db, vehicle.id)) == 2


def test_manual_mode_still_merges_live_trips_at_ordinary_stop(db, user, vehicle, trips, locs):
    live(trips, HOME, ACME, minutes=20)
    live(trips, ACME, BETA, minutes=20, gap_minutes=5)
    res = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=_merge_cfg(db, user), actor=audit.SYSTEM)
    assert res["auto_merged"] == 1


def test_private_stop_on_the_way_home_example(db, user, vehicle, trips, auto, locs):
    """A.4 "Privat stopp på vägen hem": två resor, fråga, "Alltid privat här" ger P för nästa."""
    t1 = live(trips, ACME, SCHOOL, minutes=20, start_at=MON + timedelta(hours=8))
    t2 = live(trips, SCHOOL, HOME, minutes=10, gap_minutes=8)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False,
                                 use_llm=False)
    assert {t.id for t in seg.active_trips(db, vehicle.id)} == {t1.id, t2.id}  # inget slås ihop
    assert db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.MERGE)).first() is None
    t1 = reload_trip(db, t1.id)
    assert t1.review_status != ReviewStatus.APPROVED
    assert autopilot.can_remember(db, t1, None)
    feedback.apply_decision(db, t1, user_id=user.id, actor=audit.user_actor(user.id), category=Category.PRIVATE)
    autopilot.remember_always(db, user_id=user.id, trip=reload_trip(db, t1.id), category=Category.PRIVATE,
                              purpose=None, actor=audit.user_actor(user.id))
    school = db.get(M.KnownLocation, locs["school"].id)
    assert school.type == LocationType.PRIVATE and school.confirmed
    t2 = reload_trip(db, t2.id)
    assert (t2.category, t2.approval_source) == (Category.PRIVATE, ApprovalSource.PRIVATE_PLACE)


def test_merge_keeps_weakest_approval_source(db, user, vehicle, trips, actor, locs):
    a = owner_work(trips, ACME, BETA, minutes=20)
    b = owner_work(trips, BETA, HOTEL, minutes=20, gap_minutes=5)
    a.approval_source, b.approval_source = ApprovalSource.HISTORY, ApprovalSource.ASSUMED
    b.approval_basis = {"pattern_id": None, "related_trip_id": None, "reason": "Antaget kundbesök",
                        "veto_until": "2026-09-15T09:00:00+00:00"}
    b.auto_approved_at = MON
    db.flush()
    merged = merge.merge_trips(db, [a, b], trigger="manual", actor=actor)
    assert merged.review_status == ReviewStatus.APPROVED
    assert merged.approval_source == ApprovalSource.ASSUMED
    assert merged.approval_basis["veto_until"] == "2026-09-15T09:00:00+00:00"
    left, right = merge.split_trip(db, seg.load_trip(db, merged.id), after_index=0, actor=actor)
    assert left.approval_source == right.approval_source == ApprovalSource.ASSUMED
    assert left.auto_approved_at == right.auto_approved_at == MON


def test_merge_user_and_bulk_gives_bulk_and_dissolve_restores(db, user, vehicle, trips, actor, locs):
    a = owner_work(trips, ACME, BETA, minutes=20)
    b = owner_work(trips, BETA, HOTEL, minutes=20, gap_minutes=5)
    a.approval_source, b.approval_source = ApprovalSource.USER, ApprovalSource.BULK
    db.flush()
    merged = merge.merge_trips(db, [a, b], trigger="manual", actor=actor)
    assert merged.approval_source == ApprovalSource.BULK
    restored = merge.dissolve(db, seg.load_trip(db, merged.id), actor=actor)
    assert {t.approval_source for t in restored} == {ApprovalSource.USER, ApprovalSource.BULK}


def test_unapproved_merge_has_no_approval_source(db, user, vehicle, trips, actor, locs):
    a = owner_work(trips, ACME, BETA, minutes=20)
    a.approval_source = ApprovalSource.HISTORY
    b = trips.trip(BETA, HOTEL, category=Category.WORK, review_status=ReviewStatus.PROPOSED, minutes=20,
                   gap_minutes=5)
    db.flush()
    merged = merge.merge_trips(db, [a, b], trigger="manual", actor=actor)
    assert merged.review_status == ReviewStatus.PROPOSED and merged.approval_source is None


# ----------------------------------------------------------------------------- Excel: Privat stopp


def test_excel_writes_private_stop_for_home_and_private_places(db, user, vehicle, trips, actor, locs):
    a = owner_work(trips, ACME, HOME, minutes=20, purpose="Kundbesök")
    b = owner_work(trips, HOME, BETA, minutes=20, gap_minutes=5, purpose="Kundbesök")
    merged = merge.merge_trips(db, [a, b], trigger="manual", actor=actor)
    c = owner_work(trips, BETA, OFFICE, minutes=20, gap_minutes=120, purpose="Leverans")
    d = owner_work(trips, OFFICE, ACME, minutes=20, gap_minutes=5, purpose="Leverans")
    merged2 = merge.merge_trips(db, [c, d], trigger="manual", actor=actor)
    journal = simple.build_journal(vehicle=vehicle, trips=[reload_trip(db, merged.id), reload_trip(db, merged2.id)],
                                   start=None, end=None, tz=TZ)
    rows = {r.trip_id: r for r in journal.rows}
    assert rows[merged.id].via == ("Privat stopp",)
    assert HOME not in rows[merged.id].to_text
    assert rows[merged2.id].via == (f"Kontoret, {OFFICE}",)  # övriga mellanstopp: namn och adress (1.2.1)


# ----------------------------------------------------------------------------- "Alltid" och knappar


def test_can_remember_rules(db, user, vehicle, trips, auto, locs):
    to_home = live(trips, ACME, HOME)
    to_hotel = live(trips, ACME, HOTEL)
    to_office = live(trips, ACME, OFFICE)
    to_acme = live(trips, HOME, ACME)
    for t in (to_home, to_hotel, to_office):
        assert not autopilot.can_remember(db, t, None)
    assert autopilot.can_remember(db, to_acme, None)
    assert autopilot.can_remember(db, to_acme, "Kundbesök Acme AB")
    assert not autopilot.can_remember(db, to_acme, "Service av maskin")  # aldrig bekräftat här
    assert not autopilot.can_remember(db, to_acme, "x" * 41)
    owner_work(trips, BETA, ACME, purpose="Service av maskin", start_at=MON + timedelta(days=5))
    assert autopilot.can_remember(db, to_acme, "Service av maskin")
    # Ett ärende som bara automatiken godkänt räknas inte.
    auto_trip = owner_work(trips, BETA, ACME, purpose="Leverans av delar", start_at=MON + timedelta(days=6))
    auto_trip.approval_source = ApprovalSource.HISTORY
    db.flush()
    assert not autopilot.can_remember(db, to_acme, "Leverans av delar")


def test_remember_always_work_creates_rule_and_decides_waiting_trips(db, user, vehicle, trips, auto, locs):
    t1 = live(trips, HOME, ACME, start_at=MON)
    t2 = live(trips, BETA, ACME, start_at=MON + timedelta(days=1))
    feedback.apply_decision(db, t1, user_id=user.id, actor=audit.user_actor(user.id), category=Category.WORK,
                            purpose="Kundbesök Acme AB")
    pat = autopilot.remember_always(db, user_id=user.id, trip=reload_trip(db, t1.id), category=Category.WORK,
                                    purpose="Kundbesök Acme AB", actor=audit.user_actor(user.id))
    assert pat.is_manual_rule and pat.manual_purpose == "Kundbesök Acme AB" and pat.origin_location_id is None
    t2 = reload_trip(db, t2.id)
    assert (t2.approval_source, t2.purpose) == (ApprovalSource.RULE, "Kundbesök Acme AB")
    with pytest.raises(ValueError):
        autopilot.remember_always(db, user_id=user.id, trip=live(trips, ACME, HOME), category=Category.PRIVATE,
                                  purpose=None, actor=audit.user_actor(user.id))


def test_purpose_candidates_from_owner_history(db, user, vehicle, trips, auto, locs):
    for i, p in enumerate(("Service av maskin", "Service av maskin", "Leverans av delar")):
        owner_work(trips, HOME, ACME, purpose=p, start_at=MON + timedelta(days=i))
    patterns.rebuild(db, user.id)
    t = live(trips, HOME, ACME, start_at=MON + timedelta(days=4))
    assert autopilot.purpose_candidates(db, t, user_id=user.id) == ["Service av maskin", "Leverans av delar"]


def test_automatic_trip_is_not_touched_again(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, OFFICE)
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    stamp = t.auto_approved_at
    assert run(db, user, vehicle) == {}
    assert reload_trip(db, t.id).auto_approved_at == stamp
    assert t.state == LogicalState.ACTIVE


def test_reconsider_decides_trips_when_address_is_ready(db, user, vehicle, trips, auto, locs):
    loc = locs["school"]
    loc.geocode_status = "pending"
    loc.created_at = datetime.now(UTC)
    db.flush()
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=loc.id, category=Category.PRIVATE, purpose=None)
    t = live(trips, ACME, SCHOOL)
    assert run(db, user, vehicle) == {"wait": 1}
    loc.geocode_status = "ok"
    db.flush()
    autopilot.reconsider(db, user_id=user.id, trip_ids=[t.id], actor=audit.SYSTEM)
    assert reload_trip(db, t.id).approval_source == ApprovalSource.RULE
