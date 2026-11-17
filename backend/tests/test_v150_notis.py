"""1.5.0: frågan till telefonen utgår från DriveLogs bedömning.

En resa som DriveLog redan har bedömt får en fråga som säger vad DriveLog kom fram till och
ber om ett godkännande ("Tjänsteresa – stämmer det?", "Tjänsteresa – vilket ärende?",
"Privat resa – stämmer det?"), i både självgående och manuellt läge. En resa som inte är
bedömd får exakt samma fråga som i 1.4.2 (de16760).
"""

# ruff: noqa: F811  (fixturerna importeras från test_ha_support och test_s2_telefon)
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    DecisionSource,
    NotificationKind,
    NotificationStatus,
    ReviewKind,
    ReviewState,
    ReviewStatus,
)
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.ha import actions as A
from app.services.ha import notifier, responses
from app.services.learning import feedback
from tests.conftest import reload_trip
from tests.test_ha_support import NOW, fake_ha, setup_push  # noqa: F401
from tests.test_s2_telefon import (  # noqa: F401
    SINCE,
    calls,
    check_payload,
    home,
    keys,
    live_trip,
    questions,
    suggest,
)

pytestmark = pytest.mark.new_defaults

PURPOSE = "Kundbesök Acme"
WORK_KINDS = (ReviewKind.CATEGORY, ReviewKind.PURPOSE)


# ----------------------------------------------------------------------------- hjälpare


def _setup(db, user, mode: str, hide: bool) -> None:
    setup_push(db, user, hide_locations_on_lock_screen=hide)
    automation: dict[str, Any] = {"mode": mode}
    if mode == settings_svc.MODE_SELF_DRIVING:
        automation["active_since"] = SINCE.isoformat()
    settings_svc.update_section(db, user.id, "automation", automation, allow_read_only=True)
    db.flush()


def _trip(db, trips, assessment: str, **kw: Any) -> M.LogicalTrip:
    """Resa som ägarens: bedömd som tjänst med ärende (proposed), tjänst utan ärende,
    privat, eller inte bedömd. Öppna ärenden i Granskning som pipelinen skulle ha gett."""
    if assessment == "work_purpose":
        args: dict[str, Any] = {"category": Category.WORK, "review_status": ReviewStatus.PROPOSED,
                                "purpose": PURPOSE, "purpose_confirmed": False,
                                "category_source": DecisionSource.RULE, "kinds": WORK_KINDS}
    elif assessment == "work":
        args = {"category": Category.WORK, "review_status": ReviewStatus.PROPOSED,
                "category_source": DecisionSource.RULE, "kinds": WORK_KINDS}
    elif assessment == "private":
        args = {"category": Category.PRIVATE, "review_status": ReviewStatus.PROPOSED,
                "category_source": DecisionSource.RULE, "kinds": (ReviewKind.CATEGORY,)}
    else:
        args = {"kinds": (ReviewKind.CATEGORY,)}
    args.update(kw)
    return live_trip(db, trips, **args)


def _ask(db, user, vehicle, trip, mode: str) -> M.NotificationRequest:
    if mode == settings_svc.MODE_SELF_DRIVING:
        assert notifier.ensure_open_questions(db, NOW)["created"] == 1
    else:
        notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                     trip_items=[(trip, [ReviewKind.CATEGORY, ReviewKind.PURPOSE])], now=NOW)
    (req,) = questions(db, NotificationKind.CLASSIFY)
    return req


def _deliver(db, mode: str) -> None:
    notifier.deliver_due(db, NOW if mode == settings_svc.MODE_SELF_DRIVING else NOW + timedelta(minutes=11))


def answer(db, user, req, action: str, text: str | None = None, *, source: str = "ha",
           key: str | None = None) -> dict[str, Any]:
    return responses.handle(db, user_id=user.id, public_id=req.public_id, action=action, reply_text=text,
                            idempotency_key=key or uuid.uuid4().hex, source=source,
                            now=NOW + timedelta(minutes=15))


def open_items(db, trip) -> list[str]:
    return sorted(
        i.kind for i in db.execute(
            select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == trip.id, M.ReviewItem.status == ReviewState.OPEN)
        ).scalars()
    )


def titles(req) -> list[str]:
    return [a["title"] for a in req.actions]


MODES = [settings_svc.MODE_SELF_DRIVING, settings_svc.MODE_MANUAL]


@pytest.fixture()
def no_suggestions(monkeypatch):
    suggest(monkeypatch, [], remember=False)
    monkeypatch.setattr(notifier, "previous_purposes", lambda *a, **k: [])


# ----------------------------------------------------------------------------- frågorna


@pytest.mark.parametrize("mode", MODES)
def test_work_with_purpose_asks_for_approval(db, user, vehicle, trips, fake_ha, monkeypatch, mode):
    suggest(monkeypatch, ["Något annat"], remember=True)
    _setup(db, user, mode, hide=False)
    home(trips)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, mode)
    check_payload(req)
    assert req.title == "Tjänsteresa – stämmer det?"
    assert keys(req) == ["WORKP0", "WORKTEXT", "PRIVATE", "LATER", "WORKA0"]
    assert titles(req) == ["Godkänn", "Ändra ärende", "Privat", "Senare", f"Alltid: {PURPOSE}"]
    assert req.actions[0]["purpose"] == PURPOSE and req.actions[1]["text_input"] is True
    assert req.message.startswith("tis 15/9 ") and req.message.endswith(f"4,2 mil. Ärende: {PURPOSE}.")
    route = "Hemvägen 1, Hemby → Drottninggatan 12, Örebro" if mode == "self_driving" else "Hemmet → Örebro"
    assert f" · {route} · " in req.message
    assert req.context["assessment"] == "work_purpose" and req.context["proposed_purpose"] == PURPOSE


def test_purpose_that_ends_with_a_full_stop_gets_one_full_stop(db, user, vehicle, trips, fake_ha, monkeypatch):
    """Ärenden som regeln skriver slutar ofta med punkt: notisen ska inte få två."""
    suggest(monkeypatch, [], remember=False)
    mode = settings_svc.MODE_SELF_DRIVING
    _setup(db, user, mode, hide=False)
    home(trips)
    trip = _trip(db, trips, "work_purpose", purpose="Hemresa från kunden.")
    req = _ask(db, user, vehicle, trip, mode)
    assert req.message.endswith("4,2 mil. Ärende: Hemresa från kunden.") and ".." not in req.message
    assert req.actions[0]["purpose"] == "Hemresa från kunden."  # ärendet som sparas är oförändrat


@pytest.mark.parametrize("mode", MODES)
def test_work_with_purpose_hidden_on_lock_screen(db, user, vehicle, trips, fake_ha, monkeypatch, mode):
    suggest(monkeypatch, [], remember=True)
    _setup(db, user, mode, hide=True)
    home(trips)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, mode)
    assert req.title == "Tjänsteresa – stämmer det?"
    assert titles(req) == ["Godkänn förslaget", "Ändra ärende", "Privat", "Senare", "Alltid: föreslaget ärende"]
    assert PURPOSE not in req.message and "Örebro" not in req.message and "Hemby" not in req.message
    assert all(PURPOSE not in t and "Örebro" not in t for t in titles(req))
    assert req.message.endswith("4,2 mil. Ärendet visas i appen.")
    assert PURPOSE in req.context["detail_message"]
    # Det som skickas till telefonen innehåller varken ärendet eller platserna.
    _deliver(db, mode)
    (sent,) = fake_ha.sent
    assert PURPOSE not in str(sent)
    assert all(PURPOSE not in a["title"] for a in sent["data"]["actions"])
    assert PURPOSE not in sent["message"] and "Örebro" not in sent["message"]


def test_always_is_never_offered_for_ai_or_calendar(db, user, vehicle, trips, fake_ha, monkeypatch):
    suggest(monkeypatch, [], remember=True)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    trip = _trip(db, trips, "work_purpose")
    seen: list[str] = []

    def origin(db, trip, text):
        seen.append(text)
        return "ai"

    monkeypatch.setattr(autopilot, "candidate_origin", origin)
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    assert keys(req) == ["WORKP0", "WORKTEXT", "PRIVATE", "LATER"]
    assert seen == [PURPOSE]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("hide", [False, True])
def test_work_without_purpose_asks_which(db, user, vehicle, trips, fake_ha, monkeypatch, mode, hide):
    suggest(monkeypatch, ["Kundbesök Acme", "Montage"], remember=True)
    _setup(db, user, mode, hide=hide)
    home(trips)
    trip = _trip(db, trips, "work")
    req = _ask(db, user, vehicle, trip, mode)
    check_payload(req)
    assert req.title == "Tjänsteresa – vilket ärende?"
    assert keys(req) == ["WORKP0", "WORKP1", "WORKTEXT", "PRIVATE", "LATER", "WORKA0"]
    if hide:
        assert titles(req) == ["Föreslaget ärende 1", "Föreslaget ärende 2", "Skriv ärende", "Privat", "Senare",
                               "Alltid: föreslaget ärende 1"]
        assert "Örebro" not in req.message and req.message.endswith("4,2 mil.")
    else:
        assert titles(req) == ["Kundbesök Acme", "Montage", "Skriv ärende", "Privat", "Senare",
                               "Alltid: Kundbesök Acme"]
        assert "Örebro" in req.message
    assert req.context["assessment"] == "work" and "proposed_purpose" not in req.context


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("hide", [False, True])
def test_private_asks_for_confirmation(db, user, vehicle, trips, fake_ha, no_suggestions, mode, hide):
    _setup(db, user, mode, hide=hide)
    home(trips)
    trip = _trip(db, trips, "private")
    req = _ask(db, user, vehicle, trip, mode)
    check_payload(req)
    assert req.title == "Privat resa – stämmer det?"
    assert keys(req) == ["PRIVATE", "WORKTEXT", "LATER"]
    assert titles(req) == ["Ja, privat", "Tjänst – skriv ärende", "Senare"]
    assert ("Örebro" in req.message) is (not hide)
    assert req.message.startswith("tis 15/9 ") and req.message.endswith("4,2 mil.")


# Utfallet i de16760 (1.4.2) för en resa som inte är bedömd. Testerna nedan kördes även mot
# koden i de16760 (git archive, PYTHONPATH) och gav samma rubrik, text och knappar.
UNDECIDED_SD_HIDDEN = ("Ny resa", "tis 15/9 · 4,2 mil. Tjänst eller privat?",
                       [("WORKP0", "Tjänst: föreslaget ärende"), ("WORKA0", "Alltid: föreslaget ärende"),
                        ("WORKTEXT", "Tjänst – skriv ärende"), ("PRIVATE", "Privat"),
                        ("PRIVALWAYS", "Alltid privat här"), ("LATER", "Senare")])
UNDECIDED_SD_VISIBLE_ACTIONS = [("WORKP0", "Tjänst: Kundbesök Acme"), ("WORKA0", "Alltid: Kundbesök Acme"),
                                ("WORKTEXT", "Tjänst – skriv ärende"), ("PRIVATE", "Privat"),
                                ("PRIVALWAYS", "Alltid privat här"), ("LATER", "Senare")]
UNDECIDED_MANUAL = {
    True: ("Ny resa", "Ny resa: 4,2 mil. Var detta arbete eller privat?"),
    False: ("Ny resa", "Ny resa: Hemby → Örebro. 4,2 mil. Var detta arbete eller privat?"),
}
UNDECIDED_MANUAL_ACTIONS = [("WORK", "Arbete"), ("PRIVATE", "Privat"), ("REVIEW", "Granska"), ("LATER", "Senare")]


def _pairs(req) -> list[tuple[str, str]]:
    return [(a["key"], a["title"]) for a in req.actions]


@pytest.mark.parametrize("hide", [False, True])
def test_undecided_self_driving_is_unchanged(db, user, vehicle, trips, fake_ha, monkeypatch, hide):
    suggest(monkeypatch, [PURPOSE], remember=True)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=hide)
    home(trips)
    trip = _trip(db, trips, "undecided")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    assert req.title == "Ny resa"
    if hide:
        assert (req.title, req.message, _pairs(req)) == UNDECIDED_SD_HIDDEN
    else:
        assert _pairs(req) == UNDECIDED_SD_VISIBLE_ACTIONS
        assert "Hemvägen 1, Hemby → Drottninggatan 12, Örebro · 4,2 mil. Tjänst eller privat?" in req.message
    assert "assessment" not in req.context


@pytest.mark.parametrize("hide", [False, True])
def test_undecided_manual_is_unchanged(db, user, vehicle, trips, fake_ha, no_suggestions, hide):
    _setup(db, user, settings_svc.MODE_MANUAL, hide=hide)
    trip = _trip(db, trips, "undecided")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_MANUAL)
    assert (req.title, req.message) == UNDECIDED_MANUAL[hide]
    assert _pairs(req) == UNDECIDED_MANUAL_ACTIONS


def test_multi_trip_keeps_the_old_question(db, user, vehicle, trips, fake_ha, monkeypatch):
    suggest(monkeypatch, [PURPOSE], remember=True)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    trip = _trip(db, trips, "work_purpose", quality_flags=["detour_or_multiple_trips"])
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    assert req.title == "Ny resa" and "Kan vara flera körningar." in req.message
    assert "WORKP0" not in keys(req)


@pytest.mark.parametrize("mode", MODES)
def test_mixed_trip_keeps_the_old_question(db, user, vehicle, trips, fake_ha, monkeypatch, mode):
    """Blandad resa: "Godkänn" skulle bara spara ärendet, inte godkänna – frågan som i de16760."""
    suggest(monkeypatch, [PURPOSE], remember=True)
    _setup(db, user, mode, hide=False)
    home(trips)
    trip = _trip(db, trips, "work_purpose")
    trip.has_mixed_categories = True
    db.flush()
    req = _ask(db, user, vehicle, trip, mode)
    assert req.title == "Ny resa"
    assert "assessment" not in req.context


@pytest.mark.parametrize("mode", MODES)
def test_long_purpose_is_not_offered_for_one_tap_approval(db, user, vehicle, trips, fake_ha, monkeypatch, mode):
    """Ett ärende över 200 tecken skulle kortas i knappen och sparas kortat som bekräftat."""
    long = "Kundbesök " + "x" * 250
    suggest(monkeypatch, [PURPOSE], remember=True)
    _setup(db, user, mode, hide=False)
    home(trips)
    trip = _trip(db, trips, "work_purpose", purpose=long)
    req = _ask(db, user, vehicle, trip, mode)
    assert req.title == "Tjänsteresa – vilket ärende?"
    assert req.context["assessment"] == "work"
    assert all(a.get("purpose") != long[:200] for a in req.actions)


# ----------------------------------------------------------------------------- svaren hela vägen


def _approved(db, trip, *, category: str, purpose: str | None) -> M.LogicalTrip:
    t = reload_trip(db, trip.id)
    assert t.category == category
    assert t.review_status == ReviewStatus.APPROVED
    assert t.approval_source == "user"
    if purpose is None:
        assert not t.purpose
    else:
        assert t.purpose == purpose and t.purpose_confirmed
    assert open_items(db, trip) == []
    history = feedback.history(db, trip.id)
    assert history and history[0].channel == "ha"
    assert db.execute(
        select(M.AuditEvent).where(M.AuditEvent.entity_id == str(trip.id), M.AuditEvent.actor_type == "ha")
    ).first() is not None
    return t


def _no_new_question(db, trip) -> None:
    notifier.ensure_open_questions(db, NOW + timedelta(hours=1))
    open_reqs = [r for r in questions(db) if r.status in notifier.OPEN_STATUSES and r.logical_trip_id == trip.id]
    assert open_reqs == []


CASES = [
    # (bedömning, knapp, text, kategori, ärende, regel sparas)
    ("work_purpose", "WORKP0", None, Category.WORK, PURPOSE, False),
    ("work_purpose", "WORKTEXT", "Service Bring", Category.WORK, "Service Bring", False),
    ("work_purpose", "PRIVATE", None, Category.PRIVATE, None, False),
    ("work_purpose", "WORKA0", None, Category.WORK, PURPOSE, True),
    ("work", "WORKP0", None, Category.WORK, "Kundbesök Acme", False),
    ("work", "WORKP1", None, Category.WORK, "Montage", False),
    ("work", "WORKTEXT", "Möte hos Bring", Category.WORK, "Möte hos Bring", False),
    ("work", "PRIVATE", None, Category.PRIVATE, None, False),
    ("work", "WORKA0", None, Category.WORK, "Kundbesök Acme", True),
    ("private", "PRIVATE", None, Category.PRIVATE, None, False),
    ("private", "WORKTEXT", "Kundbesök Acme", Category.WORK, "Kundbesök Acme", False),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("assessment", "button", "text", "category", "purpose", "rule"), CASES)
def test_every_button_reaches_the_journal(db, user, vehicle, trips, fake_ha, monkeypatch, calls, mode,
                                          assessment, button, text, category, purpose, rule):
    suggest(monkeypatch, ["Kundbesök Acme", "Montage"], remember=True)
    _setup(db, user, mode, hide=True)
    home(trips)
    trip = _trip(db, trips, assessment)
    req = _ask(db, user, vehicle, trip, mode)
    _deliver(db, mode)
    res = answer(db, user, req, A.action_id(button, req.public_id), text)
    assert res["outcome"] == "applied", res
    assert res["category"] == category
    _approved(db, trip, category=category, purpose=purpose)
    assert calls["remember"] == ([(trip.id, category, purpose)] if rule else [])
    db.refresh(req)
    assert req.status == NotificationStatus.ANSWERED
    if mode == settings_svc.MODE_SELF_DRIVING:
        _no_new_question(db, trip)


def test_later_snoozes_without_changing_the_trip(db, user, vehicle, trips, fake_ha, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    _deliver(db, settings_svc.MODE_SELF_DRIVING)
    assert answer(db, user, req, "LATER")["outcome"] == "snoozed"
    t = reload_trip(db, trip.id)
    assert t.review_status == ReviewStatus.PROPOSED and not t.purpose_confirmed


def test_web_answer_gives_the_same_journal(db, user, vehicle, trips, fake_ha, monkeypatch, calls):
    suggest(monkeypatch, [], remember=False)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=True)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    res = answer(db, user, req, "WORKP0", source="web")
    assert res["outcome"] == "applied"
    t = reload_trip(db, trip.id)
    assert (t.category, t.purpose, t.purpose_confirmed, t.review_status, t.approval_source) == (
        Category.WORK, PURPOSE, True, ReviewStatus.APPROVED, "user")
    assert open_items(db, trip) == []


def test_same_answer_twice(db, user, vehicle, trips, fake_ha, monkeypatch, calls):
    suggest(monkeypatch, [], remember=False)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    _deliver(db, settings_svc.MODE_SELF_DRIVING)
    first = answer(db, user, req, "WORKP0", key="ev-1")
    assert first["outcome"] == "applied"
    n = len(feedback.history(db, trip.id))
    again = answer(db, user, req, "WORKP0", key="ev-1")
    assert again["outcome"] == "duplicate" and again["notify_user"] is False
    other = answer(db, user, req, "PRIVATE", key="ev-2")  # en andra knapp efteråt ändrar inget
    assert other["outcome"] == "duplicate"
    t = reload_trip(db, trip.id)
    assert t.category == Category.WORK and t.purpose == PURPOSE
    assert len(feedback.history(db, trip.id)) == n


def test_answer_after_owner_decided_in_web(db, user, vehicle, trips, fake_ha, monkeypatch, client):
    suggest(monkeypatch, [], remember=False)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    _deliver(db, settings_svc.MODE_SELF_DRIVING)
    db.commit()
    r = client.patch(f"/api/v1/trips/{trip.id}", json={"category": "private"})
    assert r.status_code == 200, r.text
    db.expire_all()
    res = answer(db, user, req, "WORKP0")
    assert res["outcome"] == "duplicate" and res["notify_user"] is False
    t = reload_trip(db, trip.id)
    assert t.category == Category.PRIVATE and not t.purpose


@pytest.mark.parametrize("change", ["purpose_edited", "reassessed_private"])
@pytest.mark.parametrize("button", ["WORKP0", "WORKA0"])
def test_approve_refuses_when_the_proposal_changed(db, user, vehicle, trips, fake_ha, monkeypatch, calls,
                                                   change, button):
    suggest(monkeypatch, [], remember=True)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    trip = _trip(db, trips, "work_purpose")
    req = _ask(db, user, vehicle, trip, settings_svc.MODE_SELF_DRIVING)
    _deliver(db, settings_svc.MODE_SELF_DRIVING)
    t = reload_trip(db, trip.id)
    if change == "purpose_edited":
        t.purpose = "Service Bring"  # ägaren ändrade ärendet i webben (utan att godkänna)
    else:
        t.category, t.purpose = Category.PRIVATE, None  # resan bedömdes om
    db.flush()
    res = answer(db, user, req, button)
    assert res["outcome"] == "invalid" and res["code"] == "proposal_changed" and res["notify_user"] is True
    t = reload_trip(db, trip.id)
    assert t.review_status == ReviewStatus.PROPOSED
    assert t.purpose == ("Service Bring" if change == "purpose_edited" else None)
    assert calls["remember"] == []
    # De andra knapparna fungerar fortfarande: ägaren skriver det rätta ärendet.
    ok = answer(db, user, req, "WORKTEXT", "Service Bring")
    assert ok["outcome"] == "applied"
    assert reload_trip(db, trip.id).purpose == "Service Bring"


def test_month_end_reasks_with_the_assessment(db, user, vehicle, trips, fake_ha, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    _setup(db, user, settings_svc.MODE_SELF_DRIVING, hide=False)
    end_sept = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    trip = _trip(db, trips, "work_purpose", hours_ago=1 - 14 * 24)
    notifier.ensure_open_questions(db, end_sept)
    notifier.deliver_due(db, end_sept)
    notifier.deliver_due(db, end_sept + timedelta(hours=49))
    (orig,) = questions(db)
    assert orig.status == NotificationStatus.EXPIRED and orig.title == "Tjänsteresa – stämmer det?"
    october = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
    assert notifier.ensure_open_questions(db, october)["month_reasked"] == 1
    reasked = questions(db)[-1]
    assert reasked.dedupe_key == f"classify:{trip.id}:m"
    assert reasked.title == "Tjänsteresa – stämmer det?" and keys(reasked)[0] == "WORKP0"
    res = responses.handle(db, user_id=user.id, public_id=reasked.public_id, action="WORKP0", reply_text=None,
                           idempotency_key="m-1", source="ha", now=october + timedelta(minutes=5))
    assert res["outcome"] == "applied"
    assert reload_trip(db, trip.id).review_status == ReviewStatus.APPROVED


def test_keys_fit_the_integration_pattern():
    """Nycklarna följer mönstret i actions.py och i HA-integrationen 1.3.0 (oförändrad)."""
    from tests.test_ha_integration_core import core

    for key in ("WORKP0", "WORKP1", "WORKA0", "WORKTEXT", "PRIVATE", "LATER"):
        aid = A.action_id(key, "ab12cd34ef56ab78")
        assert A.parse_action_id(aid) == (key, "ab12cd34ef56ab78")
        assert core.parse_action_id(aid) == (key, "ab12cd34ef56ab78")
