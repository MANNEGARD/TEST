"""1.3.0 DEL 3b – komplett förslag på syfte och skyddsräckena efter svaret (byggkontraktet 6.B–6.C,
tilläggskontraktet 3.F) samt AI-förslaget som knapp i telefonen (3.E)."""

from __future__ import annotations

import pytest

from app.models.enums import ApprovalSource, Category, DecisionSource, LocationType, ReviewStatus
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import autopilot, engine, llm
from app.services.classify import signals as sig
from app.services.classify.llm_openai import OpenAIProvider
from app.services.ha import notifier
from app.services.learning import feedback, patterns
from tests.conftest import reload_trip
from tests.v130_ai_stod import (
    CUSTOMER,
    HOME,
    OFFICE,
    TZ,
    FakeProvider,
    answer,
    classify,
    current_purpose,
    full_context,
    live,
    local,
    places,
)

UNKNOWN = "Okändvägen 4, 333 44 Norrby"


@pytest.fixture()
def world(db, user, trips):
    full_context(db, user)
    return places(trips)


def test_supported_purpose_is_a_proposal_that_owner_confirms(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB")))
    t = reload_trip(db, t.id)
    assert (t.purpose, t.purpose_source, t.purpose_confirmed) == ("Kundbesök hos Exempel Åkeri AB.",
                                                                  DecisionSource.AI, False)
    p = current_purpose(db, t)
    assert p.needs_confirmation and p.based_on["supported"] is True
    feedback.apply_decision(db, t, user_id=user.id, actor=audit.user_actor(user.id), approve=True)
    assert reload_trip(db, t.id).purpose_confirmed is True


def test_invented_name_is_rejected_and_template_used(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    a = classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Påhittat Bolag")))
    rejected = next(s for s in a.signals if s.kind == "ai_purpose_rejected")
    assert rejected.label == "AI-förslag till syfte avvisades: innehöll namn som inte finns i underlaget"
    assert {"Påhittat", "Bolag"} <= set(rejected.data["unsupported"])
    t = reload_trip(db, t.id)
    assert t.purpose == "Tjänsteresa till Exempel Åkeri AB" and not t.purpose_confirmed
    assert current_purpose(db, t).based_on["supported"] is False


def test_unsupported_type_is_kept_but_never_auto_approved(db, user, vehicle, trips, world):
    unknown = trips.location(UNKNOWN, name="Norrby Verkstad", type=LocationType.UNKNOWN, confirmed=False)
    unknown.place_kind, unknown.osm_class = "unclear", "building=yes"
    db.flush()
    t = live(trips, HOME, UNKNOWN, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer("Utbildning hos Norrby Verkstad", supported=True)))
    t = reload_trip(db, t.id)
    p = current_purpose(db, t)
    assert t.purpose == "Utbildning hos Norrby Verkstad." and p.based_on["supported"] is False
    assert p.rationale.startswith("Förslag att bekräfta – ")
    ctx = sig.build_context(db, t, user_id=user.id, tz=TZ, tax=settings_svc.get_section(db, user.id, "tax"),
                            ai=settings_svc.get_section(db, user.id, "ai"), abbreviations={})
    assert autopilot._ai_purpose(t, ctx, sig.evaluate(ctx)) is None  # mallen "Kundbesök <namn>" i stället


def test_null_purpose_and_placeholder_give_template(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer(None)))
    assert reload_trip(db, t.id).purpose == "Tjänsteresa till Exempel Åkeri AB"
    t2 = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 16, 9, 0))
    classify(db, user, t2, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB – syfte behöver bekräftas")))
    assert reload_trip(db, t2.id).purpose == "Kundbesök hos Exempel Åkeri AB."
    t3 = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 17, 9, 0))
    classify(db, user, t3, provider=FakeProvider(answer("Syfte behöver bekräftas")))
    assert reload_trip(db, t3.id).purpose == "Tjänsteresa till Exempel Åkeri AB"


@pytest.mark.parametrize("text", ["Kundbesök hos Exempel Åkeri AB och " + "mycket " * 12,
                                  "Kundbesök\nhos Exempel Åkeri AB", "Kundbesök {Exempel Åkeri AB}"])
def test_too_long_or_wrong_form_is_rejected(db, user, trips, world, text):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    a = classify(db, user, t, provider=FakeProvider(answer(text)))
    assert any(s.kind == "ai_purpose_rejected" for s in a.signals)
    assert reload_trip(db, t.id).purpose == "Tjänsteresa till Exempel Åkeri AB"


def test_bare_generic_word_is_not_supported(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer("Tjänsteresa")))
    assert current_purpose(db, reload_trip(db, t.id)).based_on["supported"] is False


def test_schema_requires_purpose_supported_in_both_providers():
    assert "purpose_supported" in llm.ASSESSMENT_SCHEMA["required"]
    assert "purpose_needs_confirmation" not in llm.ASSESSMENT_SCHEMA["properties"]
    import app.services.classify.llm_openai as openai_mod

    assert openai_mod.ASSESSMENT_SCHEMA is llm.ASSESSMENT_SCHEMA and openai_mod.SYSTEM_PROMPT is llm.SYSTEM_PROMPT
    assert llm._coerce({"category": "work", "confidence": 0.5, "purpose": "X"})["purpose_supported"] is False
    assert OpenAIProvider  # samma schema och prompt


def test_prompt_has_the_new_rules():
    for phrase in ("Kalendern väger tyngst", "calendar_stay", "calendar_ahead", "company_domain",
                   "Privata bokningar och onlinemöten förklarar aldrig en resa", "aldrig över 80",
                   "lunchmönster", "Journalen lämnas till arbetsgivaren och Skatteverket"):
        assert phrase in llm.SYSTEM_PROMPT


def test_full_level_calls_ai_without_material(db, user, trips, world):
    t = live(trips, HOME, UNKNOWN, km=30, start_at=local(2026, 9, 15, 9, 0))
    fake = FakeProvider(answer(None, category="undecided"))
    classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    settings_svc.update_section(db, user.id, "ai", {"context_level": "minimal"})
    t2 = live(trips, HOME, UNKNOWN, km=30, start_at=local(2026, 9, 16, 9, 0))
    classify(db, user, t2, provider=fake)
    assert len(fake.documents) == 1  # minimalt: inget underlag – ingen fråga till AI


def test_purpose_from_ai_can_be_turned_off(db, user, trips, world):
    settings_svc.update_section(db, user.id, "ai", {"use_llm_for_purpose": False})
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB", category="work")))
    assert reload_trip(db, t.id).purpose_source != DecisionSource.AI


def test_ai_unavailable_falls_back_to_rules(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    a = classify(db, user, t, provider=FakeProvider(fail=True))
    assert any(s.kind == "ai_unavailable" for s in a.signals)
    assert reload_trip(db, t.id).purpose_source != DecisionSource.AI


# ----------------------------------------------------------------------------- knapparna


def test_ai_suggestion_is_a_button_only_without_confirmed_purposes(db, user, vehicle, trips, world):
    t = live(trips, HOME, UNKNOWN, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer("Tjänsteresa till Norrby", category="work")))
    t = reload_trip(db, t.id)
    items = autopilot.purpose_candidate_items(db, t, user_id=user.id)
    assert items == [("Tjänsteresa till Norrby.", "ai")]
    assert notifier._suggestion_items(db, user.id, t) == [("Tjänsteresa till Norrby.", "ai")]
    # "Alltid: …" erbjuds aldrig för ett AI-förslag.
    assert notifier._can_remember(db, t, "Tjänsteresa till Norrby.", "ai") is False
    assert notifier._can_remember(db, t, "Tjänsteresa till Norrby.", "calendar") is False

    # Med ägarens bekräftade ärenden för målet går de före – och AI-förslaget visas inte.
    for i in range(2):
        trips.trip(HOME, UNKNOWN, km=30, category=Category.WORK, purpose="Service i Norrby",
                   review_status=ReviewStatus.APPROVED, start_at=local(2026, 9, 1 + i, 9, 0))
    patterns.rebuild(db, user.id)
    items = autopilot.purpose_candidate_items(db, reload_trip(db, t.id), user_id=user.id)
    assert items == [("Service i Norrby", "history")]


def test_month_labels_and_enum():
    assert ApprovalSource.HOME_OFFICE == "home_office" and ApprovalSource.LUNCH == "lunch"
    assert engine.LLM_MAX_CONFIDENCE == 0.7
