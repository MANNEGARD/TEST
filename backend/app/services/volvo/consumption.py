"""Bensin- och elförbrukning per resa ur bilens egna avläsningar (DEL 7, 1.3.0).

Volvos API ger ingen förbrukning per resa, men observationerna sparar bilens trippstatistik
(den automatiska trippmätaren ``trip_meter_auto_km`` och snittförbrukningen för SAMMA period,
``avg_fuel_consumption_auto`` l/100 km resp. ``avg_energy_consumption_auto`` kWh/100 km ur
averageFuelConsumptionAutomatic/averageEnergyConsumptionAutomatic) och nivåerna i tanken och
batteriet. Snitten utan "Automatic" hör till förarens manuella trippmätare och används aldrig
här. Förbrukningen räknas fram så här, i ordning:

1. **Trippmätare × snitt.** Avläsningen före och efter resan, trippmätaren har inte nollställts
   emellan och har ökat ungefär lika mycket som resan är lång (±max(2 km, 15 %)): förbrukningen
   är snittets förändring viktad med sträckan, ``(snitt_efter × tripp_efter − snitt_före ×
   tripp_före) / 100``. Det stämmer även när flera resor delar samma trippmätarperiod. Snitten
   är avrundade (0,1): felet blir upp till ``0,05 × (tripp_före + tripp_efter) / 100``. Är det
   mer än 25 % av värdet avstår metoden (nästa metod prövas).
1b. **Nollställd trippmätare** (tripp_efter < tripp_före, t.ex. efter ca 4 h stillestånd) och
   tripp_efter ≈ resans sträcka: resan började på en nollställd mätare, ``snitt × tripp / 100``.
2. **Nivåskillnad** (bara bränsle; batteriet bara när den användbara kapaciteten är känd, vilket
   den inte är i dag): nettot i tanken före och efter. En ökning på mer än 1 l mellan två
   avläsningar under resan är en tankning och då går nettot inte att avgöra; en oförändrad nivå
   ger inget värde (upplösningen eller eldrift), aldrig 0 l. Finns ingen avläsning under resan
   (bara läget före och efter) syns inte en tankning alls: då krävs minst 5 l/100 km, annars
   lämnas värdet okänt.

Orimliga värden (negativa, mer än tanken rymmer, mer än 60 l eller 60 kWh per 100 km) kastas.
Värdet märks ``estimated`` ("Uppskattad ur bilens egna avläsningar"); räcker underlaget inte
lämnas det okänt och webben visar "–". Hur värdet räknades fram sparas inte. Ett värde som redan
finns (``verified``/``estimated``) skrivs aldrig över, och importerade resor rörs inte.

Starttid ur snittfarten (K1) är fortfarande avstängd, se ``trips.start_time``.
"""

from __future__ import annotations

import bisect
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, MonthClose, RawTrip, TripSegment, Vehicle, VehicleObservation
from app.models.enums import LogicalState, MonthCloseState, TripSource, ValueQuality

LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
NEAR = timedelta(hours=2)  # närmaste avläsning före start och efter slut
CLOCK_SKEW = timedelta(minutes=2)
LOOKBACK = timedelta(days=14)  # resor som DriveLog har skapat de senaste 14 dygnen
MIN_TOLERANCE_KM = 2.0
TOLERANCE_SHARE = 0.15
MAX_PER_100 = 60.0  # l/100 km och kWh/100 km
MAX_TANK_L = 100.0
REFUEL_STEP_L = 1.0
NO_TRIP_KM = 0.05
AVG_RESOLUTION = 0.1  # bilens snitt har en decimal
MAX_ROUNDING_SHARE = 0.25  # avrundningsfelet får vara högst en fjärdedel av värdet
MIN_LEVEL_PER_100 = 5.0  # l/100 km när bara läget före och efter finns (en tankning syns inte)


@dataclass(frozen=True)
class Reading:
    """De fält ur en observation som behövs (laddas utan rådata)."""

    id: str
    fetched_at: datetime
    last_seen_at: datetime
    fuel_liters: float | None = None
    battery_percent: float | None = None
    avg_fuel_consumption_auto: float | None = None
    avg_energy_consumption_auto: float | None = None
    trip_meter_auto_km: float | None = None


@dataclass(frozen=True)
class Estimate:
    fuel_liters: float | None = None
    energy_kwh: float | None = None
    fuel_method: str | None = None  # bara för tester och felsökning, sparas inte
    energy_method: str | None = None


def reading(obs: VehicleObservation | Reading) -> Reading:
    if isinstance(obs, Reading):
        return obs
    return Reading(
        id=str(obs.id), fetched_at=obs.fetched_at, last_seen_at=obs.last_seen_at or obs.fetched_at,
        fuel_liters=obs.fuel_liters, battery_percent=obs.battery_percent,
        avg_fuel_consumption_auto=obs.avg_fuel_consumption_auto,
        avg_energy_consumption_auto=obs.avg_energy_consumption_auto, trip_meter_auto_km=obs.trip_meter_auto_km,
    )


def _tolerance(distance_km: float) -> float:
    return max(MIN_TOLERANCE_KM, TOLERANCE_SHARE * distance_km)


def _plausible(value: float | None, distance_km: float, *, cap: float | None) -> float | None:
    if value is None or distance_km <= 0 or value < 0:
        return None
    if cap is not None and value > cap:
        return None
    if value / distance_km * 100 > MAX_PER_100:
        return None
    return round(value, 2)


def _precise(value: float, *trip_kms: float) -> float | None:
    """Värdet, eller None när snittens avrundning kan flytta det mer än en fjärdedel."""
    margin = sum(AVG_RESOLUTION / 2 * km / 100 for km in trip_kms)
    if value <= 0 or margin > MAX_ROUNDING_SHARE * value:
        return None
    return value


def _by_trip_meter(before: Reading, after: Reading, distance_km: float, field: str) -> tuple[float | None, str | None]:
    tb, ta = before.trip_meter_auto_km, after.trip_meter_auto_km
    avg_a = getattr(after, field)
    if tb is None or ta is None or avg_a is None:
        return None, None
    tol = _tolerance(distance_km)
    if ta >= tb:
        if abs((ta - tb) - distance_km) > tol:
            return None, None
        if tb <= NO_TRIP_KM:
            value = avg_a * ta / 100
            return (value, "trip_meter") if value <= 0 or _precise(value, ta) is not None else (None, None)
        avg_b = getattr(before, field)
        if avg_b is None:
            return None, None
        value = (avg_a * ta - avg_b * tb) / 100
        if value > 0 and _precise(value, ta, tb) is None:
            return None, None
        return value, "trip_meter"
    if abs(ta - distance_km) <= tol:
        value = avg_a * ta / 100
        return (value, "trip_meter_reset") if value <= 0 or _precise(value, ta) is not None else (None, None)
    return None, None


def _by_level(series: list[float], scale: float) -> float | None:
    """Nettot i nivån (före − efter); None när nivån ökat mellan två avläsningar (tankning/laddning)."""
    if len(series) < 2:
        return None
    for prev, nxt in zip(series, series[1:], strict=False):
        if nxt - prev > REFUEL_STEP_L * scale:
            return None
    return series[0] - series[-1]


def estimate_for_raw(
    raw: RawTrip, before: list[Any], after: list[Any], *, battery_kwh: float | None, tank_l: float | None
) -> Estimate:
    """Förbrukningen för en resa ur avläsningarna (ren funktion).

    ``before``: avläsningar fram till resans start (äldst först, den sista är läget vid avfärd).
    ``after``: avläsningar under resan och efter den (äldst först); läget vid ankomst är den
    första som hämtades vid eller efter resans slut.
    """
    distance = float(raw.distance_km or 0)
    if distance <= 0 or raw.ended_at is None:
        return Estimate()
    pre = [reading(o) for o in before]
    post = [reading(o) for o in after]
    arrived = raw.ended_at - CLOCK_SKEW

    def last_with(items: list[Reading], *fields: str) -> Reading | None:
        for r in reversed(items):
            if all(getattr(r, f) is not None for f in fields):
                return r
        return None

    def first_after_with(*fields: str) -> Reading | None:
        for r in post:
            if r.fetched_at >= arrived and all(getattr(r, f) is not None for f in fields):
                return r
        return None

    cap = min(tank_l, MAX_TANK_L) if tank_l else MAX_TANK_L
    fuel = energy = None
    fuel_method = energy_method = None

    b, a = last_with(pre, "trip_meter_auto_km"), first_after_with("trip_meter_auto_km")
    if b is not None and a is not None:
        value, method = _by_trip_meter(b, a, distance, "avg_fuel_consumption_auto")
        fuel = _plausible(value, distance, cap=cap)
        fuel_method = method if fuel is not None else None
        value, method = _by_trip_meter(b, a, distance, "avg_energy_consumption_auto")
        energy = _plausible(value, distance, cap=None)
        energy_method = method if energy is not None else None

    if fuel is None:
        fb, fa = last_with(pre, "fuel_liters"), first_after_with("fuel_liters")
        if fb is not None and fa is not None:
            series = [fb.fuel_liters] + [
                r.fuel_liters for r in post if r.fuel_liters is not None and r.fetched_at <= fa.fetched_at
            ]
            values = [float(v) for v in series if v is not None]
            net = _by_level(values, 1.0)
            if net is not None and len(values) < 3 and net / distance * 100 < MIN_LEVEL_PER_100:
                net = None  # bara före och efter: en tankning under resan syns inte, för lite att lita på
            # Oförändrad nivå säger inget (avläsningens upplösning, eller eldrift): okänt, inte 0 l.
            fuel = _plausible(net, distance, cap=cap) if net is not None and net > 0 else None
            fuel_method = "level" if fuel is not None else None

    if energy is None and battery_kwh:
        bb, ba = last_with(pre, "battery_percent"), first_after_with("battery_percent")
        if bb is not None and ba is not None:
            series = [bb.battery_percent] + [
                r.battery_percent for r in post if r.battery_percent is not None and r.fetched_at <= ba.fetched_at
            ]
            # En procentenhet motsvarar battery_kwh/100 kWh; en laddning syns som en ökning.
            net = _by_level([float(v) for v in series if v is not None], 100.0 / battery_kwh)
            energy = _plausible(net / 100 * battery_kwh if net is not None else None, distance, cap=battery_kwh)
            energy_method = "level" if energy is not None else None

    return Estimate(fuel_liters=fuel, energy_kwh=energy, fuel_method=fuel_method, energy_method=energy_method)


# ----------------------------------------------------------------------------- databasen


def _candidates(db: Session, vehicle_id: uuid.UUID, now: datetime) -> list[RawTrip]:
    rows = db.execute(
        select(RawTrip).where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.source.in_(LIVE_SOURCES),
            RawTrip.import_batch_id.is_(None),
            RawTrip.voided_at.is_(None),
            RawTrip.ended_at.is_not(None),
            RawTrip.distance_km > 0,
            RawTrip.created_at >= now - LOOKBACK,
            (RawTrip.fuel_quality == ValueQuality.UNKNOWN) | (RawTrip.energy_quality == ValueQuality.UNKNOWN),
        ).order_by(RawTrip.ended_at)
    ).scalars()
    return [
        r for r in rows
        if r.observation_ids
        and ((r.fuel_quality == ValueQuality.UNKNOWN and r.fuel_liters is None)
             or (r.energy_quality == ValueQuality.UNKNOWN and r.energy_kwh is None))
    ]


def _readings(db: Session, vehicle_id: uuid.UUID, start: datetime, end: datetime) -> list[Reading]:
    cols = (
        VehicleObservation.id, VehicleObservation.fetched_at, VehicleObservation.last_seen_at,
        VehicleObservation.fuel_liters, VehicleObservation.battery_percent,
        VehicleObservation.avg_fuel_consumption_auto, VehicleObservation.avg_energy_consumption_auto,
        VehicleObservation.trip_meter_auto_km,
    )
    out: list[Reading] = []
    for row in db.execute(
        select(*cols)
        .where(
            VehicleObservation.vehicle_id == vehicle_id,
            VehicleObservation.fetched_at <= end,
            VehicleObservation.last_seen_at >= start,
        )
        .order_by(VehicleObservation.fetched_at)
    ):
        out.append(Reading(
            id=str(row[0]), fetched_at=row[1], last_seen_at=row[2] or row[1], fuel_liters=row[3],
            battery_percent=row[4], avg_fuel_consumption_auto=row[5], avg_energy_consumption_auto=row[6],
            trip_meter_auto_km=row[7],
        ))
    return out


def _split(raw: RawTrip, readings: list[Reading]) -> tuple[list[Reading], list[Reading]]:
    """Avläsningarna före resan (till och med läget vid avfärd) och under/efter den (≤ 2 h)."""
    by_id = {r.id: r for r in readings}
    own = [by_id[i] for i in (raw.observation_ids or []) if i in by_id]
    anchor = own[0] if own else None
    assert raw.ended_at is not None
    start_ref = anchor.last_seen_at if anchor is not None else (raw.started_at or raw.start_latest or raw.ended_at)
    cutoff = anchor.fetched_at if anchor is not None else start_ref
    times = [r.fetched_at for r in readings]
    split_at = bisect.bisect_right(times, cutoff)
    before = [r for r in readings[:split_at] if r.last_seen_at >= start_ref - NEAR]
    after = [r for r in readings[split_at:] if r.fetched_at <= raw.ended_at + NEAR]
    return before, after


def _closed_months(db: Session, vehicle: Vehicle) -> tuple[set[str], Any]:
    months = set(db.execute(select(MonthClose.month).where(
        MonthClose.vehicle_id == vehicle.id,
        MonthClose.state.in_((MonthCloseState.EXPORTED, MonthCloseState.STALE)),
    )).scalars())
    if not months:
        return months, None
    from app.services import settings as settings_svc

    return months, settings_svc.user_timezone(db, vehicle.owner_user_id)


def fill_vehicle(db: Session, vehicle_id: uuid.UUID, *, now: datetime | None = None) -> int:
    """Fyller i förbrukningen för fordonets nya resor från bilen. Returnerar antalet delresor.

    Idempotent. En resa i en månad vars underlag redan har skapats lämnas orörd (samma regel
    som starttiden ur sträckan): ett underlag som kanske redan är inlämnat ändras inte.
    """
    now = now or utcnow()
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return 0
    raws = _candidates(db, vehicle_id, now)
    if not raws:
        return 0
    closed, tz = _closed_months(db, vehicle)
    if tz is not None:
        raws = [r for r in raws if r.sort_time is None or r.sort_time.astimezone(tz).strftime("%Y-%m") not in closed]
        if not raws:
            return 0
    starts = [r.started_at or r.start_earliest or r.start_latest or r.ended_at for r in raws]
    window_start = min(s for s in starts if s is not None) - NEAR - timedelta(days=1)
    window_end = max(r.ended_at for r in raws if r.ended_at is not None) + NEAR
    readings = _readings(db, vehicle_id, window_start, window_end)
    if not readings:
        return 0

    changed: list[uuid.UUID] = []
    for raw in raws:
        before, after = _split(raw, readings)
        est = estimate_for_raw(raw, before, after, battery_kwh=None, tank_l=None)
        touched = False
        if est.fuel_liters is not None and raw.fuel_quality == ValueQuality.UNKNOWN and raw.fuel_liters is None:
            raw.fuel_liters, raw.fuel_quality = est.fuel_liters, ValueQuality.ESTIMATED
            touched = True
        if est.energy_kwh is not None and raw.energy_quality == ValueQuality.UNKNOWN and raw.energy_kwh is None:
            raw.energy_kwh, raw.energy_quality = est.energy_kwh, ValueQuality.ESTIMATED
            touched = True
        if touched:
            changed.append(raw.id)
    if not changed:
        return 0
    db.flush()
    from app.services.trips import segments as seg

    trip_ids = set(db.execute(
        select(TripSegment.logical_trip_id)
        .join(LogicalTrip, LogicalTrip.id == TripSegment.logical_trip_id)
        .where(TripSegment.raw_trip_id.in_(changed), LogicalTrip.state == LogicalState.ACTIVE)
    ).scalars())
    for trip_id in trip_ids:
        trip = seg.load_trip(db, trip_id)
        if trip is not None:
            seg.refresh_logical(db, trip)
    db.flush()
    return len(changed)
