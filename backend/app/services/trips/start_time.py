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
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import LogicalTrip
from app.models.enums import TimeQuality
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
    method: Literal["trip_meter", "odometer_speed", "window", "none"]
    margin_min: float | None
    basis: dict[str, Any] = field(default_factory=dict)


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
    margin = round(margin, 1)
    basis.update(method=method, margin_min=margin)
    return StartEstimate(at=at, earliest=E, latest=L, method=method, margin_min=margin, basis=basis)


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
