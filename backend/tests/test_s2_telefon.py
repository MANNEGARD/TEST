"""S2a: frågor i telefonen i självgående läge (byggkontraktet C.4, E.3–E.5).

Nyttolaster per typ, varje svar, en fråga åt gången, påminnelse och undanläggning,
omfrågan vid månadsskiftet, ensure_open_questions, månads- och larmnotiser, en
deterministisk länk i utkorgen och att inget tappas när ingen telefon är vald.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    LocationType,
    NotificationKind,
    NotificationStatus,
    ReviewKind,
    ReviewState,
    TripSource,
)
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.export import links
from app.services.ha import actions as A
from app.services.ha import notices, notifier, responses
from tests.conftest import reload_trip
from tests.test_ha_support import (  # noqa: F401
    HOME,
    JONKOPING,
    NOW,
    OREBRO,
    fake_ha,
    recent_trip,
    setup_pull,
    setup_push,
)

pytestmark = pytest.mark.new_defaults

SINCE = datetime(2026, 9, 1, tzinfo=UTC)
KEY_RE = re.compile(r"^[A-Z]+[0-9]?$")
HOUSE_NUMBER_RE = re.compile(r"[A-Za-zåäöÅÄÖ]+ \d+[A-Za-z]?\b(?!/)")


# ----------------------------------------------------------------------------- hjälpare


@pytest.fixture()
def sd(db, user, fake_ha):
    """Självgående läge med push till en telefon och active_since före testresorna."""
    setup_push(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    db.flush()
    return fake_ha


@pytest.fixture()
def calls(monkeypatch) -> dict[str, list[Any]]:
    """Fångar anropen till autopilotens skrivande funktioner (spår 1 bygger dem)."""
    seen: dict[str, list[Any]] = {"remember": [], "confirm": [], "company": []}

    def remember(db, *, user_id, trip, category, purpose, actor):
        seen["remember"].append((trip.id, category, purpose))
        return M.LocationPattern()

    monkeypatch.setattr(autopilot, "remember_always", remember)
    monkeypatch.setattr(autopilot, "confirm_assumption", lambda db, trip, *, actor: seen["confirm"].append(trip.id))
    monkeypatch.setattr(autopilot, "set_company", lambda db, trip, text, *, actor: seen["company"].append(text))
    return seen


def suggest(monkeypatch, cands: list[str], *, remember: bool = True) -> None:
    monkeypatch.setattr(autopilot, "purpose_candidates", lambda db, trip, *, user_id, limit=2: cands[:limit])
    monkeypatch.setattr(autopilot, "can_remember", lambda db, trip, text: remember)


def live_trip(
    db, trips, start: str = HOME, end: str = OREBRO, *, hours_ago: float = 1.0,
    kinds: tuple[str, ...] = (ReviewKind.CATEGORY,), created_at: datetime | None = None,
    source: str = TripSource.VOLVO_API, **kw: Any,
) -> M.LogicalTrip:
    trip = recent_trip(trips, start, end, source=source, hours_ago=hours_ago, **kw)
    for s in trip.segments:
        s.raw_trip.created_at = created_at or (trip.ended_at or NOW)
    for kind in kinds:
        db.add(M.ReviewItem(vehicle_id=trip.vehicle_id, kind=kind, dedupe_key=f"{kind}:{trip.id}",
                            logical_trip_id=trip.id, title="Fråga", status=ReviewState.OPEN))
    db.flush()
    return trip


def home(trips) -> M.KnownLocation:
    return trips.location(HOME, name="Hemmet", type=LocationType.HOME)


def questions(db, kind: str | None = None) -> list[M.NotificationRequest]:
    q = select(M.NotificationRequest).where(M.NotificationRequest.kind.in_(notifier.SD_QUESTION_KINDS))
    if kind:
        q = select(M.NotificationRequest).where(M.NotificationRequest.kind == kind)
    return list(db.execute(q.order_by(M.NotificationRequest.created_at)).scalars())


def keys(req: M.NotificationRequest) -> list[str]:
    return [a["key"] for a in req.actions]


def check_payload(req: M.NotificationRequest) -> None:
    assert 1 <= len(req.actions) <= A.MAX_ACTIONS
    assert all(KEY_RE.match(k) for k in keys(req))
    assert len(set(keys(req))) == len(req.actions)
    assert "REVIEW" not in keys(req)  # knappen "Granska" finns inte i självgående läge


def answer(db, user, req, action: str, text: str | None = None, *, source: str = "ha", always: bool = False,
           now: datetime | None = None) -> dict[str, Any]:
    return responses.handle(db, user_id=user.id, public_id=req.public_id, action=action, reply_text=text,
                            idempotency_key=uuid.uuid4().hex, source=source, now=now or NOW + timedelta(minutes=1),
                            always=always)


# ----------------------------------------------------------------------------- nyttolaster


def test_classify_payload_with_suggestion(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Acme", "Service"])
    home(trips)
    trip = live_trip(db, trips)
    assert notifier.ensure_open_questions(db, NOW)["created"] == 1
    (req,) = questions(db)
    check_payload(req)
    assert keys(req) == ["WORKP0", "WORKA0", "WORKTEXT", "PRIVATE", "PRIVALWAYS", "LATER"]
    titles = [a["title"] for a in req.actions]
    assert titles[:2] == ["Tjänst: Kundbesök Acme", "Alltid: Kundbesök Acme"]
    assert req.actions[2]["text_input"] is True
    assert req.title == "Ny resa"
    assert req.message.endswith("Tjänst eller privat?")
    assert "Hemmet → Drottninggatan, Örebro" in req.message and "4,2 mil" in req.message
    assert "12" not in req.message.split("·")[1]  # aldrig husnummer för en obekräftad plats
    assert req.logical_trip_id == trip.id and req.dedupe_key == f"classify:{trip.id}"


def test_classify_payload_without_suggestion_or_rule(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    monkeypatch.setattr(notifier, "previous_purposes", lambda *a, **k: [])
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    check_payload(req)
    assert keys(req) == ["WORKTEXT", "PRIVATE", "LATER"]


def test_classify_multi_trip_has_no_work_shortcut(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Acme"])
    trip = live_trip(db, trips)
    trip.quality_flags = ["detour_or_multiple_trips"]
    db.flush()
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    assert "WORKP0" not in keys(req) and "WORKA0" not in keys(req)
    assert "Kan vara flera körningar." in req.message


def test_lock_screen_hides_places_but_detail_keeps_them(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    settings_svc.update_section(db, user.id, "notifications", {"hide_locations_on_lock_screen": True})
    home(trips)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    assert req.message == "tis 15/9 · 4,2 mil. Tjänst eller privat?"
    assert "Hemmet" in req.context["detail_message"]


def test_purpose_payload(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Acme", "Montage"])
    trip = live_trip(db, trips, kinds=(ReviewKind.PURPOSE,), category=Category.WORK)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    check_payload(req)
    assert req.kind == NotificationKind.PURPOSE and req.title == "Vilket ärende?"
    assert keys(req) == ["PURPOSE0", "PURPOSE1", "PURPOSEA0", "PURPOSETEXT", "PRIVATE", "LATER"]
    assert req.message == "Drottninggatan, Örebro, 4,2 mil"
    assert req.dedupe_key == f"purpose:{trip.id}"


def test_company_payload_for_unnamed_workplace(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [])
    trip = live_trip(db, trips)
    dest = db.get(M.KnownLocation, trip.end_location_id)
    dest.place_kind, dest.confirmed, dest.company = "work", False, None
    db.flush()
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    check_payload(req)
    assert req.kind == NotificationKind.COMPANY and req.title == "Vilket företag var det?"
    assert keys(req) == ["COMPANYTEXT", "PRIVATE", "LATER"]
    assert req.actions[0]["text_input"] is True


def _assumed_trip(db, trips, *, veto: timedelta = timedelta(hours=24)) -> M.LogicalTrip:
    trip = live_trip(db, trips, kinds=(), category=Category.WORK, purpose="Kundbesök Dagab")
    trip.approval_source = "assumed"
    trip.auto_approved_at = NOW
    trip.approval_basis = {"reason": "Antaget kundbesök", "veto_until": (NOW + veto).isoformat()}
    db.flush()
    return trip


def test_assumed_notice_first_time_per_place(db, user, trips, sd):
    trip = _assumed_trip(db, trips)
    req = notices.queue_assumed(db, user_id=user.id, trip=trip, now=NOW)
    assert req is not None
    check_payload(req)
    assert keys(req) == ["OK", "PRIVATE", "WORKTEXT"]
    assert [a["title"] for a in req.actions] == ["Stämmer", "Fel – privat", "Annat ärende"]
    assert req.title == "Antaget: Kundbesök Dagab"
    assert req.message == "tis 15/9 4,2 mil · Räknas som tjänst i morgon 12:00 om du inte säger emot"
    second = _assumed_trip(db, trips)
    assert notices.queue_assumed(db, user_id=user.id, trip=second, now=NOW) is None


def test_assumed_notice_hides_name_on_lock_screen(db, user, trips, sd):
    settings_svc.update_section(db, user.id, "notifications", {"hide_locations_on_lock_screen": True})
    req = notices.queue_assumed(db, user_id=user.id, trip=_assumed_trip(db, trips), now=NOW)
    assert req.title == "Antaget kundbesök" and "Dagab" not in req.message
    assert "Dagab" in req.context["detail_message"]


def test_assumed_notice_not_sent_with_veto_zero(db, user, trips, sd):
    settings_svc.update_section(db, user.id, "automation", {"assumption_veto_hours": 0})
    trip = _assumed_trip(db, trips)
    assert notices.queue_assumed(db, user_id=user.id, trip=trip, now=NOW) is None


def test_assumed_is_not_a_question(db, user, trips, sd, monkeypatch):
    """Antagande, larm och månadsnotis går fram även när en fråga redan väntar på svar."""
    suggest(monkeypatch, [], remember=False)
    live_trip(db, trips, hours_ago=3)
    notifier.ensure_open_questions(db, NOW)
    assert notifier.deliver_due(db, NOW)["sent"] == 1
    notices.queue_assumed(db, user_id=user.id, trip=_assumed_trip(db, trips), now=NOW)
    notices.queue_alert(db, user_id=user.id, code="vehicle_silent", episode="2026-09-10",
                        title="Bilen har inte hörts av", message="Det är normalt när bilen står still.", now=NOW)
    assert notifier.deliver_due(db, NOW + timedelta(minutes=1))["sent"] == 2
    assert len(sd.sent) == 3


# ----------------------------------------------------------------------------- månad och larm


@dataclass
class Note:
    code: str
    text: str
    count: int = 0
    trip_ids: list[str] = field(default_factory=list)
    web_only: bool = False


@dataclass
class Status:
    vehicle_id: uuid.UUID
    month: str
    state: str
    blockers: list[Note]
    notes: list[Note]
    export_id: uuid.UUID | None = None


def _export(db, user, vehicle, **summary: Any) -> M.ExportJob:
    job = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, status="done", sha256="ab" * 32, origin="month_close",
                      summary={"trips": 23, "km": 4120.0, "mil": 412.0, "amount": 10300.0, **summary})
    db.add(job)
    db.flush()
    return job


def test_month_ready_link_is_deterministic_and_never_stored(db, user, vehicle, monkeypatch):
    setup_pull(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    job = _export(db, user, vehicle)
    monkeypatch.setattr(
        links, "make_url", lambda job, user, exp: f"https://dl.example/{job.id.hex}.{int(exp.timestamp())}.sig"
    )
    status = Status(vehicle.id, "2026-10", "exported", [], [Note("assumed", "3 antagna", 3),
                                                           Note("missing_start_time", "1 resa", 1)], job.id)
    req = notices.queue_month_ready(db, user_id=user.id, status=status, export=job, now=NOW)
    assert req is not None and req.kind == NotificationKind.MONTHLY
    check_payload(req)
    assert req.title == "Oktober klart"
    assert req.message == (
        "23 tjänsteresor, 412 mil, 10 300 kr, varav 3 rader antagna – se dem innan du skickar."
        " 1 resa saknar starttid."
    )
    assert keys(req) == ["OPEN", "OPEN1"]
    assert req.actions[0]["uri"] == f"export-link:{job.id}" and req.actions[1]["uri"] == "/m/month/2026-10"
    assert "dl.example" not in str(req.actions) and "dl.example" not in str(req.context)
    first = notifier.outbox(db, user.id, NOW + timedelta(minutes=1))
    second = notifier.outbox(db, user.id, NOW + timedelta(minutes=2))
    (item1,), (item2,) = first, second
    url1 = item1["data"]["data"]["actions"][0]["uri"]
    assert url1 == item2["data"]["data"]["actions"][0]["uri"]
    assert url1 == f"https://dl.example/{job.id.hex}.{req.context['link_exp']}.sig"
    assert item1["data"]["data"]["actions"][1]["uri"] == "http://testserver/m/month/2026-10"
    # En ny notis för samma fil skapas aldrig.
    assert notices.queue_month_ready(db, user_id=user.id, status=status, export=job, now=NOW) is None


def test_month_ready_without_link_falls_back_to_export_page(db, user, vehicle, sd):
    job = _export(db, user, vehicle)
    status = Status(vehicle.id, "2026-10", "exported", [], [], job.id)
    req = notices.queue_month_ready(db, user_id=user.id, status=status, export=job, now=NOW)
    data = notifier.service_data(req, db=db)
    assert [a["uri"] for a in data["data"]["actions"]] == ["http://testserver/export"]
    assert keys(req) == ["OPEN"]  # inga antagna → ingen "Se antagna"


def test_month_updated_and_empty_and_waiting(db, user, vehicle, sd):
    old = _export(db, user, vehicle)
    new = _export(db, user, vehicle, trips=24, km=4154.0)
    old.superseded_by_id = new.id
    db.flush()
    status = Status(vehicle.id, "2026-10", "exported", [], [], new.id)
    req = notices.queue_month_ready(db, user_id=user.id, status=status, export=new, now=NOW)
    assert req.title == "Oktober uppdaterad" and req.message == "+1 resa, +34 km."
    empty = notices.queue_month_ready(db, user_id=user.id, status=Status(vehicle.id, "2026-11", "empty", [], []),
                                      export=None, now=NOW)
    assert empty.title == "November: inga tjänsteresor"
    waiting = Status(vehicle.id, "2026-12", "waiting",
                     [Note("month_not_over", ""), Note("open_questions", "1 resa väntar på ditt svar", 1)], [])
    w = notices.queue_month_waiting(db, user_id=user.id, status=waiting, now=NOW)
    assert w.title == "December väntar" and w.message == "1 resa väntar på ditt svar"
    assert [a["uri"] for a in w.actions] == ["/export"]
    # Högst en väntenotis per dygn.
    assert notices.queue_month_waiting(db, user_id=user.id, status=waiting, now=NOW + timedelta(hours=2)) is None
    for r in (req, empty, w):
        for word in ("Hemmet", "Örebro", vehicle.registration_number):
            assert word not in r.message


def test_alert_one_per_episode_and_resolve_without_push(db, user, sd):
    args = {"user_id": user.id, "code": "calendar_broken", "title": "Kalendern", "message": "Logga in igen.", "now": NOW}
    req = notices.queue_alert(db, episode="2026-09-14T08:00:00+00:00", **args)
    assert req is not None and req.title == "DriveLog: Kalendern" and keys(req) == ["OPEN"]
    assert req.dedupe_key == "alert:calendar_broken:2026-09-14T08:00:00+00:00"
    assert notices.queue_alert(db, episode="2026-09-14T08:00:00+00:00", **args) is None
    notifier.deliver_due(db, NOW)
    assert len(sd.sent) == 1
    assert notices.resolve_alert(db, user_id=user.id, code="calendar_broken") == 1
    notifier.deliver_due(db, NOW + timedelta(minutes=1))
    assert len(sd.sent) == 1 and len(sd.clears) == 1  # bara rensning, ingen ny push
    db.refresh(req)
    assert req.status == NotificationStatus.CANCELLED


# ----------------------------------------------------------------------------- svaren


@pytest.fixture()
def classify_req(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Acme"])
    trip = live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    notifier.deliver_due(db, NOW)
    (req,) = questions(db)
    return req, trip


def test_answer_work_pick(db, user, classify_req, calls):
    req, trip = classify_req
    res = answer(db, user, req, "WORKP0")
    assert res["outcome"] == "applied" and res["purpose"] == "Kundbesök Acme"
    t = reload_trip(db, trip.id)
    assert t.category == Category.WORK and t.purpose == "Kundbesök Acme" and t.purpose_confirmed
    assert t.review_status == "approved"
    assert calls["remember"] == []
    db.refresh(req)
    assert req.status == NotificationStatus.ANSWERED


def test_answer_work_always_saves_rule(db, user, classify_req, calls):
    req, trip = classify_req
    res = answer(db, user, req, f"DRIVELOG_WORKA0_{req.public_id}")
    assert res["outcome"] == "applied" and "Regeln sparades" in res["message"]
    assert calls["remember"] == [(trip.id, Category.WORK, "Kundbesök Acme")]


def test_answer_work_text_is_sanitized(db, user, classify_req, calls):
    req, trip = classify_req
    res = answer(db, user, req, "WORKTEXT", "  Möte‮ hos​ Bring\n AB ")
    assert res["outcome"] == "applied"
    assert reload_trip(db, trip.id).purpose == "Möte hos Bring AB"


@pytest.mark.parametrize("text", ["", "   ", "​‮", "x" * 201])
def test_answer_text_out_of_bounds_is_invalid(db, user, classify_req, text):
    req, trip = classify_req
    res = answer(db, user, req, "WORKTEXT", text)
    assert res["outcome"] == "invalid"
    assert reload_trip(db, trip.id).category == Category.UNDECIDED


def test_answer_private_and_private_always(db, user, trips, sd, monkeypatch, calls):
    suggest(monkeypatch, [])
    t1 = live_trip(db, trips, hours_ago=3)
    t2 = live_trip(db, trips, OREBRO, JONKOPING, hours_ago=2)
    notifier.ensure_open_questions(db, NOW)
    r1, r2 = questions(db)
    assert answer(db, user, r1, "PRIVATE")["outcome"] == "applied"
    assert reload_trip(db, t1.id).category == Category.PRIVATE
    assert answer(db, user, r2, "PRIVALWAYS")["outcome"] == "applied"
    assert reload_trip(db, t2.id).category == Category.PRIVATE
    assert calls["remember"] == [(t2.id, Category.PRIVATE, None)]


def test_answer_purpose_pick_and_always(db, user, trips, sd, monkeypatch, calls):
    suggest(monkeypatch, ["Kundbesök Acme", "Montage"])
    t1 = live_trip(db, trips, kinds=(ReviewKind.PURPOSE,), category=Category.WORK, hours_ago=3)
    t2 = live_trip(db, trips, kinds=(ReviewKind.PURPOSE,), category=Category.WORK, hours_ago=2)
    notifier.ensure_open_questions(db, NOW)
    r1, r2 = questions(db)
    assert answer(db, user, r1, "PURPOSE1")["outcome"] == "applied"
    assert reload_trip(db, t1.id).purpose == "Montage"
    assert answer(db, user, r2, "PURPOSEA0")["outcome"] == "applied"
    t = reload_trip(db, t2.id)
    assert t.purpose == "Kundbesök Acme" and t.category == Category.WORK
    assert calls["remember"] == [(t2.id, Category.WORK, "Kundbesök Acme")]


def test_answer_company(db, user, trips, sd, monkeypatch, calls):
    suggest(monkeypatch, [])
    trip = live_trip(db, trips)
    dest = db.get(M.KnownLocation, trip.end_location_id)
    dest.place_kind, dest.confirmed = "work", False
    db.flush()
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    res = answer(db, user, req, "COMPANYTEXT", " Bring​ ")
    assert res["outcome"] == "applied" and res["message"] == "Sparat: Kundbesök Bring."
    assert calls["company"] == ["Bring"]


def test_answer_assumed(db, user, trips, sd, calls):
    t1 = _assumed_trip(db, trips)
    r1 = notices.queue_assumed(db, user_id=user.id, trip=t1, now=NOW)
    assert answer(db, user, r1, "OK")["outcome"] == "applied"
    assert calls["confirm"] == [t1.id]
    # "Fel – privat" på en annan plats.
    t2 = _assumed_trip(db, trips)
    t2.end_location_id = trips.location(JONKOPING, confirmed=False).id
    db.flush()
    r2 = notices.queue_assumed(db, user_id=user.id, trip=t2, now=NOW)
    assert answer(db, user, r2, "PRIVATE")["outcome"] == "applied"
    assert reload_trip(db, t2.id).category == Category.PRIVATE
    # "Annat ärende" på en tredje.
    t3 = _assumed_trip(db, trips)
    t3.end_location_id = trips.location("Kungsgatan 3, 111 43 Stockholm", confirmed=False).id
    db.flush()
    r3 = notices.queue_assumed(db, user_id=user.id, trip=t3, now=NOW)
    assert answer(db, user, r3, "WORKTEXT", "Leverans")["outcome"] == "applied"
    t = reload_trip(db, t3.id)
    assert t.category == Category.WORK and t.purpose == "Leverans"


def test_action_must_exist_in_question(db, user, classify_req):
    req, _ = classify_req
    assert answer(db, user, req, "COMPANYTEXT", "Acme")["outcome"] == "invalid"
    assert answer(db, user, req, "OK")["outcome"] == "invalid"


def test_web_always_is_checked_with_can_remember(db, user, trips, sd, monkeypatch, calls):
    suggest(monkeypatch, [], remember=False)
    t1 = live_trip(db, trips, hours_ago=3)
    t2 = live_trip(db, trips, hours_ago=2)
    notifier.ensure_open_questions(db, NOW)
    r1, r2 = questions(db)
    res = answer(db, user, r1, "WORKTEXT", "Service", source="web", always=True)
    assert res["outcome"] == "applied" and "Ingen regel sparades" in res["message"]
    assert calls["remember"] == []
    monkeypatch.setattr(autopilot, "can_remember", lambda db, trip, text: True)
    res = answer(db, user, r2, "PRIVATE", source="web", always=True)
    assert calls["remember"] == [(t2.id, Category.PRIVATE, None)]
    # Från telefonen gäller always aldrig.
    assert reload_trip(db, t1.id).purpose == "Service"


def test_respond_api_passes_always(client, db, user, vehicle, monkeypatch, calls):
    from tests.conftest import TripFactory

    setup_pull(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    suggest(monkeypatch, [], remember=True)
    trip = live_trip(db, TripFactory(db, user, vehicle))
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    db.commit()
    resp = client.post(f"/api/v1/notifications/{req.public_id}/respond",
                       json={"action": "WORKTEXT", "reply_text": "Montage", "always": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "applied"
    assert calls["remember"] == [(trip.id, Category.WORK, "Montage")]


# ----------------------------------------------------------------------------- en fråga åt gången


def test_one_question_at_a_time_reminder_and_expiry(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    live_trip(db, trips, hours_ago=3)
    live_trip(db, trips, OREBRO, JONKOPING, hours_ago=2)
    assert notifier.ensure_open_questions(db, NOW)["created"] == 2
    stats = notifier.deliver_due(db, NOW)
    assert stats["sent"] == 1 and stats["digests"] == 0
    first, second = questions(db)
    assert first.status == NotificationStatus.SENT and second.status == NotificationStatus.PENDING
    assert first.expires_at == NOW + timedelta(hours=48)  # 48 timmar från leveransen
    # Ingen ny fråga medan den första väntar.
    assert notifier.deliver_due(db, NOW + timedelta(hours=23))["sent"] == 0
    # Påminnelse efter 24 timmar, bara en.
    assert notifier.deliver_due(db, NOW + timedelta(hours=24, minutes=1))["reminders"] == 1
    assert notifier.deliver_due(db, NOW + timedelta(hours=40))["reminders"] == 0
    # Efter 48 timmar läggs frågan undan och nästa får plats.
    stats = notifier.deliver_due(db, NOW + timedelta(hours=48, minutes=1))
    assert stats["expired"] == 1 and stats["sent"] == 1
    db.refresh(first)
    db.refresh(second)
    assert first.status == NotificationStatus.EXPIRED and second.status == NotificationStatus.SENT
    # Den undanlagda kan fortfarande besvaras i webben.
    assert answer(db, user, first, "PRIVATE", source="web", now=NOW + timedelta(hours=49))["outcome"] == "applied"


def test_answer_releases_next_question(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    live_trip(db, trips, hours_ago=3)
    live_trip(db, trips, OREBRO, JONKOPING, hours_ago=2)
    notifier.ensure_open_questions(db, NOW)
    notifier.deliver_due(db, NOW)
    first, second = questions(db)
    answer(db, user, first, "PRIVATE")
    assert notifier.deliver_due(db, NOW + timedelta(minutes=2))["sent"] == 1
    db.refresh(second)
    assert second.status == NotificationStatus.SENT


def test_quiet_hours_still_apply(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    night = datetime(2026, 9, 15, 20, 30, tzinfo=UTC)  # 22:30 svensk tid
    live_trip(db, trips, hours_ago=1)
    notifier.ensure_open_questions(db, night)
    assert notifier.deliver_due(db, night)["sent"] == 0
    assert notifier.deliver_due(db, datetime(2026, 9, 16, 5, 1, tzinfo=UTC))["sent"] == 1


def test_month_end_reasks_expired_question_once(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    end_sept = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    trip = live_trip(db, trips, hours_ago=1 - 14 * 24)  # slutade 29/9 kl. 09 UTC
    notifier.ensure_open_questions(db, end_sept)
    notifier.deliver_due(db, end_sept)
    notifier.deliver_due(db, end_sept + timedelta(hours=49))  # undanlagd 1 oktober
    (orig,) = questions(db)
    assert orig.status == NotificationStatus.EXPIRED
    # Samma månad: ingen omfrågan.
    assert notifier.ensure_open_questions(db, datetime(2026, 9, 30, 20, 0, tzinfo=UTC)).get("month_reasked", 0) == 0
    october = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
    assert notifier.ensure_open_questions(db, october)["month_reasked"] == 1
    assert notifier.ensure_open_questions(db, october + timedelta(hours=1)).get("month_reasked", 0) == 0
    reasked = questions(db)[-1]
    assert reasked.dedupe_key == f"classify:{trip.id}:m" and reasked.status == NotificationStatus.PENDING
    # Även den omställda frågan läggs undan efter 48 timmar – men ställs aldrig om igen.
    notifier.deliver_due(db, october + timedelta(hours=2))
    notifier.deliver_due(db, october + timedelta(hours=51))
    assert notifier.ensure_open_questions(db, october + timedelta(hours=52)) == {}
    assert len(questions(db)) == 2


# ----------------------------------------------------------------------------- ensure_open_questions


def test_ensure_skips_wait_imported_and_before_active_since(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    waiting = live_trip(db, trips, hours_ago=3)
    dest = db.get(M.KnownLocation, waiting.end_location_id)
    dest.geocode_status, dest.created_at = "pending", NOW - timedelta(minutes=10)
    live_trip(db, trips, OREBRO, JONKOPING, hours_ago=2, source=TripSource.VOLVO_IMPORT)
    live_trip(db, trips, JONKOPING, HOME, hours_ago=1.5, created_at=SINCE - timedelta(days=1))
    manual = live_trip(db, trips, HOME, JONKOPING, hours_ago=1.2, source=TripSource.MANUAL)
    stats = notifier.ensure_open_questions(db, NOW)
    assert stats.get("created", 0) == 0 and stats["waiting"] == 1 and stats["skipped_not_live"] == 1
    assert questions(db) == []
    assert manual.source == TripSource.MANUAL
    # Efter 30 minuter väntar frågan inte längre på adressen.
    assert notifier.ensure_open_questions(db, NOW + timedelta(minutes=25))["created"] == 1


def test_ensure_respects_batch_window_and_window(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    live_trip(db, trips, hours_ago=0.05)  # slutade för 3 minuter sedan
    live_trip(db, trips, hours_ago=36 * 24)  # äldre än 35 dagar
    assert notifier.ensure_open_questions(db, NOW).get("created", 0) == 0
    assert notifier.ensure_open_questions(db, NOW + timedelta(minutes=10))["created"] == 1


def test_ensure_does_nothing_in_manual_mode(db, user, trips, sd, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    live_trip(db, trips)
    assert notifier.ensure_open_questions(db, NOW) == {}
    assert questions(db) == []


def test_enqueue_for_changes_leaves_live_trips_to_ensure(db, user, vehicle, trips, sd):
    trip = live_trip(db, trips)
    created = notifier.enqueue_for_changes(
        db, user_id=user.id, vehicle=vehicle, merge_items=[], trip_items=[(trip, [ReviewKind.CATEGORY])], now=NOW
    )
    assert created == []
    imported = recent_trip(trips, OREBRO, JONKOPING, hours_ago=0.5)
    created = notifier.enqueue_for_changes(
        db, user_id=user.id, vehicle=vehicle, merge_items=[], trip_items=[(imported, [ReviewKind.CATEGORY])], now=NOW
    )
    assert len(created) == 1 and created[0].actions[0]["key"] == "WORK"  # importen som förut


# ----------------------------------------------------------------------------- utan vald telefon


def test_question_stays_in_outbox_without_phone(db, user, trips, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    setup_pull(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    error = "Ingen notify.mobile_app-tjänst är vald i DriveLog eller i integrationen."
    for i in range(notifier.MAX_PULL_FAILURES + 5):
        items = notifier.outbox(db, user.id, NOW + timedelta(hours=i * 3))
        assert [it["id"] for it in items] == [req.public_id]
        notifier.outbox_ack(db, user.id, delivered=[], failed=[{"id": req.public_id, "error": error}],
                            now=NOW + timedelta(hours=i * 3))
    db.refresh(req)
    assert req.status == NotificationStatus.PENDING and req.channel == notifier.PULL_OUT
    assert req.context.get("no_phone") is True
    # När telefonen finns levereras frågan, och 48 timmar räknas från leveransen.
    later = NOW + timedelta(days=5)
    notifier.outbox_ack(db, user.id, delivered=[req.public_id], now=later)
    db.refresh(req)
    assert req.status == NotificationStatus.SENT and req.expires_at == later + timedelta(hours=48)


def test_other_errors_still_count_attempts(db, user, trips, monkeypatch):
    suggest(monkeypatch, [], remember=False)
    setup_pull(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    notifier.outbox(db, user.id, NOW)
    (req,) = questions(db)
    notifier.outbox_ack(db, user.id, delivered=[], failed=[{"id": req.public_id, "error": "Tjänsten svarade 500"}],
                        now=NOW)
    db.refresh(req)
    assert req.delivery_attempts == 1


# ----------------------------------------------------------------------------- platsetiketter


def test_place_label_never_shows_house_number():
    from app.services.ha.messages import place_label

    home_loc = M.KnownLocation(type="home", name="Hemvägen 1", address=HOME, confirmed=True)
    private = M.KnownLocation(type="private", name="Förskolan", address=JONKOPING, confirmed=True)
    poi = M.KnownLocation(type="unknown", name="Dagab", company="Dagab", address="Lagergatan 7, 553 03 Jönköping",
                          city="Jönköping", confirmed=False)
    street = M.KnownLocation(type="unknown", name="Lagergatan 7, 553 03 Jönköping",
                             address="Lagergatan 7, 553 03 Jönköping", confirmed=False)
    confirmed = M.KnownLocation(type="customer", name="Acme AB", address=OREBRO, confirmed=True)
    assert place_label(home_loc, HOME) == "Hemmet"
    assert place_label(private, JONKOPING) == "Privat plats"
    assert place_label(poi, poi.address) == "Dagab, Jönköping"
    assert place_label(street, street.address) == "Lagergatan, Jönköping"
    assert place_label(confirmed, OREBRO) == "Acme AB"
    assert place_label(None, OREBRO) == "Drottninggatan, Örebro"
    for label in ("Dagab, Jönköping", "Lagergatan, Jönköping", "Drottninggatan, Örebro"):
        assert not re.search(r"\d", label)


# ----------------------------------------------------------------------------- /m/month


def test_review_row_verdicts(client, db, user, vehicle, monkeypatch, calls):
    from tests.conftest import TripFactory

    trips = TripFactory(db, user, vehicle)
    assumed = trips.trip(HOME, OREBRO, category=Category.WORK, purpose="Kundbesök Dagab", source=TripSource.VOLVO_API)
    assumed.approval_source = "assumed"
    ruled = trips.trip(OREBRO, JONKOPING, category=Category.WORK, purpose="Montage", source=TripSource.VOLVO_API)
    ruled.approval_source = "rule"
    mine = trips.trip(JONKOPING, HOME, category=Category.WORK, purpose="Möte")
    db.commit()
    url = "/api/v1/notifications/review-rows/{}"
    resp = client.post(url.format(assumed.id), json={"verdict": "ok"})
    assert resp.status_code == 200, resp.text
    assert calls["confirm"] == [assumed.id]
    resp = client.post(url.format(ruled.id), json={"verdict": "wrong"})
    assert resp.status_code == 200 and resp.json()["message"] == "Raden är ändrad till privat."
    assert reload_trip(db, ruled.id).category == Category.PRIVATE
    assert client.post(url.format(mine.id), json={"verdict": "ok"}).status_code == 400
    assert client.post(url.format(uuid.uuid4()), json={"verdict": "ok"}).status_code == 404
    assert client.post(url.format(assumed.id), json={"verdict": "kanske"}).status_code == 422
