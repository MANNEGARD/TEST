"""1.4.2: kraschrisker som jakten efter driftstörningen i 1.4.0 hittade (påhittade data).

Båda satt i efterbearbetningen av en ny resa, där ett ohanterat undantag stoppade hämtningen
från bilen:

* **AI-svaret.** Ett svar från OpenAI i oväntat format (innehållet som en lista med delar,
  ``message`` tomt, ``usage`` eller ``model`` av fel typ) gav TypeError/AttributeError i stället
  för ett ``LLMError``. Nu blir varje sådant svar ett ``LLMError``, och ett fel som
  leverantörskoden ändå inte förutsåg låter resan bedömas med enbart reglerna.
* **Granskningsärenden.** Ett ärende vars ``data`` inte är ett objekt (text, lista) fällde
  stängningen av avvikelser, och listan i Granskning. Nu behandlas det som ett ärende utan data.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, ReviewKind, ReviewState
from app.services import audit, internal_errors
from app.services.classify import llm
from app.services.classify.llm_openai import OpenAIProvider
from app.services.stats import anomalies
from app.services.trips import pipeline, review
from tests.conftest import reload_trip
from tests.v130_ai_stod import CUSTOMER, OFFICE, answer, classify, full_context, live, local, places

A, B = "Påhittgatan 1, Exempelby", "Låtsasvägen 2, Exempelby"
GOOD = {"category": "work", "confidence": 0.8, "purpose": "Kundbesök", "purpose_supported": True,
        "evidence": ["kalenderbokning"], "requires_review": False}


# ----------------------------------------------------------------------------- AI-svaret


def provider_answering(body: Any) -> OpenAIProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return OpenAIProvider("test-nyckel", "test-modell", transport=httpx.MockTransport(handler))


def chat(message: Any, **extra: Any) -> dict[str, Any]:
    return {"choices": [{"message": message}], "model": "test-modell", **extra}


@pytest.mark.parametrize(
    "body",
    [
        chat({"content": [{"type": "text", "text": json.dumps(GOOD)}]}),  # innehållet som delar
        chat({"content": {"category": "work"}}),  # innehållet som objekt
        chat({"content": 17}),
        chat(None),  # message saknas
        chat("text i stället för objekt"),
        chat(["lista"]),
        {"choices": [None]},
        {"choices": "fel"},
        {"choices": []},
        ["inte", "ett", "objekt"],
        chat({"content": "[1, 2]"}),  # giltig JSON men inget objekt
        chat({"content": "inte json"}),
        chat({"content": json.dumps({**GOOD, "confidence": [1]})}),
    ],
)
def test_ovantat_svar_fran_openai_blir_llmerror(body):
    with pytest.raises(llm.LLMError):
        provider_answering(body).assess({"trip": {}})


def test_svar_med_konstiga_sidofalt_gar_anda_att_anvanda():
    body = chat({"content": json.dumps({**GOOD, "evidence": 5})}, usage="okänt")
    body["model"] = ["fel", "typ"]
    result = provider_answering(body).assess({"trip": {}})
    assert result is not None
    assert result.data["category"] == "work" and result.data["evidence"] == []
    assert result.model == "test-modell"
    assert result.usage == {"input_tokens": None, "output_tokens": None}


def test_vanligt_svar_tolkas_som_forut():
    body = chat({"content": json.dumps(GOOD)}, usage={"prompt_tokens": 11, "completion_tokens": 7})
    result = provider_answering(body).assess({"trip": {}})
    assert result is not None and result.data["purpose"] == "Kundbesök"
    assert result.usage == {"input_tokens": 11, "output_tokens": 7}


class BrokenProvider:
    """En leverantör vars kod går sönder på ett sätt den inte själv fångar."""

    name, model = "openai", "test-modell"

    def __init__(self) -> None:
        self.calls = 0

    def assess(self, document: dict[str, Any]) -> llm.LLMResult | None:
        self.calls += 1
        raise TypeError("the JSON object must be str, bytes or bytearray, not list")


@pytest.fixture()
def world(db, user, trips):
    full_context(db, user)
    return places(trips)


def test_oforutsett_fel_i_ai_anropet_ger_bedomning_med_reglerna(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    broken = BrokenProvider()
    with internal_errors.tolerant_mode(True):
        a = classify(db, user, t, provider=broken)
    assert broken.calls == 1
    assert any(s.kind == "ai_unavailable" for s in a.signals)
    assert reload_trip(db, t.id).category is not None  # reglerna har bedömt resan
    # Inget delsteg räknas som misslyckat (autopiloten körs som vid ett vanligt AI-fel) …
    assert internal_errors.take_failures(db) == []
    # … men felet står i felsökningsfilens lista, med typ och kodställe.
    stored = internal_errors.entries(db, user.id)
    assert [e["step"] for e in stored] == ["classify.ai"]
    assert stored[0]["type"] == "TypeError" and stored[0]["origin"] == "classify"
    assert any("test_v142_jaktens_krascher" not in f and "engine.py" in f for f in stored[0]["where"])
    assert internal_errors.step_label("classify.ai") == "AI-bedömningen"


def test_oforutsett_fel_i_ai_anropet_kastas_i_strangt_lage(db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    with internal_errors.tolerant_mode(False), pytest.raises(TypeError):
        classify(db, user, t, provider=BrokenProvider())


def test_efter_felet_fungerar_nasta_bedomning(db, user, trips, world):
    from tests.v130_ai_stod import FakeProvider

    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    with internal_errors.tolerant_mode(True):
        classify(db, user, t, provider=BrokenProvider())
        classify(db, user, t, provider=BrokenProvider())
        fake = FakeProvider(answer("Kundbesök hos Exempel Åkeri AB"))
        classify(db, user, t, provider=fake)
    assert len(fake.documents) == 1
    stored = internal_errors.entries(db, user.id)
    assert len(stored) == 1 and stored[0]["count"] == 2  # samma fel tar en plats


# ----------------------------------------------------------------------------- granskningsärenden


def broken_item(db, vehicle, key: str, data: Any, kind: ReviewKind = ReviewKind.ANOMALY) -> M.ReviewItem:
    """Ett öppet ärende vars ``data`` inte är ett objekt (fältet är fri JSON i databasen)."""
    item, _ = review.upsert(db, vehicle_id=vehicle.id, kind=kind, dedupe_key=key, title="Trasigt ärende",
                            detail="Text", data={"type": "x"})
    item.data = data
    db.flush()
    db.expire(item)
    return item


BROKEN = ["bara text", ["en", "lista"], 17]


@pytest.mark.parametrize("data", BROKEN)
def test_trasigt_arende_faller_inte_stangningen_av_avvikelser(db, user, vehicle, trips, data):
    trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    item = broken_item(db, vehicle, "anomaly:trasigt", data)
    assert anomalies.trip_refs(item) == []
    assert anomalies.resolve_trip_ids(db, [item]) == {}
    anomalies.close_noise(db, vehicle.id, scope="business")
    anomalies.close_noise(db, vehicle.id, scope="complete")
    db.flush()
    assert db.get(M.ReviewItem, item.id).status == ReviewState.OPEN  # ett fordonsärende rörs inte


@pytest.mark.parametrize("data", BROKEN)
def test_trasigt_arende_faller_inte_sokningen_eller_omrakningen(db, user, vehicle, trips, data):
    from datetime import UTC, datetime

    t = trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    broken_item(db, vehicle, "anomaly:trasigt", data)
    broken_item(db, vehicle, "overlap:trasigt", data)
    broken_item(db, vehicle, "merge:trasigt", data, kind=ReviewKind.MERGE)
    anomalies.scan_vehicle(db, vehicle, user_id=user.id, now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
    review.close_orphaned(db, vehicle.id)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, origin="live")
    assert reload_trip(db, t.id).state is not None
    db.flush()


@pytest.mark.parametrize("data", BROKEN)
def test_trasigt_arende_faller_inte_listan_i_granskning(client, db, user, vehicle, trips, data):
    trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    anomaly = broken_item(db, vehicle, "anomaly:trasigt", data)
    merge_q = broken_item(db, vehicle, "merge:trasigt", data, kind=ReviewKind.MERGE)
    db.commit()
    resp = client.get("/api/v1/review")
    assert resp.status_code == 200, resp.text
    by_id = {i["id"]: i for i in resp.json()["items"]}
    assert by_id[str(anomaly.id)]["data"] == {} and by_id[str(anomaly.id)]["trip"] is None
    assert by_id[str(merge_q.id)]["data"] == {}

    # Frågan går inte att besvara – men svaret är ett begripligt fel, inte ett internt.
    resp = client.post(f"/api/v1/merge-questions/{merge_q.id}/answer", json={"accept": True})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_question"

    # Ärendet går att avfärda som vanligt.
    resp = client.post(f"/api/v1/review/{anomaly.id}/dismiss")
    assert resp.status_code == 200, resp.text
    assert db.execute(select(M.ReviewItem.status).where(M.ReviewItem.id == anomaly.id)).scalar_one() == ReviewState.DISMISSED


# ----------------------------------------------------------------------------- granskningens fynd (1.4.2)


class BookkeepingBoom(RuntimeError):
    pass


def test_fel_i_bokforingen_efter_ett_lyckat_ai_svar_doljs_inte(db, user, trips, world, monkeypatch):
    """Bara själva AI-anropet omfattas av det breda skyddet: ett fel i vår egen bokföring efter
    ett lyckat svar ska synas som det fel det är, inte som "Oväntat fel i AI-anropet"."""
    from app.services.classify import engine
    from tests.v130_ai_stod import FakeProvider

    def boom(db_, user_id, error):
        if error is None:
            raise BookkeepingBoom("bokföringen gick sönder")

    monkeypatch.setattr(engine, "_report_llm_outcome", boom)
    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    with internal_errors.tolerant_mode(True), pytest.raises(BookkeepingBoom):
        classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB")))
    assert internal_errors.entries(db, user.id) == []  # inget antecknas som ett AI-fel


@pytest.mark.parametrize("value", ["bara text", ["en", "lista"], 17])
def test_trasiga_fria_falt_faller_inte_klassningen_eller_autopiloten(db, user, vehicle, trips, world, value):
    from sqlalchemy import update

    from app.services.classify import autopilot
    from app.services.classify import engine as engine_mod
    from tests.v130_ai_stod import FakeProvider

    t = live(trips, OFFICE, CUSTOMER, km=30, start_at=local(2026, 9, 15, 9, 0))
    classify(db, user, t, provider=FakeProvider(answer("Kundbesök hos Exempel Åkeri AB")))
    db.flush()
    # Fälten skrivs alltid som objekt av appen; här sätts de till något annat direkt i databasen.
    db.execute(update(M.TripClassification).where(M.TripClassification.logical_trip_id == t.id).values(signals=value))
    db.execute(update(M.TripPurpose).where(M.TripPurpose.logical_trip_id == t.id).values(based_on=value))
    db.expire_all()
    trip = reload_trip(db, t.id)
    assert autopilot._based_on(autopilot._current_purpose(db, trip)) == {} and autopilot._based_on(None) == {}
    # Autopiloten läser det trasiga ärendet, klassningen läser de trasiga signalerna – ingen av dem faller.
    autopilot.run_vehicle(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM)
    db.flush()
    engine_mod.classify_trip(db, reload_trip(db, t.id), user_id=user.id, actor=audit.SYSTEM, provider=None)
    db.flush()


@pytest.mark.parametrize("value", ["bara text", ["en", "lista"], 17])
def test_trasig_resolution_faller_inte_stangningen_av_avvikelser(db, user, vehicle, trips, value):
    from sqlalchemy import update

    trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    item, _ = review.upsert(db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key="anomaly:stangd", title="Stängt",
                            detail="Text", data={"type": "x"})
    review.resolve(db, item, by="system", resolution={"auto": True, "reason": review.SCOPE_REASON})
    db.flush()
    db.execute(update(M.ReviewItem).where(M.ReviewItem.id == item.id).values(resolution=value))
    db.expire_all()
    assert review.item_resolution(db.get(M.ReviewItem, item.id)) == {}
    anomalies.close_noise(db, vehicle.id, scope="business")
    anomalies.close_noise(db, vehicle.id, scope="complete")
    # Samma nyckel skapas på nytt: det trasiga ärendet öppnas inte igen av misstag och fäller inget.
    review.upsert(db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key="anomaly:stangd", title="Stängt",
                  detail="Text", data={"type": "x"}, reopen_reason=review.SCOPE_REASON)
    db.flush()


@pytest.mark.parametrize(
    ("field", "value"),
    [("via", "text"), ("via", 5), ("via", None), ("via", ["a", 1]), ("quality_flags", None), ("quality_flags", "text"),
     ("quality_flags", 5), ("quality_flags", [1, {"a": 1}])],
)
def test_trasiga_listfalt_pa_en_resa_faller_inte_listorna(client, db, user, vehicle, trips, field, value):
    from sqlalchemy import update

    t = trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    review.upsert(db, vehicle_id=vehicle.id, kind=ReviewKind.PURPOSE, dedupe_key=f"purpose:{t.id}", title="Ärende saknas",
                  detail="Text", data={}, logical_trip_id=t.id)
    db.flush()
    db.execute(update(M.LogicalTrip).where(M.LogicalTrip.id == t.id).values(**{field: value}))
    db.commit()
    resp = client.get("/api/v1/trips")
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["items"] if i["id"] == str(t.id))
    assert isinstance(row["via"], list) and isinstance(row["quality_flags"], list)
    resp = client.get("/api/v1/review")
    assert resp.status_code == 200, resp.text


def test_vanliga_listfalt_pa_en_resa_visas_som_forut(client, db, user, vehicle, trips):
    t = trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    t.via = [{"address": "Påhittvägen 9, Exempelby", "seq": 1}]
    t.quality_flags = ["end_time_uncertain", "collection_gap"]
    db.commit()
    row = next(i for i in client.get("/api/v1/trips").json()["items"] if i["id"] == str(t.id))
    assert row["via"] == [{"address": "Påhittvägen 9, Exempelby", "seq": 1}]
    assert row["quality_flags"] == ["end_time_uncertain", "collection_gap"]


def test_anthropic_ping_gor_ett_otolkbart_svar_till_ett_vanligt_fel(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    import httpx as _httpx

    provider = llm.AnthropicProvider("test-nyckel", "test-modell")

    class Models:
        def retrieve(self, model):
            raise anthropic.APIResponseValidationError(
                response=_httpx.Response(200, request=_httpx.Request("GET", "https://api.anthropic.com/v1/models/x")),
                body=None,
            )

    monkeypatch.setattr(provider, "client", type("C", (), {"models": Models()})())
    with pytest.raises(llm.LLMError):
        provider.ping()
