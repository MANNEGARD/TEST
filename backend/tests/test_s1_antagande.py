"""S1d: platsantagandet (A.3) – "Antaget kundbesök".

classify_place, regel A med kontorstid och veto, notisen bara första gången per plats,
"Fel" som återkallar och öppnar övriga antagna resor, "Stämmer" och företagsnamnet som
skapar en egen regel, AI-vägen och att antagna resor aldrig blir historik.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    ApprovalSource,
    Category,
    LocationType,
    NotificationKind,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TripSource,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import autopilot, engine, llm
from app.services.learning import feedback, patterns
from tests.conftest import reload_trip
from tests.test_ha_support import fake_ha, setup_push  # noqa: F401

ACTIVE_SINCE = "2020-01-01T00:00:00+00:00"
HOME = "Hemvägen 1, 111 11 Exempelstad"
OFFICE = "Kontorsgatan 2, 111 22 Exempelstad"
LAGER = "Lagervägen 12, 222 33 Verkstadsby"
NONAME = "Industrigatan 7, 222 44 Verkstadsby"
ATELJE = "Ateljévägen 3, 333 55 Norrby"
ICA = "Butiksgatan 7, 111 55 Exempelstad"
# Tisdag 2026-09-15: resorna är 20 minuter, så starten 07:55 UTC slutar 10:15 svensk tid.
TUE_1015 = datetime(2026, 9, 15, 7, 55, tzinfo=UTC)
TUE_1930 = datetime(2026, 9, 15, 17, 10, tzinfo=UTC)
SAT_1015 = datetime(2026, 9, 19, 7, 55, tzinfo=UTC)


class FakeProvider:
    name, model = "fake", "fake-1"

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.documents: list[dict[str, Any]] = []

    def assess(self, document):
        self.documents.append(document)
        return llm.LLMResult(data=llm._coerce(self.data), provider=self.name, model=self.model, usage={})


@pytest.fixture()
def auto(db, user):
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    db.flush()


def _place(trips, address, *, name=None, kind="work", osm="building=warehouse", company=None):
    """En obekräftad plats som kartan har slagit upp (som S1a sparar den)."""
    loc = trips.location(address, name=name or address, type=LocationType.UNKNOWN, confirmed=False,
                         company=company)
    loc.place_kind, loc.osm_class, loc.geocode_status = kind, osm, "ok"
    loc.created_from = "api"
    loc.city = address.rsplit(" ", 1)[-1]  # S1a sparar orten utan postnummer
    trips.db.flush()
    return loc


@pytest.fixture()
def locs(trips):
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "office": trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE),
        # Lager med namn i kartan: S1a sparar namnet och company för place_kind=work.
        "dagab": _place(trips, LAGER, name="Dagab", company="Dagab"),
        "noname": _place(trips, NONAME, kind="work", osm="landuse=industrial"),
        "atelje": _place(trips, ATELJE, name="Ateljé Norr", kind="unclear", osm="building=yes"),
        "ica": _place(trips, ICA, name="ICA Nära", kind="never", osm="shop=supermarket"),
    }


def live(trips, start, end, **kw):
    return trips.trip(start, end, source=TripSource.VOLVO_API, **kw)


def run(db, user, vehicle):
    out = autopilot.run_vehicle(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM)
    db.flush()
    return out


def decide(db, user, trip):
    return autopilot.decide(db, reload_trip(db, trip.id), user_id=user.id, ctx=None, rules=None, cfg=None)


def owner(user):
    return audit.user_actor(user.id)


# ----------------------------------------------------------------------------- classify_place


@pytest.mark.parametrize(
    ("category", "typ", "kind"),
    [
        ("office", "company", "work"), ("craft", "carpenter", "work"), ("industrial", "factory", "work"),
        ("man_made", "works", "work"), ("building", "industrial", "work"), ("building", "warehouse", "work"),
        ("building", "office", "work"), ("landuse", "industrial", "work"),
        ("shop", "supermarket", "never"), ("amenity", "kindergarten", "never"), ("leisure", "park", "never"),
        ("tourism", "hotel", "never"), ("healthcare", "dentist", "never"), ("building", "residential", "never"),
        ("building", "house", "never"), ("building", "apartments", "never"), ("building", "detached", "never"),
        ("building", "terrace", "never"), ("landuse", "residential", "never"), ("place", "house", "never"),
        ("building", "yes", "unclear"), ("highway", "residential", "unclear"), ("man_made", "tower", "unclear"),
        (None, None, "unclear"), ("", "", "unclear"),
    ],
)
def test_classify_place_table(category, typ, kind):
    assert autopilot.classify_place(category, typ) == kind


# ----------------------------------------------------------------------------- regel A


def test_assumed_on_weekday_1015(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015)
    d = decide(db, user, t)
    assert (d.outcome, d.source, d.purpose, d.reason) == ("approve", "assumed", "Kundbesök Dagab", "Antaget kundbesök")
    assert run(db, user, vehicle) == {"approved": 1}
    t = reload_trip(db, t.id)
    assert (t.category, t.review_status, t.approval_source) == (Category.WORK, ReviewStatus.APPROVED, "assumed")
    assert t.purpose == "Kundbesök Dagab" and t.category_source == "pattern"
    veto = datetime.fromisoformat(t.approval_basis["veto_until"])
    assert timedelta(hours=23, minutes=59) < veto - t.auto_approved_at <= timedelta(hours=24, minutes=1)
    assert t.approval_basis["reason"] == "Antaget kundbesök"
    loc = db.get(M.KnownLocation, locs["dagab"].id)
    # Platsen blir "antagen kund" men aldrig bekräftad av antagandet.
    assert loc.assumption == "customer" and not loc.confirmed and loc.type == LocationType.UNKNOWN


def test_no_assumption_outside_office_hours_but_question_with_shortcut(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, LAGER, start_at=TUE_1930)
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.candidates[0] == "Kundbesök Dagab"
    run(db, user, vehicle)
    t = reload_trip(db, t.id)
    assert t.approval_source is None and t.review_status != ReviewStatus.APPROVED
    # Knappen "Tjänst: Kundbesök Dagab" kommer först i frågan.
    assert autopilot.purpose_candidates(db, t, user_id=user.id)[0] == "Kundbesök Dagab"
    # Tidpunkten är aldrig ett tecken på privat.
    assert t.category != Category.PRIVATE


def test_no_assumption_on_weekend(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, LAGER, start_at=SAT_1015)
    assert decide(db, user, t).outcome == "ask"


def test_office_hours_setting_is_used(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "automation",
                                {"office_hours": {"days": [0, 1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"}})
    t1 = live(trips, HOME, LAGER, start_at=TUE_1930)
    t2 = live(trips, HOME, LAGER, start_at=SAT_1015 + timedelta(hours=2))
    assert decide(db, user, t1).outcome == "approve"
    assert decide(db, user, t2).outcome == "approve"


def test_no_assumption_on_shop(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, ICA, start_at=TUE_1015)
    d = decide(db, user, t)
    assert d.outcome == "ask" and "Kundbesök ICA Nära" not in d.candidates


def test_veto_zero_gives_a_question_with_the_assumption_as_shortcut(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "automation", {"assumption_veto_hours": 0})
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.candidates[0] == "Kundbesök Dagab"
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None
    assert db.get(M.KnownLocation, locs["dagab"].id).assumption is None


def test_veto_setting_sets_veto_until(db, user, vehicle, trips, auto, locs):
    settings_svc.update_section(db, user.id, "automation", {"assumption_veto_hours": 72})
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    d = decide(db, user, t)
    veto = datetime.fromisoformat(d.veto_until)
    assert timedelta(hours=71) < veto - datetime.now(UTC) <= timedelta(hours=72)


def test_work_place_without_name_asks_for_company(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, NONAME, start_at=TUE_1015)
    d = decide(db, user, t)
    assert d.outcome == "ask_company" and d.reason == "Vilket företag var det?"
    run(db, user, vehicle)
    assert reload_trip(db, t.id).approval_source is None


@pytest.mark.parametrize("dest", ["home", "office"])
def test_never_assumed_home_or_workplace(db, user, vehicle, trips, auto, locs, dest):
    # Även om kartan skulle säga arbetsplats: hemmet och tjänstestället antas aldrig.
    loc = locs[dest]
    loc.place_kind, loc.osm_class = "work", "building=office"
    db.flush()
    t = live(trips, LAGER, HOME if dest == "home" else OFFICE, start_at=TUE_1015)
    d = decide(db, user, t)
    assert d.source != ApprovalSource.ASSUMED


def test_never_assumed_when_a_signal_points_to_private(db, user, vehicle, trips, auto, locs):
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=locs["dagab"].id, category=Category.PRIVATE,
                             purpose=None)
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    d = decide(db, user, t)
    # Ägarens privata regel (R) gäller före antagandet.
    assert (d.outcome, d.category, d.source) == ("approve", Category.PRIVATE, ApprovalSource.RULE)


def test_revoked_place_is_never_assumed(db, user, vehicle, trips, auto, locs):
    locs["dagab"].assumption = "revoked"
    db.flush()
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    d = decide(db, user, t)
    assert d.outcome == "ask" and "Kundbesök Dagab" not in d.candidates


def test_spärr1_beats_assumption(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    t.quality_flags = ["possible_missed_trips"]
    db.flush()
    assert decide(db, user, t).outcome == "ask"


def test_return_trip_after_assumed_outbound_is_assumed_without_notice(db, user, vehicle, trips, auto, locs, fake_ha):
    setup_push(db, user)
    out = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015)
    back = live(trips, LAGER, HOME, km=23.0, start_at=TUE_1015 + timedelta(hours=2))
    run(db, user, vehicle)
    out, back = reload_trip(db, out.id), reload_trip(db, back.id)
    assert out.approval_source == back.approval_source == ApprovalSource.ASSUMED
    assert back.purpose == "Hemresa efter kundbesök Dagab."
    assert back.approval_basis["veto_until"] == out.approval_basis["veto_until"]
    reqs = db.execute(select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.ASSUMED)).scalars().all()
    assert [r.logical_trip_id for r in reqs] == [out.id]
    assert db.get(M.KnownLocation, locs["home"].id).assumption is None


# ----------------------------------------------------------------------------- notisen


def test_notice_only_first_time_per_place(db, user, vehicle, trips, auto, locs, fake_ha):
    setup_push(db, user)
    t1 = live(trips, HOME, LAGER, start_at=TUE_1015)
    run(db, user, vehicle)
    t2 = live(trips, HOME, LAGER, start_at=TUE_1015 + timedelta(days=1))
    run(db, user, vehicle)
    assert reload_trip(db, t2.id).approval_source == ApprovalSource.ASSUMED  # också antagen, men utan notis
    reqs = db.execute(select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.ASSUMED)).scalars().all()
    assert len(reqs) == 1 and reqs[0].logical_trip_id == t1.id
    assert reqs[0].dedupe_key == f"assumed:{locs['dagab'].id}"


# ----------------------------------------------------------------------------- rättelser (b) och (c)


def test_wrong_revokes_and_reopens_other_assumed_trips(db, user, vehicle, trips, auto, locs):
    t1 = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015)
    back1 = live(trips, LAGER, HOME, km=23.0, start_at=TUE_1015 + timedelta(hours=2))
    t2 = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015 + timedelta(days=1))
    t3 = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015 + timedelta(days=2))
    run(db, user, vehicle)
    for t in (t1, back1, t2, t3):
        assert reload_trip(db, t.id).approval_source == ApprovalSource.ASSUMED

    t2 = feedback.apply_decision(db, reload_trip(db, t2.id), user_id=user.id, actor=owner(user),
                                 category=Category.PRIVATE, approve=True)
    assert t2._autopilot_notice == "Dagab räknas inte längre som kund. 3 andra antagna resor frågas om igen."
    assert t2.approval_source == ApprovalSource.USER and t2.category == Category.PRIVATE
    assert db.get(M.KnownLocation, locs["dagab"].id).assumption == "revoked"
    for t in (t1, back1, t3):
        t = reload_trip(db, t.id)
        assert t.approval_source is None and t.approval_basis is None and t.auto_approved_at is None
        assert t.review_status != ReviewStatus.APPROVED
        items = db.execute(
            select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == t.id, M.ReviewItem.status == ReviewState.OPEN)
        ).scalars().all()
        assert any(i.kind in (ReviewKind.CATEGORY, ReviewKind.PURPOSE) for i in items)
    # Nya resor dit antas inte längre.
    t4 = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015 + timedelta(days=7))
    assert decide(db, user, t4).outcome == "ask"
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "location.assumption_revoked")).first()


def test_wrong_reopens_trips_in_exported_month_and_marks_it_stale(db, user, vehicle, trips, auto, locs):
    # Ändrat efter granskningen: även antagna resor i en skickad månad frågas om (filen ersätts sedan).
    t1 = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015)
    t2 = live(trips, HOME, LAGER, km=23.0, start_at=datetime(2026, 10, 6, 7, 55, tzinfo=UTC))
    run(db, user, vehicle)
    mc = M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-09", state="exported")
    db.add(mc)
    db.flush()
    feedback.apply_decision(db, reload_trip(db, t2.id), user_id=user.id, actor=owner(user),
                            category=Category.PRIVATE, approve=True)
    assert reload_trip(db, t1.id).approval_source is None
    db.refresh(mc)
    assert mc.state == "stale"


def test_other_purpose_is_not_a_revocation(db, user, vehicle, trips, auto, locs):
    t1 = live(trips, HOME, LAGER, start_at=TUE_1015)
    t2 = live(trips, HOME, LAGER, start_at=TUE_1015 + timedelta(days=1))
    run(db, user, vehicle)
    t1 = feedback.apply_decision(db, reload_trip(db, t1.id), user_id=user.id, actor=owner(user),
                                 category=Category.WORK, purpose="Leverans av reservdelar", approve=True)
    assert t1._autopilot_notice is None
    assert db.get(M.KnownLocation, locs["dagab"].id).assumption == "customer"
    assert reload_trip(db, t2.id).approval_source == ApprovalSource.ASSUMED


def test_correcting_assumed_return_trip_revokes_outbound_place(db, user, vehicle, trips, auto, locs):
    out = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015)
    back = live(trips, LAGER, HOME, km=23.0, start_at=TUE_1015 + timedelta(hours=2))
    run(db, user, vehicle)
    feedback.apply_decision(db, reload_trip(db, back.id), user_id=user.id, actor=owner(user),
                            category=Category.PRIVATE, approve=True)
    assert db.get(M.KnownLocation, locs["dagab"].id).assumption == "revoked"
    assert reload_trip(db, out.id).approval_source is None


def test_confirm_creates_customer_and_rule(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    run(db, user, vehicle)
    autopilot.confirm_assumption(db, reload_trip(db, t.id), actor=owner(user))
    t = reload_trip(db, t.id)
    assert (t.approval_source, t.review_status, t.purpose) == (ApprovalSource.USER, ReviewStatus.APPROVED,
                                                              "Kundbesök Dagab")
    loc = db.get(M.KnownLocation, locs["dagab"].id)
    assert (loc.type, loc.confirmed, loc.company, loc.assumption) == (LocationType.CUSTOMER, True, "Dagab", "customer")
    pat = db.execute(select(M.LocationPattern).where(M.LocationPattern.destination_location_id == loc.id,
                                                     M.LocationPattern.is_manual_rule.is_(True))).scalar_one()
    assert (pat.manual_category, pat.manual_purpose) == (Category.WORK, "Kundbesök Dagab")
    # Därefter gäller R, även utanför kontorstid.
    t2 = live(trips, HOME, LAGER, start_at=TUE_1930 + timedelta(days=1))
    d = decide(db, user, t2)
    assert (d.outcome, d.source, d.purpose) == ("approve", ApprovalSource.RULE, "Kundbesök Dagab")


def test_confirm_refuses_a_trip_that_is_not_assumed(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, LAGER, start_at=TUE_1930)
    with pytest.raises(ValueError):
        autopilot.confirm_assumption(db, t, actor=owner(user))
    assert db.get(M.KnownLocation, locs["dagab"].id).confirmed is False


def test_company_answer_creates_customer_rule_and_approves(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, NONAME, start_at=TUE_1015)
    t2 = live(trips, HOME, NONAME, start_at=TUE_1015 + timedelta(days=1))
    run(db, user, vehicle)
    autopilot.set_company(db, reload_trip(db, t.id), "  Bring‮​  ", actor=owner(user))
    t = reload_trip(db, t.id)
    assert (t.category, t.review_status, t.purpose, t.approval_source) == (
        Category.WORK, ReviewStatus.APPROVED, "Kundbesök Bring", ApprovalSource.USER)
    loc = db.get(M.KnownLocation, locs["noname"].id)
    assert (loc.type, loc.confirmed, loc.company, loc.name) == (LocationType.CUSTOMER, True, "Bring", "Bring")
    assert loc.address == NONAME
    # Den väntande resan dit avgörs direkt av den nya regeln (R).
    t2 = reload_trip(db, t2.id)
    assert (t2.approval_source, t2.purpose) == (ApprovalSource.RULE, "Kundbesök Bring")


def test_company_answer_is_sanitized_and_empty_rejected(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, NONAME, start_at=TUE_1015)
    with pytest.raises(ValueError):
        autopilot.set_company(db, t, "​ ‮", actor=owner(user))
    assert db.get(M.KnownLocation, locs["noname"].id).confirmed is False


def test_company_answer_refused_for_home(db, user, vehicle, trips, auto, locs):
    t = live(trips, LAGER, HOME, start_at=TUE_1015)
    with pytest.raises(ValueError):
        autopilot.set_company(db, t, "Bring", actor=owner(user))
    assert db.get(M.KnownLocation, locs["home"].id).type == LocationType.HOME


# ----------------------------------------------------------------------------- AI


def _ai(monkeypatch, data):
    provider = FakeProvider(data)
    monkeypatch.setattr(engine, "get_provider", lambda db, user_id: provider)
    return provider


def _reconsider(db, user, trip):
    autopilot.reconsider(db, user_id=user.id, trip_ids=[trip.id], actor=audit.SYSTEM)
    db.flush()
    return reload_trip(db, trip.id)


AI_WORK = {"category": "work", "confidence": 0.8, "evidence": ["Namnet låter som ett företag"],
           "requires_review": True, "purpose": None}


def test_ai_work_on_unclear_place_gives_assumption(db, user, vehicle, trips, auto, locs, monkeypatch):
    provider = _ai(monkeypatch, AI_WORK)
    t = live(trips, HOME, ATELJE, start_at=TUE_1015)
    t = _reconsider(db, user, t)
    assert (t.approval_source, t.purpose) == (ApprovalSource.ASSUMED, "Kundbesök Ateljé Norr")
    # Underlaget till AI har kartans namn och typ men aldrig gatuadress eller koordinater.
    dest = provider.documents[0]["destination"]
    assert dest["poi_name"] == "Ateljé Norr" and dest["osm_class"] == "building=yes"
    text = repr(provider.documents[0])
    assert "Ateljévägen" not in text and "333 55" not in text


def test_without_provider_unclear_place_gives_question(db, user, vehicle, trips, auto, locs):
    t = live(trips, HOME, ATELJE, start_at=TUE_1015)
    t = _reconsider(db, user, t)
    assert t.approval_source is None
    assert decide(db, user, t).outcome == "ask"


def test_ai_private_gives_no_assumption(db, user, vehicle, trips, auto, locs, monkeypatch):
    _ai(monkeypatch, {**AI_WORK, "category": "private"})
    t = live(trips, HOME, ATELJE, start_at=TUE_1015)
    t = _reconsider(db, user, t)
    assert t.approval_source != ApprovalSource.ASSUMED


def test_rejected_ai_purpose_gives_template(db, user, vehicle, trips, auto, locs, monkeypatch):
    # Namnet "Pettersson" finns inte i underlaget: validate_generated avvisar ärendet.
    _ai(monkeypatch, {**AI_WORK, "purpose": "Möte med Pettersson om offert"})
    t = live(trips, HOME, ATELJE, start_at=TUE_1015)
    t = _reconsider(db, user, t)
    assert (t.approval_source, t.purpose) == (ApprovalSource.ASSUMED, "Kundbesök Ateljé Norr")


def test_ai_is_not_asked_about_unclear_place_in_manual_mode(db, user, vehicle, trips, locs, monkeypatch):
    provider = _ai(monkeypatch, AI_WORK)
    t = live(trips, HOME, ATELJE, start_at=TUE_1015)
    engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM, provider=provider, use_llm=True)
    assert provider.documents == []


def test_place_doc_never_sends_address_as_poi_name(db, user, vehicle, trips, locs):
    doc = engine._place_doc(locs["noname"], NONAME, send_street=False)
    assert "poi_name" not in doc and doc["osm_class"] == "landuse=industrial"
    assert doc["label"] == "Verkstadsby"
    assert "poi_name" not in engine._place_doc(locs["ica"], ICA, send_street=False)  # place_kind=never


# ----------------------------------------------------------------------------- aldrig historik


def test_assumed_trips_never_count_as_history(db, user, vehicle, trips, auto, locs):
    for i in range(4):
        live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015 + timedelta(days=i * 7))
    run(db, user, vehicle)
    locs["dagab"].assumption = "revoked"  # utan antagande skulle bara H kunna avgöra
    db.flush()
    t = live(trips, HOME, LAGER, km=23.0, start_at=TUE_1015 + timedelta(days=35))
    d = decide(db, user, t)
    assert d.outcome == "ask" and d.source != ApprovalSource.HISTORY
    patterns.rebuild(db, user.id)
    pat = patterns.get_patterns(db, user.id, locs["home"].id, locs["dagab"].id)[0]
    assert pat is None or not any(p.get("user_count") for p in (pat.purposes or []))
    assert autopilot.can_remember(db, t, "Kundbesök X") is False


def test_accepted_ai_purpose_is_used(db, user, vehicle, trips, auto, locs, monkeypatch):
    _ai(monkeypatch, {**AI_WORK, "purpose": "Service hos Ateljé Norr"})
    t = live(trips, HOME, ATELJE, start_at=TUE_1015)
    t = _reconsider(db, user, t)
    assert (t.approval_source, t.purpose) == (ApprovalSource.ASSUMED, "Service hos Ateljé Norr.")


# ----------------------------------------------------------------------------- hela vägen via svaren


def _answer(db, user, req, action, text=None):
    import uuid

    from app.services.ha import responses

    return responses.handle(db, user_id=user.id, public_id=req.public_id, action=action, reply_text=text,
                            idempotency_key=uuid.uuid4().hex, source="web")


def _assumed_notice(db, trip_id):
    return db.execute(select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.ASSUMED,
                                                          M.NotificationRequest.logical_trip_id == trip_id)).scalar_one()


def test_phone_ok_confirms_and_private_revokes(db, user, vehicle, trips, auto, locs, fake_ha):
    setup_push(db, user)
    t1 = live(trips, HOME, LAGER, start_at=TUE_1015)
    run(db, user, vehicle)
    assert _answer(db, user, _assumed_notice(db, t1.id), "OK")["outcome"] == "applied"
    assert db.get(M.KnownLocation, locs["dagab"].id).confirmed is True

    other = _place(trips, "Verkstadsgatan 9, 222 55 Verkstadsby", name="Exempel Verkstad AB",
                   company="Exempel Verkstad AB")
    t2 = live(trips, HOME, "Verkstadsgatan 9, 222 55 Verkstadsby", start_at=TUE_1015 + timedelta(days=1))
    t3 = live(trips, HOME, "Verkstadsgatan 9, 222 55 Verkstadsby", start_at=TUE_1015 + timedelta(days=2))
    run(db, user, vehicle)
    res = _answer(db, user, _assumed_notice(db, t2.id), "PRIVATE")
    assert res["outcome"] == "applied"
    assert db.get(M.KnownLocation, other.id).assumption == "revoked"
    assert reload_trip(db, t3.id).approval_source is None


def test_phone_ok_on_trip_no_longer_assumed_is_invalid_not_an_error(db, user, vehicle, trips, auto, locs, fake_ha):
    setup_push(db, user)
    t = live(trips, HOME, LAGER, start_at=TUE_1015)
    run(db, user, vehicle)
    req = _assumed_notice(db, t.id)
    feedback.apply_decision(db, reload_trip(db, t.id), user_id=user.id, actor=owner(user),
                            category=Category.PRIVATE, approve=True)
    req.status = "sent"  # notisen ligger kvar på telefonen
    db.flush()
    res = _answer(db, user, req, "OK")
    assert res["outcome"] == "invalid" and res["message"] == "Resan är inte längre antagen."
