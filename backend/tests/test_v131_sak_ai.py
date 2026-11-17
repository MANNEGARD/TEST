"""1.3.1, säkerhet – språkmodellens underlag och svar (påhittade bilar, platser och personer).

* logik-3: osynliga tecken och styrtecken (bidi, nollbredd, BEL) i AI:ns ärende och "evidence"
  sparas inte; synlig text, å/ä/ö är kvar.
* logik-5: ett dygnstak för AI-anrop; över taket bedöms resan av reglerna som när ingen
  leverantör finns (ingen ny fråga, inget fel) och AI prövas igen när taket släpper.
* lucka2-1: bilens VIN och registreringsnummer i annan skrivform (blanksteg, bindestreck,
  helbredd) och ägarens andra bilar tas bort ur det fullständiga underlaget.
* lucka2-2: nivån Minimalt skickar inte heller VIN eller registreringsnummer.
* lucka2-3: namnspärren prövar också ord i andra skriftsystem och helbreddstecken; riktiga namn
  ur underlaget (Łukasz, Gdańsk) godtas som förut.
* lucka2-4: ett ord som bara finns i en e-postadress i rubriken upphäver inte domänspärren.
* lucka2-5: koder och lösenord i böjd form tas bort ur fritext; vanlig text står kvar.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.services import settings as settings_svc
from app.services.classify import engine, underlag
from app.services.classify.llm import _coerce
from tests.conftest import reload_trip
from tests.test_v130_ai_underlag import build
from tests.v130_ai_stod import (
    CUSTOMER,
    OFFICE,
    FakeProvider,
    answer,
    attendee,
    classify,
    current_purpose,
    event,
    full_context,
    live,
    local,
    match,
    places,
)

VIN = "YV1ZZZ8A1B2345678"
DAY = (2026, 9, 15)


@pytest.fixture()
def world(db, user, trips):
    full_context(db, user)
    return places(trips)


def _customer_trip(trips, day: int = 15):
    return live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, day, 9, 0))


# ----------------------------------------------------------------------------- logik-3


def test_hidden_characters_in_ai_purpose_and_evidence_are_removed(db, user, trips, world):
    t = _customer_trip(trips)
    data = answer("Kundbesök hos Exempel‮ Åkeri​ AB\x07")
    data["evidence"] = ["kalender‮bokning​\x07", "platsens typ"]
    a = classify(db, user, t, provider=FakeProvider(data))
    t = reload_trip(db, t.id)
    assert t.purpose == "Kundbesök hos Exempel Åkeri AB."
    p = current_purpose(db, t)
    assert p.rationale == "AI-förslag utifrån: kalenderbokning; platsens typ"
    for s in a.signals:
        for item in (s.data or {}).get("evidence", []):
            assert not any(c in item for c in "‮​\x07")


def test_evidence_is_cleaned_in_coerce():
    data = _coerce({"category": "work", "confidence": 0.5, "purpose": None, "purpose_supported": False,
                    "evidence": ["Rad⁦ ett\x1b", "kontakt anna@kundbolaget.example", "", 7],
                    "requires_review": True})
    assert data["evidence"] == ["Rad ett", "kontakt [borttaget]", "7"]


def test_multiline_ai_purpose_is_still_rejected(db, user, trips, world):
    t = _customer_trip(trips)
    a = classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB")))
    assert any(s.kind == "ai_purpose_rejected" for s in a.signals)


# ----------------------------------------------------------------------------- logik-5


def test_daily_cap_falls_back_to_rules_and_retries_later(db, user, vehicle, trips, world, monkeypatch):
    from app.services import audit

    monkeypatch.setattr(engine, "LLM_DAILY_LIMIT", 2)
    engine._llm_calls.pop(user.id, None)
    for day in (15, 16, 17, 18):
        _customer_trip(trips, day)
    fake = FakeProvider(answer("Kundbesök hos Exempel Åkeri AB"))
    engine.classify_pending(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, provider=fake)
    assert len(fake.documents) == 2
    evidence = db.execute(select(M.TripEvidence.kind)).scalars().all()
    assert "ai_unavailable" not in evidence  # inget fel visas
    rows = db.execute(select(M.TripClassification).where(M.TripClassification.is_current.is_(True))).scalars().all()
    assert len(rows) == 4 and sorted(r.source for r in rows) == ["ai", "ai", "rule", "rule"]

    # Samma körning igen inom taket: inga fler anrop, inget skrivs om.
    engine.classify_pending(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, provider=fake)
    assert len(fake.documents) == 2

    # Dygnet har gått: resorna som bara fick reglerna bedöms nu av AI.
    engine._llm_calls.pop(user.id, None)
    engine.classify_pending(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, provider=fake)
    assert len(fake.documents) == 4
    rows = db.execute(select(M.TripClassification).where(M.TripClassification.is_current.is_(True))).scalars().all()
    assert sorted(r.source for r in rows) == ["ai"] * 4
    engine._llm_calls.pop(user.id, None)


def test_daily_cap_does_not_stop_owner_started_reassessment(db, user, trips, world, monkeypatch):
    monkeypatch.setattr(engine, "LLM_DAILY_LIMIT", 0)
    engine._llm_calls.pop(user.id, None)
    t = _customer_trip(trips)
    fake = FakeProvider(answer("Kundbesök hos Exempel Åkeri AB"))
    classify(db, user, t, provider=fake)  # force=True: ägaren bad själv om en ny bedömning
    assert len(fake.documents) == 1
    engine._llm_calls.pop(user.id, None)


def test_daily_cap_is_generous():
    assert engine.LLM_DAILY_LIMIT >= 1000


# ----------------------------------------------------------------------------- lucka2-1


def test_vehicle_identity_in_other_spellings_never_leaves(db, user, vehicle, trips, world):
    vehicle.vin = VIN
    other = M.Vehicle(owner_user_id=user.id, name="Släpbil", make="Volvo", model="XC40",
                      registration_number="XYZ 789")
    db.add(other)
    world["customer"].notes = ("Service ABC 123 hos verkstaden\nÄven ABC-123 och ＡＢＣ１２３"
                               "\nLånebil XYZ789 och xyz-789")
    db.flush()
    t = _customer_trip(trips)
    event(db, user, "Däckbyte abc 123", t.ended_at + timedelta(minutes=10), location_city="Verkstadsby",
          body_preview="Bil YV1 ZZZ8A1 B2345678 och YV1-ZZZ8A1-B2345678 lämnas")
    match(db, user, t)
    doc, _ = build(db, user, t)
    text = json.dumps(doc, ensure_ascii=False)
    for secret in ("ABC 123", "ABC-123", "abc 123", "ＡＢＣ", "XYZ789", "xyz-789", "ZZZ8A1",
                   "B2345678"):
        assert secret not in text, secret
    note = doc["destination"]["note"]
    assert note.startswith("Service [borttaget] hos verkstaden")  # resten av texten är kvar
    assert "Däckbyte [borttaget]" in text and "lämnas" in text


def test_vehicle_redaction_leaves_ordinary_text(db, user, vehicle, trips, world):
    world["customer"].notes = "Lastkaj ABC vid port 123, fråga efter Abbe"
    db.flush()
    t = _customer_trip(trips)
    doc, _ = build(db, user, t)
    assert doc["destination"]["note"] == "Lastkaj ABC vid port 123, fråga efter Abbe"


# ----------------------------------------------------------------------------- lucka2-2


def test_minimal_level_removes_vin_and_registration(db, user, vehicle, trips, world):
    settings_svc.update_section(db, user.id, "ai", {"context_level": "minimal", "send_calendar_body": True})
    vehicle.vin = VIN
    db.flush()
    t = _customer_trip(trips)
    event(db, user, "Däckbyte ABC123", t.ended_at + timedelta(minutes=10), location_city="Verkstadsby",
          body_preview=f"Bil {VIN} och ABC 123")
    match(db, user, t)
    fake = FakeProvider(answer("Kundbesök hos Exempel Åkeri AB"))
    classify(db, user, t, provider=fake)
    doc = fake.documents[-1]
    assert "level" not in doc  # minimalt underlag
    text = json.dumps(doc, ensure_ascii=False)
    for secret in ("ABC123", "ABC 123", VIN):
        assert secret not in text, secret
    assert doc["calendar"][0]["subject"] == "Däckbyte [borttaget]"


# ----------------------------------------------------------------------------- lucka2-3


@pytest.mark.parametrize("text", [
    "Kundbesök hos Ｐａｈｉｔｔａｔ Ｂｏｌａｇ",
    "Kundbesök hos Кундболагет",
    "Möte ｈｔｔｐｓ：／／ｅｖｉｌ．ｅｘ",
    "Kundbesök hos Exempel Åkeri AB och Μπαλάκος",
])
def test_names_in_other_scripts_without_support_are_rejected(db, user, trips, world, text):
    t = _customer_trip(trips)
    a = classify(db, user, t, provider=FakeProvider(answer(text)))
    assert any(s.kind == "ai_purpose_rejected" for s in a.signals)
    assert reload_trip(db, t.id).purpose == "Tjänsteresa till Exempel Åkeri AB"


def test_real_names_from_the_material_are_still_accepted(db, user, trips, world):
    t = _customer_trip(trips)
    event(db, user, "Service med Łukasz från Gdańsk", t.ended_at + timedelta(minutes=10),
          location_city="Verkstadsby")
    match(db, user, t)
    classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB med Łukasz från Gdańsk")))
    assert reload_trip(db, t.id).purpose == "Kundbesök hos Exempel Åkeri AB med Łukasz från Gdańsk."


def test_fullwidth_spelling_of_a_real_name_is_saved_normalized(db, user, trips, world):
    t = _customer_trip(trips)
    classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Ｅｘｅｍｐｅｌ Åkeri AB")))
    assert reload_trip(db, t.id).purpose == "Kundbesök hos Exempel Åkeri AB."


# ----------------------------------------------------------------------------- lucka2-4


def test_word_only_in_email_address_does_not_lift_domain_guard(db, user, trips, world):
    settings_svc.update_section(db, user.id, "calendar", {"work_domain": "exempelakeri.se"})
    t = _customer_trip(trips)
    event(db, user, "Möte anna@kundbolaget.se", t.ended_at + timedelta(minutes=10), location_city="Verkstadsby",
          attendees=[attendee("Anna Exempel", "anna@kundbolaget.se")])
    match(db, user, t)
    a = classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Kundbolaget")))
    assert any(s.kind == "ai_purpose_rejected" for s in a.signals)
    t2 = _customer_trip(trips, 16)
    event(db, user, "Möte anna@kundbolaget.se", t2.ended_at + timedelta(minutes=10), location_city="Verkstadsby",
          attendees=[attendee("Anna Exempel", "anna@kundbolaget.se")])
    match(db, user, t2)
    classify(db, user, t2, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB med Anna")))
    assert reload_trip(db, t2.id).purpose == "Kundbesök hos Exempel Åkeri AB med Anna."


# ----------------------------------------------------------------------------- lucka2-5


@pytest.mark.parametrize("line,expected", [
    ("Portkoden är 4711", "Portkoden är [borttaget]"),
    ("Larmkoden: 1234", "Larmkoden: [borttaget]"),
    ("Lösenordet: Hemligt9", "Lösenordet: [borttaget]"),
    ("wifi: Gast2026!", "wifi: [borttaget]"),
    ("Kod till grinden 2580", "Kod till grinden [borttaget]"),
    ("Teams-ID: 3456", "Teams-ID: [borttaget]"),
    ("token=ghp_abc123DEF456ghi789", "token= [borttaget]"),
    ("Portkod 4711", "Portkod [borttaget]"),
])
def test_codes_and_passwords_in_inflected_form_are_removed(line, expected):
    assert underlag._clean(line, "note", free_text=True) == expected


@pytest.mark.parametrize("line", [
    "Kodgranskning projekt Alfa",
    "Möte om kodstandard",
    "Kodgranskning: projekt X",
    "Wifi-möte hos Exempel AB",
    "Koder diskuteras med Lennart",
    "Lastkaj på baksidan, fråga efter Lennart",
])
def test_ordinary_text_with_the_words_is_kept(line):
    assert underlag._clean(line, "note", free_text=True) == line
