"""Avstämning: rekonstruerade API-resor ersätts av senare importerade originalresor."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    DecisionSource,
    LogicalState,
    ReviewKind,
    ReviewStatus,
    TripSource,
)
from app.services.trips import segments as seg
from app.services.volvo import detection
from tests.test_volvo_support import T0, B, FakeCar, connect, poll, poll_every

HOME = "Demovägen 9, Testby"
OFFICE = "Kontorsgatan 2, Provstad"


def m(minutes: float):
    return T0 + timedelta(minutes=minutes)


def detect_trip(db, vehicle) -> M.RawTrip:
    """En rekonstruerad resa 1000 → 1014 km."""
    car = FakeCar()
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(5, m(9.5))
    poll(db, vehicle, car, m(10))
    car.drive_to(B, 9, m(18))
    poll_every(db, vehicle, car, m(20), m(40))
    raw = db.execute(select(M.RawTrip).where(M.RawTrip.source == TripSource.VOLVO_API)).scalar_one()
    return raw


def trip_of(db, raw: M.RawTrip) -> M.LogicalTrip:
    trip = db.execute(
        select(M.LogicalTrip)
        .join(M.TripSegment, M.TripSegment.logical_trip_id == M.LogicalTrip.id)
        .where(M.TripSegment.raw_trip_id == raw.id)
        .order_by(M.LogicalTrip.created_at.desc())
    ).scalars().first()
    return seg.load_trip(db, trip.id)


def decide(trip: M.LogicalTrip, category=Category.WORK, purpose="Kundmöte hos Exempel AB") -> None:
    trip.category, trip.category_source, trip.review_status = category, DecisionSource.USER, ReviewStatus.APPROVED
    trip.purpose, trip.purpose_source, trip.purpose_confirmed = purpose, DecisionSource.USER, True


def imported(trips, *, start: float, km: float, category=Category.WORK, user_decision: bool = False) -> M.LogicalTrip:
    """Importerad originalresa med samma mätarintervall (importflödet sätter import_batch_id)."""
    trip = trips.trip(
        HOME, OFFICE, km=km, odometer_start=start, start_at=m(7), minutes=11, category=category,
        category_source=DecisionSource.USER if user_decision else DecisionSource.IMPORT,
        review_status=ReviewStatus.APPROVED if user_decision else ReviewStatus.IMPORTED,
        import_batch_id=uuid.uuid4(),
    )
    return trip


@pytest.fixture()
def api_raw(db, user, vehicle) -> M.RawTrip:
    connect(db, user, vehicle)
    return detect_trip(db, vehicle)


def test_one_to_one_match_supersedes_and_transfers_decision(db, user, vehicle, actor, trips, api_raw):
    rec_trip = trip_of(db, api_raw)
    decide(rec_trip)
    db.flush()
    imp = imported(trips, start=1000, km=14, category=Category.UNDECIDED)
    imp_raw = imp.segments[0].raw_trip

    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["superseded"] == 1 and out["transferred"] == 1 and out["exact"] == 1
    assert api_raw.superseded_by_id == imp_raw.id and api_raw.voided_at is not None
    assert api_raw.void_reason == "Ersatt av importerad originalresa"
    assert trip_of(db, api_raw).state == LogicalState.VOIDED
    imp = seg.load_trip(db, imp.id)
    assert imp.category == Category.WORK and imp.category_source == DecisionSource.USER
    assert imp.purpose == "Kundmöte hos Exempel AB" and imp.purpose_confirmed
    assert imp.review_status == ReviewStatus.APPROVED
    assert db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).first() is None
    # Inga dubbletter: exakt en aktiv resa täcker sträckan, och omkörning ändrar inget.
    active = [t for t in seg.active_trips(db, vehicle.id) if t.odometer_start == 1000]
    assert [t.id for t in active] == [imp.id]
    assert detection.reconcile_with_imports(db, vehicle, actor=actor)["superseded"] == 0


def test_imported_trip_learns_api_coordinates(db, user, vehicle, actor, trips, api_raw):
    imp = imported(trips, start=1000, km=14)
    imp_raw = imp.segments[0].raw_trip
    detection.reconcile_with_imports(db, vehicle, actor=actor)
    start_loc = db.get(M.KnownLocation, imp_raw.start_location_id)
    end_loc = db.get(M.KnownLocation, imp_raw.end_location_id)
    assert (start_loc.latitude, start_loc.longitude) == (api_raw.start_lat, api_raw.start_lon)
    assert (end_loc.latitude, end_loc.longitude) == (api_raw.end_lat, api_raw.end_lon)
    assert end_loc.coordinate_source == "api"


def test_existing_user_decision_on_import_is_kept(db, user, vehicle, actor, trips, api_raw):
    decide(trip_of(db, api_raw), category=Category.WORK)
    imp = imported(trips, start=1000, km=14, category=Category.PRIVATE, user_decision=True)
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["transferred"] == 0 and out["superseded"] == 1
    assert seg.load_trip(db, imp.id).category == Category.PRIVATE
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).scalar_one()
    assert item.logical_trip_id == imp.id


def test_no_transfer_without_user_decision(db, user, vehicle, actor, trips, api_raw):
    imp = imported(trips, start=1000, km=14, category=Category.WORK)
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["transferred"] == 0 and out["superseded"] == 1
    imp = seg.load_trip(db, imp.id)
    assert imp.category_source == DecisionSource.IMPORT and imp.review_status == ReviewStatus.IMPORTED


def test_partial_overlap_creates_review_item(db, user, vehicle, actor, trips, api_raw):
    decide(trip_of(db, api_raw))
    imp = imported(trips, start=1000, km=8)  # täcker bara 1000–1008 av 1000–1014
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["transferred"] == 0 and out["review_items"] == 1
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).scalar_one()
    assert item.data["reconstructed_raw_trip_id"] == str(api_raw.id)
    assert item.data["imported_raw_trip_ids"] == [str(imp.segments[0].raw_trip.id)]
    assert seg.load_trip(db, imp.id).category_source == DecisionSource.IMPORT  # inget beslut flyttat
    # Kilometrarna dubbelräknas inte: den rekonstruerade resan är makulerad.
    assert api_raw.voided_at is not None
    detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert len(db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).scalars().all()) == 1


def test_one_reconstructed_against_two_imports_is_review_case(db, user, vehicle, actor, trips, api_raw):
    decide(trip_of(db, api_raw))
    imported(trips, start=1000, km=6)
    imported(trips, start=1006, km=8)
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["superseded"] == 1 and out["transferred"] == 0 and out["exact"] == 0
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).scalar_one()
    assert len(item.data["imported_raw_trip_ids"]) == 2


def test_unrelated_import_leaves_api_trip_alone(db, user, vehicle, actor, trips, api_raw):
    imported(trips, start=2000, km=10)
    out = detection.reconcile_with_imports(db, vehicle, actor=actor)
    assert out["superseded"] == 0 and api_raw.voided_at is None


def test_detection_skips_distance_already_imported(db, user, vehicle, trips):
    connect(db, user, vehicle)
    imported(trips, start=1000, km=14)
    car = FakeCar()
    poll_every(db, vehicle, car, m(0), m(5))
    car.drive_to(B, 14, m(18))
    poll_every(db, vehicle, car, m(20), m(40))
    assert db.execute(select(M.RawTrip).where(M.RawTrip.source == TripSource.VOLVO_API)).first() is None
