"""1.3.0 DEL 1: sammanslagning med olika gränser för tjänsteresor och privata resor.

* Två avgjorda tjänsteresor i rad följer tjänstegränserna, två privata privatgränserna.
* En tjänsteresa och en privat, eller en resa som inte är avgjord (eller bara antagen), slås
  aldrig ihop automatiskt och ger ingen fråga. Stopp hemma eller vid en privat plats aldrig.
* Det ägaren ställer in är det som händer, även i självgående läge.
* En befintlig installation behåller det tidigare regelverket (``by_category = false``) tills
  ägaren sparar sektionen; då gäller reglerna bara resor från och med nu.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.db import utcnow
from app.models.enums import (
    ApprovalSource,
    Category,
    LocationType,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TripSource,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.learning import feedback, patterns, recompute
from app.services.trips import merge, pipeline
from app.services.trips import segments as seg
from tests.conftest import reload_trip
from tests.test_ha_support import setup_pull

HOME = "Hemvägen 1, 111 11 Exempelstad"
ACME = "Industrivägen 10, 222 33 Verkstadsby"
BETA = "Lagergatan 4, 333 44 Norrby"
GAMMA = "Gammagatan 3, 444 55 Söderby"
FUEL = "Macken 1, 111 44 Exempelstad"
SECRET = "Hemligvägen 5, 555 66 Norrby"
ACTIVE_SINCE = "2020-01-01T00:00:00+00:00"


@pytest.fixture()
def by_category(monkeypatch):
    """Ny installation: kategorireglerna gäller från start (ingen startpunkt)."""
    monkeypatch.setitem(settings_svc.DEFAULTS["merge"], "by_category", True)


@pytest.fixture()
def places(trips) -> dict[str, M.KnownLocation]:
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "acme": trips.location(ACME, name="Acme AB", type=LocationType.CUSTOMER, company="Acme AB"),
        "beta": trips.location(BETA, name="Beta AB", type=LocationType.CUSTOMER, company="Beta AB"),
        "fuel": trips.location(FUEL, name="Macken", type=LocationType.FUEL_STATION),
        "secret": trips.location(SECRET, name="Privat ställe", type=LocationType.PRIVATE),
    }


def evaluate(db, user, vehicle, *, apply_auto: bool = True) -> dict[str, Any]:
    cfg = settings_svc.get_section(db, user.id, "merge")
    out = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=audit.SYSTEM,
                                 apply_auto=apply_auto)
    db.flush()
    return out


WORK = {"category": Category.WORK, "purpose": "Kundbesök"}
PRIV = {"category": Category.PRIVATE}
PROPOSED_WORK = {"category": Category.WORK, "review_status": ReviewStatus.PROPOSED}
IMPORTED_WORK = {"category": Category.WORK, "review_status": ReviewStatus.IMPORTED, "purpose": "Kundbesök"}


def pair(trips, first: dict[str, Any], second: dict[str, Any], *, stop: str = BETA, minutes: float = 10,
         source: str = TripSource.VOLVO_IMPORT) -> tuple[M.LogicalTrip, M.LogicalTrip]:
    a = trips.trip(ACME, stop, km=12, minutes=20, source=source, **first)
    trips.skip(minutes=minutes)
    b = trips.trip(stop, GAMMA, km=8, minutes=15, source=source, **second)
    return a, b


def question_titles(db) -> list[str]:
    return [i.title for i in db.execute(
        select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.MERGE, M.ReviewItem.status == ReviewState.OPEN)
    ).scalars()]


# ----------------------------------------------------------------------------- tabellen 2.A.2


@pytest.mark.parametrize(
    ("first", "second", "stop", "minutes", "merged", "question"),
    [
        (WORK, WORK, BETA, 10, True, None),
        (WORK, WORK, BETA, 30, False, "Slå samman två tjänsteresor? Stillestånd: 30 minuter."),
        (WORK, WORK, BETA, 90, False, None),
        (PRIV, PRIV, BETA, 10, True, None),
        (PRIV, PRIV, BETA, 30, False, None),  # privat frågegräns 15 = ingen fråga
        (WORK, PRIV, BETA, 5, False, None),
        (WORK, PROPOSED_WORK, BETA, 5, False, None),
        (WORK, WORK, HOME, 5, False, None),
        (WORK, WORK, SECRET, 5, False, None),
        (WORK, WORK, FUEL, 5, True, None),  # tankstopp: tjänstegränsen, ingen särregel
        (IMPORTED_WORK, IMPORTED_WORK, BETA, 10, True, None),
    ],
    ids=["tjanst-10", "tjanst-30-fraga", "tjanst-90", "privat-10", "privat-30", "tjanst-privat", "tjanst-forslag",
         "hemma", "privat-plats", "tankstation", "importerade"],
)
def test_category_table(db, user, vehicle, trips, places, by_category, first, second, stop, minutes, merged, question):
    a, b = pair(trips, first, second, stop=stop, minutes=minutes)
    out = evaluate(db, user, vehicle)
    assert out["auto_merged"] == (1 if merged else 0)
    assert question_titles(db) == ([question] if question else [])
    if merged:
        (result,) = [t for t in seg.active_trips(db, vehicle.id)]
        assert result.segment_count == 2 and result.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
        assert result.category == first["category"]
    else:
        assert {t.id for t in seg.active_trips(db, vehicle.id)} == {a.id, b.id}


def test_merged_result_keeps_the_weakest_approval(db, user, vehicle, trips, places, by_category):
    a, b = pair(trips, WORK, WORK)
    b.approval_source = ApprovalSource.RULE
    evaluate(db, user, vehicle)
    (result,) = seg.active_trips(db, vehicle.id)
    assert result.review_status == ReviewStatus.APPROVED and result.approval_source == ApprovalSource.RULE
    op = db.execute(select(M.TripMerge)).scalar_one()
    assert op.trigger == "rule_a"


def test_private_question_title_and_own_limits(db, user, vehicle, trips, places, by_category):
    settings_svc.update_section(db, user.id, "merge", {"private_auto_max_minutes": 5, "private_ask_max_minutes": 40})
    pair(trips, PRIV, PRIV, minutes=30)
    out = evaluate(db, user, vehicle)
    assert out["auto_merged"] == 0
    assert question_titles(db) == ["Slå samman två privata resor? Stillestånd: 30 minuter."]


def test_import_without_automatic_merge_asks(db, user, vehicle, trips, places, by_category):
    pair(trips, IMPORTED_WORK, IMPORTED_WORK, minutes=10)
    out = evaluate(db, user, vehicle, apply_auto=False)
    assert out["auto_merged"] == 0
    assert question_titles(db) == ["Slå samman två tjänsteresor? Stillestånd: 10 minuter."]


def test_keep_separate_decision_is_respected(db, user, vehicle, trips, places, by_category):
    a, b = pair(trips, WORK, WORK, minutes=10)
    merge.remember_decision(db, vehicle.id, seg.ordered_raws(a)[-1].id, seg.ordered_raws(b)[0].id,
                            "keep_separate", "user")
    assert evaluate(db, user, vehicle)["auto_merged"] == 0
    assert question_titles(db) == []


def test_mixed_trip_is_not_decided(db, user, vehicle, trips, places, by_category):
    a, b = pair(trips, WORK, WORK, minutes=5)
    a.has_mixed_categories = True
    db.flush()
    assert merge.decided_category(a) is None
    assert evaluate(db, user, vehicle)["auto_merged"] == 0


def test_assumed_trip_is_not_merged(db, user, vehicle, trips, places, by_category):
    a, b = pair(trips, WORK, WORK, minutes=5)
    b.approval_source = ApprovalSource.ASSUMED
    db.flush()
    assert evaluate(db, user, vehicle)["auto_merged"] == 0
    assert question_titles(db) == []


def test_disabled_merges_nothing(db, user, vehicle, trips, places, by_category):
    settings_svc.update_section(db, user.id, "merge", {"enabled": False})
    pair(trips, WORK, WORK, minutes=5)
    assert evaluate(db, user, vehicle)["auto_merged"] == 0


def test_self_driving_follows_the_same_rules(db, user, vehicle, trips, places, by_category):
    """Det ägaren ställer in är det som händer: inget dolt tankstoppsundantag."""
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    pair(trips, WORK, WORK, minutes=10, source=TripSource.VOLVO_API)
    assert evaluate(db, user, vehicle)["auto_merged"] == 1


# ----------------------------------------------------------------------------- övergången


def test_existing_install_keeps_old_rules_until_saved(db, user, vehicle, trips, places):
    """by_category = false (befintlig installation, självgående): bara bekräftad tankstation, inga frågor."""
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    assert settings_svc.get_section(db, user.id, "merge")["by_category"] is False
    pair(trips, WORK, WORK, stop=BETA, minutes=10, source=TripSource.VOLVO_API)
    trips.skip(minutes=120)
    pair(trips, WORK, WORK, stop=BETA, minutes=30, source=TripSource.VOLVO_API)
    trips.skip(minutes=120)
    pair(trips, WORK, WORK, stop=FUEL, minutes=10, source=TripSource.VOLVO_API)
    out = evaluate(db, user, vehicle)
    assert out["auto_merged"] == 1 and out["questions"] == []
    merged = [t for t in seg.active_trips(db, vehicle.id) if t.segment_count == 2]
    assert len(merged) == 1 and seg.ordered_raws(merged[0])[0].end_address == FUEL


def test_saving_switches_to_category_rules_from_now_on(db, user, vehicle, trips, places):
    pair(trips, IMPORTED_WORK, IMPORTED_WORK, minutes=10)
    trips.skip(minutes=120)
    pair(trips, IMPORTED_WORK, IMPORTED_WORK, minutes=90)
    settings_svc.update_section(db, user.id, "merge", {"by_category": True, "ask_max_minutes": 120})
    cfg = settings_svc.get_section(db, user.id, "merge")
    since = merge.rules_since(cfg)
    assert cfg["by_category"] is True and since is not None
    # Äldre resor slås inte ihop och ger inga frågor i efterhand – inte ens med högre frågegräns.
    assert evaluate(db, user, vehicle)["auto_merged"] == 0
    assert question_titles(db) == []
    assert all(t.segment_count == 1 for t in seg.active_trips(db, vehicle.id))

    # En import efter övergången prövas.
    trips.skip(minutes=120)
    new = pair(trips, IMPORTED_WORK, IMPORTED_WORK, minutes=10)
    for t in new:
        seg.ordered_raws(t)[0].created_at = since + timedelta(seconds=1)
    db.flush()
    assert evaluate(db, user, vehicle)["auto_merged"] == 1


def test_category_rules_since_is_read_only_and_set_once(db, user):
    with pytest.raises(settings_svc.SettingsError, match="sätts av DriveLog"):
        settings_svc.update_section(db, user.id, "merge", {"category_rules_since": "2026-01-01T00:00:00+00:00"})
    settings_svc.update_section(db, user.id, "merge", {"by_category": True})
    first = settings_svc.get_section(db, user.id, "merge")["category_rules_since"]
    assert first
    settings_svc.update_section(db, user.id, "merge", {"by_category": True, "auto_max_minutes": 20})
    assert settings_svc.get_section(db, user.id, "merge")["category_rules_since"] == first


def test_new_install_has_category_rules_without_start(db, user, by_category):
    cfg = settings_svc.get_section(db, user.id, "merge")
    assert cfg["by_category"] is True and cfg["category_rules_since"] == ""
    assert (cfg["private_auto_max_minutes"], cfg["private_ask_max_minutes"]) == (15, 15)


def test_validation_texts(db, user):
    with pytest.raises(settings_svc.SettingsError, match="Gränsen för fråga måste vara minst lika stor"):
        settings_svc.update_section(db, user.id, "merge", {"auto_max_minutes": 30, "ask_max_minutes": 20})
    with pytest.raises(settings_svc.SettingsError, match="Gränsen för fråga för privata resor"):
        settings_svc.update_section(db, user.id, "merge", {"private_auto_max_minutes": 30,
                                                           "private_ask_max_minutes": 20})
    with pytest.raises(settings_svc.SettingsError, match="mellan 0 och 240"):
        settings_svc.update_section(db, user.id, "merge", {"private_auto_max_minutes": 500})


def test_settings_api_carries_the_new_keys(client, db, user, by_category):
    r = client.patch("/api/v1/settings/merge", json={"private_auto_max_minutes": 10, "private_ask_max_minutes": 20})
    assert r.status_code == 200, r.text
    body = client.get("/api/v1/settings").json()
    merge_cfg = body["merge"] if "merge" in body else body["settings"]["merge"]
    assert merge_cfg["private_auto_max_minutes"] == 10 and merge_cfg["by_category"] is True
    r = client.patch("/api/v1/settings/merge", json={"private_auto_max_minutes": 30, "private_ask_max_minutes": 20})
    assert r.status_code == 400 and r.json()["code"] == "invalid_setting"
    r = client.patch("/api/v1/settings/merge", json={"category_rules_since": "2026-01-01T00:00:00+00:00"})
    assert r.status_code == 400


# ----------------------------------------------------------------------------- när kategorin blir känd


def test_pipeline_merges_after_autopilot_in_the_same_run(db, user, vehicle, trips, places, by_category):
    """2.A.3: med kategoriregler slås resor ihop efter klassning och autopilot."""
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=places["beta"].id, category=Category.WORK,
                             purpose="Service av maskin")
    gamma = trips.location(GAMMA, name="Gamma AB", type=LocationType.CUSTOMER, company="Gamma AB")
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=gamma.id, category=Category.WORK,
                             purpose="Service av maskin")
    pair(trips, {}, {}, stop=BETA, minutes=10, source=TripSource.VOLVO_API)
    out = pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False)
    assert out["auto_merged"] == 1
    (result,) = seg.active_trips(db, vehicle.id)
    assert (result.review_status, result.approval_source) == (ReviewStatus.APPROVED, ApprovalSource.RULE)


def test_owner_answer_merges_via_after_decisions(db, user, vehicle, trips, places, by_category, actor, monkeypatch):
    from app.services.ha import notifier

    sent: list[Any] = []
    monkeypatch.setattr(notifier, "enqueue_for_changes", lambda db, **kw: sent.append(kw) or [])
    a, b = pair(trips, WORK, {}, minutes=30)
    feedback.apply_decision(db, b, user_id=user.id, actor=actor, category=Category.WORK, purpose="Kundbesök",
                            approve=True)
    assert question_titles(db) == ["Slå samman två tjänsteresor? Stillestånd: 30 minuter."]
    assert len(sent) == 1 and sent[0]["merge_items"] and sent[0]["trip_items"] == []


def test_owner_answer_via_recompute_reaches_the_phone(db, user, vehicle, trips, places, by_category, actor,
                                                     monkeypatch, job_queue):
    """Ägarens svar i webben räknas om i bakgrunden: frågan uppstår först när omräkningen körts."""
    from app.services.ha import notifier

    sent: list[Any] = []
    real = notifier.enqueue_for_changes
    monkeypatch.setattr(notifier, "enqueue_for_changes", lambda db, **kw: sent.append(kw) or real(db, **kw))
    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    start = utcnow() - timedelta(hours=3)
    a = trips.trip(ACME, BETA, km=12, minutes=20, start_at=start, **WORK)
    b = trips.trip(BETA, GAMMA, km=8, minutes=15, start_at=start + timedelta(minutes=50))
    db.commit()

    recompute.defer(db)
    feedback.apply_decision(db, reload_trip(db, b.id), user_id=user.id, actor=actor, category=Category.WORK,
                            purpose="Kundbesök", approve=True)
    db.commit()
    assert question_titles(db) == [] and sent == []
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    db.expire_all()
    assert question_titles(db) == ["Slå samman två tjänsteresor? Stillestånd: 30 minuter."]
    assert len(sent) == 1
    merge_question = db.execute(select(M.NotificationRequest).where(M.NotificationRequest.kind == "merge")).scalar_one()
    assert merge_question.review_item_id is not None
    assert reload_trip(db, a.id).state == "active"


def test_old_rules_after_decisions_does_nothing(db, user, vehicle, trips, places, actor):
    a, b = pair(trips, WORK, {}, minutes=10)
    feedback.apply_decision(db, b, user_id=user.id, actor=actor, category=Category.WORK, approve=True)
    assert all(t.segment_count == 1 for t in seg.active_trips(db, vehicle.id))
    assert merge.evaluate_after_decisions(db, vehicle, audit.SYSTEM) == []


# ----------------------------------------------------------------------------- 2.A.4 i efterhand


def test_changing_one_to_the_other_category_does_nothing(db, user, vehicle, trips, places, by_category, actor):
    a, b = pair(trips, WORK, PRIV, minutes=60)
    assert evaluate(db, user, vehicle)["auto_merged"] == 0
    feedback.apply_decision(db, a, user_id=user.id, actor=actor, category=Category.PRIVATE, approve=True)
    assert question_titles(db) == []  # 60 min > privat frågegräns


def test_changing_merged_trip_category_keeps_one_trip(db, user, vehicle, trips, places, by_category, actor):
    pair(trips, WORK, WORK, minutes=5)
    evaluate(db, user, vehicle)
    (merged,) = seg.active_trips(db, vehicle.id)
    feedback.apply_decision(db, merged, user_id=user.id, actor=actor, category=Category.PRIVATE, approve=True)
    assert len(seg.active_trips(db, vehicle.id)) == 1


def test_split_auto_merge_is_never_merged_again(db, user, vehicle, trips, places, by_category, actor):
    pair(trips, WORK, WORK, minutes=5)
    evaluate(db, user, vehicle)
    (merged,) = seg.active_trips(db, vehicle.id)
    merge.split_trip(db, merged, after_index=0, actor=actor)
    assert evaluate(db, user, vehicle)["auto_merged"] == 0
    assert len(seg.active_trips(db, vehicle.id)) == 2
