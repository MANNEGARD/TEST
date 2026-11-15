"""Trip Detection Engine: rekonstruerar resor ur fordonsobservationer.

Volvos API ger ingen resejournal. Positionen uppdateras bara när en resa avslutats,
och mätarställningen är ett heltal i kilometer. Motorn gör därför så här:

* *Ankare* = senaste stabila observationen (bilen står still).
* *Rörelse* = mätarställningen har ökat minst ``min_odometer_delta_km`` eller
  positionen har flyttats minst ``position_threshold_m``.
* Rörelsen anses avslutad när läget varit oförändrat i ``settle_minutes`` (bekräftat
  av en senare hämtning) eller när bilen rapporterat avstängd motor efter ändringen.
* Varje ny positionsrapport inom rörelsen är ett ankomstbesked och delar upp den i
  delresor.

Starttiden är aldrig känd: den lämnas tom och i stället anges ett intervall
(``start_earliest``–``start_latest``). Om mätarsträckan inte förklaras av
positionerna (omväg, rundresa utan ny position, uppehåll i insamlingen) flaggas
resan med ``possible_missed_trips`` – flera körningar kan ha skett.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LearningFeedback, LogicalTrip, RawTrip, TripSegment, Vehicle, VehicleObservation
from app.models.enums import (
    Category,
    DecisionSource,
    DistanceQuality,
    LogicalState,
    PositionSource,
    ReviewKind,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.places import registry as places
from app.services.places.normalize import haversine_m, valid_coordinates
from app.services.trips import review
from app.services.trips import segments as seg

logger = logging.getLogger(__name__)

CLOCK_SKEW = timedelta(minutes=2)
ROAD_FACTOR = 1.3  # väg/fågelväg vid uppskattning av mycket korta förflyttningar
LAG_KM = 2.0  # mätarställning som släpar efter positionsrapporten
MAX_AVG_SPEED_KMH = 130.0  # högsta rimliga snittfart (ger senaste möjliga start)
VOID_REASON_SUPERSEDED = "Ersatt av importerad originalresa"
_USER_SOURCES = (DecisionSource.USER, DecisionSource.HA)


@dataclass
class Eff:
    """Bilens kända läge vid en observation (saknade värden ärvs från föregående)."""

    odo: float | None = None
    odo_ts: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    pos_ts: datetime | None = None
    fuel: float | None = None

    @classmethod
    def carry(cls, prev: Eff | None, obs: VehicleObservation) -> Eff:
        prev = prev or cls()
        has_pos = valid_coordinates(obs.latitude, obs.longitude)
        return cls(
            odo=obs.odometer_km if obs.odometer_km is not None else prev.odo,
            odo_ts=obs.odometer_reported_at if obs.odometer_km is not None else prev.odo_ts,
            lat=obs.latitude if has_pos else prev.lat,
            lon=obs.longitude if has_pos else prev.lon,
            pos_ts=obs.position_reported_at if has_pos else prev.pos_ts,
            fuel=obs.fuel_liters if obs.fuel_liters is not None else prev.fuel,
        )

    @property
    def has_pos(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Step:
    obs: VehicleObservation
    eff: Eff
    odo_moved: bool
    pos_moved: bool

    @property
    def changed(self) -> bool:
        return self.odo_moved or self.pos_moved


@dataclass
class Leg:
    start_obs: VehicleObservation
    start: Eff
    end_obs: VehicleObservation
    end: Eff
    steps: list[Step] = field(default_factory=list)
    arrival: bool = False  # avslutas med en ny positionsrapport
    hard_earliest: datetime | None = None  # bilen stod bevisligen kvar till denna tidpunkt
    seen_earliest: datetime | None = None  # senaste hämtning som visade oförändrat läge (minus ett intervall)


def _dist_m(a: Eff, b: Eff) -> float | None:
    if not (a.has_pos and b.has_pos):
        return None
    return haversine_m(a.lat, a.lon, b.lat, b.lon)  # type: ignore[arg-type]


def _diff(prev: Eff, cur: Eff, settings: dict[str, Any]) -> tuple[bool, bool, bool]:
    """(mätarställning ökat, position flyttad, mätarställning backat)."""
    min_delta = float(settings.get("min_odometer_delta_km", 1.0))
    threshold = float(settings.get("position_threshold_m", 150))
    odo_moved = odo_back = False
    if prev.odo is not None and cur.odo is not None:
        delta = cur.odo - prev.odo
        odo_moved = delta >= min_delta
        odo_back = delta <= -min_delta
    d = _dist_m(prev, cur)
    pos_moved = d is not None and d >= threshold
    return odo_moved, pos_moved, odo_back


def _settled(steps: list[Step], settings: dict[str, Any]) -> bool:
    """Har läget varit oförändrat tillräckligt länge efter den senaste ändringen?"""
    idx = max(i for i, s in enumerate(steps) if s.changed)
    tail = steps[idx:]
    final = tail[0]
    stable_since = final.obs.fetched_at
    latest_seen = max(s.obs.last_seen_at for s in tail)
    stable = latest_seen - stable_since
    settle = timedelta(minutes=float(settings.get("settle_minutes", 10)))
    sightings = sum(s.obs.seen_count or 1 for s in tail)

    last = tail[-1].obs
    engine_obs = next((s.obs for s in reversed(tail) if s.obs.engine_running is not None), None)
    if engine_obs is not None and engine_obs.engine_reported_at is not None:
        newest_change = max(
            [t for t in (final.eff.odo_ts, final.eff.pos_ts) if t is not None], default=None
        )
        after_change = newest_change is None or engine_obs.engine_reported_at >= newest_change - CLOCK_SKEW
        arrived = any(s.pos_moved for s in steps)
        # En laddhybrid kan rapportera avstängd motor även under eldrift. Genvägen används
        # därför bara när en ny positionsrapport (ankomstbesked) också har kommit.
        if engine_obs.engine_running is False and after_change and arrived and sightings >= 2:
            return True
        if engine_obs.engine_running is True and engine_obs.engine_reported_at >= stable_since - CLOCK_SKEW:
            # Motorn rapporteras igång: kräv längre stillhet innan resan stängs.
            return stable >= 3 * settle and last.last_seen_at >= stable_since
    return stable >= settle


def _build_legs(anchor: VehicleObservation, anchor_eff: Eff, steps: list[Step], settings: dict[str, Any], poll: float) -> list[Leg]:
    """Delar en avslutad rörelse i delresor vid varje ny positionsrapport (ankomstbesked)."""
    last_changed = max(i for i, s in enumerate(steps) if s.changed)
    active = steps[: last_changed + 1]
    min_delta = float(settings.get("min_odometer_delta_km", 1.0))
    allowance = timedelta(seconds=poll)
    legs: list[Leg] = []
    start_obs, start_eff = anchor, anchor_eff
    pending: list[Step] = []
    reported = [t for t in (anchor_eff.pos_ts, anchor_eff.odo_ts) if t is not None]
    hard: datetime | None = max(reported) if reported else None
    seen: datetime | None = None
    prev_obs = anchor
    prev_eff = anchor_eff

    for step in active:
        if not step.changed:
            prev_obs, prev_eff = step.obs, step.eff  # bilen står still mellan två delresor
            continue
        if not pending:
            seen = prev_obs.last_seen_at - allowance
        pending.append(step)
        newer_report = step.eff.pos_ts is None or prev_eff.pos_ts is None or step.eff.pos_ts > prev_eff.pos_ts
        if step.pos_moved and newer_report:
            legs.append(
                Leg(start_obs, start_eff, step.obs, step.eff, pending, arrival=True, hard_earliest=hard, seen_earliest=seen)
            )
            start_obs, start_eff, pending = step.obs, step.eff, []
            hard = step.eff.pos_ts or step.obs.fetched_at
        prev_obs, prev_eff = step.obs, step.eff

    if pending:
        end = pending[-1]
        extra = (end.eff.odo - start_eff.odo) if (end.eff.odo is not None and start_eff.odo is not None) else 0.0
        if legs:
            last_leg = legs[-1]
            arrival_ts = last_leg.end.pos_ts
            if end.eff.odo_ts is not None and arrival_ts is not None:
                # Mätarvärdet avlästes senast vid ankomsten → hör till samma resa.
                lagging = end.eff.odo_ts <= arrival_ts + CLOCK_SKEW
            else:
                lagging = extra <= LAG_KM
            if lagging:
                last_leg.end = Eff(
                    odo=end.eff.odo, odo_ts=end.eff.odo_ts, lat=last_leg.end.lat, lon=last_leg.end.lon,
                    pos_ts=last_leg.end.pos_ts, fuel=end.eff.fuel,
                )
                last_leg.steps.extend(pending)
                last_leg.end_obs = end.obs
                pending = []
        if pending and (extra >= min_delta or not legs):
            legs.append(
                Leg(start_obs, start_eff, end.obs, end.eff, pending, arrival=False, hard_earliest=hard, seen_earliest=seen)
            )
    return legs


def _source_key(vehicle_id: uuid.UUID, leg: Leg, index: int) -> str:
    basis = f"{vehicle_id}|{leg.start_obs.id}|{leg.end_obs.id}|{index}"
    return hashlib.sha256(basis.encode()).hexdigest()


def _imported_coverage(db: Session, vehicle_id: uuid.UUID, odo_s: float, odo_e: float) -> float:
    """Hur många km av intervallet som redan täcks av importerade originalresor."""
    rows = db.execute(
        select(RawTrip.odometer_start, RawTrip.odometer_end).where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.voided_at.is_(None),
            RawTrip.import_batch_id.is_not(None),
            RawTrip.odometer_start < odo_e,
            RawTrip.odometer_end > odo_s,
        )
    ).all()
    return sum(max(0.0, min(odo_e, e) - max(odo_s, s)) for s, e in rows)


def _covered_by_imports(covered: float, length: float, tolerance: float) -> bool:
    """Täcks sträckan (i praktiken helt) av importerade resor?

    Toleransen får aldrig ensam göra en sträcka täckt: det krävs verkligt överlapp och
    minst halva sträckan, så att korta resor (1–2 km) inte försvinner utan import.
    """
    return covered > 0 and covered >= length - min(tolerance, length / 2)


def _raw_from_leg(
    db: Session,
    vehicle: Vehicle,
    user_id: uuid.UUID,
    anchor: VehicleObservation,
    leg: Leg,
    index: int,
    leg_count: int,
    settings: dict[str, Any],
    tolerance: float,
) -> RawTrip | None:
    key = _source_key(vehicle.id, leg, index)
    existing = db.execute(
        select(RawTrip.id).where(RawTrip.vehicle_id == vehicle.id, RawTrip.source_key == key).limit(1)
    ).first()
    if existing is not None:
        return None  # redan skapad (idempotent omkörning)

    flags = ["reconstructed"]
    possible_missed = False
    complete = True
    min_delta = float(settings.get("min_odometer_delta_km", 1.0))
    odo_s, odo_e = leg.start.odo, leg.end.odo
    hav_m = _dist_m(leg.start, leg.end)
    moved_pos = hav_m is not None and hav_m >= float(settings.get("position_threshold_m", 150))

    if odo_s is not None and odo_e is not None and odo_e - odo_s >= min_delta:
        distance, dq = round(odo_e - odo_s, 1), DistanceQuality.ODOMETER
        if _covered_by_imports(_imported_coverage(db, vehicle.id, odo_s, odo_e), odo_e - odo_s, tolerance):
            return None  # sträckan finns redan som verifierad importerad resa
    elif moved_pos:
        # Kortare än mätarställningens upplösning: uppskatta ur fågelvägen och märk det.
        distance, dq = round(hav_m / 1000 * ROAD_FACTOR, 1), DistanceQuality.ESTIMATED  # type: ignore[operator]
        flags.append("distance_estimated")
        if odo_s is None or odo_e is None:
            flags.append("missing_odometer")
            complete = False
    else:
        return None

    if moved_pos and dq == DistanceQuality.ODOMETER:
        limit = float(settings.get("detour_factor", 1.8)) * (hav_m / 1000) + float(settings.get("detour_slack_km", 5.0))  # type: ignore[operator]
        if distance > limit:
            flags.append("detour_or_multiple_trips")
            possible_missed = True
    if not leg.arrival and dq == DistanceQuality.ODOMETER:
        if leg.start.has_pos:
            flags.append("round_trip_unobserved")  # mätaren har ökat men ingen ny position
            possible_missed = True
        else:
            flags.append("no_position")
        complete = False
    if index == 0:
        unobserved = (leg.steps[0].obs.fetched_at - anchor.last_seen_at).total_seconds() / 60
        if unobserved > float(settings.get("max_unobserved_minutes", 45)):
            flags.append("collection_gap")
            possible_missed = True
    if leg_count > 1:
        flags.append("split_by_position_reports")
    if possible_missed:
        complete = False

    # Sluttid: bilens egen positionstidsstämpel om den är rimlig, annars en märkt uppskattning.
    lower = leg.hard_earliest
    upper = leg.end_obs.fetched_at + CLOCK_SKEW
    ended_at, end_quality = leg.end_obs.fetched_at, TimeQuality.BOUNDED
    pos_ts, odo_ts = leg.end.pos_ts, leg.end.odo_ts
    if leg.arrival and pos_ts is not None and pos_ts <= upper and (lower is None or pos_ts >= lower - CLOCK_SKEW):
        ended_at, end_quality = pos_ts, TimeQuality.REPORTED
    elif odo_ts is not None and odo_ts <= upper and (lower is None or odo_ts > lower):
        ended_at, end_quality = odo_ts, TimeQuality.ESTIMATED
    if end_quality != TimeQuality.REPORTED:
        flags.append("end_time_uncertain")

    # Starttid: okänd och hittas aldrig på. Endast gränser anges.
    #  * Senaste möjliga start: första hämtningen (eller bilens egen tidsstämpel) som
    #    visade ändrat läge, och aldrig senare än att sträckan hinns med i
    #    ``MAX_AVG_SPEED_KMH``.
    #  * Tidigaste möjliga start: bilens senaste rapport från det stillastående läget
    #    (den stod bevisligen kvar då). Har bilen rapporterat under färd (mätarställningen
    #    ökade i flera steg) snävas gränsen in till föregående oförändrade hämtning
    #    minus ett pollintervall.
    first = leg.steps[0]
    observed_in_motion = len(leg.steps) >= 2 and not first.pos_moved
    latest = first.obs.fetched_at
    if first.eff.odo_ts is not None and leg.start.odo_ts is not None and leg.start.odo_ts < first.eff.odo_ts <= latest:
        latest = first.eff.odo_ts
    if distance > 0:
        latest = min(latest, ended_at - timedelta(hours=distance / MAX_AVG_SPEED_KMH))
    latest = min(latest, ended_at)
    earliest = lower
    if observed_in_motion and leg.seen_earliest is not None:
        earliest = leg.seen_earliest if earliest is None else max(earliest, leg.seen_earliest)
        flags.append("observed_in_motion")
    if earliest is None:
        flags.append("start_lower_bound_unknown")
    else:
        earliest = min(earliest, latest)

    start_loc, _ = places.resolve_position(db, user_id, leg.start.lat, leg.start.lon)
    end_loc, _ = places.resolve_position(db, user_id, leg.end.lat, leg.end.lon)
    obs_ids = [str(leg.start_obs.id)] + [str(s.obs.id) for s in leg.steps]
    raw = RawTrip(
        vehicle_id=vehicle.id,
        source=TripSource.DEMO if vehicle.is_demo else TripSource.VOLVO_API,
        source_key=key,
        started_at=None,
        ended_at=ended_at,
        start_time_quality=TimeQuality.BOUNDED,
        end_time_quality=end_quality,
        start_earliest=earliest,
        start_latest=latest,
        odometer_start=odo_s,
        odometer_end=odo_e,
        odometer_source="api" if (odo_s is not None and odo_e is not None) else "none",
        distance_km=distance,
        distance_quality=dq,
        duration_seconds=None,
        start_address=(start_loc.address or start_loc.name) if start_loc else None,
        end_address=(end_loc.address or end_loc.name) if end_loc else None,
        start_lat=leg.start.lat,
        start_lon=leg.start.lon,
        end_lat=leg.end.lat,
        end_lon=leg.end.lon,
        position_source=PositionSource.API_POSITION if (leg.start.has_pos or leg.end.has_pos) else PositionSource.NONE,
        start_location_id=start_loc.id if start_loc else None,
        end_location_id=end_loc.id if end_loc else None,
        # API:t ger tanknivå och batterinivå, inte förbrukning per resa → okänt.
        fuel_liters=None,
        fuel_quality=ValueQuality.UNKNOWN,
        energy_kwh=None,
        energy_quality=ValueQuality.UNKNOWN,
        is_complete=complete,
        possible_missed_trips=possible_missed,
        quality_flags=sorted(set(flags)),
        observation_ids=list(dict.fromkeys(obs_ids)),
        updated_by="system",
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    return raw


def process_vehicle(
    db: Session,
    vehicle: Vehicle,
    *,
    user_id: uuid.UUID,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
    run_pipeline: bool = True,
) -> list[RawTrip]:
    """Går igenom obearbetade observationer och skapar rekonstruerade resor.

    Idempotent: kan köras hur ofta som helst. En pågående rörelse lämnas obearbetad
    tills den bekräftats avslutad av en senare observation.
    """
    now = now or utcnow()
    settings = settings or settings_svc.get_section(db, user_id, "volvo")
    tolerance = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
    poll = float(settings.get("poll_interval_seconds", 300))

    anchor = db.execute(
        select(VehicleObservation)
        .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.processed_at.is_not(None))
        .order_by(VehicleObservation.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    pending = list(
        db.execute(
            select(VehicleObservation)
            .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.processed_at.is_(None))
            .order_by(VehicleObservation.fetched_at.asc())
        ).scalars()
    )
    if anchor is None:
        if not pending:
            return []
        anchor = pending.pop(0)
        anchor.processed_at = now
    anchor_eff = Eff.carry(None, anchor)
    prev_eff = anchor_eff
    steps: list[Step] = []
    created: list[RawTrip] = []

    for obs in pending:
        eff = Eff.carry(prev_eff, obs)
        odo_moved, pos_moved, odo_back = _diff(prev_eff, eff, settings)
        if odo_back:
            review.upsert(
                db,
                vehicle_id=vehicle.id,
                kind=ReviewKind.ANOMALY,
                dedupe_key=f"odo-back:{obs.id}",
                title="Orimlig mätarställning från fordonet",
                detail=(
                    f"Bilen rapporterade {eff.odo:.0f} km efter att tidigare ha rapporterat "
                    f"{prev_eff.odo:.0f} km. Värdet används inte för att skapa resor."
                ),
                data={"type": "odometer_decreased", "observation_id": str(obs.id)},
            )
            eff.odo, eff.odo_ts = prev_eff.odo, prev_eff.odo_ts
        step = Step(obs, eff, odo_moved, pos_moved)
        if not steps and not step.changed:
            # Bilen står still: flytta fram ankaret.
            obs.processed_at = now
            anchor, anchor_eff, prev_eff = obs, eff, eff
            continue
        steps.append(step)
        prev_eff = eff
        if not _settled(steps, settings):
            continue
        legs = _build_legs(anchor, anchor_eff, steps, settings, poll)
        for i, leg in enumerate(legs):
            raw = _raw_from_leg(db, vehicle, user_id, anchor, leg, i, len(legs), settings, tolerance)
            if raw is not None:
                created.append(raw)
        for s in steps:
            s.obs.processed_at = now
        anchor, anchor_eff = obs, eff
        steps = []

    db.flush()
    if not created:
        return []
    for raw in created:
        seg.create_logical_for_raw(
            db, raw, category=Category.UNDECIDED, category_source=DecisionSource.SYSTEM,
            review_status=ReviewStatus.NEEDS_REVIEW, actor="system",
        )
    audit.record(
        db, audit.SYSTEM, "trip.detected", "vehicle", vehicle.id,
        after={"raw_trip_ids": [str(r.id) for r in created]}, user_id=user_id,
    )
    if run_pipeline:
        from app.services.trips import pipeline

        pipeline.after_trips_changed(
            db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM, apply_merge_rules=True, notify=True, origin="live"
        )
    db.flush()
    return created


# --------------------------------------------------------------- avstämning mot import


def _is_reconstructed(raw: RawTrip) -> bool:
    return raw.import_batch_id is None and raw.source in (TripSource.VOLVO_API, TripSource.DEMO) and bool(raw.observation_ids)


def _active_trip_for(db: Session, raw_id: uuid.UUID) -> LogicalTrip | None:
    trip = db.execute(
        select(LogicalTrip)
        .join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
        .where(TripSegment.raw_trip_id == raw_id, LogicalTrip.state == LogicalState.ACTIVE)
        .limit(1)
    ).scalars().first()
    return seg.load_trip(db, trip.id) if trip is not None else None


def _has_user_decision(trip: LogicalTrip | None) -> bool:
    return trip is not None and trip.category_source in _USER_SOURCES


def _transfer_decision(db: Session, source: LogicalTrip, target: LogicalTrip, actor: Actor) -> None:
    """Flyttar användarens beslut från den rekonstruerade resan till originalresan."""
    before = {"category": target.category, "purpose": target.purpose, "review_status": target.review_status}
    for s in target.segments:
        s.category_override = None
    target.category = source.category
    target.subcategory = source.subcategory
    target.category_source = source.category_source
    target.review_status = source.review_status
    target.confidence = None
    target.tax_note = None
    if source.purpose and source.purpose_confirmed and source.purpose_source in _USER_SOURCES:
        target.purpose, target.purpose_source, target.purpose_confirmed = source.purpose, source.purpose_source, True
    if source.contacts:
        target.contacts = source.contacts
    if source.notes:
        target.notes = source.notes
    target.is_favorite = target.is_favorite or source.is_favorite
    target.updated_by = actor.label
    seg.refresh_logical(db, target)
    # Inlärningshistoriken följer med till den resa som finns kvar.
    db.execute(
        update(LearningFeedback).where(LearningFeedback.logical_trip_id == source.id).values(logical_trip_id=target.id)
    )
    audit.record(
        db, actor, "trip.decision_transferred", "logical_trip", target.id, before=before,
        after={"category": target.category, "purpose": target.purpose, "from_trip_id": str(source.id)},
    )


def _void_reconstructed(db: Session, raw: RawTrip, superseded_by: RawTrip, actor: Actor, now: datetime) -> LogicalTrip | None:
    """Makulerar en rekonstruerad resa (originalposten finns kvar, märkt som ersatt)."""
    from app.services.trips import merge

    trip = _active_trip_for(db, raw.id)
    # Ingår resan i en sammanslagning löses den upp – nivå för nivå vid kedjor (A+B)+C –
    # tills den ersatta delresan ligger ensam i en journalresa. Övriga delresor (och en
    # tidigare importerad, verifierad resa) finns då kvar som egna aktiva resor; bara den
    # ersatta delresans journalresa makuleras nedan.
    for _ in range(50):
        if trip is None or trip.segment_count <= 1:
            break
        merge.dissolve(db, trip, actor=actor, reason="superseded_by_import", remember=False)
        db.flush()
        trip = _active_trip_for(db, raw.id)
    if trip is not None and trip.segment_count > 1:  # pragma: no cover - skydd mot oväntad struktur
        raise RuntimeError("Den rekonstruerade resan kunde inte lösas ut ur sammanslagningen.")
    raw.voided_at = now
    raw.void_reason = VOID_REASON_SUPERSEDED
    raw.superseded_by_id = superseded_by.id
    raw.updated_by = actor.label
    if trip is not None:
        trip.state = LogicalState.VOIDED
        trip.updated_by = actor.label
        review.resolve_for_trip(
            db,
            trip.id,
            (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION,
             ReviewKind.DATA_GAP, ReviewKind.MISSING_ODOMETER),
            by=actor.label,
            resolution={"superseded_by_import": str(superseded_by.id)},
        )
    return trip


def _learn_positions(db: Session, user_id: uuid.UUID, api_raw: RawTrip, imported: RawTrip, *, start: bool, end: bool) -> None:
    """Lär den importerade adressens plats koordinater från API-resans position."""
    from app.models import KnownLocation

    if start and imported.start_location_id is not None:
        loc = db.get(KnownLocation, imported.start_location_id)
        if loc is not None:
            places.learn_coordinates(db, loc, api_raw.start_lat, api_raw.start_lon, "api")
    if end and imported.end_location_id is not None:
        loc = db.get(KnownLocation, imported.end_location_id)
        if loc is not None:
            places.learn_coordinates(db, loc, api_raw.end_lat, api_raw.end_lon, "api")


def reconcile_with_imports(db: Session, vehicle: Vehicle, *, actor: Actor, now: datetime | None = None) -> dict[str, Any]:
    """Ersätter rekonstruerade API-resor med senare importerade originalresor.

    * 1:1-träff (samma mätarintervall inom toleransen): den rekonstruerade resan
      makuleras och användarens beslut flyttas till originalresan – om den inte redan
      har ett eget beslut fattat i DriveLog.
    * Övriga överlapp (en mot flera, delvis): den rekonstruerade resan makuleras så att
      inga kilometer dubbelräknas, och ett granskningsärende skapas när ett
      användarbeslut inte kunde föras över eller när överlappet bara är delvis.

    Anroparen kör därefter ``pipeline.after_trips_changed`` (luckor, mönster m.m.).
    """
    now = now or utcnow()
    user_id = vehicle.owner_user_id
    tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
    raws = list(
        db.execute(
            select(RawTrip).where(
                RawTrip.vehicle_id == vehicle.id,
                RawTrip.voided_at.is_(None),
                RawTrip.odometer_start.is_not(None),
                RawTrip.odometer_end.is_not(None),
            )
        ).scalars()
    )
    imported = sorted((r for r in raws if r.import_batch_id is not None), key=lambda r: r.odometer_start)
    reconstructed = sorted((r for r in raws if _is_reconstructed(r)), key=lambda r: r.odometer_start)
    out: dict[str, Any] = {"superseded": 0, "transferred": 0, "review_items": 0, "exact": 0}
    if not imported or not reconstructed:
        return out

    def overlap(a: RawTrip, b: RawTrip) -> float:
        return min(a.odometer_end, b.odometer_end) - max(a.odometer_start, b.odometer_start)

    for rec in reconstructed:
        # Träff kräver verkligt överlapp: två korta resor som bara gränsar till varandra
        # ligger annars inom toleransen och skulle felaktigt ersätta varandra.
        hits = [i for i in imported if overlap(rec, i) > tol or (
            overlap(rec, i) > 0
            and abs(i.odometer_start - rec.odometer_start) <= tol
            and abs(i.odometer_end - rec.odometer_end) <= tol
        )]
        if not hits:
            continue
        rec_trip = _active_trip_for(db, rec.id)
        single_rec_trip = rec_trip if (rec_trip is not None and rec_trip.segment_count == 1) else None
        had_decision = _has_user_decision(rec_trip)
        exact = (
            len(hits) == 1
            and abs(hits[0].odometer_start - rec.odometer_start) <= tol
            and abs(hits[0].odometer_end - rec.odometer_end) <= tol
        )
        others = [r for r in reconstructed if r.id != rec.id and r.voided_at is None and overlap(r, hits[0]) > tol]
        one_to_one = exact and not others
        problem: str | None = None

        if one_to_one:
            target = _active_trip_for(db, hits[0].id)
            _learn_positions(db, user_id, rec, hits[0], start=True, end=True)
            if had_decision and single_rec_trip is not None and target is not None and target.segment_count == 1:
                if not _has_user_decision(target):
                    conflict = (
                        target.category != Category.UNDECIDED
                        and single_rec_trip.category != target.category
                    )
                    previous = target.category
                    _transfer_decision(db, single_rec_trip, target, actor)
                    out["transferred"] += 1
                    if conflict:
                        problem = (
                            "Den importerade originalresan hade en annan kategori "
                            f"({'Arbete' if previous == Category.WORK else 'Privat'}) än ditt tidigare beslut. "
                            "Ditt beslut har behållits – kontrollera att det stämmer."
                        )
                elif single_rec_trip.category != target.category:
                    problem = (
                        "Både den importerade originalresan och den tidigare rekonstruerade resan har beslut "
                        "fattade i DriveLog, med olika kategori. Originalresans beslut har behållits – kontrollera."
                    )
            elif had_decision:
                problem = (
                    "Ditt beslut för en rekonstruerad resa kunde inte föras över automatiskt eftersom resan "
                    "ingick i en sammanslagning. Kontrollera den importerade resan."
                )
            out["exact"] += 1
        else:
            covered = sum(max(0.0, overlap(rec, i)) for i in hits)
            full = _covered_by_imports(covered, rec.odometer_end - rec.odometer_start, tol)
            if full:
                first, last = hits[0], hits[-1]
                _learn_positions(db, user_id, rec, first, start=abs(first.odometer_start - rec.odometer_start) <= tol, end=False)
                _learn_positions(db, user_id, rec, last, start=False, end=abs(last.odometer_end - rec.odometer_end) <= tol)
            if had_decision:
                problem = (
                    "En rekonstruerad resa med ditt beslut motsvarar flera eller delar av importerade resor. "
                    "Beslutet kunde inte föras över automatiskt."
                )
            elif not full:
                problem = (
                    "En rekonstruerad resa överlappar bara delvis med importerade originalresor. Den har ersatts "
                    "av originalresorna; eventuell återstående sträcka visas som en lucka i mätarställningen."
                )

        _void_reconstructed(db, rec, hits[0], actor, now)
        out["superseded"] += 1
        if problem:
            target_trip = _active_trip_for(db, hits[0].id)
            _, created = review.upsert(
                db,
                vehicle_id=vehicle.id,
                kind=ReviewKind.IMPORT_PROBLEM,
                dedupe_key=f"reconcile:{rec.id}",
                title="Kontrollera importerad resa mot tidigare rekonstruerad resa",
                detail=problem,
                logical_trip_id=target_trip.id if target_trip is not None else None,
                data={
                    "type": "reconcile",
                    "reconstructed_raw_trip_id": str(rec.id),
                    "imported_raw_trip_ids": [str(i.id) for i in hits],
                },
            )
            out["review_items"] += 1 if created else 0

    if out["superseded"]:
        audit.record(db, actor, "trip.reconciled_with_import", "vehicle", vehicle.id, after=dict(out), user_id=user_id)
    db.flush()
    return out
