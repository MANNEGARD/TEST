"""Bensin- och elförbrukning per resa ur bilens egna avläsningar (DEL 7, 1.3.0; 1.5.0).

Volvos API ger ingen förbrukning per resa, men observationerna sparar bilens trippstatistik och
nivåerna i tanken och batteriet. För varje resa räknas varje metod som går att använda fram med
både ett värde och en FELGRÄNS, och den med minst felgräns väljs (1.5.0):

1. **Automatiska trippmätaren × snitt** (``trip_meter_auto_km`` och snittet för SAMMA period,
   ``avg_fuel_consumption_auto`` l/100 km resp. ``avg_energy_consumption_auto`` kWh/100 km ur
   averageFuelConsumptionAutomatic/averageEnergyConsumptionAutomatic). Avläsningen före och
   efter resan, trippmätaren har inte nollställts emellan och har ökat ungefär lika mycket som
   resan är lång (±max(2 km, 15 %)): ``(snitt_efter × tripp_efter − snitt_före × tripp_före) /
   100``. Stämmer även när flera resor delar samma trippmätarperiod. Snitten har en decimal:
   felgränsen är ``0,05 × (tripp_före + tripp_efter) / 100``. Är den mer än 25 % av värdet
   avstår metoden.
1b. **Nollställd trippmätare** (tripp_efter < tripp_före) och tripp_efter ≈ resans sträcka:
   ``snitt × tripp / 100``, felgräns ``0,05 × tripp / 100``.
1c. **Manuella trippmätaren × dess snitt** (``trip_meter_manual_km`` och ``avg_fuel_consumption``
   / ``avg_energy_consumption``, 1.5.0) med samma regler. Snitten utan "Automatic" används bara
   tillsammans med den manuella trippmätaren, aldrig med den automatiska.
2. **Nivåskillnad.** Bilen kan rapportera nivåerna bara när tändningen slås av (ägarens bil gör
   så). Nivån FÖRE resan är därför den senast rapporterade nivån före starten medan bilen stod
   still – oavsett hur gammal rapporten är – och nivån EFTER den som rapporterades vid
   ankomsten. "Före" hittas utan att starttiden behöver vara känd: den sista avläsningen före
   ankomsten vars nivå rapporterades före resans senast möjliga start (``started_at``,
   ``start_latest`` eller ankomsten − sträckan i 130 km/h) och vars mätarställning inte har
   ökat förbi resans start. Felgränsen är upplösningen: bilen rapporterar t.ex. hela liter och
   hela procent, så före och efter är vardera fel med upp till en halv enhet och skillnaden med
   upp till en hel (12 l ± 1 l). Har bilen decimaler används de. DriveLog avrundar aldrig själv.

   * Bensin: nettot i tanken. En ökning på mer än 1 l mellan två rapporter eller totalt är en
     tankning: nettot går inte att avgöra och lämnas okänt. En oförändrad nivå ger inget värde
     (upplösningen eller eldrift), aldrig 0 l.
   * Regeln "minst 5 l/100 km när bara läget före och efter finns" (en tankning under resan syns
     inte) gäller bara bilar utan batteri. En laddhybrid drar ofta mycket lite bensin (eldrift),
     och en dold tankning ger ett lågt netto bara om den var mindre än resans förbrukning –
     tankningar är nästan alltid större, och då blir nettot negativt och lämnas okänt. Regeln
     skulle alltså nästan bara kasta riktiga värden för en laddhybrid. För en bil utan batteri
     är en förbrukning under 5 l/100 km däremot ett tecken på något dolt och lämnas okänd.
   * El: ``(procent före − procent efter) × användbar kapacitet``, bara när kapaciteten är känd
     (ägarens värde på bilen, annars ett belagt standardvärde, se ``suggested_battery_kwh``).
     Har batteriet laddats under resan (nivån ökar, laddstatus visar laddning efter "före", eller
     nivån vid ankomsten rapporterades efter ankomsten under laddning) eller laddades det när
     "före" rapporterades, går nettot inte att avgöra. Nettot kan inte bli negativt, och 0
     procentenheter ger inget värde. Värdet är netto ur batteriet: bilen kan ha laddat batteriet
     med motorn eller bromsarna under färd.

Säger nivåskillnaden emot den valda metoden utanför felgränserna behålls metoden med minst
felgräns (färddatorns snitt, som i 1.3.0) och nivåskillnaden antecknas som avvikelse i grunden.
Orimliga värden (negativa, mer än tanken eller batteriet rymmer, mer än 60 l eller 60 kWh per
100 km) kastas. Nivån efter måste ha mätarställningen vid resans slut och vara rapporterad efter
starten, och nivån före rapporterad vid resans startmätarställning; annars lämnas värdet okänt.

Värdet märks ``estimated`` ("Uppskattad ur bilens egna avläsningar") och GRUNDEN sparas
(``RawTrip.fuel_basis``/``energy_basis``: metod, värdena före och efter, felgränsen,
anteckningar). Räcker underlaget inte lämnas värdet okänt och webben visar "–". Ett värde som
redan finns (``verified``/``estimated``) skrivs aldrig över av sig självt, importerade resor
rörs inte, och resor i månader vars underlag redan är skapat rörs inte. Undantaget är el som
räknats ur batteriets kapacitet: när ägaren ändrar kapaciteten räknas just de värdena om
(``refill_after_capacity_change``).
"""

from __future__ import annotations

import bisect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, MonthClose, RawTrip, TripSegment, Vehicle, VehicleObservation
from app.models.enums import LogicalState, MonthCloseState, TripSource, ValueQuality

LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
NEAR = timedelta(hours=2)  # trippmätaren: närmaste avläsning före start och efter slut
CLOCK_SKEW = timedelta(minutes=2)
LOOKBACK = timedelta(days=14)  # resor som DriveLog har skapat de senaste 14 dygnen
LEVEL_WINDOW = timedelta(days=1)  # nivån före: avläsningar sedda högst så långt före resans tidigaste start
MAX_SPEED_KMH = 130.0  # senast möjliga start när starttiden är okänd: ankomsten − sträckan i 130 km/h
MIN_TOLERANCE_KM = 2.0
TOLERANCE_SHARE = 0.15
MAX_PER_100 = 60.0  # l/100 km och kWh/100 km
MAX_TANK_L = 100.0
REFUEL_STEP_L = 1.0
NO_TRIP_KM = 0.05
ODO_STILL_KM = 0.5  # mätarställningen "har inte ökat" förbi resans start
AVG_RESOLUTION = 0.1  # bilens snitt har en decimal
MAX_ROUNDING_SHARE = 0.25  # avrundningsfelet får vara högst en fjärdedel av värdet
MIN_LEVEL_PER_100 = 5.0  # l/100 km när bara läget före och efter finns – bara bilar utan batteri
CHARGING = "CHARGING"
BASIS_VERSION = 1

# Batteriets ANVÄNDBARA kapacitet (kWh) för Volvo V90 laddhybrid per årsmodell (1.5.0).
# Källor (offentliga, november 2026):
# * Volvo Cars pressmeddelande sept. 2021: batteriet i S90/V90/XC60 Recharge T8 ökar från 11,6 till
#   18,8 kWh NOMINELLT (ett tredje lager celler), först under årsmodell 2022.
# * Användbar kapacitet för 18,8 kWh-batteriet: 14,9 kWh (evpulse.com, tflcar.com) respektive
#   14,8 kWh (greencarreports.com). Volvos eget datablad anger bara nominell kapacitet.
# * 11,6 kWh-batteriet (årsmodell 2020–2022): användbar 9,1 kWh enligt greencarreports.com,
#   10,4 kWh enligt pod-point.com (deras egen uppskattning). Källorna går isär och 2021–2022 kan
#   ha båda batterierna → INGET standardvärde; ägaren fyller i.
# * 10,4 kWh-batteriet (2017–2019): ingen belagd användbar kapacitet → inget standardvärde.
# Bara årsmodeller där alla bilar har 18,8 kWh-batteriet får ett standardvärde.
_V90_PHEV_USABLE_KWH: tuple[tuple[int, int, float], ...] = ((2023, 2100, 14.9),)


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
    odometer_km: float | None = None
    fuel_reported_at: datetime | None = None
    battery_reported_at: datetime | None = None
    charging_status: str | None = None
    avg_fuel_consumption: float | None = None
    avg_energy_consumption: float | None = None
    trip_meter_manual_km: float | None = None


@dataclass(frozen=True)
class Estimate:
    fuel_liters: float | None = None
    energy_kwh: float | None = None
    fuel_method: str | None = None
    energy_method: str | None = None
    fuel_basis: dict[str, Any] | None = None
    energy_basis: dict[str, Any] | None = None
    # Varför värdet saknas (1.5.0, felsökningsfilen): en kort kod ur REASONS, None när värdet finns.
    fuel_reason: str | None = None
    energy_reason: str | None = None


# Skälen när ett värde inte går att räkna fram (felsökningsfilen). Nycklarna är beräkningens egna.
REASONS = {
    "no_after": "no_after_level",  # ingen nivå vid ankomsten som hör till resan
    "no_before": "no_before_level",  # ingen nivå rapporterad medan bilen stod still före resan
    "driven_between": "driven_between",  # bilen körde mellan rapporten och resan
    "refuel": "refuel",  # tankning: nettot går inte att avgöra
    "charged": "charging",  # laddning: nettot går inte att avgöra
    "unchanged": "unchanged_level",  # oförändrad nivå ger inget värde (aldrig 0)
    "too_little": "too_little",  # bil utan batteri: under 5 l/100 km med bara läget före och efter
    "implausible": "implausible",  # orimligt värde, kastat
    "no_capacity": "no_capacity",  # batteriets användbara kapacitet är okänd
}


@dataclass
class _Candidate:
    value: float
    margin: float
    method: str
    basis: dict[str, Any] = field(default_factory=dict)


def reading(obs: VehicleObservation | Reading) -> Reading:
    if isinstance(obs, Reading):
        return obs
    return Reading(
        id=str(obs.id), fetched_at=obs.fetched_at, last_seen_at=obs.last_seen_at or obs.fetched_at,
        fuel_liters=obs.fuel_liters, battery_percent=obs.battery_percent,
        avg_fuel_consumption_auto=obs.avg_fuel_consumption_auto,
        avg_energy_consumption_auto=obs.avg_energy_consumption_auto, trip_meter_auto_km=obs.trip_meter_auto_km,
        odometer_km=obs.odometer_km, fuel_reported_at=obs.fuel_reported_at,
        battery_reported_at=obs.battery_reported_at, charging_status=obs.charging_status,
        avg_fuel_consumption=obs.avg_fuel_consumption, avg_energy_consumption=obs.avg_energy_consumption,
        trip_meter_manual_km=obs.trip_meter_manual_km,
    )


# ----------------------------------------------------------------------------- kapaciteten


def suggested_battery_kwh(vehicle: Vehicle | None) -> float | None:
    """Belagt standardvärde för batteriets användbara kapacitet ur modell och årsmodell, eller None."""
    if vehicle is None or vehicle.model_year is None:
        return None
    model = (vehicle.model or "").upper()
    fuel = (vehicle.fuel_type or "").upper()
    if "V90" not in model or "ELECTRIC" not in fuel or "PETROL" not in fuel:
        return None
    for first, last, kwh in _V90_PHEV_USABLE_KWH:
        if first <= vehicle.model_year <= last:
            return kwh
    return None


def battery_capacity(vehicle: Vehicle | None) -> tuple[float | None, str | None]:
    """(kWh, källa) – ägarens värde ("owner"), annars standardvärdet ("standard"), annars (None, None)."""
    if vehicle is None:
        return None, None
    own = vehicle.usable_battery_kwh
    if isinstance(own, int | float) and own > 0:
        return float(own), "owner"
    suggested = suggested_battery_kwh(vehicle)
    return (suggested, "standard") if suggested else (None, None)


# ----------------------------------------------------------------------------- beräkningen


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


def _margin(*trip_kms: float) -> float:
    return sum(AVG_RESOLUTION / 2 * km / 100 for km in trip_kms)


def _precise(value: float, *trip_kms: float) -> float | None:
    """Värdet, eller None när snittens avrundning kan flytta det mer än en fjärdedel."""
    if value <= 0 or _margin(*trip_kms) > MAX_ROUNDING_SHARE * value:
        return None
    return value


def _by_trip_meter(
    before: Reading, after: Reading, distance_km: float, field_name: str, trip_field: str = "trip_meter_auto_km",
    prefix: str = "trip_meter",
) -> tuple[float | None, str | None, float, dict[str, Any]]:
    """(värde, metod, felgräns, grund) ur trippmätaren och snittet för samma period."""
    tb, ta = getattr(before, trip_field), getattr(after, trip_field)
    avg_a = getattr(after, field_name)
    none: tuple[None, None, float, dict[str, Any]] = (None, None, 0.0, {})
    if tb is None or ta is None or avg_a is None:
        return none
    tol = _tolerance(distance_km)
    after_part = {"trip_km": ta, "average": avg_a, "at": after.fetched_at.isoformat()}
    if ta >= tb:
        if abs((ta - tb) - distance_km) > tol:
            return none
        if tb <= NO_TRIP_KM:
            value = avg_a * ta / 100
            if value > 0 and _precise(value, ta) is None:
                return none
            return value, prefix, _margin(ta), {"before": {"trip_km": tb, "at": before.fetched_at.isoformat()},
                                                "after": after_part}
        avg_b = getattr(before, field_name)
        if avg_b is None:
            return none
        value = (avg_a * ta - avg_b * tb) / 100
        if value > 0 and _precise(value, ta, tb) is None:
            return none
        return value, prefix, _margin(ta, tb), {
            "before": {"trip_km": tb, "average": avg_b, "at": before.fetched_at.isoformat()}, "after": after_part,
        }
    if abs(ta - distance_km) <= tol:
        value = avg_a * ta / 100
        if value > 0 and _precise(value, ta) is None:
            return none
        return value, f"{prefix}_reset", _margin(ta), {
            "before": {"trip_km": tb, "at": before.fetched_at.isoformat()}, "after": after_part,
        }
    return none


def _by_level(series: list[float], scale: float) -> float | None:
    """Nettot i nivån (före − efter); None när nivån ökat mellan två avläsningar (tankning/laddning)."""
    if len(series) < 2:
        return None
    for prev, nxt in zip(series, series[1:], strict=False):
        if nxt - prev > REFUEL_STEP_L * scale:
            return None
    return series[0] - series[-1]


def _decimals(value: float) -> int:
    for n in range(0, 3):
        if abs(value * 10**n - round(value * 10**n)) < 1e-6:
            return n
    return 3


def resolution(values: list[float]) -> float:
    """Avläsningarnas upplösning: den finaste som förekommer (hela liter → 1, en decimal → 0,1)."""
    if not values:
        return 1.0
    return 10.0 ** -max(_decimals(float(v)) for v in values)


def _reported(r: Reading, name: str) -> datetime:
    at = r.fuel_reported_at if name == "fuel_liters" else r.battery_reported_at
    return at or r.fetched_at


def _is_charging(r: Reading) -> bool:
    return (r.charging_status or "").strip().upper() == CHARGING


def _latest_start(raw: RawTrip, distance: float) -> datetime:
    assert raw.ended_at is not None
    if raw.started_at is not None:
        return raw.started_at
    if raw.start_latest is not None:
        return raw.start_latest
    return raw.ended_at - timedelta(hours=distance / MAX_SPEED_KMH)


def _level_candidate(
    raw: RawTrip, readings: list[Reading], arrival: Reading, name: str, distance: float,
) -> tuple[Reading, list[Reading], float] | str:
    """("före", rapporterna efter den fram till och med ankomsten, upplösningen) eller ett skäl."""
    latest = _latest_start(raw, distance) + CLOCK_SKEW
    pool = [r for r in readings if r.fetched_at < arrival.fetched_at and getattr(r, name) is not None]
    start_odo = raw.odometer_start
    before: Reading | None = None
    for r in reversed(pool):
        if _reported(r, name) > latest:
            continue  # rapporterad under resan
        if start_odo is not None and r.odometer_km is not None and abs(r.odometer_km - start_odo) > ODO_STILL_KM:
            # Mätaren har ökat förbi resans start (en rapport under färd), eller en körning emellan.
            if r.odometer_km > start_odo:
                continue
            return "driven_between"
        before = r
        break
    if before is None:
        return "no_before"
    t0 = _reported(before, name)
    # Nivån kan vara rapporterad före en tidigare körning och ha följt med till senare rader: då
    # gäller mätarställningen på den första raden med samma rapport, inte radens egen.
    first = next((r for r in pool if _reported(r, name) == t0), before)
    if start_odo is not None and first.odometer_km is not None and abs(first.odometer_km - start_odo) > ODO_STILL_KM:
        return "driven_between"
    between = [r for r in pool if r.fetched_at > before.fetched_at and _reported(r, name) > t0]
    res = resolution([float(getattr(r, name)) for r in [*readings, arrival] if getattr(r, name) is not None])
    return before, [*between, arrival], res


def _after_mismatch(raw: RawTrip, after: Reading, name: str, distance: float) -> bool:
    """Nivån "efter" hör inte till resan: annan mätarställning än resans slut, eller rapporterad före starten."""
    end_odo = raw.odometer_end
    if end_odo is not None and after.odometer_km is not None and abs(after.odometer_km - end_odo) > ODO_STILL_KM:
        return True
    return _reported(after, name) < _latest_start(raw, distance) - CLOCK_SKEW


def estimate_for_raw(
    raw: RawTrip, before: list[Any], after: list[Any], *, battery_kwh: float | None, tank_l: float | None,
    earlier: list[Any] = (), hybrid: bool | None = None, capacity_source: str | None = None,
) -> Estimate:
    """Förbrukningen för en resa ur avläsningarna (ren funktion).

    ``before``: avläsningar fram till resans start (äldst först) inom NEAR – trippmätarens
    metoder. ``earlier``: äldre avläsningar före resan (äldst först) – nivån före kan vara
    rapporterad långt före resan. ``after``: avläsningar under resan och efter den (äldst
    först); läget vid ankomst är den första som hämtades vid eller efter resans slut.
    ``hybrid``: bilen har batteri (laddhybrid); utelämnat = om någon avläsning har batterinivå.
    """
    distance = float(raw.distance_km or 0)
    if distance <= 0 or raw.ended_at is None:
        return Estimate()
    pre = [reading(o) for o in before]
    old = [reading(o) for o in earlier]
    post = [reading(o) for o in after]
    everything = [*old, *pre, *post]
    if hybrid is None:
        hybrid = bool(battery_kwh) or any(r.battery_percent is not None for r in everything)
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
    fuel_c: list[_Candidate] = []
    energy_c: list[_Candidate] = []

    for trip_field, fuel_avg, energy_avg, prefix in (
        ("trip_meter_auto_km", "avg_fuel_consumption_auto", "avg_energy_consumption_auto", "trip_meter"),
        ("trip_meter_manual_km", "avg_fuel_consumption", "avg_energy_consumption", "trip_meter_manual"),
    ):
        b, a = last_with(pre, trip_field), first_after_with(trip_field)
        if b is None or a is None:
            continue
        for avg, out, limit, unit in ((fuel_avg, fuel_c, cap, "l"), (energy_avg, energy_c, None, "kWh")):
            value, method, margin, basis = _by_trip_meter(b, a, distance, avg, trip_field, prefix)
            ok = _plausible(value, distance, cap=limit)
            if ok is not None and method is not None:
                out.append(_Candidate(ok, round(margin, 3), method, {**basis, "unit": unit}))

    events: dict[str, str] = {}
    vol = everything  # alla avläsningar, äldst först (för nivån före och upplösningen)
    fa = first_after_with("fuel_liters")
    if fa is not None and _after_mismatch(raw, fa, "fuel_liters", distance):
        events["fuel"], fa = "no_after", None
    if fa is not None:
        found = _level_candidate(raw, [r for r in vol if r.fetched_at < fa.fetched_at], fa, "fuel_liters", distance)
        if isinstance(found, str):
            events["fuel"] = found
        else:
            fb, rest, res = found
            values = [float(fb.fuel_liters or 0)] + [float(r.fuel_liters or 0) for r in rest]
            net = _by_level(values, 1.0)
            if net is None or net < 0:
                events["fuel"] = "refuel"
            elif net == 0:
                events["fuel"] = "unchanged"
            elif not hybrid and len(values) < 3 and net / distance * 100 < MIN_LEVEL_PER_100:
                events["fuel"] = "too_little"  # bil utan batteri: en tankning under resan syns inte
            else:
                ok = _plausible(net, distance, cap=cap)
                if ok is None:
                    events["fuel"] = "implausible"
                else:
                    fuel_c.append(_Candidate(ok, round(res, 3), "level", {
                        "unit": "l", "resolution": res, "readings": len(values),
                        "before": {"value": fb.fuel_liters, "unit": "l", "at": _reported(fb, "fuel_liters").isoformat()},
                        "after": {"value": fa.fuel_liters, "unit": "l", "at": _reported(fa, "fuel_liters").isoformat()},
                    }))

    ba = first_after_with("battery_percent")
    if ba is not None and _after_mismatch(raw, ba, "battery_percent", distance):
        events["energy"], ba = "no_after", None
    if ba is not None and battery_kwh:
        found = _level_candidate(raw, [r for r in vol if r.fetched_at < ba.fetched_at], ba, "battery_percent",
                                 distance)
        if isinstance(found, str):
            events["energy"] = found
        else:
            bb, rest, res = found
            values = [float(bb.battery_percent or 0)] + [float(r.battery_percent or 0) for r in rest]
            charged = _is_charging(bb) or any(_is_charging(r) for r in rest[:-1]) or (
                _is_charging(ba) and _reported(ba, "battery_percent") > raw.ended_at + CLOCK_SKEW
            )
            # En procentenhet motsvarar battery_kwh/100 kWh; en laddning syns som en ökning.
            net = None if charged else _by_level(values, 100.0 / battery_kwh)
            if net is None or net < 0:
                events["energy"] = "charged"
            elif net == 0:
                events["energy"] = "unchanged"
            else:
                ok = _plausible(net / 100 * battery_kwh, distance, cap=battery_kwh)
                if ok is None:
                    events["energy"] = "implausible"
                else:
                    energy_c.append(_Candidate(ok, round(res / 100 * battery_kwh, 3), "battery_level", {
                        "unit": "kWh", "resolution": res, "net": True,
                        "capacity_kwh": battery_kwh, "capacity_source": capacity_source,
                        "percent_points": round(net, 4),
                        "before": {"value": bb.battery_percent, "unit": "%",
                                   "at": _reported(bb, "battery_percent").isoformat()},
                        "after": {"value": ba.battery_percent, "unit": "%",
                                  "at": _reported(ba, "battery_percent").isoformat()},
                    }))

    fuel = _choose(fuel_c, "level")
    energy = _choose(energy_c, "battery_level")
    return Estimate(
        fuel_liters=fuel.value if fuel else None, energy_kwh=energy.value if energy else None,
        fuel_method=fuel.method if fuel else None, energy_method=energy.method if energy else None,
        fuel_basis=fuel.basis if fuel else None, energy_basis=energy.basis if energy else None,
    )


def _choose(candidates: list[_Candidate], physical: str) -> _Candidate | None:
    """Metoden med minst felgräns; säger nivåskillnaden emot den antecknas avvikelsen (1.3.0-värdet behålls)."""
    if not candidates:
        return None
    best = min(candidates, key=lambda c: c.margin)
    notes: list[str] = []
    level = next((c for c in candidates if c.method == physical), None)
    deviation = None
    if level is not None and best is not level and abs(best.value - level.value) > best.margin + level.margin:
        deviation = {"method": level.method, "value": level.value, "margin": level.margin}
        notes.append("disagree")
    others = [
        {"method": c.method, "value": c.value, "margin": c.margin} for c in candidates if c is not best
    ]
    best.basis = {
        "version": BASIS_VERSION, "method": best.method, "value": best.value, "margin": best.margin,
        **best.basis, "alternatives": others, "notes": notes,
    }
    if deviation is not None:
        best.basis["deviation"] = deviation
    return best


# ----------------------------------------------------------------------------- grunden i klartext

_METHOD_TEXT = {
    "level": "Nivåskillnad i tanken",
    "battery_level": "Nivåskillnad i batteriet",
    "trip_meter": "Färddatorns snitt × automatiska trippmätaren",
    "trip_meter_reset": "Färddatorns snitt × nollställd automatisk trippmätare",
    "trip_meter_manual": "Färddatorns snitt × manuella trippmätaren",
    "trip_meter_manual_reset": "Färddatorns snitt × nollställd manuell trippmätare",
}


def _num(value: float, unit: str) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} {unit}" if unit != "%" else f"{text} %"


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def basis_out(value: Any) -> dict[str, Any] | None:
    """Grunden ur databasen (fri JSON, typkontrollerad) med en färdig text; None vid fel form."""
    if not isinstance(value, dict):
        return None
    method, amount, margin = value.get("method"), _float(value.get("value")), _float(value.get("margin"))
    unit = value.get("unit") if value.get("unit") in ("l", "kWh") else None
    if not isinstance(method, str) or amount is None or margin is None or unit is None:
        return None
    parts = [f"{_METHOD_TEXT.get(method, method)}: {_num(amount, unit)} ± {_num(margin, unit)}."]
    before, after = value.get("before"), value.get("after")
    if method in ("level", "battery_level") and isinstance(before, dict) and isinstance(after, dict):
        b, a = _float(before.get("value")), _float(after.get("value"))
        u = "l" if method == "level" else "%"
        if b is not None and a is not None:
            parts.append(f"Före {_num(b, u)}, efter {_num(a, u)}.")
        res = _float(value.get("resolution"))
        if res is not None:
            step = "hela liter" if method == "level" and res >= 1 else (
                "hela procent" if res >= 1 else f"{_num(res, u)} i taget")
            parts.append(f"Bilen rapporterar {step}, därav felgränsen.")
        if method == "level" and _float(value.get("readings")) == 2:
            parts.append("En tankning mellan avläsningarna som är mindre än resans förbrukning syns inte.")
        if method == "battery_level":
            kwh = _float(value.get("capacity_kwh"))
            if kwh is not None:
                src = "ditt värde" if value.get("capacity_source") == "owner" else "standardvärdet för modellen"
                parts.append(f"Användbar kapacitet {_num(kwh, 'kWh')} ({src}).")
            parts.append("Netto ur batteriet: bilen kan ha laddat batteriet med motorn eller bromsarna under färd.")
    elif isinstance(before, dict) and isinstance(after, dict):
        tb, ta = _float(before.get("trip_km")), _float(after.get("trip_km"))
        if tb is not None and ta is not None:
            parts.append(f"Trippmätaren {_num(tb, 'km')} → {_num(ta, 'km')}; snitten har en decimal, därav felgränsen.")
    deviation = value.get("deviation")
    if isinstance(deviation, dict):
        dv, dm = _float(deviation.get("value")), _float(deviation.get("margin"))
        dmeth = deviation.get("method")
        if dv is not None and dm is not None and isinstance(dmeth, str):
            parts.append(
                f"{_METHOD_TEXT.get(dmeth, dmeth)} gav {_num(dv, unit)} ± {_num(dm, unit)}, utanför felgränserna; "
                "metoden med minst felgräns används."
            )
    return {"method": method, "value": amount, "margin": margin, "unit": unit, "text": " ".join(parts)}


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
        VehicleObservation.trip_meter_auto_km, VehicleObservation.odometer_km, VehicleObservation.fuel_reported_at,
        VehicleObservation.battery_reported_at, VehicleObservation.charging_status,
        VehicleObservation.avg_fuel_consumption, VehicleObservation.avg_energy_consumption,
        VehicleObservation.trip_meter_manual_km,
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
            trip_meter_auto_km=row[7], odometer_km=row[8], fuel_reported_at=row[9], battery_reported_at=row[10],
            charging_status=row[11], avg_fuel_consumption=row[12], avg_energy_consumption=row[13],
            trip_meter_manual_km=row[14],
        ))
    return out


def _split(raw: RawTrip, readings: list[Reading]) -> tuple[list[Reading], list[Reading], list[Reading]]:
    """Avläsningarna före resan: äldre (``earlier``) och inom NEAR (till och med läget vid
    avfärd), och under/efter den (≤ 2 h)."""
    by_id = {r.id: r for r in readings}
    own = [by_id[i] for i in (raw.observation_ids or []) if i in by_id]
    anchor = own[0] if own else None
    assert raw.ended_at is not None
    start_ref = anchor.last_seen_at if anchor is not None else (raw.started_at or raw.start_latest or raw.ended_at)
    cutoff = anchor.fetched_at if anchor is not None else start_ref
    times = [r.fetched_at for r in readings]
    split_at = bisect.bisect_right(times, cutoff)
    earlier = [r for r in readings[:split_at] if r.last_seen_at < start_ref - NEAR]
    before = [r for r in readings[:split_at] if r.last_seen_at >= start_ref - NEAR]
    after = [r for r in readings[split_at:] if r.fetched_at <= raw.ended_at + NEAR]
    return earlier, before, after


def _closed_months(db: Session, vehicle: Vehicle) -> tuple[set[str], Any]:
    months = set(db.execute(select(MonthClose.month).where(
        MonthClose.vehicle_id == vehicle.id,
        MonthClose.state.in_((MonthCloseState.EXPORTED, MonthCloseState.STALE)),
    )).scalars())
    if not months:
        return months, None
    from app.services import settings as settings_svc

    return months, settings_svc.user_timezone(db, vehicle.owner_user_id)


def _open_month(raws: list[RawTrip], closed: set[str], tz: Any) -> list[RawTrip]:
    if tz is None:
        return raws
    return [r for r in raws if r.sort_time is None or r.sort_time.astimezone(tz).strftime("%Y-%m") not in closed]


def _refresh_trips(db: Session, changed: list[uuid.UUID]) -> None:
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
    raws = _open_month(raws, closed, tz)
    if not raws:
        return 0
    starts = [r.started_at or r.start_earliest or r.start_latest or r.ended_at for r in raws]
    window_start = min(s for s in starts if s is not None) - NEAR - LEVEL_WINDOW
    window_end = max(r.ended_at for r in raws if r.ended_at is not None) + NEAR
    readings = _readings(db, vehicle_id, window_start, window_end)
    if not readings:
        return 0
    battery_kwh, capacity_source = battery_capacity(vehicle)
    hybrid = bool(battery_kwh) or "ELECTRIC" in (vehicle.fuel_type or "").upper() or None

    changed: list[uuid.UUID] = []
    for raw in raws:
        earlier, before, after = _split(raw, readings)
        est = estimate_for_raw(raw, before, after, battery_kwh=battery_kwh, tank_l=None, earlier=earlier,
                               hybrid=hybrid, capacity_source=capacity_source)
        touched = False
        if est.fuel_liters is not None and raw.fuel_quality == ValueQuality.UNKNOWN and raw.fuel_liters is None:
            raw.fuel_liters, raw.fuel_quality, raw.fuel_basis = est.fuel_liters, ValueQuality.ESTIMATED, est.fuel_basis
            touched = True
        if est.energy_kwh is not None and raw.energy_quality == ValueQuality.UNKNOWN and raw.energy_kwh is None:
            raw.energy_kwh, raw.energy_quality = est.energy_kwh, ValueQuality.ESTIMATED
            raw.energy_basis = est.energy_basis
            touched = True
        if touched:
            changed.append(raw.id)
    if not changed:
        return 0
    _refresh_trips(db, changed)
    return len(changed)


def _capacity_based(raw: RawTrip) -> bool:
    basis = raw.energy_basis
    return (
        raw.energy_quality == ValueQuality.ESTIMATED
        and isinstance(basis, dict)
        and basis.get("method") == "battery_level"
    )


def refill_after_capacity_change(db: Session, vehicle_id: uuid.UUID, *, now: datetime | None = None) -> int:
    """Räknar om elen när batteriets kapacitet har ändrats. Returnerar antalet delresor vars el
    ändrades eller fylldes i.

    Bara el som DriveLog själv har räknat ur kapaciteten (estimated, metod battery_level) och el
    som saknas räknas om; ägarens egna och importerade värden rörs aldrig, och inte heller resor
    i månader vars underlag redan är skapat.
    """
    now = now or utcnow()
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return 0
    rows = list(db.execute(
        select(RawTrip).where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.source.in_(LIVE_SOURCES),
            RawTrip.import_batch_id.is_(None),
            RawTrip.voided_at.is_(None),
            RawTrip.created_at >= now - LOOKBACK,
            RawTrip.energy_quality.in_((ValueQuality.ESTIMATED, ValueQuality.UNKNOWN)),
        )
    ).scalars())
    closed, tz = _closed_months(db, vehicle)
    rows = _open_month(
        [r for r in rows if _capacity_based(r) or (r.energy_quality == ValueQuality.UNKNOWN and r.energy_kwh is None)],
        closed, tz,
    )
    old = {r.id: r.energy_kwh for r in rows}
    cleared: list[uuid.UUID] = []
    for raw in rows:
        if _capacity_based(raw):
            raw.energy_kwh, raw.energy_quality, raw.energy_basis = None, ValueQuality.UNKNOWN, None
            cleared.append(raw.id)
    db.flush()
    fill_vehicle(db, vehicle_id, now=now)
    if cleared:
        _refresh_trips(db, cleared)
    return sum(1 for r in rows if r.energy_kwh != old[r.id])
