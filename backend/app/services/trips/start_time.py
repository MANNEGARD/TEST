"""Starttid ur bilens egna avläsningar (byggkontraktet C.2 och D).

Volvos API ger ingen starttid. Den räknas fram ur bilens egna avläsningar när de räcker,
annars förblir den okänd och bara gränserna anges. En uträknad tid hittas aldrig på: den
märks ``estimated`` ("≈" i appen och "ca" i underlaget) och grunden sparas i
``RawTrip.start_estimate_basis``.

Algoritmen (D.2):

1. Gränserna ``E`` (tidigaste) och ``L`` (senaste möjliga start) kommer från detekteringen.
2. ``L`` snävas in av en rapport om att motorn går (inom ``[E, ended_at]``) och av första
   mätarökningen. Avstängd motor används aldrig – laddhybriden kan köra på el.
3. Kandidater i ordning: K1 trippmätaren (bara med ``K1_ENABLED``), K2 observerad fart
   (två avläsningar under färd, när ``L − E ≤ 15 min``) och K3 fönstrets mitt (när
   ``L − E ≤ 10 min``).
4. Tiden kläms till ``[E, L]`` och avrundas till hel minut.

Räcker inte K1–K3 uppskattas starttiden ur sträckan (K4, ``fill_from_distance``): bilen
rapporterar bara när resan är slut, men ankomsttiden och den uppmätta sträckan är kända.
Körtiden räknas ur sträckan och en snittfart – ägarens egna resor med kända tider (samma
rutt, annars liknande sträcka) när de är tillräckligt många, annars en enkel tabell – och
starttiden blir ankomsten minus körtiden, hållen inom ``[E, L]``. Den märks ``estimated``
precis som K2/K3, och grunden (fart, varifrån farten kommer, körtid) sparas.
"""

from __future__ import annotations

import math
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import LogicalTrip, RawTrip, TripSegment
from app.models.enums import LogicalState, TimeQuality, TripSource
from app.services import audit

# Starttid ur trippmätaren (K1) är avstängd tills ett prov mot den riktiga bilen visat att
# averageSpeedAutomatic räknas över förfluten tid och att trippmätaren nollställs vid start.
K1_ENABLED = False
START_MAX_WINDOW_MINUTES = 10  # K3: största fönster vars mitt får användas
K2_MAX_WINDOW_MINUTES = 15  # K2: största fönster för observerad fart
K1_MAX_KM_DIFF = 1.5  # trippmätaren får avvika högst så mycket från resans sträcka
K1_MIN_SPEED, K1_MAX_SPEED = 5.0, 130.0
K1_STATS_MAX_AGE = timedelta(minutes=2)  # statistiken ska vara rapporterad vid resans slut
K1_MARGIN_MIN = 5.0
K2_MIN_SPEED, K2_MAX_SPEED = 10.0, 130.0
K3_MAX_MARGIN_MIN = 5.0
# K4: starttid ur sträckan och en snittfart.
K4_TABLE: tuple[tuple[float, float], ...] = ((3, 25.0), (10, 35.0), (30, 50.0), (80, 65.0))  # (under km, km/h)
K4_TABLE_LONG = 80.0  # km/h för resor från 80 km
K4_ROUTE_MIN_TRIPS = 3  # egna resor på samma rutt som krävs
K4_SIMILAR_MIN_TRIPS = 5  # egna resor med liknande sträcka som krävs
K4_SIMILAR_SHARE = 0.3  # "liknande sträcka": inom ±30 %
K4_HISTORY_LIMIT = 500  # de senaste resorna med kända tider räcker
K4_MIN_SPEED, K4_MAX_SPEED = 5.0, 130.0  # rimlig snittfart för en egen resa
K4_MIN_MINUTES = 1.0
K4_MARGIN = {"route": (2.0, 0.25), "similar": (2.0, 0.25), "table": (3.0, 0.35)}  # (minst min, andel av körtiden)
_LIVE = (TripSource.VOLVO_API, TripSource.DEMO)
_KNOWN = (TimeQuality.EXACT, TimeQuality.REPORTED)
_ARRIVAL_KNOWN = (TimeQuality.EXACT, TimeQuality.REPORTED, TimeQuality.ESTIMATED)


@dataclass(frozen=True)
class ObsView:
    fetched_at: datetime
    odometer_km: float | None
    odometer_reported_at: datetime | None
    engine_running: bool | None
    engine_reported_at: datetime | None
    trip_meter_auto_km: float | None
    avg_speed_auto: float | None
    statistics_reported_at: datetime | None


@dataclass(frozen=True)
class StartEstimate:
    at: datetime | None
    earliest: datetime | None
    latest: datetime | None
    method: Literal["trip_meter", "odometer_speed", "window", "distance_speed", "none"]
    margin_min: float | None
    basis: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Speed:
    """Snittfarten för K4 och varifrån den kommer: ``route`` (egna resor på samma rutt),
    ``similar`` (egna resor med liknande sträcka) eller ``table`` (tabellen)."""

    kmh: float
    source: Literal["route", "similar", "table"]
    samples: int


def obs_view(obs: Any) -> ObsView:
    """ObsView ur en ``VehicleObservation`` (eller ett objekt med samma fält)."""
    return ObsView(
        fetched_at=obs.fetched_at,
        odometer_km=obs.odometer_km,
        odometer_reported_at=obs.odometer_reported_at,
        engine_running=obs.engine_running,
        engine_reported_at=obs.engine_reported_at,
        trip_meter_auto_km=getattr(obs, "trip_meter_auto_km", None),
        avg_speed_auto=getattr(obs, "avg_speed_auto", None),
        statistics_reported_at=getattr(obs, "statistics_reported_at", None),
    )


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _ceil_minute(value: datetime) -> datetime:
    floor = _floor_minute(value)
    return floor if floor == value else floor + timedelta(minutes=1)


def _clamp_round(s: datetime, earliest: datetime | None, latest: datetime) -> datetime:
    """Kläm till ``[E, L]`` och avrunda till hel minut utan att lämna intervallet."""
    if s > latest:
        s = latest
    if earliest is not None and s < earliest:
        s = earliest
    rounded = _floor_minute(s + timedelta(seconds=30))
    if rounded > latest:
        rounded = _floor_minute(latest)
    if earliest is not None and rounded < earliest:
        rounded = _ceil_minute(earliest)
    if rounded > latest or (earliest is not None and rounded < earliest):
        return s  # E och L inom samma minut: behåll den klämda tiden
    return rounded


def _none(earliest: datetime | None, latest: datetime | None) -> StartEstimate:
    return StartEstimate(at=None, earliest=earliest, latest=latest, method="none", margin_min=None, basis={})


def estimate(
    *,
    earliest: datetime | None,
    latest: datetime | None,
    start_odometer: float | None,
    ended_at: datetime | None,
    distance_km: float | None,
    steps: Sequence[ObsView],
    end: ObsView,
) -> StartEstimate:
    """Räknar fram starttiden ur bilens avläsningar (ren funktion, D.2).

    Ger ``method="none"`` och ``at=None`` när avläsningarna inte räcker; gränserna
    lämnas då oförändrade.
    """
    if latest is None or ended_at is None:
        return _none(earliest, latest)
    E, L = earliest, min(latest, ended_at)
    engine_at: datetime | None = None

    # 2. Snäva in L. Bara "motorn går" används; avstängd motor säger ingenting.
    views = [*steps, end]
    if E is not None:
        for v in views:
            t = v.engine_reported_at
            if v.engine_running is True and t is not None and E <= t <= ended_at and t < L:
                L, engine_at = t, t
    readings: list[tuple[datetime, float]] = []
    seen: set[datetime] = set()
    for v in views:
        t, d = v.odometer_reported_at, v.odometer_km
        if t is None or d is None or t in seen or t > ended_at:
            continue
        if E is not None and t < E:
            continue
        seen.add(t)
        readings.append((t, d))
    readings.sort()
    moved = [(t, d) for t, d in readings if start_odometer is not None and d > start_odometer]
    if moved and moved[0][0] < L:
        L = moved[0][0]
    if E is not None and L < E:
        return _none(earliest, latest)  # motsägelsefulla tider: hitta inte på något

    window = _minutes(L - E) if E is not None else None
    basis: dict[str, Any] = {
        "earliest": _iso(E),
        "latest": _iso(L),
        "trip_km": None,
        "avg_speed_kmh": None,
        "speed_kmh": None,
        "engine_running_at": _iso(engine_at),
        "window_minutes": round(window, 1) if window is not None else None,
    }
    s: datetime | None = None
    margin: float | None = None
    method: Literal["trip_meter", "odometer_speed", "window", "none"] = "none"

    # K1: trippmätaren och bilens snittfart (avstängd tills provet är gjort).
    if K1_ENABLED and distance_km is not None:
        km, speed, at = end.trip_meter_auto_km, end.avg_speed_auto, end.statistics_reported_at
        if (
            km is not None and speed is not None and at is not None
            and abs(km - distance_km) <= K1_MAX_KM_DIFF
            and K1_MIN_SPEED <= speed <= K1_MAX_SPEED
            and at >= ended_at - K1_STATS_MAX_AGE
        ):
            s = ended_at - timedelta(hours=km / speed)
            margin, method = K1_MARGIN_MIN, "trip_meter"
            basis.update(trip_km=km, avg_speed_kmh=speed)

    # K2: observerad fart mellan två avläsningar under färd.
    if s is None and E is not None and window is not None and window <= K2_MAX_WINDOW_MINUTES:
        for (t1, d1), (t2, d2) in zip(moved, moved[1:], strict=False):
            hours = (t2 - t1).total_seconds() / 3600
            if hours <= 0 or d2 <= d1:
                continue
            v = (d2 - d1) / hours
            if K2_MIN_SPEED <= v <= K2_MAX_SPEED:
                s = t1 - timedelta(hours=(d1 - start_odometer) / v)  # type: ignore[operator]
                margin, method = window / 2, "odometer_speed"
                basis.update(speed_kmh=round(v, 1))
                break

    # K3: fönstrets mitt när fönstret är snävt.
    if s is None and E is not None and window is not None and window <= START_MAX_WINDOW_MINUTES:
        s = E + (L - E) / 2
        margin, method = min(window / 2, K3_MAX_MARGIN_MIN), "window"

    if s is None or margin is None:
        return _none(earliest, latest)
    at = _clamp_round(s, E, L)
    if method == "odometer_speed" and E is not None:
        # Den verkliga starten kan ligga var som helst i [E, L]: marginalen täcker hela vägen.
        margin = max(_minutes(at - E), _minutes(L - at))
    margin = round(margin, 1)
    basis.update(method=method, margin_min=margin)
    return StartEstimate(at=at, earliest=E, latest=L, method=method, margin_min=margin, basis=basis)


# ----------------------------------------------------------------------------- K4: ur sträckan


def table_speed(distance_km: float) -> float:
    """En vanlig snittfart för en resa av den längden (km/h), när egna resor saknas."""
    for below, kmh in K4_TABLE:
        if distance_km < below:
            return kmh
    return K4_TABLE_LONG


def estimate_from_distance(
    *,
    ended_at: datetime | None,
    distance_km: float | None,
    earliest: datetime | None,
    latest: datetime | None,
    speed: Speed,
) -> StartEstimate:
    """Starttiden ur ankomsten, sträckan och snittfarten (ren funktion, K4).

    Körtiden är sträckan delad med farten (minst en minut). Tiden kläms till ``[E, L]`` och
    avrundas till hel minut; utan sträcka, ankomst eller med motsägelsefulla gränser ges
    ``method="none"``.
    """
    if ended_at is None or latest is None or not distance_km or distance_km <= 0 or speed.kmh <= 0:
        return _none(earliest, latest)
    E, L = earliest, min(latest, ended_at)
    if E is not None and L < E:
        return _none(earliest, latest)
    minutes = max(K4_MIN_MINUTES, distance_km / speed.kmh * 60)
    at = _clamp_round(ended_at - timedelta(minutes=minutes), E, L)
    least, share = K4_MARGIN[speed.source]
    margin = round(max(least, share * minutes), 1)
    window = _minutes(L - E) if E is not None else None
    basis: dict[str, Any] = {
        "method": "distance_speed",
        "earliest": _iso(E),
        "latest": _iso(L),
        "window_minutes": round(window, 1) if window is not None else None,
        "trip_km": round(distance_km, 1),
        "speed_kmh": round(speed.kmh, 1),
        "speed_source": speed.source,
        "samples": speed.samples,
        "duration_minutes": round(minutes, 1),
        "margin_min": margin,
    }
    return StartEstimate(at=at, earliest=E, latest=L, method="distance_speed", margin_min=margin, basis=basis)


def _known_speeds(db: Session, vehicle_id: uuid.UUID) -> list[tuple[uuid.UUID | None, uuid.UUID | None, float, float]]:
    """(start, mål, km, km/h) för ägarens senaste resor med kända tider.

    Bara resor där både start- och sluttid är kända (från bilen, importen eller ägaren själv)
    och som består av en enda delresa; uträknade och okända tider räknas aldrig. Körtiden
    tas från källan när den finns, annars ur start- och sluttiden.
    """
    rows = db.execute(
        select(LogicalTrip.start_location_id, LogicalTrip.end_location_id, LogicalTrip.distance_km,
               LogicalTrip.driving_seconds, LogicalTrip.started_at, LogicalTrip.ended_at)
        .where(
            LogicalTrip.vehicle_id == vehicle_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.segment_count == 1,
            LogicalTrip.started_at.is_not(None),
            LogicalTrip.ended_at.is_not(None),
            LogicalTrip.start_time_quality.in_(_KNOWN),
            LogicalTrip.end_time_quality.in_(_KNOWN),
            LogicalTrip.distance_km > 0,
        )
        .order_by(LogicalTrip.sort_time.desc())
        .limit(K4_HISTORY_LIMIT)
    ).all()
    out: list[tuple[uuid.UUID | None, uuid.UUID | None, float, float]] = []
    for start, end, km, driving, started, ended in rows:
        seconds = driving if driving and driving > 0 else (ended - started).total_seconds()
        if seconds <= 0:
            continue
        kmh = km / (seconds / 3600)
        if K4_MIN_SPEED <= kmh <= K4_MAX_SPEED:
            out.append((start, end, km, kmh))
    return out


def typical_speed(raw: RawTrip, history: Sequence[tuple[uuid.UUID | None, uuid.UUID | None, float, float]]) -> Speed:
    """Snittfarten för resan: samma rutt (åt båda hållen), annars liknande sträcka, annars tabellen."""
    km = raw.distance_km
    a, b = raw.start_location_id, raw.end_location_id
    if a is not None and b is not None:
        route = [v for s, e, _, v in history if {s, e} == {a, b} and s is not None and e is not None]
        if len(route) >= K4_ROUTE_MIN_TRIPS:
            return Speed(round(statistics.median(route), 1), "route", len(route))
    similar = [v for _, _, d, v in history if abs(d - km) <= K4_SIMILAR_SHARE * km]
    if len(similar) >= K4_SIMILAR_MIN_TRIPS:
        return Speed(round(statistics.median(similar), 1), "similar", len(similar))
    return Speed(table_speed(km), "table", 0)


def _distance_candidates_query(vehicle_id: uuid.UUID):
    """Resor från bilen som väntar på K4: starttiden är bara gränser, men ankomsten, sträckan
    och mätarställningen är kända och gränserna motsäger inte varandra."""
    return select(RawTrip).where(
        RawTrip.vehicle_id == vehicle_id,
        RawTrip.source.in_(_LIVE),
        RawTrip.voided_at.is_(None),
        RawTrip.started_at.is_(None),
        RawTrip.start_time_quality == TimeQuality.BOUNDED,
        RawTrip.ended_at.is_not(None),
        RawTrip.end_time_quality.in_(_ARRIVAL_KNOWN),
        RawTrip.odometer_start.is_not(None),
        RawTrip.odometer_end.is_not(None),
        RawTrip.distance_km > 0,
        RawTrip.start_latest.is_not(None),
        or_(RawTrip.start_earliest.is_(None), RawTrip.start_earliest <= RawTrip.start_latest),
    )


def has_distance_candidates(db: Session, vehicle_id: uuid.UUID) -> bool:
    return db.execute(_distance_candidates_query(vehicle_id).limit(1)).first() is not None


def fill_from_distance(db: Session, vehicle_id: uuid.UUID) -> int:
    """Ger resor från bilen med okänd starttid en uppskattning ur sträckan (K4). Idempotent.

    Rör bara delresornas starttid; en starttid som ägaren angett på journalresan gäller
    fortfarande före (``segments.refresh_logical``). Returnerar antalet uppskattade delresor.
    """
    raws = list(db.execute(_distance_candidates_query(vehicle_id).order_by(RawTrip.ended_at)).scalars())
    if not raws:
        return 0
    from app.services.trips import segments as seg

    history = _known_speeds(db, vehicle_id)
    filled: list[uuid.UUID] = []
    for raw in raws:
        est = estimate_from_distance(
            ended_at=raw.ended_at, distance_km=raw.distance_km, earliest=raw.start_earliest,
            latest=raw.start_latest, speed=typical_speed(raw, history),
        )
        if est.at is None:
            continue
        raw.started_at, raw.start_time_quality, raw.start_estimate_basis = est.at, TimeQuality.ESTIMATED, est.basis
        raw.start_earliest, raw.start_latest = est.earliest, est.latest
        seg.set_raw_sort_time(raw)
        filled.append(raw.id)
    db.flush()
    if filled:
        trip_ids = set(
            db.execute(select(TripSegment.logical_trip_id).where(TripSegment.raw_trip_id.in_(filled))).scalars()
        )
        for trip_id in trip_ids:
            trip = seg.load_trip(db, trip_id)
            if trip is not None:
                seg.refresh_logical(db, trip)
        db.flush()
    return len(filled)


def set_override(db: Session, trip: LogicalTrip, value: datetime | None, *, actor: audit.Actor) -> LogicalTrip:
    """Sparar (eller tar bort) starttiden som ägaren angett. Gränserna prövas av anroparen.

    ``segments.refresh_logical`` gör den till resans starttid (kvalitet ``exact``); utan
    angiven tid räknas starttiden om ur delresorna.
    """
    from app.services.trips import segments as seg

    before = trip.start_time_override
    trip.start_time_override = value
    audit.record(
        db, actor, "trip.start_time_override", "logical_trip", trip.id,
        before={"start_time_override": before.isoformat() if before else None},
        after={"start_time_override": value.isoformat() if value else None},
    )
    if trip.segments:
        seg.refresh_logical(db, trip)
    db.flush()
    return trip


def display(trip: LogicalTrip, tz: tzinfo) -> str:
    """"≈ 07:35" för uträknad tid, "07:35" för känd tid och "" när starttiden är okänd."""
    override = getattr(trip, "start_time_override", None)
    if override is not None:
        return override.astimezone(tz).strftime("%H:%M")
    if trip.started_at is None or trip.start_time_quality in (TimeQuality.BOUNDED, TimeQuality.UNKNOWN):
        return ""
    text = trip.started_at.astimezone(tz).strftime("%H:%M")
    return f"≈ {text}" if trip.start_time_quality == TimeQuality.ESTIMATED else text


def margin_text(basis: dict[str, Any] | None) -> str:
    """"±4 min" ur grunden, eller tom text."""
    margin = (basis or {}).get("margin_min") if isinstance(basis, dict) else None
    if not isinstance(margin, int | float) or not math.isfinite(margin):
        return ""
    return f"±{max(1, round(margin))} min"
