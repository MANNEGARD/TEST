"""Avvikelsedetektion. Varje fynd blir ett granskningsärende (kind = anomaly).

Sökningen är idempotent: ärendena har stabila nycklar, och ett ärende som användaren
avgjort eller avfärdat öppnas inte igen.

I omfånget ``business_only`` (endast tjänsteresor redovisas) är luckor i mätarställningen
privat körning. Avvikelser som enbart bygger på sådana luckor – stora kilometergap och
"missade resor" mellan journalen och fordonets mätarställning – skapas då inte, och
redan öppna sådana ärenden stängs. Övriga avvikelser påverkas inte av omfånget.
"""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import DataGap, IntegrationConnection, RawTrip, ReviewItem, Vehicle, VehicleObservation
from app.models.enums import (
    GapStatus,
    IntegrationProvider,
    IntegrationStatus,
    ReviewKind,
    ReviewState,
    TripSource,
    ValueQuality,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.trips import review

LARGE_GAP_KM = 500.0
MIN_CONSUMPTION_SAMPLES = 20
MIN_CONSUMPTION_TRIP_KM = 5.0
MAD_Z_LIMIT = 6.0
MAX_KM_PER_DAY = 2500.0
MISSED_TRIP_SETTLE = timedelta(hours=2)
_MAD_SCALE = 1.4826


# Nyckelprefix för avvikelser som enbart bygger på luckor i mätarställningen.
GAP_ANOMALY_PREFIXES = {"large_gap": "anomaly:large_gap:", "missed_trips": "anomaly:missed:"}


def _upsert(db: Session, vehicle: Vehicle, key: str, title: str, detail: str, data: dict[str, Any],
            trip_id: uuid.UUID | None = None, reopen_reason: str | None = None) -> bool:
    _, created = review.upsert(
        db,
        vehicle_id=vehicle.id,
        kind=ReviewKind.ANOMALY,
        dedupe_key=key,
        title=title,
        detail=detail,
        logical_trip_id=trip_id,
        data=data,
        reopen_reason=reopen_reason,
    )
    return created


def close_gap_anomalies(db: Session, vehicle: Vehicle) -> dict[str, int]:
    """Stänger öppna (och uppskjutna) avvikelser som enbart bygger på luckor i mätarställningen.

    Används i omfånget ``business_only``. Ärendena stängs med orsaken ``journal_scope`` så
    att de kan öppnas igen om användaren går över till fullständig körjournal.
    """
    closed: dict[str, int] = {}
    for kind, prefix in GAP_ANOMALY_PREFIXES.items():
        items = db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle.id,
                ReviewItem.kind == ReviewKind.ANOMALY,
                ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)),
                ReviewItem.dedupe_key.like(f"{prefix}%"),
            )
        ).scalars().all()
        for item in items:
            review.resolve(db, item, by="system", resolution={"auto": True, "reason": review.SCOPE_REASON})
        if items:
            closed[kind] = len(items)
    return closed


def _raws(db: Session, vehicle: Vehicle) -> list[RawTrip]:
    return list(
        db.execute(
            select(RawTrip)
            .where(RawTrip.vehicle_id == vehicle.id, RawTrip.voided_at.is_(None), RawTrip.source != TripSource.GAP_FILL)
            .order_by(RawTrip.sort_time.asc().nulls_last(), RawTrip.odometer_start.asc().nulls_last())
        ).scalars()
    )


def robust_outliers(values: list[float], *, z_limit: float = MAD_Z_LIMIT, min_abs: float = 0.0) -> list[int]:
    """Index för värden långt över medianen (modifierat z-värde med MAD)."""
    if len(values) < MIN_CONSUMPTION_SAMPLES:
        return []
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    spread = _MAD_SCALE * mad
    if spread <= 1e-9:
        # Ingen spridning alls: använd medelabsolutavvikelsen så att enstaka extremvärden ändå fångas.
        spread = 1.2533 * (sum(abs(v - med) for v in values) / len(values))
    if spread <= 1e-9:
        return []
    return [i for i, v in enumerate(values) if (v - med) / spread > z_limit and (v - med) >= min_abs]


def scan_vehicle(db: Session, vehicle: Vehicle, *, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    """Söker efter avvikelser för ett fordon. Returnerar antal nya ärenden per typ."""
    now = now or utcnow()
    scope = settings_svc.journal_scope(db, user_id)
    # Luckor i mätarställningen är privat körning när endast tjänsteresor redovisas.
    gaps_are_expected = scope == settings_svc.SCOPE_BUSINESS_ONLY
    tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
    volvo_cfg = settings_svc.get_section(db, user_id, "volvo")
    created: dict[str, int] = {}
    found: dict[str, int] = {}

    def hit(kind: str, was_created: bool) -> None:
        found[kind] = found.get(kind, 0) + 1
        if was_created:
            created[kind] = created.get(kind, 0) + 1

    raws = _raws(db, vehicle)

    # 1. Ovanligt stora kilometergap (inte när luckor är förväntad privat körning)
    large_gaps = [] if gaps_are_expected else db.execute(
        select(DataGap).where(
            DataGap.vehicle_id == vehicle.id, DataGap.is_current.is_(True), DataGap.status == GapStatus.OPEN,
            DataGap.km >= LARGE_GAP_KM,
        )
    ).scalars().all()
    for gap in large_gaps:
        hit(
            "large_gap",
            _upsert(
                db, vehicle, f"{GAP_ANOMALY_PREFIXES['large_gap']}{gap.dedupe_key}",
                f"Ovanligt stort kilometergap: {gap.km:.0f} km",
                f"Mellan mätarställning {gap.odometer_start:.0f} och {gap.odometer_end:.0f} km saknas journalposter.",
                {"type": "large_gap", "gap_id": str(gap.id), "km": gap.km},
                reopen_reason=review.SCOPE_REASON,
            ),
        )

    # 2. Orimliga mätarställningar
    for r in raws:
        if r.odometer_start is None or r.odometer_end is None:
            continue
        diff = r.odometer_end - r.odometer_start
        if diff < 0:
            hit(
                "odometer_backwards",
                _upsert(
                    db, vehicle, f"anomaly:odo_negative:{r.id}", "Mätarställningen går bakåt inom en resa",
                    f"Resan börjar på {r.odometer_start:.0f} km och slutar på {r.odometer_end:.0f} km.",
                    {"type": "odometer_backwards", "raw_trip_id": str(r.id)},
                ),
            )
        elif abs(diff - r.distance_km) > max(3.0, 0.1 * max(r.distance_km, diff)):
            hit(
                "odometer_mismatch",
                _upsert(
                    db, vehicle, f"anomaly:odo_mismatch:{r.id}", "Körsträckan stämmer inte med mätarställningen",
                    f"Resan anger {r.distance_km:.1f} km men mätarställningen ändras {diff:.0f} km.",
                    {"type": "odometer_mismatch", "raw_trip_id": str(r.id), "distance_km": r.distance_km, "odometer_km": diff},
                ),
            )
    timed = [r for r in raws if r.sort_time is not None]
    for a, b in zip(timed, timed[1:], strict=False):
        if a.odometer_end is not None and b.odometer_start is not None and b.odometer_start < a.odometer_end - tol:
            # Samma nyckel som gaps.recompute använder när ordningen sammanfaller – inga dubbla ärenden.
            if b.odometer_end is not None and a.odometer_start is not None and b.odometer_end <= a.odometer_start + tol:
                key = f"anomaly:odo_order:{a.id}:{b.id}"
            else:
                key = f"odo-overlap:{a.id}:{b.id}"
            hit(
                "odometer_backwards",
                _upsert(
                    db, vehicle, key, "Motstridiga mätarställningar",
                    f"En senare resa börjar på {b.odometer_start:.0f} km trots att föregående resa slutade på "
                    f"{a.odometer_end:.0f} km.",
                    {"type": "odometer_backwards", "first_raw_trip_id": str(a.id), "second_raw_trip_id": str(b.id)},
                ),
            )

    # 3. Möjliga dubbletter (samma tid och sträcka)
    for i, a in enumerate(timed):
        for b in timed[i + 1 : i + 6]:
            if a.started_at is None or b.started_at is None:
                continue
            if abs((b.started_at - a.started_at).total_seconds()) > 120:
                break
            if abs(a.distance_km - b.distance_km) <= 0.5:
                ids = sorted((str(a.id), str(b.id)))
                hit(
                    "possible_duplicate",
                    _upsert(
                        db, vehicle, f"anomaly:duplicate:{ids[0]}:{ids[1]}", "Möjlig dubblett",
                        "Två resor har samma starttid och körsträcka. Kontrollera om den ena är en dubblett.",
                        {"type": "possible_duplicate", "raw_trip_ids": ids},
                    ),
                )

    # 4. Motstridiga tidsstämplar
    for r in raws:
        if r.started_at is not None and r.ended_at is not None:
            elapsed = (r.ended_at - r.started_at).total_seconds()
            if elapsed < 0:
                hit(
                    "time_conflict",
                    _upsert(
                        db, vehicle, f"anomaly:time_negative:{r.id}", "Sluttiden ligger före starttiden",
                        "Resans tidsstämplar är motstridiga.", {"type": "time_conflict", "raw_trip_id": str(r.id)},
                    ),
                )
            elif r.duration_seconds is not None and r.duration_seconds > elapsed + 300:
                hit(
                    "time_conflict",
                    _upsert(
                        db, vehicle, f"anomaly:duration:{r.id}", "Körtiden är längre än resans längd i tid",
                        f"Källan anger {r.duration_seconds // 60} minuters körtid men resan varade {int(elapsed // 60)} minuter.",
                        {"type": "time_conflict", "raw_trip_id": str(r.id)},
                    ),
                )
    for a, b in zip(timed, timed[1:], strict=False):
        if a.ended_at is not None and b.started_at is not None and (b.started_at - a.ended_at).total_seconds() < -60:
            hit(
                "time_overlap",
                _upsert(
                    db, vehicle, f"overlap:{a.id}:{b.id}", "Överlappande resor",
                    "Två resor överlappar varandra i tid eller mätarställning. Kontrollera uppgifterna.",
                    {"type": "overlap", "first_raw_trip_id": str(a.id), "second_raw_trip_id": str(b.id)},
                ),
            )

    # 5. Onormal förbrukning – jämfört med fordonets egen fördelning (median/MAD)
    for attr, quality_attr, label, unit, min_abs in (
        ("fuel_liters", "fuel_quality", "bensinförbrukning", "l/100 km", 8.0),
        ("energy_kwh", "energy_quality", "elförbrukning", "kWh/100 km", 15.0),
    ):
        sample = [
            r for r in raws
            if getattr(r, attr) is not None and getattr(r, quality_attr) == ValueQuality.VERIFIED
            and r.distance_km >= MIN_CONSUMPTION_TRIP_KM
        ]
        rates = [getattr(r, attr) / r.distance_km * 100 for r in sample]
        for idx in robust_outliers(rates, min_abs=min_abs):
            r = sample[idx]
            hit(
                "abnormal_consumption",
                _upsert(
                    db, vehicle, f"anomaly:consumption:{attr}:{r.id}", f"Onormal {label}",
                    f"Resan har {rates[idx]:.1f} {unit}, medianen för fordonet är {statistics.median(rates):.1f} {unit}.",
                    {"type": "abnormal_consumption", "metric": attr, "raw_trip_id": str(r.id),
                     "value": round(rates[idx], 2), "median": round(statistics.median(rates), 2)},
                ),
            )

    # 6–7. Fordonsrapporter: orimliga hopp, missade resor, avsaknad av nya uppgifter
    observations = list(
        db.execute(
            select(VehicleObservation)
            .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.odometer_km.is_not(None))
            .order_by(VehicleObservation.fetched_at.asc())
        ).scalars()
    )
    for a, b in zip(observations, observations[1:], strict=False):
        delta = b.odometer_km - a.odometer_km  # type: ignore[operator]
        days = max((b.fetched_at - a.fetched_at).total_seconds() / 86400, 1 / 24)
        if delta < -tol:
            hit(
                "odometer_backwards",
                _upsert(
                    db, vehicle, f"anomaly:obs_backwards:{b.id}", "Fordonet rapporterade lägre mätarställning än tidigare",
                    f"Mätarställningen gick från {a.odometer_km:.0f} till {b.odometer_km:.0f} km.",
                    {"type": "odometer_backwards", "observation_id": str(b.id)},
                ),
            )
        elif delta / days > MAX_KM_PER_DAY and delta > 300:
            hit(
                "odometer_jump",
                _upsert(
                    db, vehicle, f"anomaly:obs_jump:{b.id}", "Orimligt hopp i mätarställningen",
                    f"Mätarställningen ökade {delta:.0f} km på {days * 24:.0f} timmar.",
                    {"type": "odometer_jump", "observation_id": str(b.id), "km": delta},
                ),
            )
    if observations and raws and not gaps_are_expected:
        last_obs = observations[-1]
        journal_max = max((r.odometer_end for r in raws if r.odometer_end is not None), default=None)
        seen = last_obs.last_seen_at or last_obs.fetched_at
        if (
            journal_max is not None
            and last_obs.odometer_km - journal_max > max(tol, float(volvo_cfg["min_odometer_delta_km"]))
            and now - last_obs.fetched_at >= MISSED_TRIP_SETTLE
            and now - seen <= timedelta(days=30)
        ):
            km = last_obs.odometer_km - journal_max
            hit(
                "missed_trips",
                _upsert(
                    db, vehicle, f"{GAP_ANOMALY_PREFIXES['missed_trips']}{journal_max:.0f}:{last_obs.odometer_km:.0f}",
                    f"Misstänkt missade resor: {km:.0f} km saknar journalpost",
                    f"Fordonet rapporterar {last_obs.odometer_km:.0f} km men journalen slutar på {journal_max:.0f} km. "
                    "Komplettera med Volvos körjournal eller registrera resorna manuellt.",
                    {"type": "missed_trips", "km": round(km, 1)},
                    reopen_reason=review.SCOPE_REASON,
                ),
            )

    conn: IntegrationConnection | None = None
    if vehicle.volvo_connection_id is not None:
        conn = db.get(IntegrationConnection, vehicle.volvo_connection_id)
    if conn is None and not vehicle.is_demo:
        conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    stale_open = [
        i for i in db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle.id, ReviewItem.kind == ReviewKind.ANOMALY,
                ReviewItem.status == ReviewState.OPEN, ReviewItem.dedupe_key.like("anomaly:stale:%"),
            )
        ).scalars()
    ]
    is_stale = False
    if conn is not None and conn.status in (IntegrationStatus.CONNECTED, IntegrationStatus.ERROR, IntegrationStatus.DEMO):
        stale_after = timedelta(minutes=float(volvo_cfg["stale_after_minutes"]))
        last_ok = conn.last_success_at
        latest_any = db.execute(
            select(VehicleObservation.last_seen_at)
            .where(VehicleObservation.vehicle_id == vehicle.id)
            .order_by(VehicleObservation.last_seen_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        reference = max([t for t in (last_ok, latest_any) if t is not None], default=None)
        if reference is not None and now - reference > stale_after:
            is_stale = True
            hours = (now - reference).total_seconds() / 3600
            hit(
                "stale_vehicle_data",
                _upsert(
                    db, vehicle, f"anomaly:stale:{reference.date().isoformat()}", "Inga nya fordonsuppgifter",
                    f"Senaste lyckade hämtningen från fordonet är {hours:.0f} timmar gammal. "
                    "Kontrollera Volvo-anslutningen under Integrationer.",
                    {"type": "stale_vehicle_data", "last_success_at": reference.isoformat()},
                ),
            )
    if not is_stale:
        for item in stale_open:  # uppgifterna flödar igen
            review.resolve(db, item, by="system", resolution={"auto": True, "reason": "data_fresh"})

    closed = close_gap_anomalies(db, vehicle) if gaps_are_expected else {}

    db.flush()
    return {
        "vehicle_id": str(vehicle.id),
        "scanned_at": now,
        "scope": scope,
        "found": found,
        "created": created,
        "created_total": sum(created.values()),
        "closed": closed,
        "closed_total": sum(closed.values()),
    }
