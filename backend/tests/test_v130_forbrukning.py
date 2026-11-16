"""DEL 7 (1.3.0): bensin- och elförbrukning per resa ur bilens egna avläsningar.

Trippmätare × snitt (även när två resor delar samma trippmätarperiod), nollställd trippmätare,
nivåskillnad i tanken med en tankning som hålls utanför, orimliga värden kastas, befintliga och
importerade värden rörs aldrig, och hela vägen från demobilens hämtningar till resan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import MonthCloseState, TripSource, ValueQuality
from app.services.audit import SYSTEM
from app.services.trips import pipeline
from app.services.trips import segments as seg
from app.services.volvo import consumption, demo
from app.services.volvo.consumption import Reading
from tests.conftest import reload_trip
from tests.test_volvo_support import T0

DEP = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def rd(minutes: float, *, trip: float | None = None, fuel_avg: float | None = None, energy_avg: float | None = None,
       fuel: float | None = None, battery: float | None = None, seen_minutes: float | None = None) -> Reading:
    at = DEP + timedelta(minutes=minutes)
    seen = DEP + timedelta(minutes=seen_minutes) if seen_minutes is not None else at
    return Reading(id=f"o{minutes}", fetched_at=at, last_seen_at=seen, fuel_liters=fuel, battery_percent=battery,
                   avg_fuel_consumption_auto=fuel_avg, avg_energy_consumption_auto=energy_avg,
                   trip_meter_auto_km=trip)


def raw_trip(km: float, start_min: float, end_min: float) -> M.RawTrip:
    return M.RawTrip(distance_km=km, started_at=DEP + timedelta(minutes=start_min),
                     ended_at=DEP + timedelta(minutes=end_min))


def estimate(raw, before, after, **kw):
    return consumption.estimate_for_raw(raw, before, after, battery_kwh=kw.get("battery_kwh"), tank_l=kw.get("tank_l"))


# ----------------------------------------------------------------------------- beräkningen


def test_trip_meter_times_average_gives_fuel_and_energy():
    raw = raw_trip(30, 0, 35)
    est = estimate(raw, [rd(-5, trip=0.0, fuel_avg=0.0, energy_avg=0.0)],
                   [rd(36, trip=30.2, fuel_avg=5.0, energy_avg=18.0)])
    assert est.fuel_liters == pytest.approx(1.51) and est.fuel_method == "trip_meter"
    assert est.energy_kwh == pytest.approx(5.44) and est.energy_method == "trip_meter"


def test_two_trips_in_the_same_trip_meter_period_get_their_own_share():
    """Snittets förändring viktad med sträckan: varje resa får bara sin egen del."""
    first, second = raw_trip(20, 0, 25), raw_trip(10, 60, 75)
    start = rd(-5, trip=0.0, fuel_avg=0.0)
    middle = rd(26, trip=20.0, fuel_avg=6.0)  # 1,2 l på de första 20 km
    end = rd(76, trip=30.0, fuel_avg=5.0)  # 1,5 l totalt → 0,3 l på de sista 10 km
    a = estimate(first, [start], [middle, end])
    b = estimate(second, [start, middle], [end])
    assert a.fuel_liters == pytest.approx(1.2) and b.fuel_liters == pytest.approx(0.3)
    assert a.fuel_liters + b.fuel_liters == pytest.approx(30.0 * 5.0 / 100)


def test_reset_trip_meter_uses_the_new_period_only():
    raw = raw_trip(12, 0, 15)
    est = estimate(raw, [rd(-300, trip=145.0, fuel_avg=6.1, seen_minutes=-1)], [rd(16, trip=12.1, fuel_avg=4.0)])
    assert est.fuel_liters == pytest.approx(0.48) and est.fuel_method == "trip_meter_reset"


def test_trip_meter_that_does_not_match_the_distance_is_not_used():
    raw = raw_trip(12, 0, 15)
    est = estimate(raw, [rd(-5, trip=10.0, fuel_avg=6.0)], [rd(16, trip=40.0, fuel_avg=5.0)])
    assert est.fuel_liters is None and est.energy_kwh is None


def test_level_difference_when_no_trip_meter():
    raw = raw_trip(40, 0, 40)
    est = estimate(raw, [rd(-5, fuel=45.0)], [rd(20, fuel=43.6), rd(41, fuel=42.4)])
    assert est.fuel_liters == pytest.approx(2.6) and est.fuel_method == "level"


def test_unchanged_tank_level_gives_unknown_not_zero():
    raw = raw_trip(14, 0, 18)
    assert estimate(raw, [rd(-5, fuel=45.0)], [rd(19, fuel=45.0)]).fuel_liters is None


def test_refuel_during_the_trip_is_kept_out_and_gives_unknown():
    """En tankning mitt i resan: nettot går inte att avgöra → okänt, aldrig ett påhittat värde."""
    raw = raw_trip(40, 0, 60)
    est = estimate(raw, [rd(-5, fuel=12.0)], [rd(20, fuel=10.5), rd(35, fuel=55.0), rd(61, fuel=53.0)])
    assert est.fuel_liters is None


def test_battery_level_needs_known_capacity():
    raw = raw_trip(20, 0, 30)
    before, after = [rd(-5, battery=80.0)], [rd(31, battery=60.0)]
    assert estimate(raw, before, after).energy_kwh is None  # kapaciteten är okänd i dag
    assert estimate(raw, before, after, battery_kwh=15.0).energy_kwh == pytest.approx(3.0)


@pytest.mark.parametrize(
    "before,after",
    [
        ([rd(-5, fuel=40.0)], [rd(41, fuel=41.5)]),  # negativ förbrukning
        ([rd(-5, fuel=90.0)], [rd(41, fuel=5.0)]),  # mer än 60 l/100 km
        ([rd(-5, trip=0.0, fuel_avg=0.0)], [rd(41, trip=40.0, fuel_avg=95.0)]),  # orimligt snitt
        ([rd(-5, trip=100.0, fuel_avg=9.0)], [rd(41, trip=140.0, fuel_avg=5.0)]),  # negativt ur snitten
    ],
)
def test_implausible_values_are_discarded(before, after):
    assert estimate(raw_trip(40, 0, 40), before, after).fuel_liters is None


def test_more_than_the_tank_holds_is_discarded():
    raw = raw_trip(400, 0, 300)
    est = estimate(raw, [rd(-5, fuel=60.0)], [rd(301, fuel=0.0)], tank_l=50.0)
    assert est.fuel_liters is None


def test_readings_before_arrival_do_not_count_as_the_arrival():
    raw = raw_trip(30, 0, 35)
    est = estimate(raw, [rd(-5, trip=0.0, fuel_avg=0.0)], [rd(20, trip=15.0, fuel_avg=5.0)])
    assert est.fuel_liters is None


# ----------------------------------------------------------------------------- i databasen


def observation(db, vehicle, at: datetime, **fields) -> M.VehicleObservation:
    obs = M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=at, last_seen_at=fields.pop("seen", at),
                               content_hash=f"h-{at.isoformat()}"[:64], source="volvo_api", **fields)
    db.add(obs)
    db.flush()
    return obs


def live_trip(db, trips, vehicle, *, km: float = 30.0, start: datetime = DEP, minutes: int = 35,
              source: str = TripSource.VOLVO_API, obs: tuple = ()) -> M.LogicalTrip:
    raw = trips.raw(km=km, start_at=start, minutes=minutes, source=source)
    raw.fuel_liters, raw.fuel_quality = None, ValueQuality.UNKNOWN
    raw.energy_kwh, raw.energy_quality = None, ValueQuality.UNKNOWN
    raw.observation_ids = [str(o.id) for o in obs]
    db.flush()
    return seg.create_logical_for_raw(db, raw)


def test_fill_vehicle_sets_estimated_values_on_the_trip(db, trips, vehicle):
    before = observation(db, vehicle, DEP - timedelta(minutes=5), trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0,
                         fuel_liters=40.0)
    after = observation(db, vehicle, DEP + timedelta(minutes=36), trip_meter_auto_km=30.0, avg_fuel_consumption_auto=5.0,
                        fuel_liters=38.5)
    trip = live_trip(db, trips, vehicle, obs=(before, after))
    assert consumption.fill_vehicle(db, vehicle.id) == 1
    trip = reload_trip(db, trip.id)
    raw = trip.segments[0].raw_trip
    assert (raw.fuel_liters, raw.fuel_quality) == (1.5, ValueQuality.ESTIMATED)
    assert (trip.fuel_liters, trip.fuel_quality) == (1.5, ValueQuality.ESTIMATED)
    assert (trip.energy_kwh, trip.energy_quality) == (None, ValueQuality.UNKNOWN)  # inget elsnitt → "–"
    assert "consumption" not in " ".join(trip.quality_flags or [])  # metoden sparas inte
    assert consumption.fill_vehicle(db, vehicle.id) == 0  # idempotent


def test_existing_values_are_never_overwritten(db, trips, vehicle):
    before = observation(db, vehicle, DEP - timedelta(minutes=5), trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0)
    after = observation(db, vehicle, DEP + timedelta(minutes=36), trip_meter_auto_km=30.0, avg_fuel_consumption_auto=5.0)
    trip = live_trip(db, trips, vehicle, obs=(before, after))
    raw = trip.segments[0].raw_trip
    raw.fuel_liters, raw.fuel_quality = 2.2, ValueQuality.VERIFIED
    db.flush()
    consumption.fill_vehicle(db, vehicle.id)
    db.refresh(raw)
    assert (raw.fuel_liters, raw.fuel_quality) == (2.2, ValueQuality.VERIFIED)


def test_imported_trips_are_never_touched(db, trips, vehicle):
    before = observation(db, vehicle, DEP - timedelta(minutes=5), trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0)
    after = observation(db, vehicle, DEP + timedelta(minutes=36), trip_meter_auto_km=30.0, avg_fuel_consumption_auto=5.0)
    trip = live_trip(db, trips, vehicle, obs=(before, after), source=TripSource.VOLVO_IMPORT)
    assert consumption.fill_vehicle(db, vehicle.id) == 0
    assert trip.segments[0].raw_trip.fuel_quality == ValueQuality.UNKNOWN


def test_old_trips_and_closed_months_are_left_alone(db, user, trips, vehicle):
    before = observation(db, vehicle, DEP - timedelta(minutes=5), trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0)
    after = observation(db, vehicle, DEP + timedelta(minutes=36), trip_meter_auto_km=30.0, avg_fuel_consumption_auto=5.0)
    trip = live_trip(db, trips, vehicle, obs=(before, after))
    raw = trip.segments[0].raw_trip
    db.add(M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-11", state=MonthCloseState.EXPORTED))
    db.flush()
    assert consumption.fill_vehicle(db, vehicle.id) == 0  # underlaget för november finns redan
    db.execute(M.MonthClose.__table__.delete())
    raw.created_at = datetime.now(UTC) - timedelta(days=30)
    db.flush()
    assert consumption.fill_vehicle(db, vehicle.id) == 0  # skapad för länge sedan
    raw.created_at = datetime.now(UTC)
    db.flush()
    assert consumption.fill_vehicle(db, vehicle.id) == 1


def test_pipeline_fills_consumption_after_new_trips(db, user, trips, vehicle):
    before = observation(db, vehicle, DEP - timedelta(minutes=5), fuel_liters=40.0)
    after = observation(db, vehicle, DEP + timedelta(minutes=36), fuel_liters=38.2)
    trip = live_trip(db, trips, vehicle, obs=(before, after))
    out = pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=SYSTEM, notify=False)
    assert out["consumption_estimated"] == 1
    trip = reload_trip(db, trip.id)
    assert (trip.fuel_liters, trip.fuel_quality) == (1.8, ValueQuality.ESTIMATED)


def test_demo_car_trip_gets_estimated_fuel_all_the_way(db, user):
    """Hela vägen: demobilens hämtningar → resan byggs → förbrukningen räknas fram och märks."""
    demo_car = demo.enable(db, user.id, now=T0)
    result = demo.simulate_trip(db, user.id, "commute", now=T0 + timedelta(hours=2))
    assert result["new_trip_ids"]
    raws = list(db.execute(select(M.RawTrip).where(M.RawTrip.vehicle_id == demo_car.id)).scalars())
    assert raws
    for raw in raws:
        assert raw.fuel_quality == ValueQuality.ESTIMATED
        # Demobilens snitt är 5,4 l/100 km och trippmätaren startar på noll.
        assert raw.fuel_liters == pytest.approx(raw.distance_km * 5.4 / 100, abs=0.01)
        assert raw.energy_quality == ValueQuality.UNKNOWN  # inget elsnitt och okänd batterikapacitet
