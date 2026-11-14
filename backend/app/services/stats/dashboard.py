"""Dashboardens nyckeltal och diagramdata. Alla värden hämtas ur lagrad data.

Principer:

* Kilometer redovisas per segment (privat/tjänst/ej avgjort) – aldrig dubbelräknat.
* Preliminära AI-/regelklassningar särredovisas och presenteras aldrig som godkända.
* Öppna dataluckor redovisas som ej journalförda km. Privat körning härleds aldrig
  som "total körning minus tjänsteresor" om perioden inte är avstämd.
* Bensin/el: endast uppmätta värden per resa räknas som verifierade; snitt per 100 km
  beräknas bara över sträcka som har verifierade värden.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    ChargingRecord,
    FuelRecord,
    ImportBatch,
    KnownLocation,
    LogicalTrip,
    ReviewItem,
    Vehicle,
    VehicleObservation,
)
from app.models.enums import (
    COVERAGE_LABELS_SV,
    REVIEW_KIND_LABELS_SV,
    Coverage,
    IntegrationProvider,
    LocationType,
    ReviewKind,
    ReviewState,
    TripSource,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.places import registry as places
from app.services.places.normalize import parse_address
from app.services.stats import common
from app.services.stats.common import EnergyAcc, Totals
from app.services.trips import segments as seg

MAX_BUCKETS = 800
_MONTHS_SV = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


# ----------------------------------------------------------------------------- kostnader


def _costs(
    db: Session, vehicle: Vehicle, user_id: uuid.UUID, start: datetime | None, end: datetime | None, totals: Totals
) -> dict[str, Any]:
    prices = settings_svc.get_section(db, user_id, "costs")
    fq = select(FuelRecord).where(FuelRecord.vehicle_id == vehicle.id)
    cq = select(ChargingRecord).where(ChargingRecord.vehicle_id == vehicle.id)
    if start is not None:
        fq, cq = fq.where(FuelRecord.at >= start), cq.where(ChargingRecord.started_at >= start)
    if end is not None:
        fq, cq = fq.where(FuelRecord.at < end), cq.where(ChargingRecord.started_at < end)
    refuels = list(db.execute(fq).scalars())
    charges = list(db.execute(cq).scalars())
    fuel_recorded = [r.total_cost for r in refuels if r.total_cost is not None]
    charge_recorded = [c.total_cost for c in charges if c.total_cost is not None]
    fuel_price, el_price = prices.get("fuel_price_per_liter"), prices.get("electricity_price_per_kwh")
    out: dict[str, Any] = {
        "currency": "SEK",
        "fuel": {
            "recorded": round(sum(fuel_recorded), 2) if fuel_recorded else None,
            "recorded_refuels": len(fuel_recorded),
            "estimated": round(totals.fuel.verified * fuel_price, 2) if fuel_price and totals.fuel.verified > 0 else None,
            "estimate_basis": "Verifierade liter × angivet literpris" if fuel_price else None,
        },
        "electricity": {
            "recorded": round(sum(charge_recorded), 2) if charge_recorded else None,
            "recorded_sessions": len(charge_recorded),
            "estimated": round(totals.energy.verified * el_price, 2) if el_price and totals.energy.verified > 0 else None,
            "estimate_basis": "Verifierade kWh × angivet elpris" if el_price else None,
        },
        "refuels": {
            "count": len(refuels),
            "liters": round(sum(r.liters or 0 for r in refuels), 2),
            "detected": sum(1 for r in refuels if r.source == "detected"),
        },
        "charging_sessions": {"count": len(charges), "kwh": round(sum(c.kwh or 0 for c in charges), 2)},
    }
    known = [v for v in (out["fuel"]["recorded"], out["electricity"]["recorded"]) if v is not None]
    out["recorded_total"] = round(sum(known), 2) if known else None
    return out


# ----------------------------------------------------------------------------- täckning


def coverage(
    db: Session,
    vehicle: Vehicle,
    user_id: uuid.UUID,
    trips: list[LogicalTrip],
    start: datetime | None,
    end: datetime | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Datatäckning för perioden: komplett journal, importerat urval eller ofullständig."""
    tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
    gaps = common.open_gaps(db, vehicle.id, start, end)
    gap_km = round(sum(g.km for g in gaps), 1)
    journal_km = round(sum(t.distance_km for t in trips), 1)

    first_odo, last_odo = common.first_and_last_trip_odometer(trips)
    odo_start = common.boundary_odometer(db, vehicle, start, tolerance_km=tol, now=now)
    odo_end = common.boundary_odometer(db, vehicle, end, tolerance_km=tol, now=now)
    delta = None
    if odo_start.value is not None and odo_end.value is not None:
        delta = round(odo_end.value - odo_start.value, 1)
    unreconciled = round(delta - journal_km, 1) if delta is not None else None
    missing_odometer = sum(1 for t in trips if t.odometer_start is None or t.odometer_end is None)

    batch_ids = {
        r.import_batch_id for t in trips for r in seg.ordered_raws(t) if r.import_batch_id is not None
    }
    coverages: set[str] = set()
    if batch_ids:
        coverages = {
            c for (c,) in db.execute(select(ImportBatch.coverage).where(ImportBatch.id.in_(batch_ids))).all()
        }
    only_imports = bool(trips) and all(
        r.source in (TripSource.VOLVO_IMPORT, TripSource.GAP_FILL) for t in trips for r in seg.ordered_raws(t)
    )

    reconciled = (
        bool(trips)
        and not gaps
        and missing_odometer == 0
        and delta is not None
        and unreconciled is not None
        and abs(unreconciled) <= max(tol, 0.005 * max(delta, 0))
    )
    if not trips:
        status, explanation = "no_data", "Inga journalförda resor i perioden."
    elif reconciled:
        status = Coverage.COMPLETE
        explanation = "Mätarställningen vid periodens början och slut stämmer med de journalförda resorna."
    elif gaps and only_imports and (not coverages or coverages & {Coverage.SELECTION, Coverage.UNKNOWN}):
        status = Coverage.SELECTION
        explanation = (
            f"Underlaget är ett importerat urval: {len(gaps)} luckor på totalt {gap_km:.0f} km saknar journalpost. "
            "Summan privat körning kan därför inte härledas."
        )
    else:
        status = Coverage.INCOMPLETE
        reasons = []
        if gaps:
            reasons.append(f"{len(gaps)} öppna luckor ({gap_km:.0f} km)")
        if missing_odometer:
            reasons.append(f"{missing_odometer} resor saknar mätarställning")
        if odo_start.value is None and start is not None:
            reasons.append("mätarställning vid periodens början saknas")
        if odo_end.value is None and end is not None:
            reasons.append("mätarställning vid periodens slut saknas")
        if start is None or end is None:
            reasons.append("perioden saknar fast början eller slut")
        if unreconciled is not None and not gaps and abs(unreconciled) > tol:
            reasons.append(f"{abs(unreconciled):.0f} km skiljer mellan mätarställning och journal")
        explanation = "Perioden är inte avstämd: " + "; ".join(reasons or ["underlag saknas"]) + "."

    label = {
        "no_data": "Inga data",
        Coverage.COMPLETE: COVERAGE_LABELS_SV[Coverage.COMPLETE],
        Coverage.SELECTION: COVERAGE_LABELS_SV[Coverage.SELECTION],
        Coverage.INCOMPLETE: COVERAGE_LABELS_SV[Coverage.INCOMPLETE],
    }[status]
    return {
        "status": str(status),
        "label": label,
        "explanation": explanation,
        "reconciled": reconciled,
        "open_gap_count": len(gaps),
        "open_gap_km": gap_km,
        "journal_km": journal_km,
        "odometer_start": odo_start.as_dict(),
        "odometer_end": odo_end.as_dict(),
        "odometer_delta_km": delta,
        "unreconciled_km": unreconciled,
        "first_trip_odometer": first_odo,
        "last_trip_odometer": last_odo,
        "trips_missing_odometer": missing_odometer,
        "import_coverages": sorted(coverages),
        # Privat körning får bara härledas ur mätarställningen när perioden är avstämd.
        "private_total_derivable": reconciled,
    }


# ----------------------------------------------------------------------------- fordonsläge


def vehicle_status(db: Session, vehicle: Vehicle, user_id: uuid.UUID) -> dict[str, Any]:
    odo = common.latest_odometer(db, vehicle)
    obs = db.execute(
        select(VehicleObservation)
        .where(
            VehicleObservation.vehicle_id == vehicle.id,
            VehicleObservation.latitude.is_not(None),
            VehicleObservation.longitude.is_not(None),
        )
        .order_by(VehicleObservation.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    position = None
    if obs is not None:
        loc, distance = places.resolve_position(db, user_id, obs.latitude, obs.longitude)
        position = {
            "latitude": obs.latitude,
            "longitude": obs.longitude,
            "reported_at": obs.position_reported_at,
            "fetched_at": obs.last_seen_at or obs.fetched_at,
            "place": (
                {"id": str(loc.id), "name": places.display_name(loc), "type": loc.type, "distance_m": round(distance or 0)}
                if loc is not None
                else None
            ),
            "source": obs.source,
        }
    conn = None
    if vehicle.volvo_connection_id is not None:
        from app.models import IntegrationConnection

        conn = db.get(IntegrationConnection, vehicle.volvo_connection_id)
    if conn is None and not vehicle.is_demo:
        conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    sync = {
        "provider": "volvo",
        "status": conn.status if conn is not None else "not_configured",
        "last_success_at": conn.last_success_at if conn is not None else None,
        "last_sync_at": conn.last_sync_at if conn is not None else None,
        "last_error": conn.last_error if conn is not None else None,
    }
    return {"odometer": odo.as_dict(), "position": position, "sync": sync}


def review_counts(db: Session, vehicle: Vehicle) -> dict[str, Any]:
    rows = db.execute(
        select(ReviewItem.kind, func.count())
        .where(ReviewItem.vehicle_id == vehicle.id, ReviewItem.status == ReviewState.OPEN)
        .group_by(ReviewItem.kind)
    ).all()
    by_kind = {kind: n for kind, n in rows}
    return {
        "total": sum(by_kind.values()),
        "by_kind": [
            {"kind": str(k), "label": REVIEW_KIND_LABELS_SV.get(ReviewKind(k), k) if k in set(ReviewKind) else k, "count": n}
            for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1])
        ],
    }


# ----------------------------------------------------------------------------- sammanfattning


def summary(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dashboardens nyckeltal för en period."""
    user_id = vehicle.owner_user_id
    trips = common.load_trips(db, vehicle.id, start, end)
    totals = common.totals_for(trips)
    cov = coverage(db, vehicle, user_id, trips, start, end, now=now)
    costs = _costs(db, vehicle, user_id, start, end, totals)
    return {
        "vehicle": {
            "id": str(vehicle.id),
            "name": vehicle.name,
            "registration_number": vehicle.registration_number,
            "is_demo": vehicle.is_demo,
        },
        "period": {"start": start, "end": end, "timezone": str(tz)},
        "distance": totals.distance_dict(),
        "unjournaled": {
            "open_gap_km": cov["open_gap_km"],
            "open_gap_mil": common.km_to_mil(cov["open_gap_km"]),
            "open_gap_count": cov["open_gap_count"],
            "note": "Kilometer i mätarställningen som saknar journalpost. Ingår inte i någon kategori.",
        },
        "trips": {
            "total": totals.trips,
            "by_category": totals.by_kind,
            "preliminary": totals.preliminary_trips,
            "needs_review": totals.needs_review_trips,
            "driving_hours": round(totals.driving_seconds / 3600, 1),
        },
        "fuel": totals.fuel.as_dict("l"),
        "electricity": {**totals.energy.as_dict("kWh"), "regenerated_kwh": round(totals.regen_kwh, 2)},
        "costs": costs,
        "coverage": cov,
        "review": review_counts(db, vehicle),
        **vehicle_status(db, vehicle, user_id),
    }


# ----------------------------------------------------------------------------- tidsserier


def _bucket_start(day: date, bucket: str) -> date:
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def _bucket_next(day: date, bucket: str) -> date:
    if bucket == "week":
        return day + timedelta(days=7)
    if bucket == "month":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day + timedelta(days=1)


def _bucket_label(day: date, bucket: str) -> str:
    if bucket == "week":
        iso = day.isocalendar()
        return f"v{iso.week} {iso.year}"
    if bucket == "month":
        return f"{_MONTHS_SV[day.month - 1]} {day.year}"
    return day.isoformat()


def timeseries(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    bucket: str = "day",
) -> dict[str, Any]:
    """Körsträcka, bränsle och el per dag/vecka/månad. Tomma perioder finns med."""
    if bucket not in ("day", "week", "month"):
        raise common.PeriodError("bucket måste vara day, week eller month.")
    trips = [t for t in common.load_trips(db, vehicle.id, start, end) if common.trip_time(t) is not None]
    accs: dict[date, Totals] = defaultdict(Totals)
    for t in trips:
        day = common.trip_time(t).astimezone(tz).date()  # type: ignore[union-attr]
        accs[_bucket_start(day, bucket)].add_trip(t)

    first = start.astimezone(tz).date() if start is not None else (min(accs) if accs else None)
    if end is not None:
        last = (end.astimezone(tz) - timedelta(seconds=1)).date()
    else:
        last = max(accs) if accs else None
    points: list[dict[str, Any]] = []
    if first is not None and last is not None:
        cur = _bucket_start(first, bucket)
        while cur <= last and len(points) < MAX_BUCKETS:
            t = accs.get(cur) or Totals()
            points.append(
                {
                    "bucket": cur.isoformat(),
                    "label": _bucket_label(cur, bucket),
                    "trips": t.trips,
                    "total_km": round(t.total_km, 1),
                    "work_km": round(t.work_km, 1),
                    "private_km": round(t.private_km, 1),
                    "undecided_km": round(t.undecided_km, 1),
                    "preliminary_km": round(t.preliminary_work_km + t.preliminary_private_km, 1),
                    "fuel_liters": round(t.fuel.verified, 2),
                    "fuel_km_covered": round(t.fuel.verified_km, 1),
                    "fuel_per_100km": t.fuel.per_100km,
                    "energy_kwh": round(t.energy.verified, 2),
                    "energy_km_covered": round(t.energy.verified_km, 1),
                    "energy_per_100km": t.energy.per_100km,
                    "regen_kwh": round(t.regen_kwh, 2),
                }
            )
            cur = _bucket_next(cur, bucket)
    return {"bucket": bucket, "timezone": str(tz), "points": points, "truncated": len(points) >= MAX_BUCKETS}


# ----------------------------------------------------------------------------- platser


def _place_label(
    loc: KnownLocation | None, address: str | None, *, include_private: bool, private_trip: bool
) -> tuple[str, str | None, str]:
    """(etikett, adress eller None, typ). Privata platser lämnas aldrig ut med adress."""
    if loc is not None:
        if loc.type == LocationType.HOME and loc.confirmed:
            return "Hemmet", (loc.address if include_private else None), loc.type
        if loc.type == LocationType.PRIVATE and loc.confirmed and not include_private:
            return "Privat plats", None, loc.type
        if private_trip and not include_private:
            city = loc.city or parse_address(address).city
            return (f"Privat resa ({city})" if city else "Privat resa"), None, loc.type
        return places.display_name(loc, address), loc.address or address, loc.type
    if private_trip and not include_private:
        city = parse_address(address).city
        return (f"Privat resa ({city})" if city else "Privat resa"), None, LocationType.UNKNOWN
    return (address or "Okänd plats"), address, LocationType.UNKNOWN


def _locations(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, KnownLocation]:
    if not ids:
        return {}
    return {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(ids))).scalars()}


def destinations(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Körsträcka och antal resor per destination."""
    include_private = bool(settings_svc.get_section(db, vehicle.owner_user_id, "export")["include_private_addresses"])
    trips = [t for t in common.load_trips(db, vehicle.id, start, end) if not common.is_gap_fill(t)]
    locs = _locations(db, {t.end_location_id for t in trips if t.end_location_id})
    groups: dict[str, dict[str, Any]] = {}
    for t in trips:
        loc = locs.get(t.end_location_id) if t.end_location_id else None
        private_trip = t.private_km > common.KM_EPS and t.work_km <= common.KM_EPS
        label, address, kind = _place_label(loc, t.end_address, include_private=include_private, private_trip=private_trip)
        # Anonymiserade privata mål grupperas på etiketten så att platsen inte kan särskiljas.
        key = f"label:{label}" if address is None else (f"loc:{loc.id}" if loc is not None else f"addr:{address}")
        g = groups.setdefault(
            key,
            {
                "location_id": str(loc.id) if (loc is not None and address is not None) else None,
                "label": label,
                "address": address,
                "type": str(kind),
                "trips": 0,
                "total_km": 0.0,
                "work_km": 0.0,
                "private_km": 0.0,
                "undecided_km": 0.0,
                "last_visit": None,
            },
        )
        g["trips"] += 1
        g["total_km"] += t.distance_km
        g["work_km"] += t.work_km
        g["private_km"] += t.private_km
        g["undecided_km"] += t.undecided_km
        when = t.ended_at or common.trip_time(t)
        if when is not None and (g["last_visit"] is None or when > g["last_visit"]):
            g["last_visit"] = when
    out = sorted(groups.values(), key=lambda g: (-g["total_km"], -g["trips"]))[: max(1, limit)]
    for g in out:
        for k in ("total_km", "work_km", "private_km", "undecided_km"):
            g[k] = round(g[k], 1)
    return out


def routes(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Vanligaste resvägarna (start → mål)."""
    include_private = bool(settings_svc.get_section(db, vehicle.owner_user_id, "export")["include_private_addresses"])
    trips = [t for t in common.load_trips(db, vehicle.id, start, end) if not common.is_gap_fill(t)]
    ids = {t.start_location_id for t in trips if t.start_location_id} | {t.end_location_id for t in trips if t.end_location_id}
    locs = _locations(db, ids)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for t in trips:
        private_trip = t.private_km > common.KM_EPS and t.work_km <= common.KM_EPS
        o_label, _, _ = _place_label(
            locs.get(t.start_location_id) if t.start_location_id else None, t.start_address,
            include_private=include_private, private_trip=private_trip,
        )
        d_label, _, _ = _place_label(
            locs.get(t.end_location_id) if t.end_location_id else None, t.end_address,
            include_private=include_private, private_trip=private_trip,
        )
        g = groups.setdefault(
            (o_label, d_label),
            {"origin": o_label, "destination": d_label, "trips": 0, "total_km": 0.0, "work_km": 0.0, "private_km": 0.0,
             "categories": Counter()},
        )
        g["trips"] += 1
        g["total_km"] += t.distance_km
        g["work_km"] += t.work_km
        g["private_km"] += t.private_km
        g["categories"][common.trip_kind(t)] += 1
    out = sorted(groups.values(), key=lambda g: (-g["trips"], -g["total_km"]))[: max(1, limit)]
    for g in out:
        g["average_km"] = round(g["total_km"] / g["trips"], 1) if g["trips"] else 0.0
        for k in ("total_km", "work_km", "private_km"):
            g[k] = round(g[k], 1)
        g["categories"] = dict(g["categories"])
    return out


# ----------------------------------------------------------------------------- månadsjämförelse


def monthly_comparison(
    db: Session, *, vehicle: Vehicle, tz: ZoneInfo, year: int | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Månad för månad för ett år, jämfört med samma månad året innan."""
    now = now or utcnow()
    year = year or now.astimezone(tz).year

    def year_totals(y: int) -> dict[int, Totals]:
        start, end = common.local_midnight(date(y, 1, 1), tz), common.local_midnight(date(y + 1, 1, 1), tz)
        out: dict[int, Totals] = defaultdict(Totals)
        for t in common.load_trips(db, vehicle.id, start, end):
            when = common.trip_time(t)
            if when is not None:
                out[when.astimezone(tz).month].add_trip(t)
        return out

    current, previous = year_totals(year), year_totals(year - 1)

    def row(t: Totals | None) -> dict[str, Any]:
        t = t or Totals()
        return {
            "trips": t.trips,
            "total_km": round(t.total_km, 1),
            "work_km": round(t.work_km, 1),
            "private_km": round(t.private_km, 1),
            "undecided_km": round(t.undecided_km, 1),
            "fuel_liters": round(t.fuel.verified, 2),
            "fuel_per_100km": t.fuel.per_100km,
            "energy_kwh": round(t.energy.verified, 2),
            "energy_per_100km": t.energy.per_100km,
        }

    months = []
    for m in range(1, 13):
        cur, prev = row(current.get(m)), row(previous.get(m))
        change = None
        if prev["total_km"] > 0:
            change = round((cur["total_km"] - prev["total_km"]) / prev["total_km"], 4)
        months.append(
            {"month": m, "label": _MONTHS_SV[m - 1], "current": cur, "previous": prev, "total_km_change": change}
        )
    return {"year": year, "previous_year": year - 1, "months": months}


def energy_totals(trips: list[LogicalTrip]) -> tuple[EnergyAcc, EnergyAcc, float]:
    t = common.totals_for(trips)
    return t.fuel, t.energy, t.regen_kwh


__all__ = [
    "coverage",
    "destinations",
    "energy_totals",
    "monthly_comparison",
    "review_counts",
    "routes",
    "summary",
    "timeseries",
    "vehicle_status",
    "UTC",
]
