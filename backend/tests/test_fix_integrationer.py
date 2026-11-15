"""Regressionstester för granskningsfynd i integrationerna (Volvo, Home Assistant, worker)."""

# ruff: noqa: F811  (fixturerna fake_ha och ha_token importeras)

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, DecisionSource, LogicalState, ReviewStatus, TripSource
from app.services import settings as settings_svc
from app.services.trips import merge, pipeline
from app.services.trips import segments as seg
from app.services.volvo import detection
from tests.test_ha_api import API, bearer, ha_token  # noqa: F401
from tests.test_ha_support import HA_TOKEN, HA_URL, SERVICE, fake_ha  # noqa: F401

A, B, C, D = "A-gatan 1, Stad", "B-gatan 2, Stad", "C-gatan 3, Stad", "D-gatan 4, Stad"
RECON = dict(source=TripSource.VOLVO_API, observation_ids=["obs"])


def _rules(db, user, vehicle, actor):
    cfg = settings_svc.get_section(db, user.id, "merge")
    return merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor)


def _invariant(db, vehicle):
    """(aktiva journal-km, icke-makulerade raw-km, raws utan aktiv resa, raws i flera aktiva resor)."""
    db.flush()
    db.expire_all()
    active = seg.active_trips(db, vehicle.id)
    raws = db.execute(
        select(M.RawTrip).where(M.RawTrip.vehicle_id == vehicle.id, M.RawTrip.voided_at.is_(None))
    ).scalars().all()
    count = {r.id: 0 for r in raws}
    for t in active:
        for r in seg.ordered_raws(t):
            if r.id in count:
                count[r.id] += 1
    return (
        round(sum(t.distance_km for t in active), 3),
        round(sum(r.distance_km for r in raws), 3),
        [r for r in raws if count[r.id] == 0],
        [r for r in raws if count[r.id] > 1],
    )


def _import(trips, start_addr, end_addr, *, km, odo, **kw):
    kw.setdefault("category", Category.UNDECIDED)
    kw.setdefault("review_status", ReviewStatus.IMPORTED)
    return trips.trip(start_addr, end_addr, km=km, odometer_start=odo, import_batch_id=uuid.uuid4(), **kw)


# ------------------------------------------------- data-1 / inte-2: avstämning i kedjesammanslagning


def test_reconcile_nested_merge_keeps_other_legs(db, user, vehicle, actor, trips):
    a = trips.trip(A, B, km=10, odometer_start=10000, **RECON)
    trips.trip(B, C, km=20, gap_minutes=5, **RECON)
    trips.trip(C, D, km=30, gap_minutes=5, **RECON)
    assert _rules(db, user, vehicle, actor)["auto_merged"] == 2
    _import(trips, A, B, km=10, odo=10000, start_at=a.started_at)

    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False, origin="import")
    km_active, km_raw, orphans, dupes = _invariant(db, vehicle)
    assert out["superseded"] == 1
    assert orphans == [] and dupes == []
    assert km_active == km_raw == 60.0


def test_reconcile_chain_keeps_verified_import(db, user, vehicle, actor, trips):
    _import(
        trips, A, B, km=10, odo=10000, category=Category.WORK, category_source=DecisionSource.USER,
        review_status=ReviewStatus.APPROVED, purpose="Kundbesök Exempel AB", purpose_confirmed=True,
    )
    r2 = trips.trip(B, C, km=10, gap_minutes=5, **RECON)
    trips.trip(C, D, km=10, gap_minutes=5, **RECON)
    assert _rules(db, user, vehicle, actor)["auto_merged"] == 2
    _import(trips, B, C, km=10, odo=10010, start_at=r2.started_at)

    detection.reconcile_with_imports(db, vehicle, actor=actor)
    km_active, km_raw, orphans, dupes = _invariant(db, vehicle)
    assert orphans == [] and dupes == []
    assert km_active == km_raw == 30.0
    # Den verifierade importerade resan finns kvar aktiv, med sitt beslut.
    kept = [
        t for t in seg.active_trips(db, vehicle.id)
        if any(r.import_batch_id is not None and r.odometer_start == 10000 for r in seg.ordered_raws(t))
    ]
    assert len(kept) == 1
    assert kept[0].category == Category.WORK and kept[0].purpose == "Kundbesök Exempel AB"


def test_reconcile_after_split_of_chain(db, user, vehicle, actor, trips):
    trips.trip(A, B, km=10, odometer_start=10000, **RECON)
    trips.trip(B, C, km=20, gap_minutes=5, **RECON)
    trips.trip(C, D, km=30, gap_minutes=5, **RECON)
    _rules(db, user, vehicle, actor)
    m2 = seg.active_trips(db, vehicle.id)[0]
    assert m2.segment_count == 3
    _, right = merge.split_trip(db, m2, after_index=0, actor=actor)  # (A) + (B, C) utan källresor
    raw_b = seg.ordered_raws(right)[0]
    _import(trips, B, C, km=20, odo=raw_b.odometer_start, start_at=raw_b.started_at)

    detection.reconcile_with_imports(db, vehicle, actor=actor)
    km_active, km_raw, orphans, dupes = _invariant(db, vehicle)
    assert orphans == [] and dupes == []
    assert km_active == km_raw == 60.0


def test_reconcile_allocated_single_raw_is_voided_alone(db, user, vehicle, actor, trips):
    """En resa med fördelning (två delsträckor av samma körning) makuleras utan uppdelningsförsök."""
    t = trips.trip(A, B, km=10, odometer_start=10000, **RECON)
    trips.trip(B, C, km=20, gap_minutes=600, **RECON)
    trip = seg.load_trip(db, t.id)
    seg.allocate_raw_trip(
        db, trip, seg.ordered_raws(trip)[0].id,
        [{"category": Category.WORK, "distance_km": 6}, {"category": Category.PRIVATE, "distance_km": 4}], "test",
    )
    assert len(seg.load_trip(db, t.id).segments) == 2
    _import(trips, A, B, km=10, odo=10000, start_at=t.started_at)
    detection.reconcile_with_imports(db, vehicle, actor=actor)
    km_active, km_raw, orphans, dupes = _invariant(db, vehicle)
    assert orphans == [] and dupes == []
    assert km_active == km_raw == 30.0
    assert db.get(M.LogicalTrip, t.id).state == LogicalState.VOIDED


# ------------------------------------------------- inte-1: korta rekonstruerade resor


def _m(minutes: float):
    from datetime import timedelta

    from tests.test_volvo_support import T0

    return T0 + timedelta(minutes=minutes)


NEAR = (59.012, 15.000)  # ca 1,3 km från A i test_volvo_support


@pytest.mark.parametrize("km", [1, 2, 3])
def test_short_trip_without_import_is_kept(db, user, vehicle, km):
    from tests.test_volvo_support import FakeCar, connect, poll_every

    connect(db, user, vehicle)
    car = FakeCar()
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.drive_to(NEAR, km, _m(9))
    poll_every(db, vehicle, car, _m(10), _m(40))
    rs = db.execute(select(M.RawTrip)).scalars().all()
    assert [(r.odometer_end - r.odometer_start, r.distance_km) for r in rs] == [(km, float(km))]


def test_short_trip_covered_by_import_is_skipped(db, user, vehicle, trips):
    from tests.test_volvo_support import FakeCar, connect, poll_every

    connect(db, user, vehicle)
    car = FakeCar()
    _import(trips, A, B, km=2, odo=car.odometer)
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.drive_to(NEAR, 2, _m(9))
    poll_every(db, vehicle, car, _m(10), _m(40))
    assert db.execute(select(M.RawTrip).where(M.RawTrip.import_batch_id.is_(None))).scalars().all() == []


def test_reconcile_short_neighbours_are_not_an_exact_hit(db, user, vehicle, actor, trips):
    """Två korta resor som bara gränsar till varandra ersätter inte varandra."""
    rec = trips.trip(A, B, km=2, odometer_start=10000, **RECON)
    _import(trips, B, C, km=2, odo=10002)
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["superseded"] == 0
    assert db.get(M.LogicalTrip, rec.id).state == LogicalState.ACTIVE


# ------------------------------------------------- inte-3: känd mätarställning får inte glömmas


def test_failed_odometer_fetch_on_anchor_keeps_known_odometer(db, user, vehicle):
    from app.services.volvo.client import ApiResult
    from tests.test_volvo_support import FakeCar, connect, poll, poll_every

    class FlakyCar(FakeCar):
        failing: set = set()

        def fetch(self, endpoint_name, vin):
            if endpoint_name in self.failing:
                self.calls.append(endpoint_name)
                return ApiResult("error", 503, message="Service Unavailable")
            return super().fetch(endpoint_name, vin)

    connect(db, user, vehicle)
    car = FlakyCar()
    poll(db, vehicle, car, _m(0))  # 1000 km
    car.failing = {"odometer"}
    poll(db, vehicle, car, _m(5))  # rad utan mätarställning blir ankare
    car.failing = set()
    car.moving(8, _m(9.5))
    poll(db, vehicle, car, _m(10))
    car.moving(7, _m(14.5))
    poll(db, vehicle, car, _m(15))
    car.moving(5, _m(19.5))
    car.engine, car.engine_ts, car.pos_ts = "STOPPED", _m(22), _m(22)
    poll_every(db, vehicle, car, _m(25), _m(60))
    rs = db.execute(select(M.RawTrip)).scalars().all()
    assert [(r.odometer_start, r.odometer_end) for r in rs] == [(1000, 1020)]


def test_single_unsupported_answer_does_not_stop_odometer_polling(db, user, vehicle):
    from tests.test_volvo_support import FakeCar, connect, poll, poll_every

    connect(db, user, vehicle)
    car = FakeCar()
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.unsupported.add("odometer")
    poll(db, vehicle, car, _m(10))  # ett enstaka 404
    car.unsupported.discard("odometer")
    car.calls.clear()
    car.odometer += 40
    car.odo_ts = car.pos_ts = car.engine_ts = _m(75)
    poll_every(db, vehicle, car, _m(80), _m(180))
    assert car.calls.count("odometer") > 0
    rs = db.execute(select(M.RawTrip)).scalars().all()
    assert sum(r.distance_km for r in rs) == 40


def test_repeated_unsupported_answers_mark_endpoint_unsupported(db, user, vehicle):
    from app.services.volvo import constants as C
    from tests.test_volvo_support import FakeCar, connect, poll_every

    connect(db, user, vehicle)
    car = FakeCar()
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.unsupported.add("odometer")
    poll_every(db, vehicle, car, _m(10), _m(30))
    assert vehicle.capabilities["endpoints"]["odometer"]["state"] == C.CAP_UNSUPPORTED


# ------------------------------------------------- inte-4: eftersläpande mätarställning/position


def _stale_car():
    from tests.test_volvo_support import FakeCar

    class StaleCar(FakeCar):
        stale_odometer: tuple | None = None

        def fetch(self, endpoint_name, vin):
            if endpoint_name == "odometer" and self.stale_odometer is not None:
                real = (self.odometer, self.odo_ts)
                self.odometer, self.odo_ts = self.stale_odometer
                try:
                    return super().fetch(endpoint_name, vin)
                finally:
                    self.odometer, self.odo_ts = real
            return super().fetch(endpoint_name, vin)

    return StaleCar()


def test_lagging_odometer_after_arrival_gives_one_trip(db, user, vehicle):
    from tests.test_volvo_support import B as POS_B
    from tests.test_volvo_support import connect, poll_every

    connect(db, user, vehicle)
    car = _stale_car()
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.stale_odometer = (car.odometer, car.odo_ts)
    car.drive_to(POS_B, 14, _m(18))
    poll_every(db, vehicle, car, _m(20), _m(30))  # ny position, gammal mätarställning
    car.stale_odometer = None  # 1014 (avläst 07:18) syns först nu
    poll_every(db, vehicle, car, _m(35), _m(80))
    rs = db.execute(select(M.RawTrip)).scalars().all()
    assert [(r.odometer_start, r.odometer_end, r.distance_km) for r in rs] == [(1000, 1014, 14.0)]


def test_lagging_position_after_arrival_gives_one_trip(db, user, vehicle):
    from tests.test_volvo_support import B as POS_B
    from tests.test_volvo_support import FakeCar, connect, poll_every

    connect(db, user, vehicle)
    car = FakeCar()
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.odometer += 14
    car.odo_ts = _m(18)
    car.engine, car.engine_ts = "STOPPED", _m(18)
    poll_every(db, vehicle, car, _m(20), _m(35))
    car.lat, car.lon = POS_B
    car.pos_ts = _m(18)
    poll_every(db, vehicle, car, _m(40), _m(80))
    rs = db.execute(select(M.RawTrip)).scalars().all()
    assert [(r.odometer_start, r.odometer_end, r.distance_km) for r in rs] == [(1000, 1014, 14.0)]
    assert "round_trip_unobserved" not in rs[0].quality_flags


# ------------------------------------------------- inte-5: demoplatser på riktiga resor


def test_demo_places_never_match_real_vehicle(db, user, vehicle):
    from datetime import timedelta

    from app.services.volvo import demo as volvo_demo
    from tests.test_volvo_support import T0, FakeCar, connect, poll_every

    connect(db, user, vehicle)
    volvo_demo.enable(db, user.id, now=T0 - timedelta(days=1))
    car = FakeCar()
    poll_every(db, vehicle, car, _m(0), _m(5))
    office = volvo_demo.PLACES["office"]
    car.drive_to((office["lat"] + 0.0010, office["lon"]), 30, _m(40))  # ca 110 m från demokontoret
    poll_every(db, vehicle, car, _m(45), _m(70))
    raw = db.execute(select(M.RawTrip).where(M.RawTrip.vehicle_id == vehicle.id)).scalar_one()
    assert raw.end_location_id is None and raw.end_address is None


def test_demo_disable_clears_demo_addresses_on_real_trips(db, user, vehicle):
    """Resor från före rättningen: demo-adressen tas bort från riktiga rekonstruerade resor."""
    from datetime import timedelta

    from app.services.volvo import demo as volvo_demo
    from tests.test_volvo_support import T0, FakeCar, connect, poll_every

    connect(db, user, vehicle)
    volvo_demo.enable(db, user.id, now=T0 - timedelta(days=1))
    demo_loc = db.execute(
        select(M.KnownLocation).where(M.KnownLocation.owner_user_id == user.id, M.KnownLocation.is_demo.is_(True))
    ).scalars().first()
    car = FakeCar()
    poll_every(db, vehicle, car, _m(0), _m(5))
    car.drive_to((59.3, 15.3), 30, _m(40))
    poll_every(db, vehicle, car, _m(45), _m(70))
    raw = db.execute(select(M.RawTrip).where(M.RawTrip.vehicle_id == vehicle.id)).scalar_one()
    # Simulera en äldre resa som felaktigt fick demoplatsen.
    raw.end_location_id, raw.end_address = demo_loc.id, demo_loc.address or demo_loc.name
    trip = seg.load_trip(db, db.execute(select(M.TripSegment.logical_trip_id).where(
        M.TripSegment.raw_trip_id == raw.id)).scalar_one())
    seg.refresh_logical(db, trip)
    db.flush()
    volvo_demo.disable(db, user.id)
    db.flush()
    db.expire_all()
    raw = db.get(M.RawTrip, raw.id)
    trip = db.get(M.LogicalTrip, trip.id)
    assert raw.end_location_id is None and raw.end_address is None
    assert trip.end_address is None


# ------------------------------------------------- inte-7: bortkoppling upphävs inte av livstecken


def _heartbeat(anon_client, token):
    return anon_client.post(f"{API}/ha/heartbeat", headers=bearer(token), json={
        "ha_version": "2026.11.1", "notify_services": [f"notify.{SERVICE}"], "notify_service": f"notify.{SERVICE}",
    })


def test_disconnect_survives_heartbeat_in_pull_mode(client, anon_client, ha_token, db, user, vehicle, trips):
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull"})
    db.commit()
    assert _heartbeat(anon_client, ha_token).json()["status"] == "connected"
    assert client.post(f"{API}/integrations/home-assistant/disconnect").json()["status"] == "not_configured"

    hb = _heartbeat(anon_client, ha_token)
    assert hb.status_code == 200 and hb.json()["status"] == "not_configured"
    status = client.get(f"{API}/integrations/home-assistant").json()
    assert status["status"] == "not_configured" and status["channel"] is None and status["notify_service"] is None
    assert anon_client.get(f"{API}/ha/outbox", headers=bearer(ha_token)).json()["items"] == []

    # Återanslutning sker uttryckligen i webben.
    assert client.put(f"{API}/integrations/home-assistant", json={"delivery_mode": "pull"}).status_code == 200
    assert _heartbeat(anon_client, ha_token).json()["status"] == "connected"


# ------------------------------------------------- api-2: status efter bara leveransläge


def test_put_only_delivery_mode_push_is_not_configured(client):
    body = client.put(f"{API}/integrations/home-assistant", json={"delivery_mode": "push"}).json()
    assert body["status"] == "not_configured"


def test_put_only_delivery_mode_pull_waits_for_heartbeat(client, anon_client, ha_token):
    body = client.put(f"{API}/integrations/home-assistant", json={"delivery_mode": "pull"}).json()
    assert body["status"] == "not_configured"
    assert _heartbeat(anon_client, ha_token).json()["status"] == "connected"


# ------------------------------------------------- säke-2: token följer inte med till ny adress


def test_changing_base_url_without_token_clears_token(client, fake_ha):
    client.put(f"{API}/integrations/home-assistant", json={
        "base_url": HA_URL, "token": HA_TOKEN, "notify_service": SERVICE, "delivery_mode": "push"})
    body = client.put(f"{API}/integrations/home-assistant", json={"base_url": "http://attacker.example:8080"}).json()
    assert body["has_token"] is False and body["status"] == "not_configured"
    resp = client.post(f"{API}/integrations/home-assistant/test")
    assert resp.status_code in (400, 409)
    assert fake_ha.calls == []


def test_same_base_url_keeps_token(client):
    client.put(f"{API}/integrations/home-assistant", json={
        "base_url": HA_URL, "token": HA_TOKEN, "notify_service": SERVICE, "delivery_mode": "push"})
    body = client.put(f"{API}/integrations/home-assistant", json={"base_url": HA_URL + "/"}).json()
    assert body["has_token"] is True and body["status"] == "configured"


def test_new_base_url_with_new_token_is_accepted(client):
    client.put(f"{API}/integrations/home-assistant", json={
        "base_url": HA_URL, "token": HA_TOKEN, "notify_service": SERVICE, "delivery_mode": "push"})
    body = client.put(f"{API}/integrations/home-assistant", json={
        "base_url": "http://ha2.local:8123", "token": "NY-TOKEN"}).json()
    assert body["has_token"] is True and body["base_url"] == "http://ha2.local:8123"


# ------------------------------------------------- inte-9: databasfel i ett delsteg av tick()


def test_db_error_in_one_tick_step_does_not_stop_the_rest(db, user, monkeypatch):
    from app.services.ha import notifier
    from app.services.volvo import demo as volvo_demo
    from app.worker import tasks

    db.commit()
    called = []

    def broken_demo_tick(session, now=None):
        for _ in range(2):  # andra raden krockar med uq_integration_user_provider
            session.add(M.IntegrationConnection(user_id=user.id, provider="volvo", status="not_configured"))
            session.flush()

    monkeypatch.setattr(volvo_demo, "tick", broken_demo_tick)
    monkeypatch.setattr(notifier, "deliver_due", lambda session, now=None: called.append("deliver_due") or {})
    monkeypatch.setattr(notifier, "run_scheduled", lambda session, now=None: called.append("run_scheduled") or {})
    out = tasks.tick()
    assert out["volvo_demo"] == {"error": True}
    assert called == ["deliver_due", "run_scheduled"]


def test_db_error_for_one_user_does_not_stop_delivery_to_others(db, user, monkeypatch):
    from app.services.ha import notifier

    other = M.User(email="annan@example.com", display_name="Annan", password_hash="x")
    db.add(other)
    db.flush()
    for uid in (user.id, other.id):
        db.add(M.NotificationRequest(
            user_id=uid, kind="test", status="pending", dedupe_key=f"t:{uid}", public_id=uuid.uuid4().hex[:12],
            title="Test", message="Test",
        ))
    db.flush()
    delivered = []
    first = sorted([user.id, other.id])[0]

    def deliver_user(session, user_id, now):
        if user_id == first and "fel" not in delivered:
            delivered.append("fel")
            for _ in range(2):
                session.add(M.IntegrationConnection(user_id=user.id, provider="volvo", status="not_configured"))
                session.flush()
        delivered.append(user_id)
        session.execute(select(M.User.id)).all()  # sessionen måste gå att använda
        return {"sent": 1}

    monkeypatch.setattr(notifier, "deliver_user", deliver_user)
    stats = notifier.deliver_due(db)
    assert stats["failed"] == 1 and stats["sent"] == 1
    db.flush()  # sessionen är fortfarande användbar


# ------------------------------------------------- inte-11 / api-4: HA-demoläget av/på


def _real_question(db, user, vehicle, trips, now):
    from app.models.enums import ReviewKind
    from app.services.ha import notifier
    from tests.test_ha_support import HOME, OREBRO

    trip = trips.trip(HOME, OREBRO, km=42.0, minutes=30, start_at=now - timedelta(hours=1))
    (req,) = notifier.enqueue_for_changes(db, user_id=user.id, vehicle=vehicle, merge_items=[],
                                          trip_items=[(trip, [ReviewKind.CATEGORY])], now=now)
    return trip, req


def test_ha_demo_toggle_keeps_real_pending_question(db, user, vehicle, trips, fake_ha):
    from app.db import utcnow
    from app.models.enums import NotificationStatus
    from app.services.ha import demo as ha_demo
    from tests.test_ha_support import setup_push

    now = utcnow()
    setup_push(db, user, batch_window_minutes=10, quiet_start="00:00", quiet_end="00:00")
    _, req = _real_question(db, user, vehicle, trips, now)
    ha_demo.enable(db, user.id, now)
    ha_demo.disable(db, user.id)
    db.flush()
    assert req.status == NotificationStatus.PENDING and req.channel is None


def test_ha_demo_disable_requeues_real_question_shown_on_demo_phone(db, user, vehicle, trips, fake_ha):
    from app.db import utcnow
    from app.models.enums import NotificationStatus
    from app.services.ha import demo as ha_demo
    from app.services.ha.connection import CHANNEL_DEMO

    now = utcnow()
    ha_demo.enable(db, user.id, now)
    _, req = _real_question(db, user, vehicle, trips, now)
    req.status, req.channel, req.sent_at = NotificationStatus.SENT, CHANNEL_DEMO, now  # visad i demotelefonen
    db.flush()
    ha_demo.disable(db, user.id)
    db.flush()
    assert req.status == NotificationStatus.PENDING and req.channel is None and req.sent_at is None


def test_ha_demo_disable_clears_test_notification_info(client, fake_ha):
    client.post(f"{API}/integrations/home-assistant/demo/enable")
    client.post(f"{API}/integrations/home-assistant/test-notification")
    body = client.post(f"{API}/integrations/home-assistant/demo/disable").json()
    assert body["last_test_sent_at"] is None and body["last_test_confirmed_at"] is None


# ------------------------------------------------- api-6: Volvo-demo "senast synkad"


def test_volvo_demo_last_success_follows_real_clock(db, user):
    from app.models.enums import IntegrationProvider
    from app.services import integrations
    from app.services.volvo import demo as volvo_demo
    from tests.test_volvo_support import T0

    volvo_demo.enable(db, user.id, now=T0)
    conn = integrations.get(db, user.id, IntegrationProvider.VOLVO)
    assert conn.last_success_at == T0
    volvo_demo.simulate_trip(db, user.id, "commute", now=T0 + timedelta(hours=2))
    assert conn.last_success_at == T0 + timedelta(hours=2)
