"""Granskningen av 1.1.0, området "kärna": varje test prövar ett fynd som rättats."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, LocationType, ReviewStatus, TripSource
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.export import links, month_close
from app.services.export import service as export_svc
from app.services.learning import feedback
from tests.conftest import reload_trip
from tests.test_s1_adresser import fast_clock, no_dns  # noqa: F401  (fixturerna)

ACTIVE_SINCE = "2020-01-01T00:00:00+00:00"
HOME = "Hemvägen 1, 111 11 Exempelstad"
OFFICE = "Kontorsgatan 2, 111 22 Exempelstad"
ACME = "Industrivägen 10, 222 33 Verkstadsby"
LAGER = "Lagervägen 12, 222 33 Verkstadsby"
EMPL = "Fabriksvägen 99, 111 99 Exempelstad"
TUE_1015 = datetime(2026, 9, 15, 7, 55, tzinfo=UTC)
SEP = date(2026, 9, 1)


@pytest.fixture()
def auto(db, user):
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    db.flush()


def _place(trips, address, *, name, kind="work", osm="building=warehouse", company=None):
    loc = trips.location(address, name=name, type=LocationType.UNKNOWN, confirmed=False, company=company)
    loc.place_kind, loc.osm_class, loc.geocode_status = kind, osm, "ok"
    loc.created_from = "api"
    trips.db.flush()
    return loc


@pytest.fixture()
def locs(trips):
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "office": trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE),
        "acme": trips.location(ACME, name="Acme AB", type=LocationType.CUSTOMER, company="Acme AB"),
        "dagab": _place(trips, LAGER, name="Dagab", company="Dagab"),
    }


def live(trips, start, end, **kw):
    return trips.trip(start, end, source=TripSource.VOLVO_API, **kw)


def run(db, user, vehicle):
    out = autopilot.run_vehicle(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM)
    db.flush()
    return out


def owner(user):
    return audit.user_actor(user.id)


def decide(db, user, trip, **kw):
    feedback.apply_decision(db, reload_trip(db, trip.id), user_id=user.id, actor=owner(user), **kw)
    db.flush()


def open_items(db, trip_id):
    return db.execute(select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == trip_id,
                                                 M.ReviewItem.status == "open")).scalars().all()


# 1 · Hemresan (T) följer utresan -------------------------------------------------------------
def test_return_trip_reopened_when_outbound_corrected_to_private(db, user, vehicle, trips, auto, locs):
    out = live(trips, HOME, ACME, km=30.0, start_at=TUE_1015)
    back = live(trips, ACME, HOME, km=30.0, start_at=TUE_1015 + timedelta(hours=3))
    decide(db, user, out, category=Category.WORK, purpose="Service av maskin", approve=True)
    assert reload_trip(db, back.id).approval_source == "return_trip"
    decide(db, user, out, category=Category.PRIVATE, approve=True)
    b = reload_trip(db, back.id)
    assert b.review_status != ReviewStatus.APPROVED and b.approval_source is None
    assert open_items(db, back.id)
    tz = settings_svc.user_timezone(db, user.id)
    journal, _ = export_svc.build_simple_journal(
        db, vehicle=vehicle, start=datetime(2026, 8, 31, 22, tzinfo=UTC), end=datetime(2026, 9, 30, 22, tzinfo=UTC),
        tz=tz)
    assert journal.rows == []
    st = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP, now=datetime(2026, 10, 2, 10, tzinfo=UTC))
    assert st.state != "exported"


def test_return_trip_reopened_after_rule_outbound_corrected(db, user, vehicle, trips, auto, locs):
    from app.services.learning import patterns
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["acme"].id, category=Category.WORK,
                             purpose="Kundbesök Acme AB")
    out = live(trips, HOME, ACME, km=30.0, start_at=TUE_1015)
    back = live(trips, ACME, HOME, km=30.0, start_at=TUE_1015 + timedelta(hours=3))
    run(db, user, vehicle)
    assert reload_trip(db, back.id).approval_source == "return_trip"
    decide(db, user, out, category=Category.PRIVATE, approve=True)
    b = reload_trip(db, back.id)
    assert b.approval_source is None and b.review_status != ReviewStatus.APPROVED


def test_return_purpose_follows_changed_outbound_purpose(db, user, vehicle, trips, auto, locs):
    out = live(trips, HOME, ACME, km=30.0, start_at=TUE_1015)
    back = live(trips, ACME, HOME, km=30.0, start_at=TUE_1015 + timedelta(hours=3))
    decide(db, user, out, category=Category.WORK, purpose="Service av maskin", approve=True)
    decide(db, user, out, purpose="Möte med styrelsen")
    b = reload_trip(db, back.id)
    # Antingen frågas den om igen, eller så godkänns den på nytt med rätt ärende.
    assert b.purpose is None or "service av maskin" not in b.purpose.lower()
    if b.review_status == ReviewStatus.APPROVED:
        assert "styrelsen" in b.purpose.lower()
    run(db, user, vehicle)
    b = reload_trip(db, back.id)
    assert b.review_status != ReviewStatus.APPROVED or "styrelsen" in (b.purpose or "").lower()


# 2 · "Fel" på en antagen resa öppnar även antagna rader i en exporterad månad ----------------
def test_wrong_on_assumption_reopens_exported_month_and_replaces_file(db, user, vehicle, trips, auto, locs):
    sep = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015)
    run(db, user, vehicle)
    sep = reload_trip(db, sep.id)
    assert sep.approval_source == "assumed"
    sep.approval_basis = {**sep.approval_basis, "veto_until": "2026-09-16T10:00:00+00:00"}
    db.flush()
    st = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP, now=datetime(2026, 10, 2, 10, tzinfo=UTC))
    assert st.state == "exported", [b.code for b in st.blockers]
    job1 = db.get(M.ExportJob, st.export_id)

    octt = live(trips, HOME, LAGER, km=23.0, start_at=datetime(2026, 10, 13, 7, 55, tzinfo=UTC))
    run(db, user, vehicle)
    assert reload_trip(db, octt.id).approval_source == "assumed"
    decide(db, user, octt, category=Category.PRIVATE, approve=True)
    mc = db.execute(select(M.MonthClose).where(M.MonthClose.month == "2026-09")).scalar_one()
    assert mc.state == "stale"
    sep = reload_trip(db, sep.id)
    assert sep.review_status != ReviewStatus.APPROVED and sep.approval_source is None
    assert open_items(db, sep.id)
    # Medan frågan väntar skapas ingen ny fil.
    st2 = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP,
                              now=datetime(2026, 10, 14, 12, tzinfo=UTC))
    assert st2.export_id == job1.id and any(b.code == "open_questions" for b in st2.blockers)
    # Ägaren svarar: det var en tjänsteresa ändå. Då ersätts filen.
    decide(db, user, sep, category=Category.WORK, purpose="Leverans till Dagab", approve=True)
    st3 = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP,
                              now=datetime(2026, 10, 15, 12, tzinfo=UTC))
    assert st3.state == "exported" and st3.export_id != job1.id
    db.refresh(job1)
    assert job1.superseded_by_id == st3.export_id


# 5 · Sista tjänsteresan rättas till privat efter exporten ---------------------------------------
def test_last_work_trip_corrected_after_export_revokes_old_file(db, user, vehicle, trips, auto, locs):
    t = trips.trip(HOME, ACME, category=Category.WORK, purpose="Service av maskin", source=TripSource.VOLVO_API,
                   km=30.0, start_at=TUE_1015)
    now = datetime(2026, 10, 2, 10, tzinfo=UTC)
    st = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP, now=now)
    assert st.state == "exported", [b.code for b in st.blockers]
    job = db.get(M.ExportJob, st.export_id)
    token = links.make_token(job, user, now + timedelta(days=7))
    decide(db, user, t, category=Category.PRIVATE, approve=True)
    states = [month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP, now=now + timedelta(hours=h)).state
              for h in (1, 3)]
    assert states[-1] == "empty"
    mc = db.execute(select(M.MonthClose).where(M.MonthClose.month == "2026-09")).scalar_one()
    assert mc.state == "empty" and mc.export_id is None
    db.refresh(job)
    assert job.superseded_by_id is not None
    with pytest.raises(links.LinkExpired):
        links.verify(db, token, now + timedelta(hours=60))
    # Senare kontroller lämnar månaden tom.
    st3 = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=SEP, now=now + timedelta(hours=50))
    assert st3.state == "empty"


# 3 · Arbetsgivarens egen byggnad antas inte vara en kund -------------------------------------
def test_employer_named_place_is_not_assumed_customer(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "general", {"employer_name": "Verkstads AB"})
    _place(trips, EMPL, name="Verkstads AB", company="Verkstads AB Lager 2", osm="office=company")
    t = live(trips, HOME, EMPL, km=12.0, start_at=datetime(2026, 9, 15, 5, 40, tzinfo=UTC))
    back = live(trips, EMPL, HOME, km=12.0, start_at=datetime(2026, 9, 15, 14, 30, tzinfo=UTC))
    run(db, user, vehicle)
    for trip in (reload_trip(db, t.id), reload_trip(db, back.id)):
        assert trip.approval_source is None and trip.review_status != ReviewStatus.APPROVED


def test_place_near_regular_workplace_is_not_assumed(db, user, vehicle, trips, auto, locs):
    locs["office"].latitude, locs["office"].longitude = 59.3300, 18.0600
    p = _place(trips, EMPL, name="Grannbolaget", company="Grannbolaget", osm="office=company")
    p.latitude, p.longitude = 59.3330, 18.0650  # ca 450 m bort
    db.flush()
    t = live(trips, HOME, EMPL, km=12.0, start_at=TUE_1015)
    d = autopilot.decide(db, reload_trip(db, t.id), user_id=user.id, ctx=None, rules=None, cfg=None)
    assert d.outcome != "approve"
    # Längre bort än 1 km antas platsen som vanligt.
    p.latitude = 59.36
    db.flush()
    d = autopilot.decide(db, reload_trip(db, t.id), user_id=user.id, ctx=None, rules=None, cfg=None)
    assert (d.outcome, d.source) == ("approve", "assumed")


# 4 · Röda dagar och privata ärenden (office=government m.fl.) ---------------------------------
def test_no_assumption_on_swedish_holiday(db, user, vehicle, trips, auto, locs):
    for start in (datetime(2026, 12, 25, 9, 0, tzinfo=UTC),   # juldagen (fredag)
                  datetime(2026, 4, 3, 8, 0, tzinfo=UTC),     # långfredag
                  datetime(2026, 5, 14, 8, 0, tzinfo=UTC),    # Kristi himmelsfärd
                  datetime(2026, 6, 19, 8, 0, tzinfo=UTC)):   # midsommarafton
        t = live(trips, HOME, LAGER, km=23.0, start_at=start)
        d = autopilot.decide(db, reload_trip(db, t.id), user_id=user.id, ctx=None, rules=None, cfg=None)
        assert d.outcome == "ask", (start, d)
        assert d.candidates and d.candidates[0] == "Kundbesök Dagab"
    # En vanlig vardag antas besöket.
    t = live(trips, HOME, LAGER, km=23.0, start_at=datetime(2026, 12, 22, 9, 0, tzinfo=UTC))
    d = autopilot.decide(db, reload_trip(db, t.id), user_id=user.id, ctx=None, rules=None, cfg=None)
    assert (d.outcome, d.source) == ("approve", "assumed")


def test_private_errand_offices_are_unclear():
    from app.services.places import geocode
    for cat, typ in (("office", "government"), ("office", "lawyer"), ("office", "estate_agent"),
                     ("office", "insurance"), ("office", "religion"), ("office", "employment_agency"),
                     ("office", "diplomatic"), ("office", "association"), ("office", "educational_institution"),
                     ("craft", "winery"), ("craft", "brewery"), ("craft", "distillery")):
        assert geocode.classify_place(cat, typ) == "unclear", (cat, typ)
    assert geocode.classify_place("office", "company") == "work"
    assert geocode.classify_place("craft", "carpenter") == "work"


def test_stored_government_office_not_assumed(db, user, vehicle, trips, auto, locs):
    """Platser som redan fått place_kind=work före rättelsen prövas mot osm_class."""
    _place(trips, EMPL, name="Skatteverket", company="Skatteverket", osm="office=government")
    t = live(trips, HOME, EMPL, km=12.0, start_at=TUE_1015)
    d = autopilot.decide(db, reload_trip(db, t.id), user_id=user.id, ctx=None, rules=None, cfg=None)
    assert d.outcome != "approve"


# 6 · Demobilens hem öppnar inte för kartuppslag ----------------------------------------------
def test_demo_home_does_not_unlock_geocoding(db, user, monkeypatch, no_dns):
    from app.db import utcnow
    from app.models.enums import GeocodeStatus
    from app.services.places import geocode
    from app.services.volvo import demo as volvo_demo
    from tests.test_s1_adresser import Recorder, enable, home, pending_place, reload, use

    enable(db, user)
    volvo_demo._ensure_places(db, user.id)
    loc = pending_place(db, user)
    db.commit()
    rec = use(monkeypatch, Recorder())
    geocode.run_pending(now=utcnow())
    assert rec.requests == []
    assert reload(db, loc).geocode_status == GeocodeStatus.PENDING
    # Ett obekräftat hem räcker inte heller.
    h = home(db, user)
    h.confirmed = False
    db.commit()
    geocode.run_pending(now=utcnow())
    assert rec.requests == []
    # Med det bekräftade, riktiga hemmet slås adressen upp.
    h.confirmed = True
    db.commit()
    geocode.run_pending(now=utcnow())
    assert len(rec.requests) == 1


# 7 · Karttjänsten nere i två dygn: adressen slås upp när den är uppe igen ---------------------
def test_geocoder_outage_then_recovery(db, user, vehicle, monkeypatch, no_dns):
    import httpx

    from app.db import utcnow
    from app.models.enums import GeocodeStatus
    from app.services.places import geocode
    from tests.test_s1_adresser import B, Recorder, enable, home, pending_place, reload

    enable(db, user)
    home(db, user)
    loc = pending_place(db, user)
    db.commit()
    t = utcnow()
    down = {"on": True}
    ok = Recorder()

    def handler(request):
        if down["on"]:
            raise httpx.ConnectError("nere", request=request)
        return ok(request)

    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(handler))
    for m in range(0, 48 * 60 + 1, 10):
        geocode.run_pending(now=t + timedelta(minutes=m))
    assert reload(db, loc).geocode_status == GeocodeStatus.FAILED
    down["on"] = False
    for h in range(49, 24 * 4):
        geocode.run_pending(now=t + timedelta(hours=h))
    loc = reload(db, loc)
    assert loc.geocode_status == GeocodeStatus.OK and loc.address
    assert len(ok.requests) == 1


def test_gave_up_place_is_retried_on_next_visit(db, user, vehicle, monkeypatch, no_dns):
    import httpx

    from app.db import utcnow
    from app.models.enums import GeocodeStatus
    from app.services.places import geocode
    from tests.test_s1_adresser import B, Recorder, enable, home, pending_place, reload

    enable(db, user)
    home(db, user)
    loc = pending_place(db, user)
    db.commit()
    t = utcnow()
    down = {"on": True}
    ok = Recorder()

    def handler(request):
        if down["on"]:
            raise httpx.ConnectError("nere", request=request)
        return ok(request)

    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(handler))
    for h in range(0, 24 * 40, 2):  # nere i 40 dygn: omförsöken tar slut
        geocode.run_pending(now=t + timedelta(hours=h))
    cache = db.execute(select(M.GeocodeCache)).scalar_one()
    assert cache.attempts == geocode.MAX_ATTEMPTS + geocode.SLOW_RETRIES and cache.next_attempt_at is None
    down["on"] = False
    later = t + timedelta(days=45)
    geocode.run_pending(now=later)
    assert ok.requests == []  # inget nytt besök: inget anrop
    db.expire_all()
    again = geocode.place_for_position(db, user_id=user.id, lat=B[0], lon=B[1], demo=False)
    assert again.id == loc.id
    db.commit()
    geocode.run_pending(now=later + timedelta(minutes=1))
    assert len(ok.requests) == 1 and reload(db, loc).geocode_status == GeocodeStatus.OK


def test_failed_address_blocker_says_not_found(db, user, vehicle, trips, auto, locs):
    from app.models.enums import GeocodeStatus
    t = trips.trip(HOME, ACME, category=Category.WORK, purpose="Service", source=TripSource.VOLVO_API,
                   km=30.0, start_at=TUE_1015, review_status=ReviewStatus.APPROVED)
    locs["acme"].geocode_status = GeocodeStatus.FAILED
    db.flush()
    st = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=SEP,
                              now=datetime(2026, 10, 2, 10, tzinfo=UTC))
    b = next(b for b in st.blockers if b.code == "addresses_pending")
    assert "kunde inte slås upp" in b.text and str(t.id) in b.trip_ids


# 8 · "Privat stopp" bara för resor från bilen ------------------------------------------------
@pytest.mark.parametrize("source", [TripSource.VOLVO_IMPORT, TripSource.MANUAL])
def test_private_stop_not_written_for_imported_or_manual_trips(db, user, vehicle, trips, source):
    from zoneinfo import ZoneInfo

    from app.models.enums import ReviewStatus as RS
    from app.services.export import simple
    from app.services.trips import pipeline
    from app.services.trips import segments as seg

    kund1, kund2 = "Industrivägen 10, 222 33 Verkstadsby", "Fabriksgatan 3, 333 44 Bruksort"
    trips.location(HOME, name="Hemmet", type=LocationType.HOME)
    trips.trip(kund1, HOME, category=Category.WORK, purpose="Service", review_status=RS.IMPORTED, source=source)
    trips.trip(HOME, kund2, category=Category.WORK, purpose="Service", review_status=RS.IMPORTED, source=source,
               gap_minutes=5)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False,
                                 origin="import")
    db.flush()
    rows = simple.build_journal(vehicle=vehicle, trips=seg.active_trips(db, vehicle.id), start=None, end=None,
                                tz=ZoneInfo("Europe/Stockholm")).rows
    assert len(rows) == 1
    assert rows[0].via == (HOME,)
    assert simple.PRIVATE_STOP not in rows[0].as_dict()["to"]


# 9 · Uppgraderingsmånaden: resor som telefonen aldrig frågar om hänvisas till webben ----------
def test_pre_upgrade_live_trips_are_web_only_blockers(db, user, vehicle, trips, locs):
    from app.services.trips import pipeline

    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": "2099-01-01T00:00:00+00:00"},
                                allow_read_only=True)
    t = live(trips, HOME, ACME, km=30.0, start_at=TUE_1015)
    pipeline.sync_trip_review_items(db, reload_trip(db, t.id))
    db.flush()
    assert open_items(db, t.id)
    st = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=SEP,
                              now=datetime(2026, 10, 2, 10, tzinfo=UTC))
    q = [b for b in st.blockers if b.code == "open_questions"]
    assert len(q) == 1 and q[0].web_only and "öppna webben" in q[0].text, q
    assert str(t.id) in q[0].trip_ids


def test_live_trips_after_upgrade_stay_phone_questions(db, user, vehicle, trips, auto, locs):
    from app.services.trips import pipeline

    t = live(trips, HOME, ACME, km=30.0, start_at=TUE_1015)
    pipeline.sync_trip_review_items(db, reload_trip(db, t.id))
    db.flush()
    st = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=SEP,
                              now=datetime(2026, 10, 2, 10, tzinfo=UTC))
    q = [b for b in st.blockers if b.code == "open_questions"]
    assert len(q) == 1 and not q[0].web_only
