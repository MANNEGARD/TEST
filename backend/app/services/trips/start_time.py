"""Starttid ur bilens egna avläsningar (byggkontraktet C.2 och D).

STUBBE (G0): ``estimate`` räknar ännu inte fram någon tid (som i dag: okänd starttid med
gränser). ``set_override`` sparar ägarens angivna starttid och ``display`` ger texten som
appen visar. S1b bygger algoritmen och kopplar in den i detekteringen och segmenten.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import LogicalTrip
from app.models.enums import TimeQuality
from app.services import audit

# Starttid ur trippmätaren (K1) är avstängd tills ett prov mot den riktiga bilen är gjort.
K1_ENABLED = False
START_MAX_WINDOW_MINUTES = 10


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
    """Ren funktion. (Stubbe: ingen tid räknas fram; gränserna behålls.)"""
    return StartEstimate(at=None, earliest=earliest, latest=latest, method="none", margin_min=None, basis={})


def set_override(db: Session, trip: LogicalTrip, value: datetime | None, *, actor: audit.Actor) -> LogicalTrip:
    """Sparar (eller tar bort) starttiden som ägaren angett. Gränserna prövas av anroparen.

    Hur den slår igenom på ``started_at`` sköts av ``segments.refresh_logical`` (S1b).
    """
    before = trip.start_time_override
    trip.start_time_override = value
    audit.record(
        db, actor, "trip.start_time_override", "logical_trip", trip.id,
        before={"start_time_override": before.isoformat() if before else None},
        after={"start_time_override": value.isoformat() if value else None},
    )
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
