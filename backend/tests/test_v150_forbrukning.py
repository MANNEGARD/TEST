"""1.5.0: bensin och el ur bilens egna nivåer, så precist som underlaget tillåter (beställning 3).

Ägarens bil rapporterar mätarställning, position och nivåer bara när tändningen slås av.
Testerna spelar upp ägarens verkliga förlopp (scratchpad-tidslinjen: tolv avläsningar,
minuter relativt sista ankomsten) med PÅHITTADE koordinater som ger samma avstånd
(A–B 7 km, A–C 144 km, B–C 140 km). Inga riktiga adresser, VIN eller registreringsnummer.

* 194-kilometersresan: 25 → 13 l = 12 l ± 1 l; batteriet 100 → 5 % = 95 procentenheter ×
  kapaciteten ± 1 procentenhet. Årsmodell 2021 har inget belagt standardvärde → elen är
  okänd tills ägaren fyller i kapaciteten, och fylls då i efterhand.
* De två korta resorna (8 km): tanken 26 → 25 l ger 1 l ± 1 l (laddhybrid: ingen golvregel),
  25 → 25 l ger inget värde (aldrig 0 l); batteriet 100 → 100 ger inget värde, 100 → 81 ger
  19 procentenheter × kapaciteten.
* Metodvalet (minst felgräns vinner, nivåskillnaden när två säger emot varandra), decimaler,
  tankning och laddning före, under och mellan resor, spärrarna, efterhandsfallet, en bil som
  rapporterar under färd (samma värden som i de16760) och migreringen upp och ned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select

import app.models as M
from alembic import command
from app.models.enums import MonthCloseState, TripSource, ValueQuality
from app.services.audit import SYSTEM
from app.services.trips import pipeline
from app.services.trips import segments as seg
from app.services.volvo import consumption, detection
from app.services.volvo.consumption import Reading
from tests.conftest import reload_trip
from tests.test_enprocess_sqlite import alembic_config

ARRIVAL = datetime(2026, 11, 17, 7, 16, tzinfo=UTC)  # sista ankomsten (minut 0 i tidslinjen)
# Påhittade platser med samma fågelvägsavstånd som i ägarens förlopp.
A = (59.0, 15.0)
B = (59.06295, 15.0)  # 7 km från A
C = (59.7428, 12.9178)  # 144 km från A, 140 km från B
PLACES = {"A": A, "B": B, "C": C}
ODO0 = 40_000.0

# Ur tidslinjen: (hämtad, senast sedd, sedd, motor igång, motor rapp., mätarökning, mätare rapp.,
# plats, position rapp., tripp auto, bränsle, bränsle rapp., batteri, batteri rapp., elräckvidd,
# bensinräckvidd, laddstatus, bearbetad)
TIMELINE = [
    (-1355.3, -1203.1, 31, False, -2156.5, 0.0, -2156.5, "A", -2156.6, 0.1, 26.0, -2156.5, 100.0, -2156.5, 36, 310, "IDLE", True),
    (-1198.1, -1188.1, 6, True, -1198.5, 0.0, -2156.5, "A", -2156.6, 0.1, 26.0, -2156.5, 100.0, -1198.5, 35, 310, "IDLE", True),
    (-1186.1, -1186.1, 1, False, -1186.3, 8.0, -1186.3, "B", -1186.2, 8.5, 25.0, -1186.3, 100.0, -1186.3, 28, 290, "IDLE", True),
    (-1184.1, -1178.1, 4, False, -1185.8, 8.0, -1185.8, "B", -1186.2, 8.5, 25.0, -1185.8, 100.0, -1185.8, 28, 290, "IDLE", True),
    (-1176.1, -1170.1, 4, True, -1177.0, 8.0, -1185.8, "B", -1186.2, 8.5, 25.0, -1185.8, 100.0, -1185.8, 28, 290, "IDLE", True),
    (-1168.1, -1150.1, 10, False, -1169.1, 16.0, -1169.1, "A", -1169.6, 15.9, 25.0, -1169.1, 81.0, -1169.1, 23, 290, "CHARGING", True),
    (-1148.1, -1094.1, 28, False, -1169.1, 16.0, -1169.1, "A", -1169.6, 15.9, 25.0, -1169.1, 89.0, -1148.6, 35, 290, "CHARGING", True),
    (-1092.1, -486.0, 303, False, -1169.1, 16.0, -1169.1, "A", -1169.6, 15.9, 25.0, -1169.1, 100.0, -1093.8, 35, 290, "DONE", True),
    (-483.2, -124.4, 180, False, -1169.1, 16.0, -1169.1, "A", -1169.6, 15.9, 25.0, -1169.1, 100.0, -1093.8, 35, 290, "DONE", True),
    (-122.4, -122.4, 1, False, -1169.1, 16.0, -1169.1, "A", -1169.6, 15.9, 25.0, -1169.1, 100.0, -122.9, 32, 290, "IDLE", True),
    (-120.4, -0.3, 61, True, -120.6, 16.0, -1169.1, "A", -1169.6, 15.9, 25.0, -1169.1, 100.0, -122.9, 32, 290, "IDLE", True),
    (1.7, 1.7, 1, False, 0.5, 210.0, 0.5, "C", 0.0, 194.2, 13.0, 0.5, 5.0, 0.5, 0, 200, "IDLE", False),
]


def m(minutes: float) -> datetime:
    return ARRIVAL + timedelta(minutes=minutes)


def owner_car(db, vehicle) -> list[M.VehicleObservation]:
    """Ägarens bil med de tolv avläsningarna; alla obearbetade utom den första."""
    vehicle.make, vehicle.model, vehicle.model_year, vehicle.fuel_type = "Volvo", "V90", 2021, "PETROL/ELECTRIC"
    out = []
    for i, row in enumerate(TIMELINE):
        (fetched, seen, count, engine, engine_at, odo, odo_at, place, pos_at, trip, fuel, fuel_at, batt, batt_at,
         erange, frange, charging, _processed) = row
        lat, lon = PLACES[place]
        obs = M.VehicleObservation(
            vehicle_id=vehicle.id, source="volvo_api", fetched_at=m(fetched), last_seen_at=m(seen), seen_count=count,
            engine_running=engine, engine_reported_at=m(engine_at), odometer_km=ODO0 + odo,
            odometer_reported_at=m(odo_at), latitude=lat, longitude=lon, position_reported_at=m(pos_at),
            statistics_reported_at=m(odo_at), trip_meter_auto_km=trip, avg_speed=76.0, fuel_liters=fuel,
            fuel_reported_at=m(fuel_at), battery_percent=batt, battery_reported_at=m(batt_at),
            electric_range_km=erange, fuel_range_km=frange, charging_status=charging,
            content_hash=f"tidslinje-{i}", processed_at=m(fetched) if i == 0 else None,
        )
        db.add(obs)
        out.append(obs)
    db.flush()
    return out


def replay(db, user, vehicle) -> list[M.RawTrip]:
    """Resedetekteringen som i 1.4.1: resorna skapas utan förbrukning (ingen efterbearbetning)."""
    obs = owner_car(db, vehicle)
    obs[-1].last_seen_at, obs[-1].seen_count = m(3.7), 2  # ankomsten sedd två gånger, som i 1.4.1
    db.flush()
    detection.process_vehicle(db, vehicle, user_id=user.id, now=m(4.0), run_pipeline=False,
                              settings={"poll_interval_seconds": 120})
    raws = list(db.execute(select(M.RawTrip).where(M.RawTrip.vehicle_id == vehicle.id)
                           .order_by(M.RawTrip.odometer_start)).scalars())
    return raws


def by_km(raws: list[M.RawTrip]) -> dict[str, M.RawTrip]:
    long = next(r for r in raws if r.distance_km > 100)
    short = sorted((r for r in raws if r.distance_km < 20), key=lambda r: r.odometer_start or 0)
    return {"long": long, "out": short[0], "back": short[1]}


# ----------------------------------------------------------------------------- ägarens förlopp


def test_owner_timeline_gives_12_litres_and_waits_for_the_capacity(db, user, vehicle):
    raws = replay(db, user, vehicle)
    trips = by_km(raws)
    long = trips["long"]
    assert long.distance_km == pytest.approx(194.0)
    # Som 1.4.1 lämnade den: inga värden.
    assert (long.fuel_liters, long.fuel_quality, long.energy_kwh) == (None, ValueQuality.UNKNOWN, None)

    assert consumption.fill_vehicle(db, vehicle.id) >= 2
    db.refresh(long)
    assert (long.fuel_liters, long.fuel_quality) == (12.0, ValueQuality.ESTIMATED)
    basis = long.fuel_basis
    assert basis["method"] == "level" and basis["margin"] == 1.0 and basis["resolution"] == 1.0
    assert (basis["before"]["value"], basis["after"]["value"]) == (25.0, 13.0)
    # Nivån före rapporterades vid förra parkeringen, drygt 19 timmar före ankomsten.
    assert basis["before"]["at"] == m(-1169.1).isoformat()
    text = consumption.basis_out(basis)["text"]
    assert "12 l ± 1 l" in text and "hela liter" in text
    # Årsmodell 2021: användbar kapacitet inte belagd (11,6 eller 18,8 kWh-batteriet) → elen okänd.
    assert consumption.suggested_battery_kwh(vehicle) is None
    assert (long.energy_kwh, long.energy_quality) == (None, ValueQuality.UNKNOWN)

    # Ägaren fyller i kapaciteten: elen fylls i efterhand.
    vehicle.usable_battery_kwh = 9.1
    db.flush()
    assert consumption.refill_after_capacity_change(db, vehicle.id) == 2  # 194 km och 8 km hem fylls i
    db.refresh(long)
    assert long.energy_quality == ValueQuality.ESTIMATED
    assert long.energy_kwh == pytest.approx(95 * 9.1 / 100, abs=0.01)
    e = long.energy_basis
    assert e["method"] == "battery_level" and e["net"] is True and e["capacity_source"] == "owner"
    assert e["margin"] == pytest.approx(0.091) and e["percent_points"] == 95.0
    assert (e["before"]["value"], e["after"]["value"]) == (100.0, 5.0)
    assert "Netto ur batteriet" in consumption.basis_out(e)["text"]


def test_the_two_short_trips_in_the_timeline(db, user, vehicle):
    """8 km bort: 26 → 25 l = 1 l ± 1 l (en laddhybrid kan dra lite bensin; ingen golvregel),
    batteriet 100 → 100 = inget värde. 8 km hem: 25 → 25 l = inget värde (aldrig 0 l), batteriet
    100 → 81 = 19 procentenheter (laddningen startade efter ankomsten: nivån rapporterades när
    tändningen slogs av, samma minut som mätarställningen)."""
    trips = by_km(replay(db, user, vehicle))
    vehicle.usable_battery_kwh = 9.1
    db.flush()
    consumption.fill_vehicle(db, vehicle.id)
    out, back = trips["out"], trips["back"]
    db.refresh(out)
    db.refresh(back)
    assert (out.fuel_liters, out.fuel_basis["margin"]) == (1.0, 1.0)
    assert (out.energy_kwh, out.energy_quality) == (None, ValueQuality.UNKNOWN)
    assert (back.fuel_liters, back.fuel_quality) == (None, ValueQuality.UNKNOWN)
    assert back.energy_kwh == pytest.approx(19 * 9.1 / 100, abs=0.01)


def test_owner_trip_from_141_gets_fuel_and_energy_at_the_first_processing(db, user, vehicle):
    """Efterhandsfallet: resan skapad av 1.4.1 utan värden och utan starttid får bensin (och el,
    när kapaciteten finns) vid första efterbearbetningen efter uppdateringen."""
    trips = by_km(replay(db, user, vehicle))
    long = trips["long"]
    long.started_at = None  # som 1.4.1: okänd starttid, bara gränser
    vehicle.usable_battery_kwh = 9.1
    db.flush()
    out = pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=SYSTEM, notify=False)
    assert out["consumption_estimated"] >= 1
    db.refresh(long)
    assert (long.fuel_liters, long.fuel_quality) == (12.0, ValueQuality.ESTIMATED)
    assert long.energy_kwh == pytest.approx(8.645, abs=0.01)
    trip_id = db.execute(select(M.TripSegment.logical_trip_id).where(M.TripSegment.raw_trip_id == long.id)).scalar_one()
    trip = reload_trip(db, trip_id)
    assert (trip.fuel_liters, trip.fuel_quality) == (12.0, ValueQuality.ESTIMATED)


def test_unknown_start_time_uses_the_bounds_and_the_odometer(db, user, vehicle):
    trips = by_km(replay(db, user, vehicle))
    long = trips["long"]
    long.started_at, long.start_latest = None, None  # inte ens en senast möjlig start
    db.flush()
    consumption.fill_vehicle(db, vehicle.id)
    db.refresh(long)
    assert long.fuel_liters == 12.0


# ----------------------------------------------------------------------------- rena beräkningar

DEP = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def rd(minutes: float, *, seen: float | None = None, **fields) -> Reading:
    at = DEP + timedelta(minutes=minutes)
    return Reading(id=f"o{minutes}", fetched_at=at, last_seen_at=DEP + timedelta(minutes=seen) if seen is not None else at,
                   **fields)


def raw_trip(km: float, start_min: float | None, end_min: float, *, odo: float | None = None) -> M.RawTrip:
    return M.RawTrip(distance_km=km, started_at=DEP + timedelta(minutes=start_min) if start_min is not None else None,
                     ended_at=DEP + timedelta(minutes=end_min), odometer_start=odo)


def est(raw, before, after, **kw):
    return consumption.estimate_for_raw(raw, before, after, battery_kwh=kw.pop("battery_kwh", None),
                                        tank_l=kw.pop("tank_l", None), **kw)


def test_the_method_with_the_smallest_margin_wins():
    """Kort manuell trippmätare: felgränsen 0,05 × (10 + 50) / 100 = 0,03 l slår nivåns ±1 l."""
    raw = raw_trip(40, 0, 40)
    before = [rd(-5, fuel_liters=40.0, trip_meter_manual_km=10.0, avg_fuel_consumption=6.0)]
    after = [rd(41, fuel_liters=37.0, trip_meter_manual_km=50.0, avg_fuel_consumption=6.3)]
    e = est(raw, before, after)
    assert e.fuel_method == "trip_meter_manual" and e.fuel_liters == pytest.approx(2.55)
    assert e.fuel_basis["margin"] == pytest.approx(0.03)
    assert [a["method"] for a in e.fuel_basis["alternatives"]] == ["level"]
    # Lång manuell trippmätare (avrundningen växer med sträckan): nivån har mindre felgräns.
    before = [rd(-5, fuel_liters=40.0, trip_meter_manual_km=1500.0, avg_fuel_consumption=6.0)]
    after = [rd(41, fuel_liters=37.0, trip_meter_manual_km=1540.0, avg_fuel_consumption=6.0)]
    e = est(raw, before, after)
    assert e.fuel_method == "level" and (e.fuel_liters, e.fuel_basis["margin"]) == (3.0, 1.0)


def test_two_methods_that_disagree_keep_the_smallest_margin_and_note_the_level():
    """Som i de16760: färddatorns värde behålls när tankmätaren avviker; nivån antecknas."""
    raw = raw_trip(40, 0, 40)
    before = [rd(-5, fuel_liters=40.0, trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0)]
    after = [rd(41, fuel_liters=33.0, trip_meter_auto_km=40.0, avg_fuel_consumption_auto=5.0)]  # 2 l mot 7 l
    e = est(raw, before, after)
    assert e.fuel_method == "trip_meter" and e.fuel_liters == 2.0
    assert e.fuel_basis["deviation"] == {"method": "level", "value": 7.0, "margin": 1.0}
    assert "disagree" in e.fuel_basis["notes"]
    assert "utanför felgränserna" in consumption.basis_out(e.fuel_basis)["text"]
    # Granskningens fall: 30 km, snitt 5,0 och tanken 26 → 23 l gav 1,5 l i de16760.
    raw = raw_trip(30, 0, 30)
    before = [rd(-5, fuel_liters=26.0, trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0)]
    after = [rd(31, fuel_liters=23.0, trip_meter_auto_km=30.0, avg_fuel_consumption_auto=5.0)]
    e = est(raw, before, after)
    assert (e.fuel_method, e.fuel_liters) == ("trip_meter", 1.5)


def test_level_after_must_belong_to_the_trip():
    """Uteblir tanknivån vid ankomsten får resan inte nästa resas liter (och inte tvärtom)."""
    # X: 1000 → 1020 utan nivå vid ankomsten; Y: 1020 → 1040 med nivån 34 l.
    x = raw_trip(20, 0, 20, odo=1000.0)
    x.odometer_end = 1020.0
    y = raw_trip(20, 60, 80, odo=1020.0)
    y.odometer_end = 1040.0
    readings = [rd(-5, fuel_liters=40.0, odometer_km=1000.0), rd(21, odometer_km=1020.0),
                rd(81, fuel_liters=34.0, odometer_km=1040.0)]
    assert est(x, readings[:1], readings[1:], hybrid=True).fuel_liters is None
    assert est(y, readings[:2], readings[2:], hybrid=True).fuel_liters is None  # nivån före är från 1000
    # Ankomstraden för X har ny mätarställning men nivån rapporterades före X.
    old = DEP - timedelta(minutes=1)
    readings = [rd(-5, fuel_liters=40.0, odometer_km=1000.0, fuel_reported_at=old),
                rd(21, fuel_liters=40.0, odometer_km=1020.0, fuel_reported_at=old),
                rd(81, fuel_liters=34.0, odometer_km=1040.0, fuel_reported_at=DEP + timedelta(minutes=80.5))]
    assert est(x, readings[:1], readings[1:], hybrid=True).fuel_liters is None
    assert est(y, readings[:2], readings[2:], hybrid=True).fuel_liters is None


def test_level_from_two_readings_says_a_small_refuel_is_hidden():
    raw = raw_trip(50, 0, 60)
    e = est(raw, [rd(-5, fuel_liters=40.0)], [rd(61, fuel_liters=38.0)], hybrid=True)
    assert "En tankning mellan avläsningarna" in consumption.basis_out(e.fuel_basis)["text"]


def test_a_car_with_decimals_uses_them():
    raw = raw_trip(40, 0, 40)
    e = est(raw, [rd(-5, fuel_liters=40.37)], [rd(41, fuel_liters=37.92)])
    assert e.fuel_liters == pytest.approx(2.45) and e.fuel_basis["margin"] == pytest.approx(0.01)
    e = est(raw, [rd(-5, battery_percent=80.5)], [rd(41, battery_percent=60.0)], battery_kwh=14.9)
    assert e.energy_kwh == pytest.approx(20.5 * 0.149, abs=0.005) and e.energy_basis["resolution"] == 0.1


def test_level_before_is_the_last_report_while_parked_regardless_of_age():
    """Bilen rapporterar bara vid avstängning: nivån före är 19 timmar gammal, starttiden okänd."""
    raw = raw_trip(194, None, 0, odo=1016.0)
    raw.start_latest = DEP - timedelta(minutes=89)
    old = [rd(-1168, seen=-1094, fuel_liters=25.0, odometer_km=1016.0, fuel_reported_at=DEP - timedelta(minutes=1169))]
    near = [rd(-120, seen=-0.3, fuel_liters=25.0, odometer_km=1016.0, fuel_reported_at=DEP - timedelta(minutes=1169))]
    after = [rd(1.7, fuel_liters=13.0, odometer_km=1210.0, fuel_reported_at=DEP + timedelta(minutes=0.5))]
    assert est(raw, near, after, earlier=old).fuel_liters == 12.0
    assert est(raw, [], after, earlier=old).fuel_liters == 12.0  # bara den gamla rapporten


def test_driving_between_the_level_report_and_the_trip_gives_unknown():
    raw = raw_trip(30, 0, 30, odo=1050.0)
    e = est(raw, [rd(-60, fuel_liters=40.0, odometer_km=1000.0)], [rd(31, fuel_liters=36.0, odometer_km=1080.0)])
    assert e.fuel_liters is None


@pytest.mark.parametrize("case", ["before", "during", "between"])
def test_refuelling_keeps_the_net_unknown_or_starts_after_it(case):
    raw = raw_trip(40, 0, 40)
    if case == "before":
        # Tankat medan bilen stod, och bilen rapporterade den nya nivån: räknas från den.
        e = est(raw, [rd(-300, fuel_liters=10.0), rd(-60, fuel_liters=55.0)], [rd(41, fuel_liters=52.0)])
        assert e.fuel_liters == 3.0
    elif case == "during":
        e = est(raw, [rd(-5, fuel_liters=12.0)], [rd(20, fuel_liters=10.0), rd(25, fuel_liters=50.0),
                                                  rd(41, fuel_liters=48.0)])
        assert e.fuel_liters is None
    else:
        # Tankat mellan två resor utan ny rapport: nivån efter är högre än före → okänt.
        e = est(raw, [rd(-60, fuel_liters=20.0)], [rd(41, fuel_liters=50.0)])
        assert e.fuel_liters is None


def test_low_net_only_for_a_car_without_battery():
    raw = raw_trip(50, 0, 60)
    before, after = [rd(-5, fuel_liters=40.0)], [rd(61, fuel_liters=38.0)]
    assert est(raw, before, after).fuel_liters is None  # 4 l/100 km, bil utan batteri: okänt
    assert est(raw, before, after, hybrid=True).fuel_liters == 2.0  # laddhybrid: eldrift är vanligt


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ([rd(-60, battery_percent=81.0, charging_status="CHARGING")], [rd(41, battery_percent=60.0)], None),
        ([rd(-60, battery_percent=100.0, charging_status="DONE")], [rd(41, battery_percent=60.0)], 40.0),
        ([rd(-60, battery_percent=80.0)], [rd(20, battery_percent=70.0, charging_status="CHARGING"),
                                           rd(41, battery_percent=60.0)], None),
        ([rd(-60, battery_percent=50.0)], [rd(41, battery_percent=70.0)], None),
        ([rd(-60, battery_percent=50.0)], [rd(41, battery_percent=50.0)], None),
    ],
)
def test_charging_between_and_during_trips(before, after, expected):
    e = est(raw_trip(40, 0, 40), before, after, battery_kwh=10.0)
    assert e.energy_kwh == (pytest.approx(expected / 10) if expected is not None else None)


def test_charging_after_arrival_reported_later_gives_unknown():
    """Nivån vid ankomsten rapporterades först under laddningen: nettot går inte att avgöra."""
    raw = raw_trip(40, 0, 40)
    late = rd(50, battery_percent=70.0, charging_status="CHARGING", battery_reported_at=DEP + timedelta(minutes=49))
    assert est(raw, [rd(-60, battery_percent=80.0)], [late], battery_kwh=10.0).energy_kwh is None


def test_unknown_capacity_gives_no_energy():
    e = est(raw_trip(40, 0, 40), [rd(-60, battery_percent=80.0)], [rd(41, battery_percent=60.0)])
    assert e.energy_kwh is None


def test_standard_capacity_only_where_the_usable_capacity_is_documented(vehicle):
    vehicle.model, vehicle.fuel_type = "V90 Recharge T8", "PETROL/ELECTRIC"
    for year, expected in ((2018, None), (2020, None), (2021, None), (2022, None), (2023, 14.9), (2025, 14.9)):
        vehicle.model_year = year
        assert consumption.suggested_battery_kwh(vehicle) == expected
    vehicle.fuel_type = "PETROL"
    assert consumption.suggested_battery_kwh(vehicle) is None
    vehicle.fuel_type, vehicle.usable_battery_kwh = "PETROL/ELECTRIC", 15.2
    assert consumption.battery_capacity(vehicle) == (15.2, "owner")


def test_basis_from_the_database_is_type_checked():
    assert consumption.basis_out("text") is None
    assert consumption.basis_out({"method": "level", "value": "12", "margin": 1, "unit": "l"}) is None
    out = consumption.basis_out({"method": "level", "value": 12.0, "margin": 1.0, "unit": "l", "before": 3})
    assert out is not None and out["text"].startswith("Nivåskillnad i tanken: 12 l ± 1 l.")


# ----------------------------------------------------------------------------- en bil som rapporterar under färd

# Värden ur de16760 (consumption.estimate_for_raw i 1.4.2, körd på samma indata när testet skrevs).
DURING_DRIVE = [
    # (sträcka, före, efter, fält) → de16760 gav
    ("trip_meter", 1.51, 5.44),
    ("shared_period", 0.3, None),
    ("reset", 0.48, None),
    ("level_series", 2.6, None),
    ("battery", None, 3.0),
]


def _during(kind):
    if kind == "trip_meter":
        return raw_trip(30, 0, 35), [rd(-5, trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0,
                                         avg_energy_consumption_auto=0.0, fuel_liters=40.0)], \
            [rd(36, trip_meter_auto_km=30.2, avg_fuel_consumption_auto=5.0, avg_energy_consumption_auto=18.0,
                fuel_liters=38.0)], None
    if kind == "shared_period":
        return raw_trip(10, 60, 75), [rd(-5, trip_meter_auto_km=0.0, avg_fuel_consumption_auto=0.0),
                                      rd(26, trip_meter_auto_km=20.0, avg_fuel_consumption_auto=6.0)], \
            [rd(76, trip_meter_auto_km=30.0, avg_fuel_consumption_auto=5.0)], None
    if kind == "reset":
        return raw_trip(12, 0, 15), [rd(-300, seen=-1, trip_meter_auto_km=145.0, avg_fuel_consumption_auto=6.1)], \
            [rd(16, trip_meter_auto_km=12.1, avg_fuel_consumption_auto=4.0)], None
    if kind == "level_series":
        return raw_trip(40, 0, 40), [rd(-5, fuel_liters=45.0)], [rd(20, fuel_liters=43.6), rd(41, fuel_liters=42.4)], None
    return raw_trip(20, 0, 30), [rd(-5, battery_percent=80.0)], [rd(10, battery_percent=72.0),
                                                                 rd(31, battery_percent=60.0)], 15.0


@pytest.mark.parametrize("kind,fuel,energy", DURING_DRIVE)
def test_a_car_reporting_while_driving_gets_the_same_values_as_before(kind, fuel, energy):
    raw, before, after, kwh = _during(kind)
    e = est(raw, before, after, battery_kwh=kwh)
    assert e.fuel_liters == (pytest.approx(fuel) if fuel is not None else None)
    assert e.energy_kwh == (pytest.approx(energy) if energy is not None else None)


# ----------------------------------------------------------------------------- i databasen: spärrar och kapacitet


def observation(db, vehicle, at: datetime, **fields) -> M.VehicleObservation:
    obs = M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=at, last_seen_at=fields.pop("seen", at),
                               content_hash=f"h-{at.isoformat()}"[:64], source="volvo_api", **fields)
    db.add(obs)
    db.flush()
    return obs


def live_trip(db, trips, vehicle, *, source: str = TripSource.VOLVO_API, obs: tuple = ()) -> M.LogicalTrip:
    raw = trips.raw(km=30.0, start_at=DEP, minutes=35, source=source)
    raw.fuel_liters, raw.fuel_quality = None, ValueQuality.UNKNOWN
    raw.energy_kwh, raw.energy_quality = None, ValueQuality.UNKNOWN
    raw.observation_ids = [str(o.id) for o in obs]
    db.flush()
    return seg.create_logical_for_raw(db, raw)


def battery_trip(db, trips, vehicle, **kw):
    before = observation(db, vehicle, DEP - timedelta(minutes=5), fuel_liters=40.0, battery_percent=80.0)
    after = observation(db, vehicle, DEP + timedelta(minutes=36), fuel_liters=38.0, battery_percent=50.0)
    return live_trip(db, trips, vehicle, obs=(before, after), **kw)


def test_changed_capacity_recomputes_only_values_from_the_capacity(db, trips, vehicle):
    vehicle.fuel_type, vehicle.usable_battery_kwh = "PETROL/ELECTRIC", 10.0
    trip = battery_trip(db, trips, vehicle)
    assert consumption.fill_vehicle(db, vehicle.id) == 1
    raw = trip.segments[0].raw_trip
    assert raw.energy_kwh == pytest.approx(3.0)
    vehicle.usable_battery_kwh = 12.0
    db.flush()
    assert consumption.refill_after_capacity_change(db, vehicle.id) == 1
    db.refresh(raw)
    assert raw.energy_kwh == pytest.approx(3.6) and raw.energy_basis["capacity_kwh"] == 12.0
    assert reload_trip(db, trip.id).energy_kwh == pytest.approx(3.6)
    assert raw.fuel_liters == 2.0  # bensinen rörs inte
    # Ägarens egna värde rörs aldrig.
    raw.energy_kwh, raw.energy_quality, raw.energy_basis = 4.4, ValueQuality.VERIFIED, None
    vehicle.usable_battery_kwh = 9.0
    db.flush()
    assert consumption.refill_after_capacity_change(db, vehicle.id) == 0
    db.refresh(raw)
    assert (raw.energy_kwh, raw.energy_quality) == (4.4, ValueQuality.VERIFIED)


def test_removed_capacity_makes_the_energy_unknown_again(db, trips, vehicle):
    vehicle.fuel_type, vehicle.usable_battery_kwh = "PETROL/ELECTRIC", 10.0
    trip = battery_trip(db, trips, vehicle)
    consumption.fill_vehicle(db, vehicle.id)
    vehicle.usable_battery_kwh = None
    db.flush()
    assert consumption.refill_after_capacity_change(db, vehicle.id) == 1
    raw = trip.segments[0].raw_trip
    db.refresh(raw)
    assert (raw.energy_kwh, raw.energy_quality, raw.energy_basis) == (None, ValueQuality.UNKNOWN, None)


def test_locks_existing_imported_and_closed_months(db, user, trips, vehicle):
    vehicle.fuel_type, vehicle.usable_battery_kwh = "PETROL/ELECTRIC", 10.0
    trip = battery_trip(db, trips, vehicle)
    raw = trip.segments[0].raw_trip
    raw.fuel_liters, raw.fuel_quality = 2.2, ValueQuality.VERIFIED
    db.flush()
    consumption.fill_vehicle(db, vehicle.id)
    db.refresh(raw)
    assert (raw.fuel_liters, raw.fuel_quality, raw.fuel_basis) == (2.2, ValueQuality.VERIFIED, None)
    assert raw.energy_quality == ValueQuality.ESTIMATED
    # Underlaget för månaden är skapat: kapacitetsändringen rör inte resan.
    db.add(M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-11", state=MonthCloseState.EXPORTED))
    vehicle.usable_battery_kwh = 20.0
    db.flush()
    assert consumption.refill_after_capacity_change(db, vehicle.id) == 0
    db.refresh(raw)
    assert raw.energy_kwh == pytest.approx(3.0)


def test_imported_trips_are_never_touched(db, trips, vehicle):
    vehicle.fuel_type, vehicle.usable_battery_kwh = "PETROL/ELECTRIC", 10.0
    trip = battery_trip(db, trips, vehicle, source=TripSource.VOLVO_IMPORT)
    assert consumption.fill_vehicle(db, vehicle.id) == 0
    assert trip.segments[0].raw_trip.energy_quality == ValueQuality.UNKNOWN


def test_capacity_in_the_api_and_the_basis_in_the_trip_details(client, db, trips, vehicle):
    vehicle.fuel_type = "PETROL/ELECTRIC"
    trip = battery_trip(db, trips, vehicle)
    consumption.fill_vehicle(db, vehicle.id)
    db.commit()
    resp = client.get("/api/v1/vehicles")
    car = next(v for v in resp.json() if v["id"] == str(vehicle.id))
    assert (car["usable_battery_kwh"], car["suggested_battery_kwh"]) == (None, None)
    resp = client.patch(f"/api/v1/vehicles/{vehicle.id}", json={"usable_battery_kwh": 10.0})
    assert resp.status_code == 200 and resp.json()["usable_battery_kwh"] == 10.0
    detail = client.get(f"/api/v1/trips/{trip.id}").json()
    raw = detail["raw_trips"][0]
    assert raw["energy_kwh"] == pytest.approx(3.0) and raw["energy_basis"]["margin"] == pytest.approx(0.1)
    assert raw["fuel_basis"]["text"].startswith("Nivåskillnad i tanken: 2 l ± 1 l.")
    assert client.patch(f"/api/v1/vehicles/{vehicle.id}", json={"usable_battery_kwh": -1}).status_code == 422


def test_capacity_is_saved_even_if_the_recomputation_fails(client, db, trips, vehicle, monkeypatch):
    """Omräkningen är ett delsteg i skyddsnätet: i driften sparas kapaciteten ändå."""
    from app.services import internal_errors

    def boom(*_args, **_kwargs):
        raise RuntimeError("omräkningen föll")

    monkeypatch.setattr(consumption, "refill_after_capacity_change", boom)
    with internal_errors.tolerant_mode():
        resp = client.patch(f"/api/v1/vehicles/{vehicle.id}", json={"usable_battery_kwh": 9.5})
    assert resp.status_code == 200 and resp.json()["usable_battery_kwh"] == 9.5
    db.expire_all()
    assert db.get(M.Vehicle, vehicle.id).usable_battery_kwh == 9.5


# ----------------------------------------------------------------------------- migreringen


def test_migration_0005_up_and_down(tmp_path):
    url = f"sqlite:///{tmp_path}/v150.sqlite3"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    cols = lambda t: {c["name"] for c in inspect(engine).get_columns(t)}  # noqa: E731
    assert {"fuel_basis", "energy_basis"} <= cols("raw_trips") and "usable_battery_kwh" in cols("vehicles")
    command.downgrade(cfg, "0004")
    assert not ({"fuel_basis", "energy_basis"} & cols("raw_trips")) and "usable_battery_kwh" not in cols("vehicles")
    command.upgrade(cfg, "head")
    assert "usable_battery_kwh" in cols("vehicles")
    engine.dispose()


def test_every_named_step_has_a_label_in_the_list_of_internal_errors():
    """Omräkningen efter ändrad batterikapacitet körs som ett eget delsteg; ett fel där ska visas
    med ett namn, inte med den råa koden."""
    import re
    from pathlib import Path

    from app.services import internal_errors

    app_dir = Path(__file__).resolve().parents[1] / "app"
    names = {n for f in app_dir.rglob("*.py")
             for n in re.findall(r'run_step\(\s*db,\s*"([a-z_.]+)"', f.read_text(encoding="utf-8"))}
    assert "vehicle.capacity_refill" in names
    assert names <= set(internal_errors.STEP_LABELS), names - set(internal_errors.STEP_LABELS)
