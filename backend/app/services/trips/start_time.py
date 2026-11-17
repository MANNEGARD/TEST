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
precis som K2/K3, och grunden (fart, varifrån farten kommer, körtid) sparas. Felmarginalen
växer med spridningen i ägarens egna resor. En resa som FINNS I ett underlag som månadsskiftet
redan har skapat lämnas orörd, så att ett inlämnat underlag inte ändras i efterhand; en resa som
tillkommit efter att filen skapades (t.ex. när månadens fil skapats i förväg) får starttid som
vanligt (``month_close.protection``, samma regel för steget ur motorstarten och förbrukningen).

Bilens egen rapport om motorstarten (1.5.0, ``fill_from_engine``, körs före K4): bilen
rapporterar "motorn igång" direkt med egen tidsstämpel, även när mätarställning och position
bara rapporteras när tändningen slås av. Den SISTA övergången "stod still" → "motorn igång"
före ankomsten letas upp bland de sparade avläsningarna. Starttiden blir bilens tidsstämpel
för motorstarten (avrundad till hel minut) när den ligger mellan de två hämtningarna (± två
minuters klockskillnad), annars minuten mitt emellan hämtningarna när de ligger högst
``ENGINE_SHORT_WINDOW_MINUTES`` isär. Bilens tidsstämpel får kvalitet ``reported`` – bilens
tid, utan "≈" och "ca" (metod ``engine_start``); minuten mitt emellan är uträknad och får
kvalitet ``estimated`` med felmarginal, "≈" och "ca" (metod ``poll_midpoint``). Avläsningar där
motorstatusen inte kunde läsas räknas inte alls. Grunden sparas. Ger tiden en
orimligt hög snittfart (``plausible_speed_kmh``), eller är fönstret längre, blir den bara
senaste möjliga start och K4 uppskattar som förut. En fastställd starttid ändras aldrig av sig
själv; bara ägaren kan ange en annan.

Statusen "bilen används" (Volvos command-accessibility, ``VehicleObservation.car_in_use``)
räknas lika med "motorn igång": en avläsning visar att bilen används när motorn är igång ELLER
statusen är UNAVAILABLE med orsaken CAR_IN_USE. Laddhybriden kan starta på el, och då kan
"motorn igång" komma först en bit in i resan. Starttiden blir den tidigaste av bilens egna
tidsstämplar (motorstartens och statusens) inom fönstret; grunden anger då vilket besked som
gav tiden (``start_signal``: ``engine``, ``car_in_use`` eller ``engine_and_car_in_use``, och
``in_use_reported_at``). Det är inte prövat att den riktiga bilen svarar CAR_IN_USE under färd:
utan statusen blir allt som förut, och statusen AVAILABLE räknas aldrig som att bilen stod
still (det kräver fortfarande motorn avstängd). Metoden är ``engine_start`` även när statusen
gav tiden, så att den ligger fast och visas som bilens egen tid.

Tider som räknar med starttiden som känd (kalenderns avfärd, pendlingskedjan, AI-underlaget)
använder ``known_start``: en uppskattning ur sträckan räknas där som okänd, precis som
stilleståndet mellan resor (``merge.stop_gap``) redan bedöms med gränserna.
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
# Rimlig snittfart för en egen resa. Långsammare än 15 km/h i snitt är nästan alltid en
# sluttid som ligger efter själva körningen (t.ex. parkering i en importerad resa).
K4_MIN_SPEED, K4_MAX_SPEED = 15.0, 130.0
K4_MIN_MINUTES = 1.0
K4_MARGIN = {"route": (2.0, 0.25), "similar": (2.0, 0.25), "table": (3.0, 0.35)}  # (minst min, andel av körtiden)
# 1.5.0: starttid ur bilens egen rapport om motorstarten (fill_from_engine).
ENGINE_METHODS = ("engine_start", "poll_midpoint")  # fastställda: rörs aldrig igen av sig själv
ENGINE_CLOCK_SKEW = timedelta(minutes=2)  # som detection.CLOCK_SKEW
# "Kort" fönster mellan två hämtningar: täcker hämtning var 2:a–5:e minut med marginal för
# en fördröjd hämtning. Mitten ligger då högst 3 minuter från den verkliga starten.
ENGINE_SHORT_WINDOW_MINUTES = 6.0
ENGINE_SPEED_FACTOR = 2.0  # se plausible_speed_kmh
ENGINE_LOOKBACK = timedelta(days=14)  # resor så långt bakåt (från senaste avläsningen) prövas
ENGINE_NO_EARLIEST = timedelta(hours=24)  # avläsningar så långt före ankomsten när E saknas
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
    # Kvartilerna av de egna resornas fart (km/h): felmarginalen följer spridningen.
    low_kmh: float | None = None
    high_kmh: float | None = None


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
    wanted = ended_at - timedelta(minutes=minutes)
    at = _clamp_round(wanted, E, L)
    held = wanted > L or (E is not None and wanted < E)
    least, share = K4_MARGIN[speed.source]
    margin = max(least, share * minutes)
    if speed.low_kmh and speed.high_kmh:
        # Spretar de egna resorna blir marginalen så stor som kvartilerna säger.
        slow, fast = distance_km / speed.low_kmh * 60, distance_km / speed.high_kmh * 60
        margin = max(margin, slow - minutes, minutes - fast)
    if E is not None:
        # Starten ligger alltid inom [E, L]: marginalen sträcker sig aldrig längre.
        margin = min(margin, max(_minutes(at - E), _minutes(L - at), 1.0))
    margin = round(margin, 1)
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
        "held_to_window": held,
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
            return _own_speed(route, "route")
    similar = [v for _, _, d, v in history if abs(d - km) <= K4_SIMILAR_SHARE * km]
    if len(similar) >= K4_SIMILAR_MIN_TRIPS:
        return _own_speed(similar, "similar")
    return Speed(table_speed(km), "table", 0)


def _own_speed(values: list[float], source: Literal["route", "similar"]) -> Speed:
    """Medianen av de egna resornas fart, med kvartilerna för felmarginalen."""
    low, _, high = statistics.quantiles(values, n=4, method="inclusive")
    return Speed(round(statistics.median(values), 1), source, len(values), round(low, 1), round(high, 1))


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


def _protection(db: Session, vehicle_id: uuid.UUID) -> Any:
    """Resorna som finns i ett redan skapat underlag (``month_close.protection``)."""
    from app.services.export import month_close

    return month_close.protection(db, vehicle_id)


def _distance_plan(db: Session, vehicle_id: uuid.UUID) -> list[tuple[RawTrip, StartEstimate]]:
    """Resorna som ska få en uppskattning ur sträckan, med uppskattningen.

    En resa som finns i ett underlag som månadsskiftet redan har skapat (eller som skulle
    flytta till en annan månad med ett skapat underlag) lämnas orörd: ett underlag som kanske
    redan är inlämnat ändras inte i efterhand. En resa som tillkommit efter filen finns inte i den.
    """
    raws = list(db.execute(_distance_candidates_query(vehicle_id).order_by(RawTrip.ended_at)).scalars())
    if not raws:
        return []
    history = _known_speeds(db, vehicle_id)
    locks = _protection(db, vehicle_id)
    plan: list[tuple[RawTrip, StartEstimate]] = []
    for raw in raws:
        est = estimate_from_distance(
            ended_at=raw.ended_at, distance_km=raw.distance_km, earliest=raw.start_earliest,
            latest=raw.start_latest, speed=typical_speed(raw, history),
        )
        if est.at is None:
            continue
        if locks.blocks(raw, est.at):
            continue
        plan.append((raw, est))
    return plan


def has_distance_candidates(db: Session, vehicle_id: uuid.UUID) -> bool:
    """Finns det resor som efterbearbetningen kan ge en starttid (ur motorstarten eller ur
    sträckan)? Insamlingen kör då efterbearbetningen även när ingen ny resa har hittats."""
    if engine_plan(db, vehicle_id):
        return True
    if db.execute(_distance_candidates_query(vehicle_id).limit(1)).first() is None:
        return False
    return bool(_distance_plan(db, vehicle_id))


def fill_from_distance(db: Session, vehicle_id: uuid.UUID) -> int:
    """Ger resor från bilen med okänd starttid en uppskattning ur sträckan (K4). Idempotent.

    Rör bara delresornas starttid; en starttid som ägaren angett på journalresan gäller
    fortfarande före (``segments.refresh_logical``). Returnerar antalet uppskattade delresor.
    """
    plan = _distance_plan(db, vehicle_id)
    if not plan:
        return 0
    from app.services.trips import segments as seg

    filled: list[uuid.UUID] = []
    for raw, est in plan:
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


# ----------------------------------------------------------------------------- 1.5.0: ur motorstarten


@dataclass(frozen=True)
class EngineChange:
    """Vad steget ur motorstarten gör med en delresa.

    ``kind`` är ``set`` (starttiden fastställs, ``at`` och ``basis`` satta) eller ``narrow``
    (bara senaste möjliga start snävas in till ``latest``; K4 uppskattar sedan som förut).
    """

    raw: RawTrip
    kind: Literal["set", "narrow"]
    at: datetime | None
    latest: datetime | None
    basis: dict[str, Any]


def plausible_speed_kmh(distance_km: float) -> float:
    """Högsta snittfart (km/h) som en starttid ur motorstarten får ge för sträckan.

    Dubbla K4-tabellens vanliga snittfart, aldrig över detekteringens ``MAX_AVG_SPEED_KMH``:
    under 3 km 50, under 10 km 70, under 30 km 100 och därefter 130 km/h. En laddhybrid som
    startar på el kan rapportera "motorn igång" först en bit in i resan; då blir snittfarten
    från motorstarten orimligt hög, och tiden används bara som senaste möjliga start.
    """
    from app.services.volvo.detection import MAX_AVG_SPEED_KMH

    return min(MAX_AVG_SPEED_KMH, ENGINE_SPEED_FACTOR * table_speed(distance_km))


def _in_use(row: Any) -> bool:
    """Statusen "bilen används" (UNAVAILABLE med orsaken CAR_IN_USE, kolumnen ``car_in_use``).

    Bara ``True`` räknas. Statusen AVAILABLE (``False``) räknas inte som att bilen stod still
    när motorstatusen saknas: det är inte prövat att bilen svarar CAR_IN_USE under färd, så
    "stod still" kräver fortfarande att motorn rapporterades avstängd.
    """
    return getattr(row, "car_in_use", None) is True


def find_engine_start(
    rows: Sequence[Any], *, earliest: datetime | None, ended_at: datetime,
) -> tuple[Any, Any] | None:
    """(föregående avläsning, första avläsningen där bilen används) för den SISTA övergången
    "stod still" → "används" före ankomsten, eller ``None`` (ren funktion).

    ``rows`` är bilens avläsningar sorterade på ``fetched_at``. En avläsning visar att bilen
    används när motorn är igång eller statusen är "bilen används" (``car_in_use``, se
    ``_in_use``). Avläsningar hämtade efter ankomsten räknas inte, och inte heller en avslutande
    rad med motorn av som rapporterades vid ankomsten. Bilen ska ha använts i alla avläsningar
    från övergången fram till ankomsten (sågs den stå still emellan var det en annan körning),
    och övergången ska ligga efter resans tidigaste gräns.
    """
    # En avläsning där motorstatusen inte kunde läsas (None) och statusen inte säger "används"
    # räknas inte alls, så att ett enstaka fel mitt i resan inte blir en start.
    seen = [r for r in rows if r.fetched_at <= ended_at and (r.engine_running is not None or _in_use(r))]
    while seen and seen[-1].engine_running is False and (
        seen[-1].engine_reported_at is not None and seen[-1].engine_reported_at >= ended_at - ENGINE_CLOCK_SKEW
    ):
        seen.pop()  # ankomstraden
    i = len(seen)
    while i > 0 and (seen[i - 1].engine_running is True or _in_use(seen[i - 1])):
        i -= 1
    if i == len(seen) or i == 0:
        return None  # motorn gick inte vid ankomsten, eller övergången syns inte
    prev, first_on = seen[i - 1], seen[i]
    if earliest is not None and first_on.fetched_at < earliest:
        return None
    return prev, first_on


def engine_estimate(
    rows: Sequence[Any], *, earliest: datetime | None, latest: datetime | None, ended_at: datetime,
    distance_km: float | None,
) -> tuple[Literal["set", "narrow", "none"], datetime | None, datetime | None, dict[str, Any]]:
    """Starttiden ur övergången till "används" – motorn igång eller statusen "bilen används"
    (ren funktion).

    Ger ``("set", tid, None, grund)``, ``("narrow", None, ny senaste start, grund)`` eller
    ``("none", None, None, {})``. Se modulens docstring för reglerna.
    """
    found = find_engine_start(rows, earliest=earliest, ended_at=ended_at)
    if found is None:
        return "none", None, None, {}
    prev, first_on = found
    lo = prev.last_seen_at - ENGINE_CLOCK_SKEW
    hi = first_on.fetched_at + ENGINE_CLOCK_SKEW
    gap = _minutes(first_on.fetched_at - prev.last_seen_at)
    in_use = _in_use(first_on)
    # Motorstartens tidsstämpel gäller bara när avläsningen visar motorn igång: vid start på el
    # (bara statusen) är den tidsstämpeln förra avstängningen.
    engine_on = first_on.engine_running is True
    reported = first_on.engine_reported_at if engine_on or not in_use else None
    in_use_at = getattr(first_on, "accessibility_reported_at", None) if in_use else None
    basis: dict[str, Any] = {
        "polled_before": _iso(prev.last_seen_at),
        "polled_running": _iso(first_on.fetched_at),
        "engine_reported_at": _iso(reported),
        "window_minutes": round(gap, 1),
        "earliest": _iso(earliest),
        "trip_km": round(distance_km, 1) if distance_km else None,
    }
    # Bilens egna tidsstämplar inom fönstret mellan hämtningarna; den tidigaste är starten.
    candidates = [(t, name) for t, name in ((reported, "engine"), (in_use_at, "car_in_use"))
                  if t is not None and lo <= t <= hi]
    chosen = min(t for t, _ in candidates) if candidates else None
    if in_use:
        # Vilket besked som gav tiden (utan statusen förblir grunden som förut).
        signals = [name for t, name in candidates if t == chosen] or (
            ["engine", "car_in_use"] if engine_on else ["car_in_use"])
        basis.update(in_use_reported_at=_iso(in_use_at), start_signal="_and_".join(signals))
    if chosen is not None:
        method, raw_at = "engine_start", chosen
    elif gap <= ENGINE_SHORT_WINDOW_MINUTES:
        method, raw_at = "poll_midpoint", prev.last_seen_at + (first_on.fetched_at - prev.last_seen_at) / 2
    else:
        # Långt fönster: ingen starttid, men starten låg senast vid första avläsningen med motorn igång.
        new_latest = min(latest, first_on.fetched_at) if latest is not None else first_on.fetched_at
        basis.update(method="engine_window", latest=_iso(new_latest))
        return "narrow", None, new_latest, basis
    if raw_at >= ended_at or (earliest is not None and raw_at < earliest - ENGINE_CLOCK_SKEW):
        return "none", None, None, {}  # motsägelsefulla tider: hellre okänd än fel
    at = _clamp_round(raw_at, earliest, min(latest, ended_at) if latest is not None else ended_at)
    # Snittfarten räknas på den oavrundade tiden, så att avrundningen inte avgör rimligheten.
    speed = distance_km / (_minutes(ended_at - raw_at) / 60) if distance_km else None
    basis.update(method=method, avg_speed_kmh=round(speed, 1) if speed is not None else None)
    if method == "poll_midpoint":
        basis["margin_min"] = round(gap / 2, 1)  # uträknad: högst halva fönstret fel
    too_fast = speed is not None and speed > plausible_speed_kmh(distance_km or 0.0)
    # Senaste möjliga start (t.ex. den första mätarökningen hos en bil som rapporterar under färd):
    # en motorstart först då eller senare är för sen – bilen rullade redan.
    too_late = latest is not None and raw_at >= latest
    if too_fast or too_late:
        # Troligen en sen motorstart (laddhybrid på el): bara senaste möjliga start.
        new_latest = min(latest, raw_at) if latest is not None else raw_at
        basis.update(method="engine_late", latest=_iso(new_latest),
                     max_speed_kmh=plausible_speed_kmh(distance_km or 0.0))
        return "narrow", None, new_latest, basis
    return "set", at, None, basis


def _engine_candidates_query(vehicle_id: uuid.UUID, since: datetime):
    """Delresor från bilen med okänd eller uppskattad starttid (aldrig en fastställd)."""
    overridden = (
        select(TripSegment.raw_trip_id)
        .join(LogicalTrip, LogicalTrip.id == TripSegment.logical_trip_id)
        .where(LogicalTrip.start_time_override.is_not(None))
    )
    return select(RawTrip).where(
        RawTrip.vehicle_id == vehicle_id,
        RawTrip.source.in_(_LIVE),
        RawTrip.voided_at.is_(None),
        RawTrip.start_time_quality.in_((TimeQuality.BOUNDED, TimeQuality.UNKNOWN, TimeQuality.ESTIMATED)),
        RawTrip.ended_at.is_not(None),
        RawTrip.ended_at >= since,
        RawTrip.end_time_quality.in_(_ARRIVAL_KNOWN),
        RawTrip.id.not_in(overridden),
    )


def engine_plan(db: Session, vehicle_id: uuid.UUID) -> list[EngineChange]:
    """Delresorna som ska få en starttid ur motorstarten (eller en snävare senaste start).

    Bara ändringar som verkligen ändrar något kommer med, så att planen blir tom när steget
    redan har körts (insamlingen kör efterbearbetningen bara när planen inte är tom).
    En resa som finns i ett redan skapat underlag (eller som skulle flytta till en annan månad
    med ett skapat underlag) lämnas; en resa som tillkommit efter filen finns inte i den.
    """
    from app.models import VehicleObservation

    newest = db.execute(
        select(VehicleObservation.fetched_at).where(VehicleObservation.vehicle_id == vehicle_id)
        .order_by(VehicleObservation.fetched_at.desc()).limit(1)
    ).scalar_one_or_none()
    if newest is None:
        return []
    raws = list(db.execute(
        _engine_candidates_query(vehicle_id, newest - ENGINE_LOOKBACK).order_by(RawTrip.ended_at)
    ).scalars())
    if not raws:
        return []
    locks = _protection(db, vehicle_id)
    plan: list[EngineChange] = []
    for raw in raws:
        if isinstance(raw.start_estimate_basis, dict) and raw.start_estimate_basis.get("method") in ENGINE_METHODS:
            continue
        ended = raw.ended_at
        lower = (raw.start_earliest or ended - ENGINE_NO_EARLIEST) - ENGINE_CLOCK_SKEW
        rows = list(db.execute(
            select(VehicleObservation).where(
                VehicleObservation.vehicle_id == vehicle_id,
                VehicleObservation.last_seen_at >= lower,
                VehicleObservation.fetched_at <= ended,
            ).order_by(VehicleObservation.fetched_at)
        ).scalars())
        latest = raw.start_latest
        if latest is None and raw.start_time_quality == TimeQuality.ESTIMATED:
            latest = raw.started_at
        kind, at, new_latest, basis = engine_estimate(
            rows, earliest=raw.start_earliest, latest=latest, ended_at=ended, distance_km=raw.distance_km,
        )
        if kind == "none":
            continue
        if kind == "narrow":
            current = raw.start_latest
            if new_latest is None or (current is not None and new_latest >= current - timedelta(seconds=1)):
                continue
            if raw.start_earliest is not None and new_latest < raw.start_earliest:
                continue
        if locks.blocks(raw, at):
            continue
        plan.append(EngineChange(raw=raw, kind=kind, at=at, latest=new_latest, basis=basis))
    return plan


def fill_from_engine(db: Session, vehicle_id: uuid.UUID) -> int:
    """Fastställer starttiden ur bilens egen rapport om motorstarten (1.5.0). Idempotent.

    Körs före K4. En fastställd starttid (metod ``engine_start``, kvalitet ``reported``, eller
    ``poll_midpoint``, kvalitet ``estimated``) rörs sedan aldrig av steget, K4, omräkningar eller sammanslagningar;
    bara ägarens angivna starttid gäller före. Varje ändring antecknas i resans historik.
    Returnerar antalet delresor som fick en starttid.
    """
    plan = engine_plan(db, vehicle_id)
    if not plan:
        return 0
    from app.services.trips import segments as seg

    changed: dict[uuid.UUID, EngineChange] = {}
    filled = 0
    for change in plan:
        raw = change.raw
        if change.kind == "set":
            # Bilens egen tidsstämpel är bilens tid; minuten mitt emellan hämtningarna är uträknad.
            quality = TimeQuality.REPORTED if change.basis.get("method") == "engine_start" else TimeQuality.ESTIMATED
            raw.started_at, raw.start_time_quality = change.at, quality
            raw.start_estimate_basis = change.basis
            filled += 1
        else:
            raw.start_latest = change.latest
            if raw.start_time_quality == TimeQuality.ESTIMATED and raw.started_at is not None and (
                change.latest is not None and raw.started_at > change.latest
            ):
                # Uppskattningen ligger efter den nya gränsen: K4 räknar om den i samma körning.
                raw.started_at, raw.start_time_quality, raw.start_estimate_basis = None, TimeQuality.BOUNDED, None
        seg.set_raw_sort_time(raw)
        changed[raw.id] = change
    db.flush()
    links = db.execute(
        select(TripSegment.logical_trip_id, TripSegment.raw_trip_id).where(TripSegment.raw_trip_id.in_(list(changed)))
    ).all()
    for trip_id in {t for t, _ in links}:
        trip = seg.load_trip(db, trip_id)
        if trip is None:
            continue
        before = {"started_at": _iso(trip.started_at), "start_time_quality": trip.start_time_quality}
        seg.refresh_logical(db, trip)
        for raw_id in [r for t, r in links if t == trip_id]:
            change = changed[raw_id]
            audit.record(
                db, audit.SYSTEM, "trip.start_time_engine" if change.kind == "set" else "trip.start_latest_engine",
                "logical_trip", trip_id, before=before,
                after={"started_at": _iso(trip.started_at), "start_time_quality": trip.start_time_quality,
                       "raw_trip_id": str(raw_id), "basis": change.basis},
            )
    db.flush()
    return filled


def unknown_start_reasons(db: Session, raw: RawTrip, locks: Any, history: Any = None) -> list[str]:
    """Varför delresans starttid är okänd, som korta koder i stegens ordning (felsökningsfilen).

    Tom lista när starttiden är känd. Skriver ingenting. Koderna: ``imported``, ``voided``,
    ``owner_set`` (ägaren har angett starttiden på journalresan), ``closed_month`` (resan finns i
    ett skapat underlag, eller tiden skulle flytta den till en annan månad med ett skapat
    underlag), ``no_arrival``; steget ur motorstarten: ``no_readings``, ``too_old``, ``no_engine_start``,
    ``engine_window`` (för långt mellan hämtningarna), ``engine_late`` (orimligt sen motorstart);
    K4: ``no_odometer``, ``no_distance``, ``no_latest_start``, ``contradictory_bounds``,
    ``no_distance_estimate``; ``pending`` när steget skulle ge en tid vid nästa efterbearbetning.
    """
    from app.models import VehicleObservation

    if raw.started_at is not None and raw.start_time_quality not in (TimeQuality.BOUNDED, TimeQuality.UNKNOWN):
        return []
    if raw.source not in _LIVE:
        return ["imported"]
    if raw.voided_at is not None:
        return ["voided"]
    overridden = db.execute(
        select(LogicalTrip.id).join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
        .where(TripSegment.raw_trip_id == raw.id, LogicalTrip.start_time_override.is_not(None)).limit(1)
    ).first()
    if overridden is not None:
        return ["owner_set"]
    if locks.in_file(raw):
        return ["closed_month"]
    ended = raw.ended_at
    if ended is None or raw.end_time_quality not in _ARRIVAL_KNOWN:
        return ["no_arrival"]
    reasons: list[str] = []
    newest = db.execute(
        select(VehicleObservation.fetched_at).where(VehicleObservation.vehicle_id == raw.vehicle_id)
        .order_by(VehicleObservation.fetched_at.desc()).limit(1)
    ).scalar_one_or_none()
    if newest is None:
        reasons.append("no_readings")
    elif ended < newest - ENGINE_LOOKBACK:
        reasons.append("too_old")
    else:
        lower = (raw.start_earliest or ended - ENGINE_NO_EARLIEST) - ENGINE_CLOCK_SKEW
        rows = list(db.execute(
            select(VehicleObservation).where(
                VehicleObservation.vehicle_id == raw.vehicle_id,
                VehicleObservation.last_seen_at >= lower,
                VehicleObservation.fetched_at <= ended,
            ).order_by(VehicleObservation.fetched_at)
        ).scalars())
        kind, at, _, basis = engine_estimate(
            rows, earliest=raw.start_earliest, latest=raw.start_latest, ended_at=ended, distance_km=raw.distance_km,
        )
        if kind == "set":
            return ["closed_month" if locks.blocks(raw, at) else "pending"]
        reasons.append("no_engine_start" if kind == "none" else str(basis.get("method") or "engine_window"))
    if raw.odometer_start is None or raw.odometer_end is None:
        reasons.append("no_odometer")
    elif not raw.distance_km or raw.distance_km <= 0:
        reasons.append("no_distance")
    elif raw.start_latest is None:
        reasons.append("no_latest_start")
    elif raw.start_earliest is not None and raw.start_earliest > raw.start_latest:
        reasons.append("contradictory_bounds")
    else:
        speeds = history if history is not None else _known_speeds(db, raw.vehicle_id)
        est = estimate_from_distance(
            ended_at=ended, distance_km=raw.distance_km, earliest=raw.start_earliest, latest=raw.start_latest,
            speed=typical_speed(raw, speeds),
        )
        if est.at is None:
            reasons.append("no_distance_estimate")
        else:
            reasons.append("closed_month" if locks.blocks(raw, est.at) else "pending")
    return reasons


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


def from_distance(trip: LogicalTrip) -> bool:
    """Är resans starttid en uppskattning ur sträckan (K4)? En angiven starttid är det aldrig."""
    if getattr(trip, "start_time_override", None) is not None or trip.start_time_quality != TimeQuality.ESTIMATED:
        return False
    segments = sorted(trip.segments or [], key=lambda s: (s.seq, s.part))
    raw = segments[0].raw_trip if segments else None
    basis = raw.start_estimate_basis if raw is not None else None
    return isinstance(basis, dict) and basis.get("method") == "distance_speed"


def known_start(trip: LogicalTrip) -> datetime | None:
    """Starttiden för det som räknar med den som känd (kalenderns avfärd, pendlingskedjan,
    AI-underlaget). En uppskattning ur sträckan kan vara fel med en kvart eller mer och räknas
    därför som okänd där, så som stilleståndet mellan resor bedöms med gränserna."""
    return None if from_distance(trip) else trip.started_at


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
