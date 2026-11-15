"""Regressionstester för granskningsfynd i integrationerna (Volvo, Home Assistant, worker)."""

from __future__ import annotations

import uuid

import pytest

from sqlalchemy import select

import app.models as M
from app.models.enums import Category, DecisionSource, LogicalState, ReviewStatus, TripSource
from app.services import settings as settings_svc
from app.services.trips import merge, pipeline
from app.services.trips import segments as seg
from app.services.volvo import detection

A, B, C, D = "A-gatan 1, Stad", "B-gatan 2, Stad", "C-gatan 3, Stad", "D-gatan 4, Stad"
API = dict(source=TripSource.VOLVO_API, observation_ids=["obs"])


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
    a = trips.trip(A, B, km=10, odometer_start=10000, **API)
    trips.trip(B, C, km=20, gap_minutes=5, **API)
    trips.trip(C, D, km=30, gap_minutes=5, **API)
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
    r2 = trips.trip(B, C, km=10, gap_minutes=5, **API)
    trips.trip(C, D, km=10, gap_minutes=5, **API)
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
    trips.trip(A, B, km=10, odometer_start=10000, **API)
    trips.trip(B, C, km=20, gap_minutes=5, **API)
    trips.trip(C, D, km=30, gap_minutes=5, **API)
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
    t = trips.trip(A, B, km=10, odometer_start=10000, **API)
    trips.trip(B, C, km=20, gap_minutes=600, **API)
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
    rec = trips.trip(A, B, km=2, odometer_start=10000, **API)
    _import(trips, B, C, km=2, odo=10002)
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["superseded"] == 0
    assert db.get(M.LogicalTrip, rec.id).state == LogicalState.ACTIVE
