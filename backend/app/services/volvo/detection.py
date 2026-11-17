"""Trip Detection Engine: rekonstruerar resor ur fordonsobservationer.

Volvos API ger ingen resejournal. Positionen uppdateras bara när en resa avslutats,
och mätarställningen är ett heltal i kilometer. Motorn gör därför så här:

* *Ankare* = senaste stabila observationen (bilen står still).
* *Rörelse* = mätarställningen har ökat minst ``min_odometer_delta_km`` eller
  positionen har flyttats minst ``position_threshold_m``.
* En ankomst (ny positionsrapport) med kompletta uppgifter – mätarvärdet avläst vid
  ankomsten och minst fågelvägen – stänger rörelsen direkt (1.5.0, ``_arrival_complete``).
* Annars anses rörelsen avslutad när läget varit oförändrat i ``settle_minutes`` (bekräftat
  av en senare hämtning) eller när bilen rapporterat avstängd motor efter ändringen.
* Ett mätarvärde som släpar efter en redan stängd resa förs till den resan i stället för att
  bli en egen resa (1.5.0, ``_attach_late_odometer``); frågan till telefonen väntar ändå som
  förut (``question_hold_until``).
* Varje ny positionsrapport inom rörelsen är ett ankomstbesked och delar upp den i
  delresor.

Starttiden rapporteras inte av bilen. Den räknas fram ur bilens egna avläsningar när de
räcker (``trips.start_time.estimate``, märkt ``estimated``); annars lämnas den tom och i
stället anges ett intervall (``start_earliest``–``start_latest``), och pipelinen uppskattar
den sedan ur sträckan (``trips.start_time.fill_from_distance``, också märkt ``estimated``). Om mätarsträckan inte förklaras av
positionerna (omväg, rundresa utan ny position, uppehåll i insamlingen) flaggas
resan med ``possible_missed_trips`` – flera körningar kan ha skett.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

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
from app.services.places import geocode
from app.services.places import registry as places
from app.services.places.normalize import haversine_m, valid_coordinates
from app.services.trips import review, start_time
from app.services.trips import segments as seg

logger = logging.getLogger(__name__)

CLOCK_SKEW = timedelta(minutes=2)
ROAD_FACTOR = 1.3  # väg/fågelväg vid uppskattning av mycket korta förflyttningar
LAG_KM = 2.0  # mätarställning som släpar efter positionsrapporten
LAG_SETTLE_FACTOR = 3  # längre väntan när bara mätarställning eller position har ändrats
MAX_AVG_SPEED_KMH = 130.0  # högsta rimliga snittfart (ger senaste möjliga start)
OLD_REPLAY_ROWS = 200  # hur många avläsningar bakåt väntan från före 1.5.0 spelas upp (_old_movement)
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


def _is_arrival(step: Step, prev: Eff) -> bool:
    """Är steget ett ankomstbesked: flyttad position med en nyare positionsrapport?"""
    newer_report = step.eff.pos_ts is None or prev.pos_ts is None or step.eff.pos_ts > prev.pos_ts
    return step.pos_moved and newer_report


def _started_after(obs: VehicleObservation, odo_ts: datetime | None, arrived_at: datetime) -> bool:
    """Kan mätarvärdet innehålla en ny körning: avläsningen rapporterar att motorn har startat
    efter ankomsten, och mätarvärdet är inte bevisligen avläst före den starten?"""
    if obs.engine_running is not True:
        return False
    started = obs.engine_reported_at
    if started is None:
        return True
    return started > arrived_at and (odo_ts is None or odo_ts >= started)


def _restarted(steps: list[Step], odo_ts: datetime | None, arrived_at: datetime | None) -> bool:
    """``_started_after`` för varje avläsning i ``steps`` – även de oförändrade. Ägarens bil
    rapporterar motorstarten på en egen rad utan ny mätarställning eller position, och när
    mätarökningen sedan kommer är motorn redan av igen. Utan tidsstämpel för ankomsten räknas
    varje avläsning med motorn igång som en ny start."""
    if arrived_at is None:
        return any(s.obs.engine_running is True for s in steps)
    return any(_started_after(s.obs, odo_ts, arrived_at) for s in steps)


def _engine_started_since(
    db: Session, vehicle_id: uuid.UUID, arrived_at: datetime, until_fetched: datetime, odo_ts: datetime | None
) -> bool:
    """Har bilen, i någon avläsning hämtad senast ``until_fetched``, rapporterat en motorstart
    efter ankomsten som mätarvärdet (``odo_ts``) kan vara avläst efter? Som ``_restarted``,
    men för avläsningar som redan är bearbetade (raden med motorstarten kan vara ankaret)."""
    conditions = [
        VehicleObservation.vehicle_id == vehicle_id,
        VehicleObservation.engine_running.is_(True),
        VehicleObservation.engine_reported_at > arrived_at,
        VehicleObservation.fetched_at <= until_fetched,
    ]
    if odo_ts is not None:
        conditions.append(VehicleObservation.engine_reported_at <= odo_ts)
    return db.execute(select(VehicleObservation.id).where(*conditions).limit(1)).first() is not None


def _arrival_complete(anchor_eff: Eff, steps: list[Step], settings: dict[str, Any]) -> bool:
    """Är rörelsen avslutad med en ankomst vars uppgifter är kompletta (1.5.0)?

    En bil som bara rapporterar när tändningen slås av skickar position och mätarställning
    i samma ögonblick. Då behöver DriveLog inte vänta: resan kan stängas vid den hämtning som
    först ser ankomsten. Kompletta uppgifter betyder här:

    * det senaste ändrade steget är ett ankomstbesked (ny positionsrapport, flyttad position),
      eller steg efter det där bara mätarställningen har ändrats och ingen nyare
      positionsrapport har kommit;
    * mätarställningen är avläst vid ankomsten: bilens tidsstämpel för mätarvärdet ligger
      inom ``CLOCK_SKEW`` från positionens. Ett äldre värde kan sakna en del av sträckan, och
      ett mycket senare kan innehålla nästa körning;
    * mätarsträckan är minst fågelvägen (med mätarens upplösning i marginal) – annars är
      mätarvärdet bevisligen gammalt, vad tidsstämpeln än säger;
    * har mätarställningen ökat efter ankomsten har ingen avläsning efter ankomsten (även en
      oförändrad) rapporterat att motorn har startat igen före mätarvärdet (då kan ökningen
      höra till nästa körning).

    Motorstatus och "sett två gånger" krävs inte. Saknas något av detta gäller den vanliga
    väntan i ``_settled`` oförändrad.
    """
    prev = anchor_eff
    leg_start = anchor_eff
    arrival: Step | None = None
    for step in steps:
        if _is_arrival(step, prev):
            if arrival is not None:
                leg_start = arrival.eff
            arrival = step
        elif arrival is not None and step.pos_moved:
            return False  # en flyttad position utan nyare rapport: oklart läge
        prev = step.eff
    if arrival is None:
        return False
    final = steps[-1].eff
    arrived_at, odo_ts = arrival.eff.pos_ts, final.odo_ts
    if arrived_at is None or odo_ts is None or final.pos_ts != arrived_at:
        return False
    after = steps[steps.index(arrival) + 1:]
    if any(s.odo_moved for s in after) and _restarted(after, odo_ts, arrived_at):
        return False  # bilen har startat igen: ökningen kan höra till nästa körning
    if not (arrived_at - CLOCK_SKEW <= odo_ts <= arrived_at + CLOCK_SKEW):
        return False
    if final.odo is None or leg_start.odo is None:
        return False
    if _dist_m(leg_start, final) is None:
        return False
    return not _stale_at_arrival(leg_start, final, settings)


def _stale_at_arrival(start: Eff, end: Eff, settings: dict[str, Any]) -> bool:
    """Är mätarvärdet vid ankomsten bevisligen gammalt: mätarsträckan kortare än fågelvägen?

    Mätarställningen är ett heltal, så en kilometer (eller ``min_odometer_delta_km``) räknas
    som marginal. Då måste en senare ökning utan ny positionsrapport höra till samma körning –
    annars räknas sträckan två gånger (en uppskattning ur fågelvägen plus mätarökningen).
    """
    crow_m = _dist_m(start, end)
    if crow_m is None or start.odo is None or end.odo is None:
        return False
    resolution = max(float(settings.get("min_odometer_delta_km", 1.0)), 1.0)
    return end.odo - start.odo + resolution < crow_m / 1000


def _catches_up(start: Eff, end: Eff, new_odo: float, settings: dict[str, Any]) -> bool:
    """Hör en senare mätarökning till en ankomst vars mätarvärde var bevisligen gammalt?

    Ja när mätarvärdet vid ankomsten var gammalt (``_stale_at_arrival``) och resan med ökningen
    inte blir längre än vad som räknas som omväg (``detour_factor`` × fågelvägen +
    ``detour_slack_km``). Är ökningen större kan den innehålla en annan körning: då gäller
    reglerna från före 1.5.0.
    """
    crow_m = _dist_m(start, end)
    if crow_m is None or start.odo is None or not _stale_at_arrival(start, end, settings):
        return False
    limit = float(settings.get("detour_factor", 1.8)) * (crow_m / 1000) + float(settings.get("detour_slack_km", 5.0))
    return new_odo - start.odo <= limit


def _settled(steps: list[Step], settings: dict[str, Any]) -> bool:
    """Har läget varit oförändrat tillräckligt länge efter den senaste ändringen?

    Detta är väntan från före 1.5.0. En ankomst med kompletta uppgifter stängs dessutom
    direkt (``_arrival_complete``).
    """
    idx = max(i for i, s in enumerate(steps) if s.changed)
    tail = steps[idx:]
    final = tail[0]
    stable_since = final.obs.fetched_at
    latest_seen = max(s.obs.last_seen_at for s in tail)
    stable = latest_seen - stable_since
    settle = timedelta(minutes=float(settings.get("settle_minutes", 10)))
    sightings = sum(s.obs.seen_count or 1 for s in tail)

    # Mätarställning och position kommer från olika API:er och kan släpa efter varandra.
    # Har bara den ena ändrats (och den andra är känd) väntar vi längre innan rörelsen
    # stängs, så att ett sent värde hamnar i samma resa i stället för att bli en ny
    # resa för samma körning (dubbla kilometer).
    odo_changed = any(s.odo_moved for s in steps)
    pos_changed = any(s.pos_moved for s in steps)
    one_sided = (pos_changed and not odo_changed and final.eff.odo is not None) or (
        odo_changed and not pos_changed and final.eff.has_pos
    )
    if one_sided:
        return stable >= LAG_SETTLE_FACTOR * settle

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
            # 1.5.0: var mätarvärdet vid ankomsten bevisligen gammalt hör ökningen till samma
            # resa, vad tidsstämpeln än säger. Före 1.5.0 blev den en egen "resa" utan ny
            # position, och sträckan räknades två gånger (uppskattningen ur fågelvägen plus ökningen).
            # Men aldrig när en avläsning efter ankomsten (även en oförändrad) har rapporterat en ny
            # motorstart: då kan ökningen vara nästa körning, och 1.4.2:s regler gäller.
            end_idx = next(i for i, s in enumerate(steps) if s.obs is last_leg.end_obs)
            lagging = lagging or (
                end.eff.odo is not None
                and not _restarted(steps[end_idx + 1:], end.eff.odo_ts, arrival_ts)
                and _catches_up(last_leg.start, last_leg.end, end.eff.odo, settings)
            )
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
    gap_info: dict[str, Any] | None = None
    if index == 0:
        unobserved = (leg.steps[0].obs.fetched_at - anchor.last_seen_at).total_seconds() / 60
        if unobserved > float(settings.get("max_unobserved_minutes", 45)):
            flags.append("collection_gap")
            possible_missed = True
            # När hämtningen låg nere och hur mycket mätaren ökade under tiden (1.3.0).
            first_odo = leg.steps[0].obs.odometer_km
            gap_info = {
                "from": anchor.last_seen_at.isoformat(),
                "to": leg.steps[0].obs.fetched_at.isoformat(),
                "odometer_km": round(first_odo - odo_s, 1) if first_odo is not None and odo_s is not None else None,
            }
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

    # Starttid: hittas aldrig på. Först gränserna:
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

    # Starttiden räknas fram ur bilens egna avläsningar när de räcker (märks "estimated"
    # med grunden sparad); annars förblir den okänd och bara gränserna anges.
    est = start_time.estimate(
        earliest=earliest, latest=latest, start_odometer=odo_s, ended_at=ended_at, distance_km=distance,
        steps=[start_time.obs_view(s.obs) for s in leg.steps], end=start_time.obs_view(leg.end_obs),
    )
    started_at, start_quality, start_basis = None, TimeQuality.BOUNDED, None
    if est.at is not None:
        started_at, start_quality, start_basis = est.at, TimeQuality.ESTIMATED, est.basis
        earliest, latest = est.earliest, est.latest

    # Demoplatser matchas aldrig mot ett riktigt fordon (och tvärtom).
    demo = bool(vehicle.is_demo)
    # Okända stopp får en ny obekräftad plats som väntar på adress (inget nätverk här).
    start_loc = geocode.place_for_position(db, user_id=user_id, lat=leg.start.lat, lon=leg.start.lon, demo=demo)
    end_loc = geocode.place_for_position(db, user_id=user_id, lat=leg.end.lat, lon=leg.end.lon, demo=demo)
    obs_ids = [str(leg.start_obs.id)] + [str(s.obs.id) for s in leg.steps]
    raw = RawTrip(
        vehicle_id=vehicle.id,
        source=TripSource.DEMO if vehicle.is_demo else TripSource.VOLVO_API,
        source_key=key,
        started_at=started_at,
        ended_at=ended_at,
        start_time_quality=start_quality,
        start_estimate_basis=start_basis,
        end_time_quality=end_quality,
        start_earliest=earliest,
        start_latest=latest,
        odometer_start=odo_s,
        odometer_end=odo_e,
        odometer_source="api" if (odo_s is not None and odo_e is not None) else "none",
        distance_km=distance,
        distance_quality=dq,
        duration_seconds=None,
        start_address=geocode.address_for(start_loc),
        end_address=geocode.address_for(end_loc),
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
        collection_gap=gap_info,
        updated_by="system",
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    return raw


def _anchor_eff(db: Session, vehicle_id: uuid.UUID, anchor: VehicleObservation) -> Eff:
    """Ankarets kända läge, med ärvda värden från tidigare observationer.

    Saknar ankarraden mätarställning eller position (t.ex. ett misslyckat anrop) används
    det senast kända värdet före ankaret. Annars skulle nästa ökning inte märkas och den
    körda sträckan tappas utan spår.
    """
    eff = Eff.carry(None, anchor)
    base = (VehicleObservation.vehicle_id == vehicle_id, VehicleObservation.fetched_at <= anchor.fetched_at)
    if eff.odo is None:
        row = db.execute(
            select(VehicleObservation.odometer_km, VehicleObservation.odometer_reported_at)
            .where(*base, VehicleObservation.odometer_km.is_not(None))
            .order_by(VehicleObservation.fetched_at.desc())
            .limit(1)
        ).first()
        if row is not None:
            eff.odo, eff.odo_ts = row
    if not eff.has_pos:
        rows = db.execute(
            select(VehicleObservation.latitude, VehicleObservation.longitude, VehicleObservation.position_reported_at)
            .where(*base, VehicleObservation.latitude.is_not(None), VehicleObservation.longitude.is_not(None))
            .order_by(VehicleObservation.fetched_at.desc())
            .limit(20)
        ).all()
        for lat, lon, ts in rows:
            if valid_coordinates(lat, lon):
                eff.lat, eff.lon, eff.pos_ts = lat, lon, ts
                break
    return eff


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    return a is not None and b is not None and abs((a - b).total_seconds()) < 1


def _arrival_raw(db: Session, vehicle_id: uuid.UUID, at: Eff) -> RawTrip | None:
    """Den rekonstruerade resa som slutade med just den här positionsrapporten (1.5.0)."""
    if at.pos_ts is None or not at.has_pos:
        return None
    rows = db.execute(
        select(RawTrip)
        .where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.voided_at.is_(None),
            RawTrip.import_batch_id.is_(None),
            RawTrip.source.in_((TripSource.VOLVO_API, TripSource.DEMO)),
            RawTrip.end_time_quality == TimeQuality.REPORTED,
            RawTrip.ended_at >= at.pos_ts - timedelta(seconds=1),
            RawTrip.ended_at <= at.pos_ts + timedelta(seconds=1),
        )
        .order_by(RawTrip.created_at.desc())
        .limit(5)
    ).scalars().all()
    for raw in rows:
        if (
            _same_instant(raw.ended_at, at.pos_ts)
            and raw.end_lat is not None and raw.end_lon is not None
            and abs(raw.end_lat - at.lat) < 1e-9 and abs(raw.end_lon - at.lon) < 1e-9  # type: ignore[operator]
        ):
            return raw
    return None


def _raw_catches_up(raw: RawTrip, step: Step, arrived_at: datetime, settings: dict[str, Any]) -> bool:
    """``_catches_up`` för en redan skapad resa – men aldrig när bilen har rapporterat en
    motorstart efter ankomsten (då kan ökningen vara nästa körning)."""
    if _started_after(step.obs, step.eff.odo_ts, arrived_at):
        return False
    start = Eff(odo=raw.odometer_start, lat=raw.start_lat, lon=raw.start_lon)
    end = Eff(odo=raw.odometer_end, lat=raw.end_lat, lon=raw.end_lon)
    return step.eff.odo is not None and _catches_up(start, end, step.eff.odo, settings)


def _observation_ids(raw: RawTrip) -> list[str]:
    value = raw.observation_ids
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _attach_late_odometer(
    db: Session,
    vehicle: Vehicle,
    user_id: uuid.UUID,
    step: Step,
    prev: Eff,
    settings: dict[str, Any],
    *,
    old_waiting: bool,
) -> RawTrip | None:
    """För ett släpande mätarvärde till den resa som redan har stängts vid ankomsten (1.5.0).

    Bara när det är säkert att det är samma körning:

    * bara mätarställningen har ökat, ingen ny positionsrapport har kommit sedan ankomsten
      (samma tidsstämpel), och bilen har inte rapporterat en ny motorstart efter ankomsten
      som mätarvärdet kan vara avläst efter – varken på raden med mätarökningen eller på någon
      tidigare rad efter ankomsten (``_started_after``, ``_engine_started_since``);
    * mätarvärdet är avläst senast vid ankomsten (bilens tidsstämpel högst ``CLOCK_SKEW``
      efter positionens) – eller, utan tidsstämpel, en ökning på högst ``LAG_KM`` medan
      väntan från före 1.5.0 ännu inte hade stängt rörelsen (då hade värdet hamnat i samma
      resa förut också) – eller resans mätarsträcka är kortare än fågelvägen, så att värdet
      vid ankomsten bevisligen var gammalt, och ökningen ryms inom omvägsgränsen utan att
      bilen har rapporterat en ny motorstart (``_raw_catches_up``);
    * resan som slutade med just den positionsrapporten finns, är inte makulerad och slutar
      på den mätarställning bilen hade före ökningen.

    Resans slutmätarställning och sträcka rättas, och rättningen syns i resans historik.
    Returnerar resan (även när värdet redan förts dit vid en tidigare körning), annars None:
    då gäller de vanliga reglerna.
    """
    if not step.odo_moved or step.pos_moved or step.eff.odo is None or prev.odo is None:
        return None
    arrived_at = prev.pos_ts
    if arrived_at is None or not _same_instant(step.eff.pos_ts, arrived_at):
        return None
    if _started_after(step.obs, step.eff.odo_ts, arrived_at) or _engine_started_since(
        db, vehicle.id, arrived_at, step.obs.fetched_at, step.eff.odo_ts
    ):
        return None  # bilen har startat igen: ökningen kan höra till nästa körning
    raw = _arrival_raw(db, vehicle.id, prev)
    if raw is None or raw.odometer_start is None or raw.odometer_end is None:
        return None
    odo_ts = step.eff.odo_ts
    if odo_ts is not None:
        read_at_arrival = odo_ts <= arrived_at + CLOCK_SKEW
    else:
        read_at_arrival = old_waiting and step.eff.odo - prev.odo <= LAG_KM
    if not (read_at_arrival or _raw_catches_up(raw, step, arrived_at, settings)):
        return None
    obs_id = str(step.obs.id)
    if obs_id in _observation_ids(raw):
        return raw  # redan förd till resan (omkörning)
    if abs(raw.odometer_end - prev.odo) > 1e-6:
        return None
    _correct_end_odometer(db, vehicle, user_id, raw, step, settings)
    return raw


def _correct_end_odometer(
    db: Session, vehicle: Vehicle, user_id: uuid.UUID, raw: RawTrip, step: Step, settings: dict[str, Any]
) -> None:
    """Rättar resans slutmätarställning och sträcka efter ett släpande mätarvärde."""
    before = {
        "odometer_end": raw.odometer_end, "distance_km": raw.distance_km, "distance_quality": raw.distance_quality,
    }
    min_delta = float(settings.get("min_odometer_delta_km", 1.0))
    raw.odometer_end = step.eff.odo
    flags = set(raw.quality_flags or [])
    odo_km = raw.odometer_end - raw.odometer_start  # type: ignore[operator]
    if odo_km >= min_delta:
        raw.distance_km, raw.distance_quality = round(odo_km, 1), DistanceQuality.ODOMETER
        flags.discard("distance_estimated")
        crow_m = (
            haversine_m(raw.start_lat, raw.start_lon, raw.end_lat, raw.end_lon)  # type: ignore[arg-type]
            if None not in (raw.start_lat, raw.start_lon, raw.end_lat, raw.end_lon) else None
        )
        if crow_m is not None and crow_m >= float(settings.get("position_threshold_m", 150)):
            # Samma kontroll av omväg som när resan skapas (_raw_from_leg).
            limit = float(settings.get("detour_factor", 1.8)) * (crow_m / 1000) + float(settings.get("detour_slack_km", 5.0))
            if raw.distance_km > limit:
                flags.add("detour_or_multiple_trips")
                raw.possible_missed_trips = True
                raw.is_complete = False
    raw.quality_flags = sorted(flags)
    raw.observation_ids = list(dict.fromkeys([*_observation_ids(raw), str(step.obs.id)]))
    raw.updated_by = "system"
    after = {
        "odometer_end": raw.odometer_end, "distance_km": raw.distance_km, "distance_quality": raw.distance_quality,
        "reason": "late_odometer", "observation_id": str(step.obs.id),
    }
    trip = _active_trip_for(db, raw.id)
    if trip is None:
        db.flush()
        audit.record(db, audit.SYSTEM, "trip.update", "raw_trip", raw.id, before=before, after=after, user_id=user_id)
        return
    parts = sorted((s for s in trip.segments if s.raw_trip_id == raw.id), key=lambda s: s.part)
    if len(parts) == 1:
        parts[0].distance_km = raw.distance_km
    elif parts:
        # Fördelad på delsträckor: det tillkomna läggs på den sista, så att summan av
        # delarna alltid är resans uppmätta sträcka.
        diff = raw.distance_km - sum(s.distance_km for s in parts)
        parts[-1].distance_km = round(parts[-1].distance_km + diff, 3)
    trip.updated_by = "system"
    seg.refresh_logical(db, trip)
    db.flush()
    audit.record(db, audit.SYSTEM, "trip.update", "logical_trip", trip.id, before=before, after=after, user_id=user_id)


def _attach_late_position(
    db: Session, vehicle: Vehicle, user_id: uuid.UUID, step: Step, prev: Eff, settings: dict[str, Any]
) -> RawTrip | None:
    """För en position som kommer efter att resan redan har stängts till den resan (1.5.0).

    Väntan från före 1.5.0 kunde stänga en rörelse innan positionen hade kommit (genvägen med
    avstängd motor räknade en tidigare ankomst i samma rörelse). Resan blev då en "rundresa"
    utan ny position, och positionen sedan en egen resa med sträcka uppskattad ur fågelvägen –
    samma kilometer två gånger. Nu förs positionen till den resan när det är säkert att det är
    samma ankomst:

    * bara positionen har flyttats (ny positionsrapport), mätarställningen är oförändrad och
      positionen är inte rapporterad efter en ny motorstart;
    * den senaste resan slutar på just den mätarställningen, saknar ankomst (flaggad
      ``round_trip_unobserved``) och har ingen senare resa efter sig;
    * positionens tidsstämpel ligger inom ``CLOCK_SKEW`` från resans sluttid (mätarvärdets
      tidsstämpel). Utan tidsstämplar gäller reglerna från före 1.5.0.

    Resans slutposition, slutplats och sluttid rättas, flaggorna räknas om och rättningen syns
    i resans historik.
    """
    if not step.pos_moved or step.odo_moved or prev.odo is None or step.eff.pos_ts is None:
        return None
    if prev.pos_ts is not None and step.eff.pos_ts <= prev.pos_ts:
        return None
    if step.obs.engine_running is True and (
        step.obs.engine_reported_at is None or step.obs.engine_reported_at < step.eff.pos_ts
    ):
        return None  # positionen är rapporterad efter en ny motorstart: kan vara nästa körning
    raw = db.execute(
        select(RawTrip)
        .where(
            RawTrip.vehicle_id == vehicle.id,
            RawTrip.voided_at.is_(None),
            RawTrip.import_batch_id.is_(None),
            RawTrip.source.in_((TripSource.VOLVO_API, TripSource.DEMO)),
            RawTrip.odometer_end.is_not(None),
        )
        .order_by(RawTrip.odometer_end.desc(), RawTrip.created_at.desc())
        .limit(1)
    ).scalars().first()
    if raw is None or raw.odometer_end is None or abs(raw.odometer_end - prev.odo) > 1e-6:
        return None
    if str(step.obs.id) in _observation_ids(raw):
        return raw  # redan förd till resan (omkörning)
    flags = set(raw.quality_flags or [])
    if "round_trip_unobserved" not in flags or raw.end_time_quality == TimeQuality.REPORTED:
        return None
    if raw.ended_at is None or abs((step.eff.pos_ts - raw.ended_at).total_seconds()) > CLOCK_SKEW.total_seconds():
        return None
    before = {"ended_at": raw.ended_at.isoformat(), "end_lat": raw.end_lat, "end_lon": raw.end_lon,
              "end_address": raw.end_address}
    demo = bool(vehicle.is_demo)
    end_loc = geocode.place_for_position(db, user_id=user_id, lat=step.eff.lat, lon=step.eff.lon, demo=demo)
    raw.end_lat, raw.end_lon = step.eff.lat, step.eff.lon
    raw.end_location_id = end_loc.id if end_loc else None
    raw.end_address = geocode.address_for(end_loc)
    raw.ended_at, raw.end_time_quality = step.eff.pos_ts, TimeQuality.REPORTED
    raw.position_source = PositionSource.API_POSITION
    flags -= {"round_trip_unobserved", "end_time_uncertain"}
    crow_m = (
        haversine_m(raw.start_lat, raw.start_lon, raw.end_lat, raw.end_lon)  # type: ignore[arg-type]
        if None not in (raw.start_lat, raw.start_lon) else None
    )
    if crow_m is not None and raw.distance_quality == DistanceQuality.ODOMETER:
        # Samma kontroll av omväg som när resan skapas (_raw_from_leg).
        limit = float(settings.get("detour_factor", 1.8)) * (crow_m / 1000) + float(settings.get("detour_slack_km", 5.0))
        if (raw.distance_km or 0) > limit:
            flags.add("detour_or_multiple_trips")
    raw.possible_missed_trips = bool(flags & {"detour_or_multiple_trips", "collection_gap"})
    raw.is_complete = not raw.possible_missed_trips and not flags & {"missing_odometer", "no_position"}
    if raw.start_latest is not None and raw.distance_km:
        raw.start_latest = min(raw.start_latest, raw.ended_at - timedelta(hours=raw.distance_km / MAX_AVG_SPEED_KMH))
        if raw.start_earliest is not None:
            raw.start_earliest = min(raw.start_earliest, raw.start_latest)
    raw.quality_flags = sorted(flags)
    raw.observation_ids = list(dict.fromkeys([*_observation_ids(raw), str(step.obs.id)]))
    raw.updated_by = "system"
    seg.set_raw_sort_time(raw)
    after = {"ended_at": raw.ended_at.isoformat(), "end_lat": raw.end_lat, "end_lon": raw.end_lon,
             "end_address": raw.end_address, "reason": "late_position", "observation_id": str(step.obs.id)}
    trip = _active_trip_for(db, raw.id)
    db.flush()
    if trip is None:
        audit.record(db, audit.SYSTEM, "trip.update", "raw_trip", raw.id, before=before, after=after, user_id=user_id)
        return raw
    trip.updated_by = "system"
    seg.refresh_logical(db, trip)
    db.flush()
    audit.record(db, audit.SYSTEM, "trip.update", "logical_trip", trip.id, before=before, after=after, user_id=user_id)
    return raw


def _old_movement(db: Session, vehicle_id: uuid.UUID, anchor: VehicleObservation, settings: dict[str, Any]) -> list[Step]:
    """Rörelsen som väntan från före 1.5.0 ännu inte hade stängt vid ankaret.

    Räknas fram ur de bearbetade avläsningarna, utan att något sparas: från den senaste
    avläsningen som setts oförändrad i minst ``LAG_SETTLE_FACTOR`` × ``settle_minutes`` (där hade
    den gamla väntan alltid stängt rörelsen) spelas avläsningarna upp fram till ankaret med
    samma regler som förut. Varje avläsning gav förut en ny bedömning först när nästa kom, och
    då med alla dess hämtningar – därför blir uppspelningen densamma som förloppet var.
    Ryms hela fordonets historik i de senaste ``OLD_REPLAY_ROWS`` avläsningarna börjar
    uppspelningen vid den första, som den gamla motorn. Tom lista när väntan inte hade någon
    öppen rörelse, eller när ingen sådan avläsning finns bland de senaste ``OLD_REPLAY_ROWS``.
    """
    settle = timedelta(minutes=float(settings.get("settle_minutes", 10)))
    rows = list(
        db.execute(
            select(VehicleObservation)
            .where(
                VehicleObservation.vehicle_id == vehicle_id,
                VehicleObservation.processed_at.is_not(None),
                VehicleObservation.fetched_at <= anchor.fetched_at,
            )
            .order_by(VehicleObservation.fetched_at.desc())
            .limit(OLD_REPLAY_ROWS)
        ).scalars()
    )
    start = next(
        (i for i, row in enumerate(rows) if row.last_seen_at - row.fetched_at >= LAG_SETTLE_FACTOR * settle), None
    )
    if start is None and rows and len(rows) < OLD_REPLAY_ROWS:
        start = len(rows) - 1  # hela historiken ryms: den gamla väntan började vid första avläsningen
    if start is None or start == 0:
        return []
    replay = list(reversed(rows[:start]))
    prev = _anchor_eff(db, vehicle_id, rows[start])
    steps: list[Step] = []
    for obs in replay:
        eff = Eff.carry(prev, obs)
        odo_moved, pos_moved, odo_back = _diff(prev, eff, settings)
        if odo_back:
            eff.odo, eff.odo_ts = prev.odo, prev.odo_ts
        step = Step(obs, eff, odo_moved, pos_moved)
        prev = eff
        if not steps and not step.changed:
            continue
        steps.append(step)
        if _settled(steps, settings):
            steps = []
    return steps


def question_hold_until(db: Session, trip: LogicalTrip, user_id: uuid.UUID) -> datetime | None:
    """När frågan till telefonen om en resa från bilen tidigast får skickas (1.5.0).

    En ankomst med kompletta uppgifter stängs direkt, men frågan ska inte komma tidigare än
    med väntan från före 1.5.0: när ankomsten först sågs plus ``settle_minutes``. Då hinner en
    fortsatt körning efter ett kort stopp slås ihop med resan först. Hade bilen rapporterat
    motorn avstängd vid ankomsten stängde väntan från före 1.5.0 resan redan vid nästa
    hämtning (genvägen i ``_settled``); då väntar frågan bara dit, som förut. None för resor
    som inte är rekonstruerade ur bilens avläsningar.
    """
    raws = seg.ordered_raws(trip)
    if not raws:
        return None
    last = raws[-1]
    if not _is_reconstructed(last) or last.end_lat is None or last.end_lon is None:
        return None
    ids: list[uuid.UUID] = []
    for value in _observation_ids(last):
        try:
            ids.append(uuid.UUID(value))
        except ValueError:
            continue
    if not ids:
        return None
    seen = [
        row
        for row in db.execute(
            select(
                VehicleObservation.fetched_at, VehicleObservation.latitude, VehicleObservation.longitude,
                VehicleObservation.engine_running, VehicleObservation.engine_reported_at,
                VehicleObservation.position_reported_at, VehicleObservation.odometer_reported_at,
            ).where(VehicleObservation.id.in_(ids))
        ).all()
        if row.latitude is not None and row.longitude is not None
        and abs(row.latitude - last.end_lat) < 1e-9 and abs(row.longitude - last.end_lon) < 1e-9
    ]
    if not seen:
        return None
    first = min(seen, key=lambda row: row.fetched_at)
    cfg = settings_svc.get_section(db, user_id, "volvo")
    reported = (first.position_reported_at, first.odometer_reported_at)
    if (
        first.engine_running is False
        and first.engine_reported_at is not None
        and None not in reported
        and first.engine_reported_at >= max(reported) - CLOCK_SKEW  # type: ignore[type-var]
    ):
        # Genvägen från före 1.5.0: motorn av efter ankomsten, stängd vid andra hämtningen.
        return first.fetched_at + timedelta(seconds=float(cfg.get("poll_interval_seconds", 300)))
    return first.fetched_at + timedelta(minutes=float(cfg.get("settle_minutes", 10)))


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
    # Lås fordonsraden så att två samtidiga insamlingar (två tick-uppgifter, eller tick och
    # manuell synk) inte bearbetar samma observationer och skapar samma resa två gånger.
    # Efter låset ser frågorna nedan det den andra transaktionen redan har committat.
    db.flush()
    db.execute(select(Vehicle.id).where(Vehicle.id == vehicle.id).with_for_update())
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
            _estimate_existing(db, vehicle, user_id, run_pipeline)
            return []
        anchor = pending.pop(0)
        anchor.processed_at = now
    anchor_eff = _anchor_eff(db, vehicle.id, anchor)
    prev_eff = anchor_eff
    steps: list[Step] = []
    created: list[RawTrip] = []
    corrected: list[RawTrip] = []
    # Väntan från före 1.5.0 följs vid sidan av (``old_steps``): rörelsen som den ännu inte
    # hade stängt. Det gör att (1) ingen resa med ankomst stängs senare än förut – stänger den
    # gamla väntan rörelsen stängs den pågående också (se ``forced`` nedan) – och (2) ett
    # släpande mätarvärde utan tidsstämpel förs till den stängda resan precis så länge den
    # gamla väntan hade tagit med det i samma resa.
    old_steps = _old_movement(db, vehicle.id, anchor, settings)

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
        old_closed = False
        if old_steps or step.changed:
            old_steps.append(step)
            if _settled(old_steps, settings):
                old_closed, old_steps = True, []
        if not steps and not step.changed:
            # Bilen står still: flytta fram ankaret.
            obs.processed_at = now
            anchor, anchor_eff, prev_eff = obs, eff, eff
            continue
        if not steps:
            late = _attach_late_odometer(
                db, vehicle, user_id, step, prev_eff, settings, old_waiting=bool(old_steps),
            ) or _attach_late_position(db, vehicle, user_id, step, prev_eff, settings)
            if late is not None:
                # Ett släpande mätarvärde för en resa som redan har stängts: resan rättas i
                # stället för att värdet blir en ny "resa" för samma körning.
                if late not in corrected and late not in created:
                    corrected.append(late)
                obs.processed_at = now
                anchor, anchor_eff, prev_eff = obs, eff, eff
                continue
        steps.append(step)
        prev_eff = eff
        # En ankomst med kompletta uppgifter stängs direkt (1.5.0); annars gäller väntan.
        # Stänger väntan från före 1.5.0 rörelsen stängs den här också, så att ingen resa skapas
        # senare än förut. (Kommer positionen efter att en sådan rörelse har stängts förs den
        # till resan, se ``_attach_late_position``, i stället för att bli en egen resa.)
        if not (old_closed or _arrival_complete(anchor_eff, steps, settings) or _settled(steps, settings)):
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
    if corrected and run_pipeline and not created:
        from app.services.trips import pipeline

        pipeline.after_trips_changed(
            db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM, apply_merge_rules=True, notify=True,
            origin="live", isolate_steps=True,
        )
        db.flush()
        return []
    if not created:
        _estimate_existing(db, vehicle, user_id, run_pipeline)
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

        # Resan är skapad: varje delsteg efter det körs för sig, så att ett fel i ett av dem
        # aldrig tar resan med sig (1.4.2, internal_errors.run_step).
        pipeline.after_trips_changed(
            db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM, apply_merge_rules=True, notify=True,
            origin="live", isolate_steps=True,
        )
    db.flush()
    return created


def _estimate_existing(db: Session, vehicle: Vehicle, user_id: uuid.UUID, run_pipeline: bool) -> None:
    """Resor med okänd starttid som ännu inte fått en uppskattning ur sträckan (t.ex. från
    före 1.2.1) får den vid nästa insamling, även när ingen ny resa hittas. Pipelinen gör
    uppskattningen först och räknar sedan om flaggor, klassning och frågor."""
    if not run_pipeline or not start_time.has_distance_candidates(db, vehicle.id):
        return
    from app.services.trips import pipeline

    pipeline.after_trips_changed(
        db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM, apply_merge_rules=True, notify=True, origin="live",
        isolate_steps=True,
    )
    db.flush()


# --------------------------------------------------------------- uppehåll i hämtningen (1.3.0)

_WEEKDAYS = ("mån", "tis", "ons", "tors", "fre", "lör", "sön")
GAP_ADVICE_LONG = "Importera Volvos körjournal för dagen under Import, eller dela upp resan i tjänst och privat."
GAP_ADVICE_SHORT = "Importera gärna Volvos körjournal."


@dataclass(frozen=True)
class CollectionGap:
    """När hämtningen från Volvo låg nere före en resa, och hur mycket mätaren ökade."""

    start: datetime
    end: datetime
    odometer_km: float | None


def parse_collection_gap(value: Any) -> CollectionGap | None:
    """``RawTrip.collection_gap`` som CollectionGap, eller None (saknas eller trasigt)."""
    if not isinstance(value, dict):
        return None
    try:
        start = datetime.fromisoformat(str(value.get("from")))
        end = datetime.fromisoformat(str(value.get("to")))
    except ValueError:
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    km = value.get("odometer_km")
    odo = float(km) if isinstance(km, (int, float)) and not isinstance(km, bool) else None
    return CollectionGap(start=start, end=end, odometer_km=odo)


def collection_gap_of(trip: LogicalTrip) -> CollectionGap | None:
    """Uppehållet före resan: första delresan (i ordning) som har sparade uppgifter."""
    for raw in seg.ordered_raws(trip):
        gap = parse_collection_gap(raw.collection_gap)
        if gap is not None:
            return gap
    return None


def _km_text(km: float) -> str:
    value = round(km, 1)
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}".replace(".", ",")


def _span(gap: CollectionGap, tz: Any, *, form: str) -> tuple[str, str]:
    """(från, till) som text i användarens tidszon."""
    a, b = gap.start.astimezone(tz), gap.end.astimezone(tz)
    if a.date() == b.date():
        if form == "long":
            return f"{_WEEKDAYS[a.weekday()]} {a.day}/{a.month} {a:%H:%M}", f"{b:%H:%M}"
        return f"{a:%H:%M}", f"{b:%H:%M}"
    if form == "long":
        return (f"{_WEEKDAYS[a.weekday()]} {a.day}/{a.month} {a:%H:%M}",
                f"{_WEEKDAYS[b.weekday()]} {b.day}/{b.month} {b:%H:%M}")
    return f"{_WEEKDAYS[a.weekday()]} {a:%H:%M}", f"{_WEEKDAYS[b.weekday()]} {b:%H:%M}"


def collection_gap_text(gap: CollectionGap | None, tz: Any, *, form: Literal["long", "short"]) -> str:
    """Varningen för ett uppehåll i hämtningen: när, hur många km och vad ägaren kan göra.

    ``long`` används i webben och i granskningsärendet, ``short`` i telefonen. Utan sparade
    uppgifter (resor från före 1.3.0) blir texten allmän.
    """
    if tz is None:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(settings_svc.default_timezone())
    if gap is None:
        if form == "long":
            return f"Hämtningen från Volvo hade ett uppehåll före resan, så den kan vara flera körningar. {GAP_ADVICE_LONG}"
        return "Kan vara flera körningar."
    lo, hi = _span(gap, tz, form=form)
    if form == "long":
        head = f"Hämtningen från Volvo låg nere mellan {lo} och {hi}."
        if gap.odometer_km is not None:
            middle = f"Under tiden ökade mätaren med {_km_text(gap.odometer_km)} km, så resan kan vara flera körningar."
        else:
            middle = "Resan kan vara flera körningar."
        return f"{head} {middle} {GAP_ADVICE_LONG}"
    if gap.odometer_km is not None:
        return (f"Volvo-hämtningen låg nere {lo}–{hi} och mätaren ökade {_km_text(gap.odometer_km)} km – "
                f"kan vara flera körningar. {GAP_ADVICE_SHORT}")
    return f"Volvo-hämtningen låg nere {lo}–{hi} – kan vara flera körningar. {GAP_ADVICE_SHORT}"


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


def _decision_summary(trip: LogicalTrip | None) -> dict[str, Any] | None:
    """Ägarens beslut på en resa: kategori och (bekräftat) ärende, och en text att visa."""
    if trip is None:
        return None
    purpose = trip.purpose.strip() if trip.purpose and trip.purpose_confirmed and trip.purpose.strip() else None
    if trip.category == Category.WORK:
        text = f"Arbete – {purpose}" if purpose else "Arbete"
    elif trip.category == Category.PRIVATE:
        text = "Privat"
    else:
        text = "Delad mellan arbete och privat" if trip.has_mixed_categories else "Ej avgjord"
    return {"category": trip.category, "purpose": purpose, "text": text}


def _previous_text(previous: dict[str, Any] | None) -> str:
    if previous is None:
        return ""
    return f" Ditt beslut var: {previous['text']}. Lägg in det på rätt resa nedan."


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
        # En öppen fråga i telefonen om den ersatta resan avbryts direkt (F3).
        try:
            from app.services.ha import notifier

            notifier.cancel_for_trip(db, trip.id, force=True)
        except Exception:  # notiser får aldrig stoppa avstämningen
            logger.exception("Kunde inte avbryta frågor om en ersatt resa")
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
        # Ägarens beslut i klartext, läst före makuleringen (F8).
        previous = _decision_summary(rec_trip) if had_decision else None
        untransferred = False
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
                untransferred = True
                problem = (
                    "Ditt beslut för en rekonstruerad resa kunde inte föras över automatiskt eftersom resan "
                    "ingick i en sammanslagning. Kontrollera den importerade resan."
                    + _previous_text(previous)
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
                untransferred = True
                problem = (
                    "En rekonstruerad resa med ditt beslut motsvarar flera eller delar av importerade resor. "
                    "Beslutet kunde inte föras över automatiskt."
                    + _previous_text(previous)
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
                    **(
                        {"previous_category": previous["category"], "previous_purpose": previous["purpose"]}
                        if previous is not None and untransferred
                        else {}
                    ),
                },
            )
            out["review_items"] += 1 if created else 0

    if out["superseded"]:
        audit.record(db, actor, "trip.reconciled_with_import", "vehicle", vehicle.id, after=dict(out), user_id=user_id)
    db.flush()
    return out
