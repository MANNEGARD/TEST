"""Bränsle och laddning: sammanställning och detektering av tankningar.

Tre datatyper hålls isär:

* Verifierad förbrukning – uppmätt per resa i bilens egen körjournal.
* Uppskattad förbrukning – beräknad; märks alltid som uppskattad.
* Okänd förbrukning – sträcka utan mätvärden.

Tanknivå och bilens genomsnittsförbrukning från API:t är inte förbrukning per resa
och används aldrig som sådan. Tanknivån används bara för att föreslå tankningstillfällen.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChargingRecord, FuelRecord, Vehicle, VehicleObservation
from app.models.enums import ValueQuality
from app.services import settings as settings_svc
from app.services.stats import common
from app.services.stats.common import Totals

MIN_REFUEL_LITERS = 5.0
MANUAL_NEAR = timedelta(hours=6)
DETECTED_NEAR = timedelta(minutes=45)


def fuel_records(db: Session, vehicle_id, start: datetime | None, end: datetime | None) -> list[FuelRecord]:
    q = select(FuelRecord).where(FuelRecord.vehicle_id == vehicle_id)
    if start is not None:
        q = q.where(FuelRecord.at >= start)
    if end is not None:
        q = q.where(FuelRecord.at < end)
    return list(db.execute(q.order_by(FuelRecord.at.asc())).scalars())


def charging_records(db: Session, vehicle_id, start: datetime | None, end: datetime | None) -> list[ChargingRecord]:
    q = select(ChargingRecord).where(ChargingRecord.vehicle_id == vehicle_id)
    if start is not None:
        q = q.where(ChargingRecord.started_at >= start)
    if end is not None:
        q = q.where(ChargingRecord.started_at < end)
    return list(db.execute(q.order_by(ChargingRecord.started_at.asc())).scalars())


def monthly_breakdown(
    db: Session, *, vehicle: Vehicle, start: datetime | None, end: datetime | None, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """Per månad: verifierad bensin/el med täckningsgrad samt tankningar och laddningar."""
    months: dict[str, Totals] = defaultdict(Totals)
    for t in common.load_trips(db, vehicle.id, start, end):
        when = common.trip_time(t)
        if when is not None:
            months[when.astimezone(tz).strftime("%Y-%m")].add_trip(t)
    refuels: dict[str, list[FuelRecord]] = defaultdict(list)
    for r in fuel_records(db, vehicle.id, start, end):
        refuels[r.at.astimezone(tz).strftime("%Y-%m")].append(r)
    charges: dict[str, list[ChargingRecord]] = defaultdict(list)
    for c in charging_records(db, vehicle.id, start, end):
        charges[c.started_at.astimezone(tz).strftime("%Y-%m")].append(c)

    out = []
    for key in sorted(set(months) | set(refuels) | set(charges)):
        t = months.get(key) or Totals()
        fr, cr = refuels.get(key, []), charges.get(key, [])
        fuel_costs = [r.total_cost for r in fr if r.total_cost is not None]
        charge_costs = [c.total_cost for c in cr if c.total_cost is not None]
        out.append(
            {
                "month": key,
                "total_km": round(t.total_km, 1),
                "fuel_verified_l": round(t.fuel.verified, 2),
                "fuel_verified_km": round(t.fuel.verified_km, 1),
                "fuel_estimated_l": round(t.fuel.estimated, 2),
                "fuel_unknown_km": round(t.fuel.unknown_km, 1),
                "fuel_coverage": t.fuel.coverage,
                "fuel_per_100km": t.fuel.per_100km,
                "energy_verified_kwh": round(t.energy.verified, 2),
                "energy_verified_km": round(t.energy.verified_km, 1),
                "energy_estimated_kwh": round(t.energy.estimated, 2),
                "energy_unknown_km": round(t.energy.unknown_km, 1),
                "energy_coverage": t.energy.coverage,
                "energy_per_100km": t.energy.per_100km,
                "regen_kwh": round(t.regen_kwh, 2),
                "refuels": len(fr),
                "refuel_liters": round(sum(r.liters or 0 for r in fr), 2),
                "fuel_cost": round(sum(fuel_costs), 2) if fuel_costs else None,
                "charging_sessions": len(cr),
                "charged_kwh": round(sum(c.kwh or 0 for c in cr), 2),
                "charging_cost": round(sum(charge_costs), 2) if charge_costs else None,
            }
        )
    return out


def summary(
    db: Session, *, vehicle: Vehicle, start: datetime | None, end: datetime | None, tz: ZoneInfo
) -> dict[str, Any]:
    """Sammanställning av bensin och el för en period, med tydlig datakvalitet."""
    trips = common.load_trips(db, vehicle.id, start, end)
    totals = common.totals_for(trips)
    fr = fuel_records(db, vehicle.id, start, end)
    cr = charging_records(db, vehicle.id, start, end)
    prices = settings_svc.get_section(db, vehicle.owner_user_id, "costs")
    fuel_costs = [r.total_cost for r in fr if r.total_cost is not None]
    charge_costs = [c.total_cost for c in cr if c.total_cost is not None]
    priced = [r for r in fr if r.total_cost is not None and r.liters]
    avg_price = round(sum(r.total_cost for r in priced) / sum(r.liters for r in priced), 2) if priced else None  # type: ignore[misc]

    latest_avg = db.execute(
        select(VehicleObservation)
        .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.avg_fuel_consumption.is_not(None))
        .order_by(VehicleObservation.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    fuel = totals.fuel.as_dict("l")
    electricity = {**totals.energy.as_dict("kWh"), "regenerated_kwh": round(totals.regen_kwh, 2)}
    fuel_price, el_price = prices.get("fuel_price_per_liter"), prices.get("electricity_price_per_kwh")
    return {
        "period": {"start": start, "end": end, "timezone": str(tz)},
        "total_km": round(totals.total_km, 1),
        "fuel": {
            **fuel,
            "data_types": {
                "verified": "Uppmätt per resa i bilens körjournal",
                "estimated": "Beräknad – inte uppmätt",
                "unknown": "Sträcka utan mätvärden",
            },
        },
        "electricity": electricity,
        "refuels": {
            "count": len(fr),
            "liters": round(sum(r.liters or 0 for r in fr), 2),
            "manual": sum(1 for r in fr if r.source == "manual"),
            "detected": sum(1 for r in fr if r.source == "detected"),
            "cost": round(sum(fuel_costs), 2) if fuel_costs else None,
            "average_price_per_liter": avg_price,
        },
        "charging": {
            "count": len(cr),
            "kwh": round(sum(c.kwh or 0 for c in cr), 2),
            "cost": round(sum(charge_costs), 2) if charge_costs else None,
        },
        "estimated_costs": {
            "fuel": round(totals.fuel.verified * fuel_price, 2) if fuel_price and totals.fuel.verified > 0 else None,
            "electricity": round(totals.energy.verified * el_price, 2) if el_price and totals.energy.verified > 0 else None,
            "note": "Uppskattning: verifierad förbrukning × angivet pris i inställningarna.",
        },
        # Bilens eget snitt redovisas separat och blandas aldrig ihop med förbrukning per resa.
        "vehicle_reported_average": (
            {
                "fuel_l_per_100km": latest_avg.avg_fuel_consumption,
                "energy_kwh_per_100km": latest_avg.avg_energy_consumption,
                "fetched_at": latest_avg.fetched_at,
                "note": "Bilens eget genomsnitt (trippmätare) – inte uppmätt förbrukning per resa.",
            }
            if latest_avg is not None
            else None
        ),
        "monthly": monthly_breakdown(db, vehicle=vehicle, start=start, end=end, tz=tz),
    }


def detect_refuels(db: Session, vehicle: Vehicle, *, min_liters: float = MIN_REFUEL_LITERS) -> list[FuelRecord]:
    """Föreslår tankningstillfällen där tanknivån ökat mellan två fordonsrapporter.

    Volymen är en uppskattning (nivåskillnaden) och märks som sådan. Manuella poster
    skrivs aldrig över: finns en manuell tankning nära i tid skapas inget förslag.
    """
    observations = list(
        db.execute(
            select(VehicleObservation)
            .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.fuel_liters.is_not(None))
            .order_by(VehicleObservation.fetched_at.asc())
        ).scalars()
    )
    existing = list(db.execute(select(FuelRecord).where(FuelRecord.vehicle_id == vehicle.id)).scalars())
    created: list[FuelRecord] = []
    for prev, cur in zip(observations, observations[1:], strict=False):
        delta = cur.fuel_liters - prev.fuel_liters  # type: ignore[operator]
        if delta < min_liters:
            continue
        at = cur.fuel_reported_at or cur.fetched_at
        if prev.fetched_at <= at <= cur.fetched_at:
            pass  # fordonets egen tidsstämpel ligger inom fönstret
        else:
            at = cur.fetched_at
        near_manual = any(r.source == "manual" and abs(r.at - at) <= MANUAL_NEAR for r in existing)
        # Även en manuell post någonstans mellan de två rapporterna räknas som samma tankning.
        between_manual = any(
            r.source == "manual" and prev.fetched_at - timedelta(minutes=5) <= r.at <= cur.fetched_at + timedelta(minutes=5)
            for r in existing
        )
        near_detected = any(r.source == "detected" and abs(r.at - at) <= DETECTED_NEAR for r in existing)
        if near_manual or between_manual or near_detected:
            continue
        record = FuelRecord(
            vehicle_id=vehicle.id,
            at=at,
            liters=round(delta, 1),
            liters_quality=ValueQuality.ESTIMATED,
            odometer_km=cur.odometer_km,
            full_tank=False,
            source="detected",
            note=(
                f"Förslag: tanknivån ökade från {prev.fuel_liters:.0f} till {cur.fuel_liters:.0f} liter mellan två "
                "fordonsrapporter. Volymen är en uppskattning – bekräfta eller korrigera."
            ),
        )
        db.add(record)
        existing.append(record)
        created.append(record)
    db.flush()
    return created
