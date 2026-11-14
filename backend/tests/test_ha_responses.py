"""Svar på notiser: knappar, textsvar, sammanslagning, idempotens och expirering."""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    DecisionSource,
    NotificationKind,
    NotificationStatus,
    ReviewKind,
    ReviewState,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.ha import actions as A
from app.services.ha import notifier, responses
from app.services.trips import merge
from app.services.trips import segments as seg
from tests.conftest import reload_trip
from tests.test_ha_support import (  # noqa: F401
    HOME,
    JONKOPING,
    NOW,
    OREBRO,
    after_batch,
    deliver,
    enqueue_classify,
    fake_ha,
    places,
    recent_trip,
    setup_push,
)

T = after_batch(30)


def sent_question(db, user, vehicle, trips, **trip_kw):
    setup_push(db, user)
    places(trips)
    trip = recent_trip(trips, **trip_kw)
    (req,) = enqueue_classify(db, user, vehicle, trip)
    deliver(db, after_batch())
    db.refresh(req)
    assert req.status == NotificationStatus.SENT
    return req, trip


def press(db, user, req, key, event="ev-00000001", reply_text=None, source="ha", now=T, public_id=None):
    return responses.handle(
        db, user_id=user.id, public_id=public_id if public_id is not None else req.public_id,
        action=A.action_id(key, req.public_id), reply_text=reply_text, idempotency_key=event, source=source, now=now,
    )


def test_button_work(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    result = press(db, user, req, A.WORK)
    assert result["outcome"] == "applied" and result["category"] == Category.WORK
    trip = reload_trip(db, trip.id)
    assert trip.category == Category.WORK and trip.category_source == DecisionSource.HA
    fb = db.execute(select(M.LearningFeedback).where(M.LearningFeedback.logical_trip_id == trip.id)).scalar_one()
    assert fb.channel == "ha"
    db.refresh(req)
    assert req.status == NotificationStatus.ANSWERED and req.answered_at == T
    # Efter "Arbete" ställs en följdfråga om syftet direkt (förbi samlingsfönstret).
    follow = db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.PURPOSE)
    ).scalar_one()
    assert result["follow_up"] == follow.public_id and follow.status == NotificationStatus.SENT


def test_button_private(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    result = press(db, user, req, A.PRIVATE, public_id="")  # id:t läses ur åtgärdssträngen
    assert result["outcome"] == "applied"
    trip = reload_trip(db, trip.id)
    assert trip.category == Category.PRIVATE
    assert result["follow_up"] is None


def purpose_question(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    places(trips)
    trip = recent_trip(trips, HOME, JONKOPING, km=150.0, category=Category.WORK)
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                          trip_items=[(trip, [ReviewKind.PURPOSE])], now=NOW)
    deliver(db, after_batch())
    return req, trip


def test_text_reply_sets_purpose(db, user, vehicle, trips, fake_ha):
    req, trip = purpose_question(db, user, vehicle, trips, fake_ha)
    result = press(db, user, req, A.PURPOSE_TEXT, reply_text="  Kundmöte hos Jönköpingsbolaget  ")
    assert result["outcome"] == "applied" and result["purpose"] == "Kundmöte hos Jönköpingsbolaget"
    trip = reload_trip(db, trip.id)
    assert trip.purpose == "Kundmöte hos Jönköpingsbolaget" and trip.purpose_confirmed
    assert trip.purpose_source == DecisionSource.HA


def test_empty_text_reply_is_invalid_and_can_be_retried(db, user, vehicle, trips, fake_ha):
    req, trip = purpose_question(db, user, vehicle, trips, fake_ha)
    result = press(db, user, req, A.PURPOSE_TEXT, reply_text="   ")
    assert result["outcome"] == "invalid" and result["notify_user"] is True
    result = press(db, user, req, A.PURPOSE_TEXT, event="ev-00000002", reply_text="Service")
    assert result["outcome"] == "applied"


def test_previous_purpose_button(db, user, vehicle, trips, fake_ha):
    req, trip = purpose_question(db, user, vehicle, trips, fake_ha)
    req.actions = [{"key": "PURPOSE0", "title": "Kundbesök", "purpose": "Kundbesök"}, *req.actions]
    db.flush()
    result = press(db, user, req, "PURPOSE0")
    assert result["outcome"] == "applied"
    assert reload_trip(db, trip.id).purpose == "Kundbesök"


def merge_setup(db, user, vehicle, trips):
    setup_push(db, user)
    t1 = recent_trip(trips, HOME, OREBRO, km=12.4, hours_ago=2, minutes=20, category=Category.WORK, purpose="Kundbesök")
    trips.skip(minutes=38)
    trips.trip(OREBRO, JONKOPING, km=30.6, minutes=20, category=Category.WORK, purpose="Kundbesök")
    cfg = settings_svc.get_section(db, user.id, "merge")
    (item,) = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=audit.SYSTEM)["questions"]
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[item], trip_items=[],
                                          now=NOW)
    deliver(db, after_batch())
    return req, item, t1


def test_merge_yes(db, user, vehicle, trips, fake_ha):
    req, item, _ = merge_setup(db, user, vehicle, trips)
    result = press(db, user, req, A.MERGE_YES)
    assert result["outcome"] == "applied" and result["merged_trip_id"]
    (trip,) = seg.active_trips(db, vehicle.id)
    assert round(trip.distance_km, 1) == 43.0
    db.refresh(item)
    assert item.status == ReviewState.RESOLVED and item.resolution["decision"] == "merged"


def test_merge_no(db, user, vehicle, trips, fake_ha):
    req, item, _ = merge_setup(db, user, vehicle, trips)
    result = press(db, user, req, A.MERGE_NO)
    assert result["outcome"] == "applied" and "merged_trip_id" not in result
    assert len(seg.active_trips(db, vehicle.id)) == 2
    db.refresh(item)
    assert item.status == ReviewState.RESOLVED and item.resolution["decision"] == "keep_separate"


def test_double_press_and_same_event_are_duplicates(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    first = press(db, user, req, A.WORK)
    assert first["outcome"] == "applied"
    # Samma knapptryck levereras två gånger (t.ex. omförsök från HA-kön).
    again = press(db, user, req, A.WORK)
    assert again["outcome"] == "duplicate" and again["first_outcome"] == "applied"
    # Ett andra, annat knapptryck ändrar ingenting – första svaret gäller.
    other = press(db, user, req, A.PRIVATE, event="ev-00000002")
    assert other["outcome"] == "duplicate" and other["notify_user"] is False
    assert reload_trip(db, trip.id).category == Category.WORK
    feedback_rows = db.execute(
        select(M.LearningFeedback).where(M.LearningFeedback.logical_trip_id == trip.id)
    ).scalars().all()
    assert len(feedback_rows) == 1


def test_expired_reply_changes_nothing(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    late = req.expires_at + timedelta(minutes=1)
    result = press(db, user, req, A.WORK, now=late)
    assert result["outcome"] == "expired" and result["notify_user"] is True
    assert reload_trip(db, trip.id).category == Category.UNDECIDED
    db.refresh(req)
    assert req.status == NotificationStatus.EXPIRED
    web = press(db, user, req, A.WORK, event="ev-web-0001", source="web", now=late)
    assert web["outcome"] == "applied"


def test_unknown_question_and_action_are_invalid(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    bad = responses.handle(db, user_id=user.id, public_id="0000000000000000", action="WORK",
                           idempotency_key="ev-okand-1", now=T)
    assert bad["outcome"] == "invalid"
    wrong = press(db, user, req, A.MERGE_YES, event="ev-00000003")
    assert wrong["outcome"] == "invalid"
    garbage = responses.handle(db, user_id=user.id, public_id=req.public_id, action="NÅGOT_ANNAT",
                               idempotency_key="ev-okand-2", now=T)
    assert garbage["outcome"] == "invalid"
    assert reload_trip(db, trip.id).category == Category.UNDECIDED


def test_other_users_question_is_invalid(db, user, vehicle, trips, fake_ha):
    req, _ = sent_question(db, user, vehicle, trips)
    other = M.User(email="annan@example.com", display_name="Annan", password_hash="x")
    db.add(other)
    db.flush()
    result = responses.handle(db, user_id=other.id, public_id=req.public_id, action="WORK",
                              idempotency_key="ev-annan-1", now=T)
    assert result["outcome"] == "invalid"


def test_review_button_changes_nothing(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    result = press(db, user, req, A.REVIEW)
    assert result["outcome"] == "opened"
    db.refresh(req)
    assert req.status == NotificationStatus.SENT
    assert reload_trip(db, trip.id).category == Category.UNDECIDED


def test_later_snoozes_and_redelivers(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    result = press(db, user, req, A.LATER)
    assert result["outcome"] == "snoozed"
    db.refresh(req)
    assert req.status == NotificationStatus.PENDING and req.send_after == T + timedelta(minutes=120)
    before = len(fake_ha.sent)
    deliver(db, T + timedelta(minutes=121))
    assert len(fake_ha.sent) == before + 1
    db.refresh(req)
    assert req.status == NotificationStatus.SENT


def test_answer_in_web_clears_phone_notification(db, user, vehicle, trips, fake_ha):
    req, trip = sent_question(db, user, vehicle, trips)
    result = press(db, user, req, A.PRIVATE, event="ev-web-0002", source="web")
    assert result["outcome"] == "applied"
    deliver(db, T + timedelta(minutes=1))
    assert fake_ha.clears[-1]["data"]["tag"] == f"drivelog-{req.public_id}"
