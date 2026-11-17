"""Trip Detection Engine: resor rekonstrueras ur fordonsobservationer."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    DistanceQuality,
    LogicalState,
    PositionSource,
    ReviewKind,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.services import settings as settings_svc
from app.services.volvo import detection
from tests.test_volvo_support import C_, T0, A, B, FakeCar, connect, poll, poll_every


def raws(db) -> list[M.RawTrip]:
    return list(db.execute(select(M.RawTrip).order_by(M.RawTrip.odometer_start)).scalars())


@pytest.fixture()
def car(db, user, vehicle) -> FakeCar:
    connect(db, user, vehicle)
    return FakeCar()


def m(minutes: float):
    return T0 + timedelta(minutes=minutes)


def observed_trip(db, vehicle, car, start_min: float = 0, dest=B, km: int = 14) -> None:
    """Bilen står still, kör (mätarställningen rapporteras under färd), anländer och står still."""
    poll_every(db, vehicle, car, m(start_min), m(start_min + 5))
    car.moving(5, m(start_min + 9.5))
    poll(db, vehicle, car, m(start_min + 10))
    car.moving(6, m(start_min + 14.5))
    poll(db, vehicle, car, m(start_min + 15))
    car.drive_to(dest, km - 11, m(start_min + 18))
    poll_every(db, vehicle, car, m(start_min + 20), m(start_min + 40))


def test_simple_observed_trip(db, user, vehicle, car):
    observed_trip(db, vehicle, car)
    (raw,) = raws(db)
    assert raw.source == TripSource.VOLVO_API and raw.voided_at is None
    assert raw.odometer_start == 1000 and raw.odometer_end == 1014 and raw.distance_km == 14
    assert raw.distance_quality == DistanceQuality.ODOMETER and raw.odometer_source == "api"
    assert raw.position_source == PositionSource.API_POSITION
    assert (raw.start_lat, raw.end_lat) == (A[0], B[0])
    # Sluttid = bilens egen positionstidsstämpel.
    assert raw.ended_at == m(18) and raw.end_time_quality == TimeQuality.REPORTED
    # Starttiden räknas fram ur mätarökningarna under färd (5 km på 07:09:30, 11 km på
    # 07:14:30 → 72 km/h) och märks som uträknad. Gränserna och grunden sparas.
    assert raw.start_time_quality == TimeQuality.ESTIMATED and raw.started_at == m(5)
    assert raw.start_earliest is not None and raw.start_latest is not None
    assert raw.start_earliest <= raw.started_at <= raw.start_latest <= m(9.5)
    assert raw.start_estimate_basis["method"] == "odometer_speed"
    assert raw.start_estimate_basis["margin_min"] <= 5
    assert raw.fuel_liters is None and raw.fuel_quality == ValueQuality.UNKNOWN
    assert raw.energy_kwh is None and raw.energy_quality == ValueQuality.UNKNOWN
    assert raw.possible_missed_trips is False and raw.is_complete is True
    assert "reconstructed" in raw.quality_flags and raw.observation_ids
    # Journalresa skapad som oavgjord och obligatoriskt granskad.
    seg = db.execute(select(M.TripSegment).where(M.TripSegment.raw_trip_id == raw.id)).scalar_one()
    trip = db.get(M.LogicalTrip, seg.logical_trip_id)
    assert trip.state == LogicalState.ACTIVE and trip.started_at == raw.started_at
    assert trip.start_time_quality == TimeQuality.ESTIMATED and "start_time_uncertain" not in trip.quality_flags
    assert trip.category in (Category.UNDECIDED, Category.WORK, Category.PRIVATE)
    assert trip.review_status in (ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED, ReviewStatus.AUTO)
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.detected")).scalar_one()


def test_trip_is_not_closed_while_still_moving(db, user, vehicle, car):
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(5, m(9))
    poll(db, vehicle, car, m(10))
    car.moving(6, m(14))
    poll(db, vehicle, car, m(15))
    assert raws(db) == []  # ingen ankomst och inget stillastående än
    pending = db.execute(select(M.VehicleObservation).where(M.VehicleObservation.processed_at.is_(None))).scalars().all()
    assert len(pending) == 2


def test_silent_trip_has_bounds_and_start_estimated_from_distance(db, user, vehicle, car):
    """Bilen rapporterar först efter ankomst: gränserna kommer från bilen, och starttiden
    uppskattas ur sträckan (1.2.1, K4) – märkt som uppskattning, aldrig som bilens tid."""
    parked_since = car.odo_ts
    poll_every(db, vehicle, car, m(0), m(30))
    car.drive_to(B, 14, m(42))  # ingen rapport under färd
    poll_every(db, vehicle, car, m(45), m(70))
    (raw,) = raws(db)
    assert raw.start_time_quality == TimeQuality.ESTIMATED
    assert raw.start_estimate_basis["method"] == "distance_speed"
    assert raw.start_earliest == parked_since  # bilen stod bevisligen still vid sin senaste rapport
    assert raw.start_latest < raw.ended_at == m(42)
    assert raw.start_latest <= m(42) - timedelta(hours=14 / detection.MAX_AVG_SPEED_KMH)
    assert raw.start_earliest <= raw.started_at <= raw.start_latest
    assert raw.duration_seconds is None  # bilens egen körtid finns inte
    trip = db.execute(select(M.LogicalTrip)).scalar_one()
    assert trip.start_time_quality == TimeQuality.ESTIMATED and "start_time_uncertain" not in trip.quality_flags


def test_round_trip_without_position_change_is_flagged(db, user, vehicle, car):
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(10, m(14))
    poll(db, vehicle, car, m(15))
    car.drive_to(A, 14, m(40))  # tillbaka till samma plats
    poll_every(db, vehicle, car, m(45), m(80))
    (raw,) = raws(db)
    assert raw.distance_km == 24
    assert raw.possible_missed_trips is True and raw.is_complete is False
    assert "round_trip_unobserved" in raw.quality_flags
    trip = db.execute(select(M.LogicalTrip)).scalar_one()
    item = db.execute(
        select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.DATA_GAP, M.ReviewItem.logical_trip_id == trip.id)
    ).scalar_one()
    assert "datalucka" in item.title.lower()


def test_odometer_increase_with_unchanged_position_report_is_flagged(db, user, vehicle, car):
    """Positionen rapporteras inte alls om – mätaren har ändå ökat."""
    poll_every(db, vehicle, car, m(0), m(5))
    car.odometer += 9
    car.odo_ts = m(20)
    poll_every(db, vehicle, car, m(25), m(60))
    (raw,) = raws(db)
    assert raw.possible_missed_trips and not raw.is_complete
    assert "round_trip_unobserved" in raw.quality_flags
    assert raw.end_time_quality != TimeQuality.REPORTED


def test_detour_is_flagged_as_possible_multiple_trips(db, user, vehicle, car):
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(20, m(25))
    poll(db, vehicle, car, m(26))
    car.drive_to(B, 25, m(55))  # 45 km för 10 km fågelväg
    poll_every(db, vehicle, car, m(60), m(80))
    (raw,) = raws(db)
    assert raw.distance_km == 45
    assert "detour_or_multiple_trips" in raw.quality_flags
    assert raw.possible_missed_trips and not raw.is_complete


def test_normal_road_distance_is_not_a_detour(db, user, vehicle, car):
    observed_trip(db, vehicle, car, km=20)  # 20 km väg för 10 km fågelväg
    (raw,) = raws(db)
    assert "detour_or_multiple_trips" not in raw.quality_flags and not raw.possible_missed_trips


def test_collection_gap_is_flagged(db, user, vehicle, car):
    poll_every(db, vehicle, car, m(0), m(10))
    car.drive_to(B, 14, m(100))
    # Insamlingen låg nere i två timmar.
    poll_every(db, vehicle, car, m(130), m(150))
    (raw,) = raws(db)
    assert "collection_gap" in raw.quality_flags
    assert raw.possible_missed_trips and not raw.is_complete


def test_several_trips_in_sequence(db, user, vehicle, car):
    observed_trip(db, vehicle, car, start_min=0, dest=B, km=14)
    observed_trip(db, vehicle, car, start_min=60, dest=C_, km=15)
    first, second = raws(db)
    assert (first.odometer_start, first.odometer_end) == (1000, 1014)
    assert (second.odometer_start, second.odometer_end) == (1014, 1029)
    assert second.start_lat == B[0] and second.end_lon == C_[1]
    assert second.start_earliest >= first.ended_at
    assert len(db.execute(select(M.LogicalTrip).where(M.LogicalTrip.state == LogicalState.ACTIVE)).scalars().all()) >= 1


def test_short_stop_inside_one_movement_splits_into_legs(db, user, vehicle, car):
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(6, m(9))
    poll(db, vehicle, car, m(10))
    car.drive_to(B, 8, m(18))
    poll(db, vehicle, car, m(20))  # framme vid B
    car.moving(5, m(24))  # kör vidare efter kort stopp
    poll(db, vehicle, car, m(25))
    car.drive_to(C_, 11, m(33))
    poll_every(db, vehicle, car, m(35), m(60))
    first, second = raws(db)
    assert (first.odometer_start, first.odometer_end) == (1000, 1014)
    assert (second.odometer_start, second.odometer_end) == (1014, 1030)
    assert first.ended_at == m(18) and second.ended_at == m(33)
    # 1.5.0: ankomsten till B hade kompletta uppgifter och stängdes direkt (hämtningen 07:20),
    # innan bilen körde vidare. De två resorna kommer därför från var sin rörelse och märks
    # inte längre "uppdelad efter positionsrapporter".
    assert "split_by_position_reports" not in first.quality_flags
    assert second.start_earliest >= first.ended_at - timedelta(minutes=2)
    # Starten efter stoppet räknas fram ur farten och ligger inom gränserna (efter ankomsten till B).
    assert second.start_time_quality == TimeQuality.ESTIMATED
    assert second.start_earliest <= second.started_at <= second.start_latest <= m(24)


def test_detection_is_idempotent(db, user, vehicle, car):
    observed_trip(db, vehicle, car)
    assert len(raws(db)) == 1
    assert detection.process_vehicle(db, vehicle, user_id=user.id) == []
    poll_every(db, vehicle, car, m(45), m(60))  # oförändrat läge
    # Även en full omkörning från början ger inga dubbletter.
    for obs in db.execute(select(M.VehicleObservation)).scalars():
        obs.processed_at = None
    db.flush()
    assert detection.process_vehicle(db, vehicle, user_id=user.id) == []
    assert len(raws(db)) == 1
    assert len(db.execute(select(M.TripSegment)).scalars().all()) == 1


def test_engine_stopped_closes_trip_early(db, user, vehicle, car):
    """Genvägen med avstängd motor gäller när ankomstens uppgifter inte är kompletta (1.5.0:
    med kompletta uppgifter stängs resan redan vid första hämtningen, se test_v150_ankomst.py).
    Här är mätarvärdet avläst fyra minuter före positionen."""
    settings_svc.update_section(db, user.id, "volvo", {"settle_minutes": 60})
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(5, m(9))
    poll(db, vehicle, car, m(10))
    car.drive_to(B, 9, m(18))
    car.odo_ts = m(14)
    poll(db, vehicle, car, m(20))
    assert raws(db) == []
    poll(db, vehicle, car, m(25))  # bekräftat av en andra hämtning
    assert len(raws(db)) == 1


def test_decreasing_odometer_is_reported_not_used(db, user, vehicle, car):
    poll_every(db, vehicle, car, m(0), m(5))
    car.odometer -= 50
    car.odo_ts = m(9)
    poll_every(db, vehicle, car, m(10), m(40))
    assert raws(db) == []
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.ANOMALY)).scalar_one()
    assert item.data["type"] == "odometer_decreased"


def test_demo_vehicle_trips_get_demo_source(db, user, vehicle):
    from app.services import vehicles

    conn = connect(db, user, vehicle)
    demo_car = vehicles.demo_vehicle(db, user.id)
    demo_car.vin, demo_car.volvo_connection_id = "DEMO0000000000099", conn.id
    db.flush()
    car = FakeCar(is_demo=True)
    observed_trip(db, demo_car, car)
    (raw,) = raws(db)
    assert raw.source == TripSource.DEMO and raw.vehicle_id == demo_car.id
