"""Kalendern runt en resa till en annan ort: hela vistelsen och de närmaste dagarna (1.3.0).

* ``stay_events``: för utresan till en ort där ägaren blir kvar (första resan hemifrån i en
  tjänsteresa, eller hemifrån till en ort mer än 50 km bort) och för hemresan därifrån –
  bokningarna under hela vistelsen, märkta med om de ligger på eller nära resmålet.
* ``ahead_events``: resa dagen före (ägarens beslut J) – en utresa utan egen bokning samma dag
  bedöms mot bokningarna de två följande dagarna på resmålets ort; hemresan mot de två dagarna
  före avresan på orten man lämnar.

Bara läsning. Bokningarnas text är data, aldrig instruktioner; privata bokningar och
onlinemöten används aldrig till ärendet.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CalendarEvent, KnownLocation, LogicalTrip
from app.models.enums import LocationType, LogicalState, TripSource
from app.services.calendar import calendar_text
from app.services.places.normalize import fold, haversine_m, parse_address, valid_coordinates

FAR_KM = 50.0
STAY_MAX_DAYS = 7
STAY_MAX_EVENTS = 40
AHEAD_DAYS = 2
NEAR_M = 30_000
_PRIVATE = ("personal", "private", "confidential")
_GENERIC_NAME_WORDS = {"kontoret", "kontor", "office", "hotell", "hotel", "kund", "lager", "hemmet"}


@dataclass
class StayContext:
    place: str
    start: date
    end: date | None  # hemresans dag när den är känd
    basis: str  # "hemresa känd" | "veckans återstående dagar"
    direction: str  # "out" | "home"
    events: list[tuple[CalendarEvent, bool | None]] = field(default_factory=list)  # (bokning, near_destination)

    @property
    def near(self) -> list[CalendarEvent]:
        return [ev for ev, near in self.events if near and not is_private(ev) and not is_online(ev)]


@dataclass
class AheadContext:
    place: str
    days: list[date]
    direction: str  # "out" | "home"
    events: list[CalendarEvent] = field(default_factory=list)

    @property
    def usable(self) -> list[CalendarEvent]:
        return [ev for ev in self.events if not is_private(ev) and not is_online(ev)]


def is_private(ev: CalendarEvent) -> bool:
    return (ev.sensitivity or "").lower() in _PRIVATE


def is_online(ev: CalendarEvent) -> bool:
    from app.services.calendar import matching

    physical = bool(ev.location_name or ev.location_address) and not matching._looks_online(ev)
    return not physical and (matching._is_online(ev) or calendar_text.looks_online(ev.body_preview))


def _is_home(loc: KnownLocation | None) -> bool:
    return loc is not None and loc.confirmed and loc.type == LocationType.HOME


def _city(loc: KnownLocation | None, address: str | None) -> str | None:
    return (loc.city if loc is not None and loc.city else None) or parse_address(address).city


def _local_day(when: datetime | None, tz: Any) -> date | None:
    return when.astimezone(tz).date() if when is not None else None


def direction(trip: LogicalTrip, origin: KnownLocation | None, destination: KnownLocation | None) -> str | None:
    """"out" för en utresa hemifrån till en annan ort, "home" för hemresan, annars None."""
    far = (trip.distance_km or 0) > FAR_KM
    if _is_home(origin) and not _is_home(destination) and (far or trip.journey_id is not None):
        return "out"
    if _is_home(destination) and not _is_home(origin) and (far or trip.journey_id is not None):
        return "home"
    return None


def _events_between(db: Session, user_id: uuid.UUID, is_demo: bool, lo: datetime, hi: datetime) -> list[CalendarEvent]:
    return list(db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == user_id,
            CalendarEvent.is_demo.is_(is_demo),
            CalendarEvent.removed_at.is_(None),
            CalendarEvent.is_cancelled.is_(False),
            CalendarEvent.is_all_day.is_(False),
            CalendarEvent.end_at >= lo,
            CalendarEvent.start_at < hi,
        ).order_by(CalendarEvent.start_at).limit(STAY_MAX_EVENTS * 3)
    ).scalars())


def _day_start(day: date, tz: Any) -> datetime:
    return datetime.combine(day, time.min, tz)


def mentions_place(
    ev: CalendarEvent, place: KnownLocation | None, city: str | None, abbreviations: dict[str, str],
    work_domain: str | None,
) -> bool:
    """Nämner bokningen resmålets ort, platsens namn/företag eller en deltagardomän som stämmer?"""
    from app.services.calendar import matching

    tokens = matching._tokens(
        " ".join([ev.subject or "", ev.location_name or "", ev.location_address or "", ev.location_city or "",
                  calendar_text.clean_body(ev.body_preview) or ""]),
        abbreviations,
    )
    if city and fold(city) in tokens:
        return True
    if place is not None and place.type not in (LocationType.HOME, LocationType.PRIVATE):
        names = matching._tokens(f"{place.name or ''} {place.company or ''}", abbreviations)
        distinctive = names - _GENERIC_NAME_WORDS - ({fold(city)} if city else set())
        if distinctive and distinctive <= tokens:
            return True
        for domain in calendar_text.external_domains(ev.attendees, work_domain):
            if calendar_text.domain_matches(domain, place.company, place.name):
                return True
    return False


def _near(
    ev: CalendarEvent, place: KnownLocation | None, city: str | None, lat: float | None, lon: float | None,
    abbreviations: dict[str, str], work_domain: str | None, days_known: bool,
) -> bool | None:
    if is_private(ev):
        return None
    if valid_coordinates(ev.latitude, ev.longitude) and valid_coordinates(lat, lon):
        return haversine_m(ev.latitude, ev.longitude, lat, lon) <= NEAR_M  # type: ignore[arg-type]
    if mentions_place(ev, place, city, abbreviations, work_domain):
        return True
    if is_online(ev):
        return None
    if ev.location_name or ev.location_address or ev.location_city:
        return False  # en fysisk plats som inte är resmålet
    return True if days_known else None


def _next_home_trip(db: Session, trip: LogicalTrip, homes: set[uuid.UUID], until: datetime) -> LogicalTrip | None:
    if trip.sort_time is None or not homes:
        return None
    return db.execute(
        select(LogicalTrip).where(
            LogicalTrip.vehicle_id == trip.vehicle_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.source != TripSource.GAP_FILL,
            LogicalTrip.sort_time > trip.sort_time,
            LogicalTrip.sort_time <= until,
            LogicalTrip.end_location_id.in_(homes),
        ).order_by(LogicalTrip.sort_time.asc()).limit(1)
    ).scalar_one_or_none()


def _previous_departure(db: Session, trip: LogicalTrip, homes: set[uuid.UUID], since: datetime) -> LogicalTrip | None:
    if trip.sort_time is None or not homes:
        return None
    return db.execute(
        select(LogicalTrip).where(
            LogicalTrip.vehicle_id == trip.vehicle_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.source != TripSource.GAP_FILL,
            LogicalTrip.sort_time < trip.sort_time,
            LogicalTrip.sort_time >= since,
            LogicalTrip.start_location_id.in_(homes),
        ).order_by(LogicalTrip.sort_time.desc()).limit(1)
    ).scalar_one_or_none()


def _homes(db: Session, user_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.execute(
        select(KnownLocation.id).where(
            KnownLocation.owner_user_id == user_id, KnownLocation.confirmed.is_(True),
            KnownLocation.type == LocationType.HOME,
        )
    ).scalars())


def _target(ctx: Any, way: str) -> tuple[KnownLocation | None, str | None, float | None, float | None]:
    """(plats, ort, lat, lon) för orten där ägaren vistas."""
    loc = ctx.destination if way == "out" else ctx.origin
    address = ctx.trip.end_address if way == "out" else ctx.trip.start_address
    lat = loc.latitude if loc is not None else None
    lon = loc.longitude if loc is not None else None
    return loc, _city(loc, address), lat, lon


def stay_events(db: Session, trip: LogicalTrip, ctx: Any, *, is_demo: bool, work_domain: str | None) -> StayContext | None:
    """Bokningarna under hela vistelsen för ut- och hemresan (se modulens beskrivning)."""
    way = direction(trip, ctx.origin, ctx.destination)
    if way is None or trip.sort_time is None:
        return None
    place, city, lat, lon = _target(ctx, way)
    label = city or (place.name if place is not None else None)
    if not label:
        return None
    tz = ctx.tz
    homes = _homes(db, ctx.user_id)
    when = trip.ended_at or trip.sort_time
    if way == "out":
        start_day = _local_day(when, tz)
        back = _next_home_trip(db, trip, homes, when + timedelta(days=STAY_MAX_DAYS))  # type: ignore[operator]
        end_day = _local_day(back.started_at or back.sort_time, tz) if back is not None else None
        basis = "hemresa känd" if back is not None else "veckans återstående dagar"
        last_day = end_day or (start_day + timedelta(days=STAY_MAX_DAYS - 1))  # type: ignore[operator]
    else:
        last_day = _local_day(trip.started_at or trip.sort_time, tz)
        out = _previous_departure(db, trip, homes, trip.sort_time - timedelta(days=STAY_MAX_DAYS))
        start_day = _local_day(out.ended_at or out.sort_time, tz) if out is not None else None
        basis = "hemresa känd" if out is not None else "veckans återstående dagar"
        if start_day is None:
            start_day = last_day - timedelta(days=STAY_MAX_DAYS - 1)  # type: ignore[operator]
        end_day = last_day
    if start_day is None or last_day is None:
        return None
    if (last_day - start_day).days >= STAY_MAX_DAYS:
        last_day = start_day + timedelta(days=STAY_MAX_DAYS - 1)
    days_known = basis == "hemresa känd"
    events = _events_between(db, ctx.user_id, is_demo, _day_start(start_day, tz),
                             _day_start(last_day + timedelta(days=1), tz))
    marked = [
        (ev, _near(ev, place, city, lat, lon, ctx.abbreviations, work_domain, days_known))
        for ev in events[:STAY_MAX_EVENTS]
    ]
    return StayContext(place=label, start=start_day, end=end_day, basis=basis, direction=way, events=marked)


def ahead_events(db: Session, trip: LogicalTrip, ctx: Any, *, is_demo: bool, work_domain: str | None) -> AheadContext | None:
    """Resa dagen före (beslut J): bokningar på orten de två följande (eller föregående) dagarna."""
    way = direction(trip, ctx.origin, ctx.destination)
    if way is None:
        return None
    place, city, _lat, _lon = _target(ctx, way)
    label = city or (place.name if place is not None else None)
    if not label:
        return None
    tz = ctx.tz
    if way == "out":
        base = _local_day(trip.ended_at or trip.sort_time, tz)
        if base is None:
            return None
        days = [base + timedelta(days=i) for i in range(1, AHEAD_DAYS + 1)]
    else:
        base = _local_day(trip.started_at or trip.sort_time, tz)
        if base is None:
            return None
        days = [base - timedelta(days=i) for i in range(AHEAD_DAYS, 0, -1)]
    events = _events_between(db, ctx.user_id, is_demo, _day_start(days[0], tz), _day_start(days[-1] + timedelta(days=1), tz))
    hits = [ev for ev in events if mentions_place(ev, place, city, ctx.abbreviations, work_domain)]
    if not any(not is_private(ev) and not is_online(ev) for ev in hits):
        return None
    return AheadContext(place=label, days=days, direction=way, events=hits[:STAY_MAX_EVENTS])


__all__ = ["AheadContext", "StayContext", "ahead_events", "direction", "is_online", "is_private", "mentions_place",
           "stay_events"]
