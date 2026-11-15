"""Regressionstester för granskningsfynden om klassificering, AI-underlag och kalender (klas-*)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, IntegrationStatus, LocationType, ReviewKind, ReviewStatus
from app.services import audit, integrations
from app.services import settings as settings_svc
from app.services.calendar import matching, sync
from app.services.classify import engine, llm, purpose
from app.services.learning import feedback, patterns
from app.services.places import registry as places
from tests.conftest import BASE_TIME, reload_trip

API = "/api/v1"
HOME = "Hemvägen 1, Hemby"
OFFICE = "Kontorsgatan 4, Storstad"
CUSTOMER = "Lagergatan 7, Storstad"
HOTEL = "Strandgatan 30, Sjöstad"
GYM = "Idrottsvägen 3, Hemby"
UNKNOWN = "Okändagatan 99, Annanstad"
FUEL = "Macken 2, Mellanby"
WEST = "Hamngatan 5, Västerstad"

UNDECIDED = {"category": "undecided", "confidence": 0.2, "purpose": None, "evidence": [],
             "purpose_needs_confirmation": True, "requires_review": True}


class FakeProvider:
    name, model = "fake", "fake-1"

    def __init__(self, data: dict[str, Any] | None = None, error: Exception | None = None):
        self.data = data
        self.error = error
        self.documents: list[dict[str, Any]] = []

    def assess(self, document):
        self.documents.append(document)
        if self.error:
            raise self.error
        if self.data is None:
            return None
        return llm.LLMResult(data=llm._coerce(self.data), provider=self.name, model=self.model, usage={})


def ai_answer(category: str = "work", purpose_text: str | None = None, conf: float = 0.9) -> dict[str, Any]:
    return {"category": category, "confidence": conf, "purpose": purpose_text, "evidence": ["x"],
            "purpose_needs_confirmation": False, "requires_review": False}


def setup_places(trips):
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME, lat=59.123456, lon=14.654321),
        "office": trips.location(OFFICE, name="Storstadskontoret", type=LocationType.OFFICE),
        "customer": trips.location(CUSTOMER, name="Exempelbolaget", type=LocationType.CUSTOMER, company="Exempelbolaget"),
        "hotel": trips.location(HOTEL, name="Hotell Stranden", type=LocationType.HOTEL),
        "gym": trips.location(GYM, name="Gymmet", type=LocationType.PRIVATE),
    }


def classify(db, user, trip, **kw):
    return engine.classify_trip(db, trip, user_id=user.id, actor=audit.SYSTEM, **kw)


def event(db, user, subject, start, minutes=120, **kw) -> M.CalendarEvent:
    ev = M.CalendarEvent(
        user_id=user.id, calendar_id=kw.pop("calendar_id", "cal"), external_id=f"ev-{subject}-{start.isoformat()}",
        subject=subject, start_at=start, end_at=start + timedelta(minutes=minutes), **kw,
    )
    db.add(ev)
    db.flush()
    return ev


def context(db, user, trip):
    return engine.sig.build_context(
        db, trip, user_id=user.id, tz=settings_svc.user_timezone(db, user.id),
        tax=settings_svc.get_section(db, user.id, "tax"), ai=settings_svc.get_section(db, user.id, "ai"),
        abbreviations={},
    )


def dumped(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False).casefold()


def assert_absent(doc: dict[str, Any], *needles: str) -> None:
    text = dumped(doc)
    for needle in needles:
        assert needle.casefold() not in text, f"{needle!r} finns i AI-underlaget: {text}"


# ----------------------------------------------------------------------------- klas-1: AI-underlagets sekretess


def test_klas1_calendar_subject_never_sent_when_disabled(db, user, trips):
    setup_places(trips)
    settings_svc.update_section(db, user.id, "ai", {"send_calendar_subjects": False})
    t = trips.trip(HOME, OFFICE, km=30, minutes=30)
    event(db, user, "Lönesamtal med Kalle Karlsson HEMLIGT", t.ended_at + timedelta(minutes=10),
          location_name="Storstadskontoret")
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    fake = FakeProvider(UNDECIDED)
    classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    doc = fake.documents[0]
    assert "calendar" not in doc and doc["rule_based_purpose"] is None
    assert_absent(doc, "Lönesamtal", "Kalle", "Karlsson", "HEMLIGT")
    # Gränssnittets signaletiketter är oförändrade (där får rubriken synas).
    assert any("Lönesamtal" in e.label for e in db.execute(select(M.TripEvidence)).scalars())


def test_klas1_unconfirmed_destination_street_not_sent(db, user, trips):
    setup_places(trips)
    trips.trip(HOME, "Patientvägen 12, Annanstad", category=Category.WORK, review_status=ReviewStatus.IMPORTED,
               purpose="Kundbesök")
    trips.skip(hours=20)
    patterns.rebuild(db, user.id)
    t = trips.trip(HOME, "Patientvägen 12, Annanstad")
    fake = FakeProvider(UNDECIDED)
    classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    doc = fake.documents[0]
    assert doc["destination"]["label"] == "Annanstad"
    assert_absent(doc, "Patientvägen", "Hemvägen")


def test_klas1_confirmed_place_with_default_name_sends_only_city(db, user, trips):
    setup_places(trips)
    addr = "Industrigatan 14, Annanstad"
    loc = places.resolve_address(db, user.id, addr, created_from="import")
    loc.type, loc.confirmed = LocationType.OFFICE, True  # bekräftad typ men aldrig omdöpt
    db.flush()
    t = trips.trip(HOME, addr, km=30, minutes=30)
    event(db, user, "Privat", t.ended_at + timedelta(minutes=5), sensitivity="private", location_city="Annanstad")
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    fake = FakeProvider(UNDECIDED)
    classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    doc = fake.documents[0]
    assert doc["destination"]["label"] == "Annanstad"
    assert_absent(doc, "Industrigatan")


def test_klas1_private_calendar_event_subject_and_location_not_sent(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, OFFICE, km=30, minutes=30)
    event(db, user, "Läkarbesök onkologen", t.ended_at + timedelta(minutes=10), sensitivity="private",
          location_name="Sjukhusvägen 3, Storstad", location_city="Storstad")
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    ctx = context(db, user, t)
    doc = engine.build_llm_document(ctx, engine.sig.evaluate(ctx), None)
    assert doc["calendar"] and doc["calendar"][0]["subject"] is None and doc["calendar"][0]["marked_private"]
    assert_absent(doc, "Läkarbesök", "onkologen", "Sjukhusvägen")


def test_klas1_calendar_location_only_with_street_addresses(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, CUSTOMER, km=30, minutes=30)
    event(db, user, "Kundmöte Exempelbolaget", t.ended_at + timedelta(minutes=10),
          location_name="Lagergatan 7, Storstad", location_city="Storstad")
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    ctx = context(db, user, t)
    doc = engine.build_llm_document(ctx, engine.sig.evaluate(ctx), None)
    assert doc["calendar"][0]["subject"] == "Kundmöte Exempelbolaget"
    assert "location" not in doc["calendar"][0]
    assert_absent(doc, "Lagergatan")
    ctx.ai = {**ctx.ai, "send_street_addresses": True}
    doc = engine.build_llm_document(ctx, engine.sig.evaluate(ctx), None)
    assert doc["calendar"][0]["location"] == "Lagergatan 7, Storstad"


def test_klas1_whole_document_never_contains_private_data(db, user, trips):
    """Söker igenom hela det serialiserade dokumentet efter kalenderrubriker, gatuadresser,
    hemadress, privata platsnamn och koordinater – oavsett i vilket fält de skulle hamna."""
    p = setup_places(trips)
    settings_svc.update_section(db, user.id, "ai", {"send_calendar_subjects": False, "send_street_addresses": False})
    actor = audit.user_actor(user.id)
    # Användarbekräftade syften som råkar innehålla adresser och privata platsnamn.
    for _ in range(3):
        old = trips.trip(HOME, "Industrigatan 14, Annanstad")
        feedback.apply_decision(db, old, user_id=user.id, actor=actor, category=Category.WORK,
                                purpose="Möte Industrigatan 14 efter Gymmet, Hemvägen 1")
        trips.skip(days=1)
    t = trips.trip(HOME, "Industrigatan 14, Annanstad", km=30, minutes=30)
    event(db, user, "Hemlig rubrik Zeta", t.ended_at + timedelta(minutes=10), location_city="Annanstad",
          location_name="Kvarnbacken 7, Annanstad", body_preview="Ring 59.123456, 14.654321")
    event(db, user, "Privat tid Omega", t.ended_at + timedelta(minutes=20), sensitivity="private",
          location_city="Annanstad")
    matching.match_trip(db, t, user_id=user.id, is_demo=False)
    ctx = context(db, user, t)
    rules = engine.sig.evaluate(ctx)
    proposal = purpose.propose(db, ctx, rules)
    terms = engine.private_location_terms(db, user.id)
    doc = engine.build_llm_document(ctx, rules, proposal, private_terms=terms)
    assert_absent(
        doc, "Hemlig rubrik", "Zeta", "Omega", "Industrigatan", "Kvarnbacken", "Hemvägen", "Idrottsvägen",
        "Gymmet", "59.12", "14.65",
    )
    # Även med tillåtna rubriker och adresser skickas aldrig hemadress, privata platser eller koordinater.
    ctx.ai = {**ctx.ai, "send_calendar_subjects": True, "send_street_addresses": True, "send_calendar_body": True}
    doc = engine.build_llm_document(ctx, rules, proposal, private_terms=terms)
    assert_absent(doc, "Hemvägen", "Idrottsvägen", "Gymmet", "Omega", "59.12", "14.65")
    assert "Hemlig rubrik Zeta" in json.dumps(doc, ensure_ascii=False)
    assert p["home"].latitude == 59.123456  # originaldata orörd


def test_klas1_document_sent_through_provider_is_sanitized(db, user, trips):
    """Hela flödet: det leverantören faktiskt får innehåller inte hemmets eller privata platsers namn."""
    setup_places(trips)
    actor = audit.user_actor(user.id)
    for text in ("Kundbesök efter Gymmet", "Leverans efter Gymmet", "Möte efter Gymmet"):
        old = trips.trip(HOME, UNKNOWN)
        feedback.apply_decision(db, old, user_id=user.id, actor=actor, category=Category.WORK, purpose=text)
        trips.skip(days=1)
    patterns.rebuild(db, user.id)
    t = trips.trip(HOME, UNKNOWN)
    fake = FakeProvider(UNDECIDED)
    classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    assert_absent(fake.documents[0], "Gymmet", "Hemvägen", "Okändagatan", "Idrottsvägen")


# ----------------------------------------------------------------------------- klas-2: fabriceringsskydd


@pytest.mark.parametrize(
    "text",
    [
        "kundbesök hos volvo lastvagnar med anna i göteborg",
        "Kundbesök hos volvo lastvagnar med anna i göteborg",
        "Resa Storstad-Göteborg för möte med kund",
        "Kundbesök hos Exempelbolaget-Scania",
        "Möte med Storstadsbanken",
        "Möte med Storstads Bygg",
        "Möte kund-Scania",
        "Möte med iCA och vOLVO",
        "Besök hos Exemp",
        "Kundbesök hos Volvo Lastvagnar med Anna",
    ],
)
def test_klas2_invented_names_are_rejected(db, user, trips, text):
    setup_places(trips)
    t = trips.trip(OFFICE, CUSTOMER)
    ok, missing = purpose.validate_generated(text, context(db, user, t))
    assert not ok and missing


@pytest.mark.parametrize(
    "text",
    [
        "Kundbesök hos Exempelbolaget",
        "kundbesök hos exempelbolaget",
        "Kundbesök hos Exempelbolaget i Storstad",
        "Möte på Storstadskontoret",
        "Uppföljning hos Exempelbolagets kontor",
        "Besök hos Exempelbolaget – syfte behöver bekräftas",
    ],
)
def test_klas2_grounded_purposes_are_accepted(db, user, trips, text):
    setup_places(trips)
    t = trips.trip(OFFICE, CUSTOMER)
    ok, missing = purpose.validate_generated(text, context(db, user, t))
    assert ok, missing


def test_klas2_lowercase_invented_purpose_is_not_stored(db, user, trips):
    setup_places(trips)
    fake = FakeProvider(ai_answer("work", "kundbesök hos volvo lastvagnar med anna i göteborg"))
    t = trips.trip(OFFICE, CUSTOMER)
    a = classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    assert "volvo" not in (t.purpose or "").casefold() and t.purpose_source != "ai"
    rejected = next(s for s in a.signals if s.kind == "ai_purpose_rejected")
    assert {"volvo", "anna", "göteborg"} <= set(rejected.data["unsupported"])


# ----------------------------------------------------------------------------- klas-3: onödiga AI-anrop


def test_klas3_llm_not_called_again_when_inputs_unchanged(db, user, trips):
    setup_places(trips)
    fake = FakeProvider(ai_answer("work", "Kundbesök hos Exempelbolaget"))
    t = trips.trip(OFFICE, CUSTOMER)
    first = classify(db, user, t, provider=fake)
    second = classify(db, user, t, provider=fake)
    third = classify(db, user, t, provider=fake)
    assert (first.changed, second.changed, third.changed) == (True, False, False)
    assert len(fake.documents) == 1
    assert t.purpose == "Kundbesök hos Exempelbolaget." and t.purpose_source == "ai"
    classify(db, user, t, provider=fake, force=True)  # uttrycklig omklassning anropar igen
    assert len(fake.documents) == 2


def test_klas3_failed_llm_call_is_retried_without_rewriting(db, user, trips):
    setup_places(trips)
    failing = FakeProvider(error=llm.LLMError("nere", retryable=True))
    t = trips.trip(OFFICE, CUSTOMER)
    assert classify(db, user, t, provider=failing).changed
    rows_before = len(db.execute(select(M.TripClassification)).scalars().all())
    again = classify(db, user, t, provider=failing)
    assert again.changed is False and len(failing.documents) == 2
    assert len(db.execute(select(M.TripClassification)).scalars().all()) == rows_before
    ok = FakeProvider(ai_answer("work", "Kundbesök hos Exempelbolaget"))
    assert classify(db, user, t, provider=ok).changed
    assert t.purpose_source == "ai"


def test_klas3_approved_trip_without_purpose_not_sent_forever(db, user, vehicle, trips):
    setup_places(trips)
    actor = audit.user_actor(user.id)
    t = trips.trip(HOME, UNKNOWN)
    feedback.apply_decision(db, t, user_id=user.id, actor=actor, category=Category.WORK)  # godkänd, utan syfte
    fake = FakeProvider(UNDECIDED)
    for _ in range(4):
        engine.classify_pending(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, provider=fake)
    t = reload_trip(db, t.id)
    assert t.review_status == ReviewStatus.APPROVED and t.category == Category.WORK
    assert len(fake.documents) == 1


# ----------------------------------------------------------------------------- klas-5: kalenderpost utan plats


def test_klas5_locationless_entry_not_matched_on_arrival_home(db, user, trips):
    setup_places(trips)
    t = trips.trip(UNKNOWN, HOME, km=20, minutes=25)
    event(db, user, "Middag med svärföräldrarna", t.ended_at + timedelta(minutes=10))
    assert matching.match_trip(db, t, user_id=user.id, is_demo=False) == []
    a = classify(db, user, t)
    assert a.category != Category.WORK and t.purpose is None


def test_klas5_locationless_entry_is_not_work_evidence(db, user, trips):
    setup_places(trips)
    t = trips.trip(GYM, "Skolvägen 2, Hemby", km=4, minutes=10)
    event(db, user, "Hämta barnen", t.ended_at + timedelta(minutes=5), minutes=15)
    rows = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert rows and rows[0].geo_basis == "no_location"
    a = classify(db, user, t)
    assert a.category == Category.UNDECIDED and t.purpose is None
    assert not any(s.kind == "calendar_match" for s in a.signals)


def test_klas5_locationless_entry_cannot_make_known_office_auto(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, OFFICE, km=30, minutes=30)
    event(db, user, "Familjemiddag", t.ended_at + timedelta(minutes=10))
    (m,) = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert m.is_selected  # bokningen syns som kontext …
    a = classify(db, user, t)
    assert a.review_status != ReviewStatus.AUTO  # … men ger inte ytterligare underlag för automatik
    assert t.purpose_confirmed is False


# ----------------------------------------------------------------------------- klas-6: bortvald kalender


def _connect(db, user) -> M.IntegrationConnection:
    conn = sync.save_credentials(db, user.id, client_id="app-id-1234", client_secret="hemligt", tenant="common")
    integrations.set_secrets(conn, {"access_token": "at-1", "refresh_token": "rt-1"})
    conn.status = IntegrationStatus.CONNECTED
    conn.token_expires_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    return conn


def _graph_event(i, start, **kw):
    return {"id": f"id-{i}", "subject": f"Möte {i}", "iCalUId": f"uid-{i}",
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
            "end": {"dateTime": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
            "isOnlineMeeting": False, "showAs": "busy", "sensitivity": "normal", **kw}


def test_klas6_events_of_deselected_calendar_are_retired(db, user, trips):
    _connect(db, user)
    setup_places(trips)
    now = datetime.now(UTC)
    t = trips.trip(HOME, OFFICE, km=30, minutes=30, start_at=now - timedelta(hours=2))

    def handler(request: httpx.Request) -> httpx.Response:
        if "/calendars/work/" in request.url.path:
            return httpx.Response(200, json={"value": [_graph_event(1, now + timedelta(days=1))]})
        if "/calendars/family/" in request.url.path:
            return httpx.Response(200, json={"value": [
                _graph_event(2, t.ended_at + timedelta(minutes=5), subject="Familjemiddag",
                             location={"displayName": "Storstadskontoret"})]})
        return httpx.Response(404, json={})

    settings_svc.update_section(db, user.id, "calendar", {"calendar_ids": ["work", "family"]})
    sync.sync_user(db, user.id, now=now, transport=httpx.MockTransport(handler))
    assert any(m.is_selected for m in matching.match_trip(db, t, user_id=user.id, is_demo=False))
    settings_svc.update_section(db, user.id, "calendar", {"calendar_ids": ["work"]})
    result = sync.sync_user(db, user.id, now=now, transport=httpx.MockTransport(handler))
    assert result["removed"] == 1
    family = db.execute(select(M.CalendarEvent).where(M.CalendarEvent.calendar_id == "family")).scalar_one()
    assert family.removed_at is not None
    assert matching.match_trip(db, t, user_id=user.id, is_demo=False) == []


def test_klas6_deselecting_calendar_via_api_retires_events(client, db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, OFFICE, km=30, minutes=30)
    event(db, user, "Familjemiddag", t.ended_at + timedelta(minutes=5), calendar_id="family",
          location_name="Storstadskontoret")
    event(db, user, "Planering", t.ended_at + timedelta(days=1), calendar_id="work")
    (m,) = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert m.is_selected
    db.commit()
    r = client.put(f"{API}/integrations/microsoft/calendars", json={"calendar_ids": ["work"]})
    assert r.status_code == 200
    db.expire_all()
    evs = {e.calendar_id: e for e in db.execute(select(M.CalendarEvent)).scalars()}
    assert evs["family"].removed_at is not None and evs["work"].removed_at is None
    assert db.execute(select(M.CalendarMatch).where(M.CalendarMatch.logical_trip_id == t.id)).scalars().all() == []


# ----------------------------------------------------------------------------- klas-7: resa från bostaden


def _business_round_trip(trips):
    trips.trip(HOME, CUSTOMER, category=Category.WORK, purpose="Kundbesök på Exempelbolaget")
    trips.skip(hours=3)
    trips.trip(CUSTOMER, HOME, category=Category.WORK, purpose="Hemresa efter kundbesök på Exempelbolaget")


def test_klas7_home_to_home_after_business_return_is_not_return_trip(db, user, trips):
    setup_places(trips)
    _business_round_trip(trips)
    trips.skip(days=3)
    loop = trips.trip(HOME, HOME, km=12, minutes=40)
    a = classify(db, user, loop)
    assert not any(s.kind == "return_trip" for s in a.signals)
    assert a.category != Category.WORK and loop.subcategory != "return_home"


def test_klas7_home_to_hotel_after_completed_business_trip(db, user, trips):
    setup_places(trips)
    _business_round_trip(trips)
    trips.skip(days=10)
    t = trips.trip(HOME, HOTEL, km=150, minutes=120)
    a = classify(db, user, t)
    assert not any(s.kind == "hotel" for s in a.signals)
    assert a.category != Category.WORK


def test_klas7_real_return_trip_still_detected(db, user, trips):
    setup_places(trips)
    trips.trip(HOME, CUSTOMER, category=Category.WORK, purpose="Kundbesök på Exempelbolaget")
    trips.skip(hours=3)
    back = trips.trip(CUSTOMER, HOME)
    a = classify(db, user, back)
    assert any(s.kind == "return_trip" for s in a.signals) and a.category == Category.WORK


# ----------------------------------------------------------------------------- klas-8: arbetsresa med stopp


def test_klas8_commute_with_refuel_stop_gets_tax_note(db, user, trips):
    p = setup_places(trips)
    trips.location(FUEL, name="Macken", type=LocationType.FUEL_STATION)
    db.add(M.WorkplacePeriod(user_id=user.id, kind="location", location_id=p["office"].id))
    db.flush()
    leg1 = trips.trip(HOME, FUEL, km=5, minutes=8)
    trips.skip(minutes=6)
    leg2 = trips.trip(FUEL, OFFICE, km=25, minutes=25)
    classify(db, user, leg1)
    a = classify(db, user, leg2)
    assert a.category != Category.WORK
    assert leg2.tax_note and "arbetsresor" in leg2.tax_note
    assert any(s.kind == "commute" for s in a.signals)
    assert not any(s.kind == "regular_workplace" for s in a.signals)


def test_klas8_visit_from_customer_to_workplace_is_still_work(db, user, trips):
    p = setup_places(trips)
    db.add(M.WorkplacePeriod(user_id=user.id, kind="location", location_id=p["office"].id))
    db.flush()
    trips.trip(HOME, CUSTOMER, km=20, minutes=20)
    trips.skip(minutes=60)
    t = trips.trip(CUSTOMER, OFFICE, km=10, minutes=15)
    a = classify(db, user, t)
    assert any(s.kind == "regular_workplace" for s in a.signals) and t.tax_note is None


# ----------------------------------------------------------------------------- klas-9: kalenderkonflikter


def test_klas9_no_conflict_for_intermediate_stop(db, user, vehicle, trips):
    setup_places(trips)
    trips.location(WEST, name="Hamnkontoret", type=LocationType.CUSTOMER, company="Hamnbolaget")
    trips.location(FUEL, name="Macken", type=LocationType.FUEL_STATION)
    leg1 = trips.trip(HOME, FUEL, km=40, minutes=40, start_at=BASE_TIME)
    leg2 = trips.trip(FUEL, WEST, km=45, minutes=40, start_at=BASE_TIME + timedelta(minutes=50))
    event(db, user, "Kundmöte Hamnbolaget", BASE_TIME + timedelta(minutes=100), location_name="Hamnkontoret",
          location_address="Hamngatan 5, Västerstad", location_city="Västerstad")
    r1 = matching.match_trip(db, leg1, user_id=user.id, is_demo=False)
    r2 = matching.match_trip(db, leg2, user_id=user.id, is_demo=False)
    assert not any(m.is_conflict for m in r1)
    assert any(m.is_selected for m in r2)
    assert db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.CALENDAR_CONFLICT)).scalars().all() == []


def test_klas9_no_conflict_for_hybrid_online_meeting(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, CUSTOMER, km=40, minutes=40, start_at=BASE_TIME)
    event(db, user, "Ledningsgrupp", t.ended_at + timedelta(minutes=5), is_online=True,
          online_provider="teamsForBusiness", location_name="Konferensrum Vänern",
          location_address="Storgatan 1, Huvudorten", location_city="Huvudorten")
    rows = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert not any(m.is_conflict for m in rows)
    a = classify(db, user, t)
    assert "Konflikt mellan kalender och fordonsdata" not in a.caps


def test_klas9_real_conflict_is_still_flagged(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, CUSTOMER, km=40, minutes=40, start_at=BASE_TIME)
    event(db, user, "Kundmöte", t.ended_at + timedelta(minutes=5), location_name="Hamnkontoret",
          location_address="Hamngatan 5, Västerstad", location_city="Västerstad")
    (m,) = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert m.is_conflict


# ----------------------------------------------------------------------------- klas-10: inställt möte


def test_klas10_user_selected_match_dropped_when_event_cancelled(db, user, trips):
    setup_places(trips)
    t = trips.trip(HOME, UNKNOWN, km=30, minutes=30)
    ev = event(db, user, "Kundmöte Annanstad", t.ended_at + timedelta(minutes=10), location_city="Annanstad")
    (m,) = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    matching.set_user_choice(db, t, m, selected=True)
    classify(db, user, t)
    assert t.purpose == "Kundmöte Annanstad."
    ev.is_cancelled = True
    db.flush()
    rows = matching.match_trip(db, t, user_id=user.id, is_demo=False)
    assert rows == []
    a = classify(db, user, t, force=True)
    assert not any(s.kind.startswith("calendar") for s in a.signals)
    assert t.purpose is None


# ----------------------------------------------------------------------------- klas-11: Graph-fel vid kalenderlistan


def test_klas11_list_calendars_graph_errors_become_calendar_errors(db, user):
    _connect(db, user)
    with pytest.raises(sync.CalendarError) as err:
        sync.list_calendars(db, user.id, transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "120"}, json={})))
    assert err.value.code == "throttled"
    with pytest.raises(sync.CalendarError) as err:
        sync.list_calendars(db, user.id, transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    assert err.value.code == "graph_error"


def test_klas11_rotated_token_survives_failed_listing(db, user):
    conn = _connect(db, user)
    conn.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600,
                                             "token_type": "Bearer"})
        return httpx.Response(503, json={})

    with pytest.raises(sync.CalendarError):
        sync.list_calendars(db, user.id, transport=httpx.MockTransport(handler))
    assert integrations.secrets(conn)["refresh_token"] == "rt-2"


# ----------------------------------------------------------------------------- klas-12: giltighetsperiod


def test_klas12_validity_period_wins_over_regular_workplace_type(db, user, trips):
    p = setup_places(trips)
    p["office"].type = LocationType.REGULAR_WORKPLACE
    db.add(M.WorkplacePeriod(user_id=user.id, kind="location", location_id=p["office"].id,
                             valid_to=(BASE_TIME - timedelta(days=30)).date()))
    db.flush()
    t = trips.trip(HOME, OFFICE)
    a = classify(db, user, t)
    assert t.tax_note is None and t.subcategory != "commute"
    assert not any(s.kind == "commute" for s in a.signals)


def test_klas12_type_still_used_without_periods(db, user, trips):
    p = setup_places(trips)
    p["office"].type = LocationType.REGULAR_WORKPLACE
    db.flush()
    t = trips.trip(HOME, OFFICE)
    a = classify(db, user, t)
    assert any(s.kind == "commute" for s in a.signals) and t.tax_note


# ----------------------------------------------------------------------------- klas-13: avvisat mönster


def test_klas13_rejected_pattern_is_not_used(db, user, trips):
    setup_places(trips)
    actor = audit.user_actor(user.id)
    for _ in range(5):
        t = trips.trip(HOME, UNKNOWN)
        feedback.apply_decision(db, t, user_id=user.id, actor=actor, category=Category.WORK, purpose="Kundbesök Annanstad")
        trips.skip(days=1)
    for pat in db.execute(select(M.LocationPattern)).scalars():
        pat.status = "rejected"
    db.flush()
    t = trips.trip(HOME, UNKNOWN)
    a = classify(db, user, t)
    assert not any(s.kind in ("confirmed_history", "imported_history") for s in a.signals)
    assert a.category == Category.UNDECIDED and t.purpose != "Kundbesök Annanstad"
    patterns.rebuild(db, user.id)  # ombyggnad behåller användarens avvisning
    assert {p.status for p in db.execute(select(M.LocationPattern)).scalars()} == {"rejected"}


# ----------------------------------------------------------------------------- api-1 / api-5


def test_api1_disconnect_can_forget_app_credentials(client, db, user):
    _connect(db, user)
    db.commit()
    r = client.post(f"{API}/integrations/microsoft/disconnect")
    assert r.status_code == 200 and r.json()["status"] == "configured"
    _connect(db, user)
    db.commit()
    r = client.post(f"{API}/integrations/microsoft/disconnect", json={"forget_app": True})
    assert r.status_code == 200 and r.json()["status"] == "not_configured"
    db.expire_all()
    conn = integrations.get(db, user.id, "microsoft")
    assert not (conn.config or {}).get("client_id") and not integrations.secrets(conn)


def test_api5_status_reports_demo_events(client, db, user):
    r = client.get(f"{API}/integrations/microsoft/status")
    assert r.status_code == 200 and r.json()["demo_events"] == 0
    event(db, user, "Demo", BASE_TIME, is_demo=True)
    db.commit()
    assert client.get(f"{API}/integrations/microsoft/status").json()["demo_events"] == 1
