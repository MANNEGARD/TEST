"""Notismotorn: skapande, leverans (push/pull), policy, påminnelser och sammanställningar."""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    IntegrationStatus,
    NotificationKind,
    NotificationStatus,
    ReviewKind,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.ha import actions as A
from app.services.ha import connection, demo, notifier, responses
from app.services.trips import merge
from tests.test_ha_support import (  # noqa: F401
    HA_TOKEN,
    HOME,
    JONKOPING,
    NOW,
    OREBRO,
    SERVICE,
    after_batch,
    deliver,
    enqueue_classify,
    fake_ha,
    places,
    recent_trip,
    setup_pull,
    setup_push,
)


def requests_of(db, kind=None):
    q = select(M.NotificationRequest)
    if kind:
        q = q.where(M.NotificationRequest.kind == kind)
    return db.execute(q.order_by(M.NotificationRequest.created_at)).scalars().all()


# ----------------------------------------------------------------------------- skapande


def test_enqueue_creates_question_and_respects_batch_window(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    places(trips)
    trip = recent_trip(trips)
    (req,) = enqueue_classify(db, user, vehicle, trip)
    assert req.kind == NotificationKind.CLASSIFY and req.status == NotificationStatus.PENDING
    assert req.send_after == NOW + timedelta(minutes=10)
    assert deliver(db, NOW + timedelta(minutes=5))["sent"] == 0
    assert fake_ha.sent == []
    # Samma resa ger aldrig en andra fråga.
    assert enqueue_classify(db, user, vehicle, trip) == []


def test_no_questions_for_historic_trips(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    old = recent_trip(trips, hours_ago=49)
    assert enqueue_classify(db, user, vehicle, old) == []


def test_no_questions_when_kind_disabled_or_notifications_off(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, kinds={"classify": False})
    trip = recent_trip(trips)
    assert enqueue_classify(db, user, vehicle, trip) == []
    settings_svc.update_section(db, user.id, "notifications", {"kinds": {"classify": True}, "enabled": False})
    assert enqueue_classify(db, user, vehicle, trip) == []


def test_no_questions_without_channel(db, user, vehicle, trips):
    trip = recent_trip(trips)
    assert enqueue_classify(db, user, vehicle, trip) == []


def test_demo_vehicle_never_gets_real_notifications(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    vehicle.is_demo = True
    db.flush()
    trip = recent_trip(trips)
    assert enqueue_classify(db, user, vehicle, trip) == []
    assert fake_ha.calls == []


# ----------------------------------------------------------------------------- push


def test_push_delivery_payload(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, hide_locations_on_lock_screen=False)
    places(trips)
    trip = recent_trip(trips)
    (req,) = enqueue_classify(db, user, vehicle, trip)
    stats = deliver(db, after_batch())
    assert stats["sent"] == 1
    (payload,) = fake_ha.sent
    assert payload["message"] == "Ny resa: Hemmet → Örebro. 4,2 mil. Var detta arbete eller privat?"
    data = payload["data"]
    assert data["tag"] == f"drivelog-{req.public_id}"
    assert data["action_data"]["drivelog_id"] == req.public_id
    assert data["url"] == f"http://testserver/m/q/{req.public_id}"
    titles = [a["title"] for a in data["actions"]]
    assert titles[:3] == ["Arbete", "Privat", "Granska"]
    for a in data["actions"]:
        assert a["action"].startswith("DRIVELOG_") and a["action"].endswith(req.public_id)
        assert A.parse_action_id(a["action"])[1] == req.public_id
    db.refresh(req)
    assert req.status == NotificationStatus.SENT and req.channel == "ha_push"


def test_lock_screen_text_hides_places(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)  # hide_locations_on_lock_screen är på som standard
    places(trips)
    trip = recent_trip(trips)
    (req,) = enqueue_classify(db, user, vehicle, trip)
    deliver(db, after_batch())
    (payload,) = fake_ha.sent
    assert payload["message"] == "Ny resa: 4,2 mil. Var detta arbete eller privat?"
    for word in ("Hemmet", "Örebro", "Drottninggatan", "Hemvägen"):
        assert word not in payload["message"] and word not in payload["title"]
    # Detaljerna finns bara i den inloggade appen.
    assert "Örebro" in req.context["detail_message"]


def test_texts_never_contain_street_addresses(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, hide_locations_on_lock_screen=False)
    # Bekräftade platser utan eget namn har adressen som namn – den får aldrig visas.
    trips.location(OREBRO, confirmed=True)
    trips.location(JONKOPING, type="private")
    t1 = recent_trip(trips, HOME, OREBRO)
    t2 = recent_trip(trips, OREBRO, JONKOPING, hours_ago=0.5, minutes=10)
    enqueue_classify(db, user, vehicle, t1)
    enqueue_classify(db, user, vehicle, t2)
    for req in requests_of(db):
        text = req.message + " " + req.context["detail_message"]
        for street in ("Drottninggatan", "Storgatan", "Hemvägen", "12", "702"):
            assert street not in text, text
    assert "Privat plats" in requests_of(db)[1].context["detail_message"]
    assert "Örebro" in requests_of(db)[0].context["detail_message"]


def test_ha_offline_retries_without_losing_question(db, user, vehicle, trips, fake_ha, caplog):
    conn = setup_push(db, user)
    trip = recent_trip(trips)
    (req,) = enqueue_classify(db, user, vehicle, trip)
    fake_ha.offline = True
    t = after_batch()
    stats = deliver(db, t)
    assert stats["sent"] == 0 and stats["failed"] == 1
    db.refresh(req)
    assert req.status == NotificationStatus.PENDING and req.channel is None
    assert req.delivery_attempts == 1 and req.send_after > t and req.last_error
    # Fortfarande nere: nytt försök med längre väntan.
    t2 = req.send_after
    deliver(db, t2)
    db.refresh(req)
    assert req.status == NotificationStatus.PENDING and req.delivery_attempts == 2
    assert req.send_after - t2 > t2 - t - timedelta(seconds=1) or req.send_after > t2
    assert conn.last_error
    # Återanslutning: nästa försök levererar frågan.
    fake_ha.offline = False
    deliver(db, req.send_after)
    db.refresh(req)
    assert req.status == NotificationStatus.SENT and len(fake_ha.sent) == 1
    assert conn.status == IntegrationStatus.CONNECTED and conn.last_error is None
    assert caplog.records and HA_TOKEN not in caplog.text  # token loggas aldrig


def test_ha_auth_failure_marks_reauth_and_keeps_question(db, user, vehicle, trips, fake_ha):
    conn = setup_push(db, user)
    (req,) = enqueue_classify(db, user, vehicle, recent_trip(trips))
    fake_ha.status = 401
    deliver(db, after_batch())
    db.refresh(req)
    assert req.status == NotificationStatus.PENDING
    assert conn.status == IntegrationStatus.NEEDS_REAUTH


# ----------------------------------------------------------------------------- pull


def test_pull_outbox_and_ack(db, user, vehicle, trips, fake_ha):
    setup_pull(db, user)
    (req,) = enqueue_classify(db, user, vehicle, recent_trip(trips))
    assert notifier.outbox(db, user.id, now=NOW) == []  # samlingsfönstret har inte gått ut
    items = notifier.outbox(db, user.id, now=after_batch())
    (item,) = items
    assert item["id"] == req.public_id and item["type"] == "notify" and item["service"] == SERVICE
    assert item["data"]["data"]["action_data"]["drivelog_id"] == req.public_id
    db.refresh(req)
    assert req.status == NotificationStatus.PENDING  # inte levererad förrän kvitterad
    # Okvitterat: samma notis finns kvar i utkorgen vid nästa hämtning.
    assert [i["id"] for i in notifier.outbox(db, user.id, now=after_batch(12))] == [req.public_id]
    result = notifier.outbox_ack(db, user.id, delivered=[req.public_id], now=after_batch(12))
    assert result == {"acknowledged": 1, "failed": 0}
    db.refresh(req)
    assert req.status == NotificationStatus.SENT and req.channel == "ha_pull"
    assert notifier.outbox(db, user.id, now=after_batch(13)) == []
    assert fake_ha.calls == []  # pull-läget gör inga anrop mot HA


def test_pull_failed_ack_keeps_item_in_outbox(db, user, vehicle, trips):
    setup_pull(db, user)
    (req,) = enqueue_classify(db, user, vehicle, recent_trip(trips))
    notifier.outbox(db, user.id, now=after_batch())
    notifier.outbox_ack(db, user.id, delivered=[], failed=[{"id": req.public_id, "error": "Tjänsten saknas"}])
    db.refresh(req)
    assert req.last_error == "Tjänsten saknas" and req.status == NotificationStatus.PENDING
    assert [i["id"] for i in notifier.outbox(db, user.id, now=after_batch(12))] == [req.public_id]


def test_pull_answered_question_is_cleared_from_phone(db, user, vehicle, trips):
    setup_pull(db, user)
    (req,) = enqueue_classify(db, user, vehicle, recent_trip(trips))
    notifier.outbox(db, user.id, now=after_batch())
    notifier.outbox_ack(db, user.id, delivered=[req.public_id])
    # Besvaras i webben → notisen ska tas bort från telefonen.
    responses.handle(db, user_id=user.id, public_id=req.public_id, action="WORK", idempotency_key="webb-svar-1",
                     source="web", now=after_batch(15))
    (item,) = notifier.outbox(db, user.id, now=after_batch(16))
    assert item["type"] == "clear" and item["data"] == {"message": "clear_notification",
                                                        "data": {"tag": f"drivelog-{req.public_id}"}}
    notifier.outbox_ack(db, user.id, delivered=[req.public_id])
    assert notifier.outbox(db, user.id, now=after_batch(17)) == []


# ----------------------------------------------------------------------------- policy


def test_quiet_hours_defer_delivery(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    night = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)  # 22:00 svensk tid
    trip = trips.trip(HOME, OREBRO, km=42, minutes=30, start_at=night - timedelta(hours=1))
    (req,) = enqueue_classify(db, user, vehicle, trip, now=night)
    stats = deliver(db, night + timedelta(minutes=15))
    assert stats["sent"] == 0 and stats["deferred_quiet"] == 1 and fake_ha.sent == []
    morning = datetime(2026, 9, 16, 5, 1, tzinfo=UTC)  # 07:01 svensk tid
    assert deliver(db, morning)["sent"] == 1
    db.refresh(req)
    assert req.status == NotificationStatus.SENT


def test_test_notification_bypasses_quiet_hours(db, user, fake_ha):
    setup_push(db, user)
    night = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)
    req = notifier.create_test(db, user.id, night)
    assert notifier.deliver_now(db, req, night) is True
    assert len(fake_ha.sent) == 1


def test_max_per_day(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, max_per_day=2, digest_threshold=10, batch_window_minutes=0)
    for i in range(3):
        trip = recent_trip(trips, hours_ago=3 - i, minutes=10)
        enqueue_classify(db, user, vehicle, trip)
        deliver(db, NOW + timedelta(minutes=i))
    assert len(fake_ha.sent) == 2
    stats = deliver(db, NOW + timedelta(hours=1))
    assert stats["deferred_cap"] == 1 and len(fake_ha.sent) == 2
    # Nästa dygn går den tredje frågan iväg.
    deliver(db, datetime(2026, 9, 16, 5, 30, tzinfo=UTC))
    assert len(fake_ha.sent) == 3


def test_digest_when_many_questions(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, digest_threshold=3)
    reqs = []
    for i in range(4):
        reqs += enqueue_classify(db, user, vehicle, recent_trip(trips, hours_ago=4 - i, minutes=10))
    stats = deliver(db, after_batch())
    assert stats["digests"] == 1 and stats["sent"] == 1
    (payload,) = fake_ha.sent
    assert payload["message"] == "4 resor behöver granskas i körjournalen."
    assert payload["data"]["url"] == "http://testserver/review"
    (digest,) = requests_of(db, NotificationKind.DIGEST)
    for r in reqs:
        db.refresh(r)
        assert r.digest_id == digest.id and r.status == NotificationStatus.SENT
    # Frågorna kan fortfarande besvaras i webben; när alla är klara stängs samlingsnotisen.
    for n, r in enumerate(reqs):
        res = responses.handle(db, user_id=user.id, public_id=r.public_id, action="PRIVATE",
                               idempotency_key=f"svar-nr-{n}", source="web", now=after_batch(20))
        assert res["outcome"] == "applied"
    db.refresh(digest)
    assert digest.status == NotificationStatus.CANCELLED


def test_three_questions_are_sent_individually(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, digest_threshold=3)
    for i in range(3):
        enqueue_classify(db, user, vehicle, recent_trip(trips, hours_ago=4 - i, minutes=10))
    stats = deliver(db, after_batch())
    assert stats["digests"] == 0 and stats["sent"] == 3


def test_reminder_then_expiry(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, reminder_after_hours=20, max_reminders=1, expire_after_hours=72)
    (req,) = enqueue_classify(db, user, vehicle, recent_trip(trips))
    t0 = after_batch()
    deliver(db, t0)
    assert deliver(db, t0 + timedelta(hours=19))["reminders"] == 0
    stats = deliver(db, t0 + timedelta(hours=20, minutes=1))
    assert stats["reminders"] == 1
    assert fake_ha.sent[-1]["title"].startswith("Påminnelse")
    assert fake_ha.sent[-1]["data"]["tag"] == fake_ha.sent[0]["data"]["tag"]  # ersätter den gamla notisen
    # Högst en påminnelse.
    assert deliver(db, t0 + timedelta(hours=45))["reminders"] == 0
    stats = deliver(db, NOW + timedelta(hours=72, minutes=1))
    assert stats["expired"] == 1
    db.refresh(req)
    assert req.status == NotificationStatus.EXPIRED
    assert fake_ha.clears and fake_ha.clears[-1]["data"]["tag"] == f"drivelog-{req.public_id}"
    # Svar från telefonen efter expirering ändrar inget, men webben kan fortfarande svara.
    res = responses.handle(db, user_id=user.id, public_id=req.public_id, action=f"DRIVELOG_WORK_{req.public_id}",
                           idempotency_key="sent-svar-1", source="ha", now=NOW + timedelta(hours=73))
    assert res["outcome"] == "expired"
    trip = db.get(M.LogicalTrip, req.logical_trip_id)
    assert trip.category == Category.UNDECIDED
    res = responses.handle(db, user_id=user.id, public_id=req.public_id, action="WORK",
                           idempotency_key="webb-svar-2", source="web", now=NOW + timedelta(hours=74))
    assert res["outcome"] == "applied"
    db.refresh(trip)
    assert trip.category == Category.WORK


def test_question_cancelled_when_handled_in_web(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    trip = recent_trip(trips)
    (req,) = enqueue_classify(db, user, vehicle, trip)
    deliver(db, after_batch())
    from app.services.learning import feedback

    feedback.apply_decision(db, trip, user_id=user.id, actor=audit.user_actor(user.id), category=Category.PRIVATE)
    assert notifier.cancel_for_trip(db, trip.id) == 1
    db.refresh(req)
    assert req.status == NotificationStatus.CANCELLED
    deliver(db, after_batch(12))
    assert fake_ha.clears and fake_ha.clears[0]["data"]["tag"] == f"drivelog-{req.public_id}"


# ----------------------------------------------------------------------------- sammanslagning


def merge_item(db, user, vehicle, trips):
    t1 = recent_trip(trips, HOME, OREBRO, km=12.4, hours_ago=2, minutes=20, category=Category.WORK, purpose="Kundbesök")
    trips.skip(minutes=38)
    t2 = trips.trip(OREBRO, JONKOPING, km=30.6, minutes=20, category=Category.WORK, purpose="Kundbesök")
    cfg = settings_svc.get_section(db, user.id, "merge")
    result = merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=audit.SYSTEM)
    (item,) = result["questions"]
    return item, t1, t2


def test_merge_question_text(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    item, *_ = merge_item(db, user, vehicle, trips)
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[item], trip_items=[],
                                          now=NOW)
    assert req.message == "Två resor med 38 minuters stillestånd. Ska de slås samman?"
    deliver(db, after_batch())
    assert [a["title"] for a in fake_ha.sent[0]["data"]["actions"]][:3] == ["Ja", "Nej", "Granska"]
    # Avgörs i webben → frågan avbryts.
    assert notifier.cancel_for_review_item(db, item.id) == 1


# ----------------------------------------------------------------------------- demo


def test_demo_mode_never_connected_and_never_calls_ha(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    vehicle.is_demo = True
    trip = recent_trip(trips)
    db.flush()
    result = demo.enable(db, user.id, NOW)
    assert result["status"] == IntegrationStatus.DEMO
    status = connection.public_status(db, user.id)
    assert status["status"] == "demo" and status["demo"] is True and status["status"] != "connected"
    assert "token" not in status and status["has_token"] is True
    # Testnotis och demofrågor visas på den simulerade telefonen.
    inbox = demo.inbox(db, user.id)
    assert {r.kind for r in inbox} >= {NotificationKind.TEST}
    assert fake_ha.calls == []
    # Demofordonets nya resor ger frågor i demoläget – fortfarande utan HA-anrop.
    enqueue_classify(db, user, vehicle, trip)
    deliver(db, after_batch())
    assert fake_ha.calls == []
    disabled = demo.disable(db, user.id)
    assert disabled["status"] == IntegrationStatus.CONFIGURED  # aldrig "connected" utan ny verifiering


# ----------------------------------------------------------------------------- sammanställningar


def test_weekly_summary_numbers_match_data(db, user, vehicle, trips, fake_ha):
    setup_push(db, user)
    sunday = datetime(2026, 9, 20, 16, 5, tzinfo=UTC)  # söndag 18:05 svensk tid
    monday = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
    trips.trip(HOME, OREBRO, km=200.0, start_at=monday, category=Category.WORK)
    trips.trip(OREBRO, HOME, km=286.0, start_at=monday + timedelta(days=1), category=Category.WORK)
    trips.trip(HOME, JONKOPING, km=82.0, start_at=monday + timedelta(days=2), category=Category.PRIVATE)
    for d in range(3):
        trips.trip(HOME, OREBRO, km=10.0, start_at=monday + timedelta(days=3, hours=d))  # ej avgjorda
    # Förra veckans resor och demodata räknas inte.
    trips.trip(HOME, OREBRO, km=999.0, start_at=monday - timedelta(days=2), category=Category.WORK)

    stats = notifier.run_scheduled(db, now=sunday)
    assert stats["weekly"] == 1
    (req,) = requests_of(db, NotificationKind.WEEKLY)
    assert req.message == ("Veckans körjournal: 48,6 mil i tjänsten, 8,2 mil privat och "
                           "3 resor som behöver granskas.")
    assert req.status == NotificationStatus.SENT and len(fake_ha.sent) == 1
    # Idempotent per vecka.
    assert notifier.run_scheduled(db, now=sunday + timedelta(minutes=30))["weekly"] == 0
    assert len(requests_of(db, NotificationKind.WEEKLY)) == 1


def test_weekly_summary_not_before_time(db, user, fake_ha):
    setup_push(db, user)
    assert notifier.run_scheduled(db, now=datetime(2026, 9, 20, 15, 0, tzinfo=UTC))["weekly"] == 0
    assert notifier.run_scheduled(db, now=datetime(2026, 9, 19, 17, 0, tzinfo=UTC))["weekly"] == 0


def test_daily_summary(db, user, vehicle, trips, fake_ha):
    setup_push(db, user, kinds={"daily": True, "weekly": False})
    evening = datetime(2026, 9, 15, 18, 35, tzinfo=UTC)  # 20:35 svensk tid
    trips.trip(HOME, OREBRO, km=42.0, start_at=evening - timedelta(hours=5))
    stats = notifier.run_scheduled(db, now=evening)
    assert stats["daily"] == 1
    (req,) = requests_of(db, NotificationKind.DAILY)
    assert req.message == "Dagens körjournal: 1 resa, 4,2 mil. 1 resa behöver granskas."
    assert notifier.run_scheduled(db, now=evening + timedelta(minutes=10))["daily"] == 0


@pytest.mark.parametrize("hide", [True, False])
def test_purpose_question_text_and_buttons(db, user, vehicle, trips, fake_ha, hide):
    setup_push(db, user, hide_locations_on_lock_screen=hide)
    places(trips)
    trip = recent_trip(trips, HOME, JONKOPING, km=150.0, category=Category.WORK, purpose=None)
    reqs = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                        trip_items=[(trip, [ReviewKind.PURPOSE])], now=NOW)
    (req,) = reqs
    assert req.kind == NotificationKind.PURPOSE
    if hide:
        assert "Jönköping" not in req.message
    else:
        assert req.message == "Du har en resa till Jönköping. Vad var syftet?"
    deliver(db, after_batch())
    acts = fake_ha.sent[0]["data"]["actions"]
    text = next(a for a in acts if a.get("behavior") == "textInput")
    assert text["title"] == "Ange nytt syfte"
    assert acts[-1]["title"] == "Öppna körjournal"
