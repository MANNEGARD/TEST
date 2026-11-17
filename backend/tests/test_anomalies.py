"""Avvikelsedetektion: varje fall, idempotens och avfärdade ärenden."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import app.models as M
from app.models.enums import Category, GapKind, GapStatus, IntegrationStatus, ReviewKind, ReviewState
from app.services.stats import anomalies
from app.services.trips import review
from tests.conftest import BASE_TIME

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def scan(db, user, vehicle, now=NOW):
    return anomalies.scan_vehicle(db, vehicle, user_id=user.id, now=now)


def items(db, vehicle, status=ReviewState.OPEN):
    return list(
        db.execute(
            select(M.ReviewItem).where(
                M.ReviewItem.vehicle_id == vehicle.id, M.ReviewItem.kind == ReviewKind.ANOMALY,
                M.ReviewItem.status == status,
            )
        ).scalars()
    )


def obs(db, vehicle, at: datetime, km: float | None, fuel: float | None = None) -> M.VehicleObservation:
    o = M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=at, last_seen_at=at, odometer_km=km, fuel_liters=fuel,
                             content_hash=f"h{at.timestamp()}{km}{fuel}")
    db.add(o)
    db.flush()
    return o


def test_clean_journal_gives_no_anomalies(db, user, vehicle, trips):
    for _ in range(3):
        trips.trip(category=Category.WORK, purpose="Kundbesök", km=12.0, gap_minutes=90)
    result = scan(db, user, vehicle)
    assert result["created_total"] == 0 and result["found"] == {}


def test_odometer_anomalies(db, user, vehicle, trips):
    t1 = trips.trip(km=10.0)
    raw = t1.segments[0].raw_trip
    raw.odometer_end = raw.odometer_start - 5  # bakåt inom resan
    t2 = trips.trip(km=10.0, gap_minutes=60)
    raw2 = t2.segments[0].raw_trip
    raw2.odometer_end = raw2.odometer_start + 40  # 10 km enligt resan, 40 km enligt mätaren
    db.flush()
    result = scan(db, user, vehicle)
    assert result["created"]["odometer_backwards"] >= 1
    assert result["created"]["odometer_mismatch"] == 1
    titles = {i.title for i in items(db, vehicle)}
    assert "Mätarställningen går bakåt inom en resa" in titles
    assert "Körsträckan stämmer inte med mätarställningen" in titles


def test_duplicate_time_conflict_and_overlap(db, user, vehicle, trips):
    t1 = trips.trip(km=10.0, minutes=30)
    raw1 = t1.segments[0].raw_trip
    trips.trip(km=10.2, start_at=raw1.started_at + timedelta(seconds=40), odometer_start=raw1.odometer_end + 1)
    t3 = trips.trip(km=5.0, gap_minutes=240)
    raw3 = t3.segments[0].raw_trip
    raw3.duration_seconds = 3 * 3600  # längre körtid än resans längd
    db.flush()
    result = scan(db, user, vehicle)
    assert result["created"]["possible_duplicate"] == 1
    assert result["created"]["time_overlap"] == 1
    assert result["created"]["time_conflict"] == 1


def test_negative_elapsed_time(db, user, vehicle, trips):
    t = trips.trip(km=5.0)
    raw = t.segments[0].raw_trip
    raw.ended_at = raw.started_at - timedelta(minutes=5)
    db.flush()
    assert scan(db, user, vehicle)["created"]["time_conflict"] == 1


def test_large_gap(db, user, vehicle, complete_scope):
    db.add(M.DataGap(vehicle_id=vehicle.id, kind=GapKind.ODOMETER, dedupe_key="odo:1000:1700", odometer_start=1000,
                     odometer_end=1700, km=700.0, status=GapStatus.OPEN))
    db.add(M.DataGap(vehicle_id=vehicle.id, kind=GapKind.ODOMETER, dedupe_key="odo:2000:2050", odometer_start=2000,
                     odometer_end=2050, km=50.0, status=GapStatus.OPEN))
    db.flush()
    result = scan(db, user, vehicle)
    assert result["created"] == {"large_gap": 1}
    assert items(db, vehicle)[0].title == "Ovanligt stort kilometergap: 700 km"


def test_consumption_gives_no_anomaly(db, user, vehicle, trips):
    """1.3.2: förbrukningen redovisas inte till arbetsgivaren och blir aldrig ett ärende."""
    raws = [trips.trip(km=20.0, gap_minutes=60).segments[0].raw_trip for _ in range(22)]
    for i, r in enumerate(raws):
        r.fuel_liters = round(20.0 * (0.065 + 0.001 * (i % 5)), 3)  # 6,5–6,9 l/100 km
        r.energy_kwh, r.energy_quality = None, "unknown"
    raws[5].fuel_liters = 20.0 * 0.40  # 40 l/100 km
    db.flush()
    result = scan(db, user, vehicle)
    assert result["created"] == {} and items(db, vehicle) == []


def test_observation_jump_backwards_and_missed_trips(db, user, vehicle, trips, complete_scope):
    trips.trip(km=10.0)  # journalen slutar på 10010 km
    obs(db, vehicle, NOW - timedelta(days=3), 10010)
    obs(db, vehicle, NOW - timedelta(days=3, hours=-2), 11500)  # +1490 km på 2 h
    obs(db, vehicle, NOW - timedelta(days=2), 11400)  # bakåt
    obs(db, vehicle, NOW - timedelta(hours=5), 11450)
    result = scan(db, user, vehicle)
    assert result["created"]["odometer_jump"] == 1
    assert result["created"]["odometer_backwards"] == 1
    assert result["created"]["missed_trips"] == 1
    missed = next(i for i in items(db, vehicle) if i.data.get("type") == "missed_trips")
    assert missed.data["km"] == 1440.0


def test_stale_vehicle_data_and_auto_resolve(db, user, vehicle):
    conn = M.IntegrationConnection(user_id=user.id, provider="volvo", status=IntegrationStatus.CONNECTED,
                                   last_success_at=NOW - timedelta(days=3))
    db.add(conn)
    db.flush()
    vehicle.volvo_connection_id = conn.id
    db.flush()
    assert scan(db, user, vehicle)["created"] == {"stale_vehicle_data": 1}
    conn.last_success_at = NOW - timedelta(minutes=10)
    db.flush()
    assert scan(db, user, vehicle)["created_total"] == 0
    assert items(db, vehicle) == []
    (resolved,) = items(db, vehicle, ReviewState.RESOLVED)
    assert resolved.resolution["reason"] == "data_fresh"


def test_scan_is_idempotent_and_respects_dismissal(db, user, vehicle, trips):
    t = trips.trip(km=10.0)
    raw = t.segments[0].raw_trip
    raw.odometer_end = raw.odometer_start + 40
    db.flush()
    first = scan(db, user, vehicle)
    assert first["created_total"] == 1
    second = scan(db, user, vehicle)
    assert second["created_total"] == 0 and second["found"] == first["found"]
    assert len(items(db, vehicle)) == 1
    (item,) = items(db, vehicle)
    review.resolve(db, item, by="user", resolution={"dismissed": True}, dismissed=True)
    db.flush()
    third = scan(db, user, vehicle)
    assert third["created_total"] == 0 and items(db, vehicle) == []
    assert BASE_TIME < NOW
