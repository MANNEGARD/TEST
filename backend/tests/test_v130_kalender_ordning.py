"""1.3.0 – kalendern + AI först för ärendets text (tilläggskontraktet 3.E), ärendet ur kalendern
utan fråga (beslut A) och "Ärendet stämmer inte", hela vistelsen (3.D) och resa dagen före (beslut J)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import ApprovalSource, Category, DecisionSource, ReviewKind, ReviewState, ReviewStatus
from app.services import settings as settings_svc
from app.services.classify import signals as sig
from app.services.export import month_close
from app.services.learning import patterns
from app.services.trips import pipeline
from tests.conftest import reload_trip
from tests.v130_ai_stod import (
    CUSTOMER,
    HBG_OFFICE,
    HOME,
    JKPG_OFFICE,
    OTHER_CITY,
    TZ,
    FakeProvider,
    answer,
    attendee,
    classify,
    current_purpose,
    decide,
    event,
    full_context,
    live,
    local,
    match,
    places,
    run,
    self_driving,
)

API = "/api/v1"
RULE_TEXT = "Service hos Exempel Åkeri AB"
LENNART = attendee("Lennart Exempel", "lennart@exempelakeri.se")


def echo(doc):
    """Ekar rubriken på den starkt matchade bokningen (eller inget ärende)."""
    for ev in doc.get("calendar_day") or []:
        matched = ev.get("matched_to_this_trip") or {}
        if matched.get("strength") == "strong" and not ev["marked_private"]:
            return answer(ev["subject"])
    return answer(None)


@pytest.fixture()
def world(db, user, trips):
    full_context(db, user)
    settings_svc.update_section(db, user.id, "calendar", {"work_domain": "exempelakeri.se"})
    locs = places(trips)
    pat = patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["customer"].id,
                                   category=Category.WORK, purpose=RULE_TEXT)
    db.flush()
    locs["rule"] = pat
    return locs


def visit(db, user, trips, day: int, subject: str | None, **ev_kw):
    t = live(trips, HOME, CUSTOMER, km=40, start_at=local(2026, 9, day, 8, 0))
    if subject:
        ev_kw.setdefault("location_city", "Verkstadsby")
        event(db, user, subject, t.ended_at + timedelta(minutes=10), **ev_kw)
    match(db, user, t)
    return t


# ----------------------------------------------------------------------------- ordningen


def test_same_route_three_days_three_bookings_three_purposes(db, user, trips, world):
    fake = FakeProvider(echo)
    texts = []
    for day, subject in ((15, "Offertgenomgång med Lennart"), (16, "Leveransplanering"), (17, "Avtalsuppföljning")):
        t = visit(db, user, trips, day, subject, attendees=[LENNART])
        classify(db, user, t, provider=fake)
        t = reload_trip(db, t.id)
        p = current_purpose(db, t)
        assert p.source == DecisionSource.AI and p.based_on["calendar"] == "strong" and p.needs_confirmation
        texts.append(t.purpose)
    assert texts == ["Offertgenomgång med Lennart.", "Leveransplanering.", "Avtalsuppföljning."]


def test_no_booking_gives_rule_text(db, user, trips, world):
    t = visit(db, user, trips, 15, None)
    fake = FakeProvider(echo)
    classify(db, user, t, provider=fake)
    assert reload_trip(db, t.id).purpose == RULE_TEXT and fake.documents == []  # fast regeltext: ingen AI


def test_rule_in_always_mode_wins_over_booking(db, user, trips, world):
    world["rule"].manual_purpose_mode = "always"
    db.flush()
    t = visit(db, user, trips, 15, "Offertgenomgång")
    classify(db, user, t, provider=FakeProvider(echo))
    assert reload_trip(db, t.id).purpose == RULE_TEXT


def test_private_booking_is_never_purpose_and_only_time_is_sent(db, user, trips, world):
    world["rule"].is_manual_rule = False
    db.flush()
    t = visit(db, user, trips, 15, "Tandläkare Doktor Exempelsson", sensitivity="private")
    fake = FakeProvider(echo)
    classify(db, user, t, provider=fake)
    t = reload_trip(db, t.id)
    assert "Tandläkare" not in (t.purpose or "")
    (ev,) = fake.documents[-1]["calendar_day"]
    assert ev["subject"] == "Privat bokning" and ev["marked_private"] and ev["body"] is None
    assert "Exempelsson" not in str(fake.documents[-1])


def test_weak_match_is_unsupported_proposal(db, user, trips, world):
    world["rule"].is_manual_rule = False
    world["customer"].type = "customer"
    db.flush()
    t = live(trips, HOME, CUSTOMER, km=40, start_at=local(2026, 9, 15, 8, 0))
    event(db, user, "Avstämning", t.ended_at + timedelta(minutes=10))  # bara tiden stämmer
    (m,) = match(db, user, t)
    assert m.geo_basis == "no_location"
    classify(db, user, t, provider=FakeProvider(answer("Avstämning hos Exempel Åkeri AB")))
    t = reload_trip(db, t.id)
    p = current_purpose(db, t)
    assert p.based_on["calendar"] == "weak" and p.based_on["supported"] is False and not t.purpose_confirmed


def test_conflict_gives_no_calendar_text(db, user, trips, world):
    world["rule"].is_manual_rule = False
    db.flush()
    t = live(trips, HOME, CUSTOMER, km=40, start_at=local(2026, 9, 15, 8, 0))
    event(db, user, "Workshop Karlstad", t.ended_at + timedelta(minutes=10), location_name="Konferens Karlstad",
          location_city="Karlstad")
    rows = match(db, user, t)
    assert any(r.is_conflict for r in rows)
    classify(db, user, t, provider=FakeProvider(echo))
    assert "Workshop" not in (reload_trip(db, t.id).purpose or "")


def test_ai_down_gives_rule_calendar_proposal(db, user, trips, world):
    t = visit(db, user, trips, 15, "Offertgenomgång")
    classify(db, user, t, provider=FakeProvider(fail=True))
    t = reload_trip(db, t.id)
    assert t.purpose == "Offertgenomgång hos Exempel Åkeri AB."
    assert current_purpose(db, t).based_on == {"calendar_event_id": current_purpose(db, t).based_on["calendar_event_id"],
                                                "calendar": "strong"}


def test_ai_invents_name_then_next_step(db, user, trips, world):
    t = visit(db, user, trips, 15, "Offertgenomgång", attendees=[LENNART])
    a = classify(db, user, t, provider=FakeProvider(answer("Offertgenomgång med Lennart och Påhittsson")))
    assert any(s.kind == "ai_purpose_rejected" for s in a.signals)
    assert reload_trip(db, t.id).purpose == "Offertgenomgång hos Exempel Åkeri AB."


# ----------------------------------------------------------------------------- beslut A och "stämmer inte"


def test_strong_booking_with_rule_sets_confirmed_calendar_purpose(client, db, user, vehicle, trips, world):
    self_driving(db, user)
    t = visit(db, user, trips, 15, "Offertgenomgång med Lennart", attendees=[LENNART])
    classify(db, user, t, provider=FakeProvider(echo))
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert (t.review_status, t.approval_source) == (ReviewStatus.APPROVED, ApprovalSource.RULE)
    assert t.purpose == "Offertgenomgång med Lennart." and t.purpose_confirmed
    p = current_purpose(db, t)
    assert p.based_on["from_calendar"] is True and p.based_on["calendar_event_ids"]
    status = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=date(2026, 9, 1),
                                  now=datetime(2026, 10, 2, 8, 0, tzinfo=TZ))
    note = next(n for n in status.notes if n.code == "purpose_from_calendar")
    assert note.text == "Ärende ur kalendern: 1 resa" and note.trip_ids == [str(t.id)]

    db.commit()
    r = client.post(f"{API}/trips/{t.id}/purpose/reject-calendar")
    assert r.status_code == 200, r.text
    t = reload_trip(db, t.id)
    assert t.purpose == RULE_TEXT and not t.purpose_confirmed and t.review_status == ReviewStatus.APPROVED
    rows = db.execute(select(M.CalendarMatch).where(M.CalendarMatch.logical_trip_id == t.id)).scalars().all()
    assert all(not m.is_selected and m.decided_by == "user" for m in rows)
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == t.id,
                                                 M.ReviewItem.kind == ReviewKind.PURPOSE)).scalar_one()
    assert item.status == ReviewState.OPEN
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.calendar_purpose_rejected")).first()
    again = client.post(f"{API}/trips/{t.id}/purpose/reject-calendar")
    assert again.status_code == 400 and again.json()["code"] == "not_from_calendar"


def test_k_with_strong_booking_uses_calendar_purpose(db, user, vehicle, trips, world):
    self_driving(db, user)
    settings_svc.update_section(db, user.id, "automation", {"home_office_is_work": True})
    t = live(trips, HOME, JKPG_OFFICE, km=150, minutes=100, start_at=local(2026, 9, 15, 7, 0))
    event(db, user, "Intervjuer med Lennart", t.ended_at + timedelta(minutes=20), location_name="Jönköpingskontoret",
          attendees=[LENNART])
    match(db, user, t)
    d = decide(db, user, t)
    assert d.source == ApprovalSource.HOME_OFFICE and d.purpose == "Intervjuer med Lennart på Jönköpingskontoret."
    assert d.calendar_event_ids


# ----------------------------------------------------------------------------- vistelsen och dagen före


def test_two_day_stay_gives_summary_on_out_and_home(db, user, vehicle, trips, world):
    out = live(trips, HOME, JKPG_OFFICE, km=150, minutes=100, start_at=local(2026, 9, 14, 7, 0))
    back = live(trips, JKPG_OFFICE, HOME, km=150, minutes=100, start_at=local(2026, 9, 15, 16, 0))
    event(db, user, "Intervjuer", local(2026, 9, 14, 10, 0), location_name="Jönköpingskontoret",
          attendees=[LENNART])
    event(db, user, "Kundbesök i JKPG", local(2026, 9, 15, 9, 0), attendees=[LENNART])
    other = event(db, user, "Workshop", local(2026, 9, 15, 13, 0), location_name="Konferens Karlstad",
                  location_city="Karlstad")
    for t in (out, back):
        match(db, user, t)

    def summary(doc):
        stay = doc["calendar_stay"]
        assert stay["place"] == "Jönköping" and stay["basis"] == "hemresa känd"
        near = {e["subject"]: e["near_destination"] for e in stay["events"]}
        assert near == {"Intervjuer": True, "Kundbesök i JKPG": True, "Workshop": False}
        if doc["destination"]["name"] == "Hemmet":
            return answer("Hemresa från Jönköping efter intervjuer och kundbesök med Lennart")
        return answer("Rest till Jönköping för intervjuer och kundbesök med Lennart")

    fake = FakeProvider(summary)
    classify(db, user, out, provider=fake)
    classify(db, user, back, provider=fake)
    assert reload_trip(db, out.id).purpose == "Rest till Jönköping för intervjuer och kundbesök med Lennart."
    assert reload_trip(db, back.id).purpose == "Hemresa från Jönköping efter intervjuer och kundbesök med Lennart."
    assert len(fake.documents) == 2 and other


def test_no_bookings_on_stay_follows_ordinary_path(db, user, trips, world):
    out = live(trips, HOME, JKPG_OFFICE, km=150, minutes=100, start_at=local(2026, 9, 14, 7, 0))
    fake = FakeProvider(answer(None))
    classify(db, user, out, provider=fake)
    assert fake.documents[-1]["calendar_stay"] is None and fake.documents[-1]["calendar_ahead"] is None


def test_sunday_trip_with_bookings_ahead(db, user, vehicle, trips, world):
    t = live(trips, HOME, HBG_OFFICE, km=250, minutes=160, start_at=local(2026, 9, 20, 17, 0))
    event(db, user, "Möte HBG med Lennart", local(2026, 9, 21, 9, 0), attendees=[LENNART])
    event(db, user, "Kundträff Helsingborg", local(2026, 9, 22, 9, 0))
    match(db, user, t)
    ctx = sig.build_context(db, reload_trip(db, t.id), user_id=user.id, tz=TZ,
                            tax=settings_svc.get_section(db, user.id, "tax"),
                            ai=settings_svc.get_section(db, user.id, "ai"),
                            abbreviations=settings_svc.get_section(db, user.id, "abbreviations")["map"])
    assert ctx.calendar_ahead is not None and len(ctx.calendar_ahead.usable) == 2
    rules = sig.evaluate(ctx)
    s = next(s for s in rules.signals if s.kind == "calendar_ahead")
    assert s.label == "Bokningar på Helsingborg de närmaste dagarna" and s.direction == Category.WORK
    assert sig.calendar_support(ctx) == "strong"
    fake = FakeProvider(answer("Rest till Helsingborg inför möte med Lennart på måndag"))
    classify(db, user, t, provider=fake)
    assert fake.documents[-1]["calendar_ahead"]["place"] == "Helsingborg"
    assert reload_trip(db, t.id).purpose == "Rest till Helsingborg inför möte med Lennart på måndag."


def test_sunday_without_bookings_k_or_question(db, user, vehicle, trips, world):
    self_driving(db, user)
    settings_svc.update_section(db, user.id, "automation", {"home_office_is_work": True})
    t = live(trips, HOME, HBG_OFFICE, km=250, minutes=160, start_at=local(2026, 9, 20, 17, 0))
    assert decide(db, user, t).source == ApprovalSource.HOME_OFFICE
    t2 = live(trips, HOME, OTHER_CITY, km=250, minutes=160, start_at=local(2026, 9, 27, 17, 0))
    assert decide(db, user, t2).outcome == "ask"
    pipeline.sync_trip_review_items(db, reload_trip(db, t2.id))
