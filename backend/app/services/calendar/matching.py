"""Matchning mellan resor och kalenderhändelser.

En bokning matchas aldrig bara för att den ligger samma dag: det krävs närhet i tid
till resans ankomst (eller avfärd) OCH att platsen inte talar emot. Kalendern ger
kontext, inte bevis. En fysisk bokning på annan ort samtidigt som resan flaggas som
konflikt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CalendarEvent, CalendarMatch, KnownLocation, LogicalTrip, Vehicle
from app.models.enums import LocationType, LogicalState, ReviewKind, TripSource
from app.services import settings as settings_svc
from app.services.classify import textnorm
from app.services.places.normalize import fold, haversine_m, parse_address, valid_coordinates
from app.services.trips import review
from app.services.trips import segments as seg

DEPARTURE_BEFORE_END_MIN = 30
DEPARTURE_AFTER_END_MIN = 120
CONFLICT_MIN_TIME_SCORE = 0.6
CONFLICT_DISTANCE_M = 30_000


@dataclass
class Candidate:
    event: CalendarEvent
    relation: str  # arrival | during | departure
    time_score: float
    geo_score: float
    geo_basis: str
    is_conflict: bool

    @property
    def score(self) -> float:
        if self.is_conflict:
            return 0.0
        return round(self.time_score * (0.35 + 0.65 * self.geo_score), 3)


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def time_relation(
    trip_start: datetime | None, trip_end: datetime | None, ev: CalendarEvent, *, before_min: float, after_min: float
) -> tuple[str, float] | None:
    """Hur resan förhåller sig till bokningen i tid, eller None om den inte gör det."""
    best: tuple[str, float] | None = None
    if trip_end is not None:
        delta = (trip_end - ev.start_at).total_seconds() / 60  # < 0 = ankomst före mötets start
        if -before_min <= delta <= after_min:
            if -30 <= delta <= 10:
                score = 1.0
            elif delta < -30:
                score = _lerp(delta, -before_min, -30, 0.3, 1.0)
            else:
                score = _lerp(delta, 10, after_min, 1.0, 0.3)
            best = ("arrival", round(score, 3))
        elif ev.start_at < trip_end < ev.end_at:
            best = ("during", 0.5)
    if trip_start is not None:
        delta = (trip_start - ev.end_at).total_seconds() / 60  # > 0 = avfärd efter mötets slut
        if -DEPARTURE_BEFORE_END_MIN <= delta <= DEPARTURE_AFTER_END_MIN:
            score = 1.0 if -10 <= delta <= 30 else (
                _lerp(delta, -DEPARTURE_BEFORE_END_MIN, -10, 0.5, 1.0) if delta < -10
                else _lerp(delta, 30, DEPARTURE_AFTER_END_MIN, 1.0, 0.3)
            )
            cand = ("departure", round(score * 0.85, 3))  # avfärd väger något lättare än ankomst
            if best is None or cand[1] > best[1]:
                best = cand
    return best


def _tokens(text: str | None, abbreviations: dict[str, str]) -> set[str]:
    expanded = textnorm.expand_abbreviations(text or "", abbreviations)
    return {fold(t) for t in textnorm.tokens(expanded) if len(t) >= 3}


def geo_match(
    ev: CalendarEvent,
    place: KnownLocation | None,
    address: str | None,
    lat: float | None,
    lon: float | None,
    abbreviations: dict[str, str],
) -> tuple[float, str, bool]:
    """Returnerar (poäng 0–1, grund, konflikt)."""
    plat = place.latitude if place is not None and place.latitude is not None else lat
    plon = place.longitude if place is not None and place.longitude is not None else lon
    if valid_coordinates(ev.latitude, ev.longitude) and valid_coordinates(plat, plon):
        d = haversine_m(ev.latitude, ev.longitude, plat, plon)  # type: ignore[arg-type]
        if d <= 300:
            return 1.0, "coordinates", False
        if d <= 2000:
            return 0.7, "coordinates_near", False
        if d <= 10_000:
            return 0.3, "coordinates_same_area", False
        return 0.0, "coordinates_far", d >= CONFLICT_DISTANCE_M and not _is_online(ev)

    ev_text_tokens = _tokens(f"{ev.location_name or ''} {ev.location_address or ''}", abbreviations)
    subject_tokens = _tokens(ev.subject, abbreviations)
    parsed = parse_address(address or (place.address if place else None))
    place_city = fold((place.city if place else None) or parsed.city or "")
    ev_city = fold(ev.location_city or "")

    if place is not None and place.confirmed and place.type not in (LocationType.HOME, LocationType.PRIVATE):
        names = _tokens(f"{place.name or ''} {place.company or ''}", abbreviations) - {place_city}
        generic = {"kontoret", "kontor", "office", "hotell", "hotel", "kund", "lager"}
        distinctive = names - generic
        if distinctive and (distinctive <= ev_text_tokens or distinctive <= subject_tokens or
                            len(distinctive & (ev_text_tokens | subject_tokens)) >= max(1, len(distinctive) - 1)):
            return 0.9, "place_name", False
    if parsed.street and ev.location_address:
        street = fold(parsed.street)
        if street and street in fold(ev.location_address) and (not place_city or place_city in fold(ev.location_address) or not ev_city or ev_city == place_city):
            return 0.9, "address", False
    if place_city and (place_city == ev_city or place_city in ev_text_tokens or place_city in subject_tokens):
        return 0.6, "city", False

    has_physical = bool(ev.location_name or ev.location_address) and not _looks_online(ev)
    if has_physical:
        # Bokningen har en fysisk plats som inte liknar resans mål. Ett onlinemöte (t.ex. Teams
        # med bokat rum) kan följas var som helst ifrån och är därför ingen konflikt.
        conflict = bool(ev_city and place_city and ev_city != place_city) and not _is_online(ev)
        return 0.0, "different_place", conflict
    if place is not None and place.confirmed and place.type in (LocationType.HOME, LocationType.PRIVATE):
        return 0.0, "no_location", False  # bokning utan platsstöd matchas aldrig mot bostaden eller privat plats
    if _is_online(ev):
        return 0.15, "online", False
    return 0.2, "no_location", False


def _is_online(ev: CalendarEvent) -> bool:
    return bool(ev.is_online or ev.online_provider) or _looks_online(ev)


def _looks_online(ev: CalendarEvent) -> bool:
    text = fold(f"{ev.location_name or ''}")
    return any(k in text for k in ("teams", "zoom", "meet.google", "http://", "https://", "skype", "webex", "online"))


def candidates_for_trip(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    is_demo: bool,
    cal: dict[str, Any],
    abbreviations: dict[str, str],
) -> list[Candidate]:
    start, end = trip.started_at, trip.ended_at
    if start is None and end is None:
        return []
    before, after = float(cal["arrival_window_before_minutes"]), float(cal["arrival_window_after_minutes"])
    lo = (start or end) - timedelta(minutes=max(after, DEPARTURE_AFTER_END_MIN) + 24 * 60)  # type: ignore[operator]
    hi = (end or start) + timedelta(minutes=before + 60)  # type: ignore[operator]
    events = db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == user_id,
            CalendarEvent.is_demo.is_(is_demo),
            CalendarEvent.removed_at.is_(None),
            CalendarEvent.is_cancelled.is_(False),
            CalendarEvent.end_at >= lo,
            CalendarEvent.start_at <= hi,
        )
    ).scalars()
    origin = db.get(KnownLocation, trip.start_location_id) if trip.start_location_id else None
    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    raws = seg.ordered_raws(trip)
    first, last = (raws[0], raws[-1]) if raws else (None, None)
    out: list[Candidate] = []
    for ev in events:
        if ev.is_all_day or (ev.show_as or "").lower() == "free":
            continue  # heldagsposter och "ledig"-markeringar säger inget om en enskild resa
        rel = time_relation(start, end, ev, before_min=before, after_min=after)
        if rel is None:
            continue
        relation, tscore = rel
        if relation == "departure":
            geo, basis, conflict = geo_match(
                ev, origin, trip.start_address, first.start_lat if first else None, first.start_lon if first else None,
                abbreviations,
            )
            conflict = False  # avfärd från annan plats än mötet är ingen konflikt, bara ingen träff
        else:
            geo, basis, conflict = geo_match(
                ev, dest, trip.end_address, last.end_lat if last else None, last.end_lon if last else None, abbreviations
            )
        if geo == 0.0 and not conflict:
            continue
        if conflict and _later_trip_reaches(db, trip, ev, abbreviations):
            continue  # mellanstopp på väg till mötet: en senare resa når mötesplatsen i tid
        out.append(
            Candidate(ev, relation, tscore, geo, basis, conflict and tscore >= CONFLICT_MIN_TIME_SCORE)
        )
    out = [c for c in out if c.score > 0 or c.is_conflict]
    out.sort(key=lambda c: (c.is_conflict, -c.score))
    return out


def _later_trip_reaches(db: Session, trip: LogicalTrip, ev: CalendarEvent, abbreviations: dict[str, str]) -> bool:
    """Når en senare resa (som startar före mötets slut) bokningens plats?"""
    if trip.sort_time is None:
        return False
    later = db.execute(
        select(LogicalTrip)
        .where(
            LogicalTrip.vehicle_id == trip.vehicle_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.id != trip.id,
            LogicalTrip.sort_time > trip.sort_time,
            LogicalTrip.sort_time <= ev.end_at,
        )
        .order_by(LogicalTrip.sort_time)
        .limit(5)
    ).scalars()
    for nxt in later:
        if nxt.source == TripSource.GAP_FILL:
            continue
        dest = db.get(KnownLocation, nxt.end_location_id) if nxt.end_location_id else None
        raws = seg.ordered_raws(nxt)
        last = raws[-1] if raws else None
        geo, basis, conflict = geo_match(
            ev, dest, nxt.end_address, last.end_lat if last else None, last.end_lon if last else None, abbreviations
        )
        if geo >= 0.6 and not conflict:
            return True
    return False


def match_trip(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    is_demo: bool,
    cal: dict[str, Any] | None = None,
    abbreviations: dict[str, str] | None = None,
) -> list[CalendarMatch]:
    """Räknar om kalenderträffarna för en resa. Användarens egna val bevaras."""
    cal = cal or settings_svc.get_section(db, user_id, "calendar")
    abbreviations = abbreviations if abbreviations is not None else settings_svc.get_section(db, user_id, "abbreviations")["map"]
    existing = {
        m.calendar_event_id: m
        for m in db.execute(select(CalendarMatch).where(CalendarMatch.logical_trip_id == trip.id)).scalars()
    }
    # Användarval för en bokning som sedan ställts in eller tagits bort i Outlook gäller inte längre.
    for ev_id, m in list(existing.items()):
        if m.decided_by != "user":
            continue
        ev = db.get(CalendarEvent, ev_id)
        if ev is None or ev.is_cancelled or ev.removed_at is not None:
            db.delete(m)
            del existing[ev_id]
    user_rows = [m for m in existing.values() if m.decided_by == "user"]
    user_selected = any(m.is_selected for m in user_rows)
    user_rejected = {m.calendar_event_id for m in user_rows if not m.is_selected}

    cands = candidates_for_trip(db, trip, user_id=user_id, is_demo=is_demo, cal=cal, abbreviations=abbreviations) \
        if cal.get("enabled", True) else []
    min_score = float(cal["min_match_score"])
    best = next(
        (c for c in cands if not c.is_conflict and c.score >= min_score and c.event.id not in user_rejected), None
    )
    seen: set[uuid.UUID] = set()
    for c in cands[:6]:
        seen.add(c.event.id)
        row = existing.get(c.event.id)
        if row is not None and row.decided_by == "user":
            row.score, row.time_score, row.geo_score, row.geo_basis, row.relation = (
                c.score, c.time_score, c.geo_score, c.geo_basis, c.relation,
            )
            continue
        if row is None:
            row = CalendarMatch(logical_trip_id=trip.id, calendar_event_id=c.event.id, relation=c.relation,
                                score=c.score, time_score=c.time_score, geo_score=c.geo_score)
            db.add(row)
        row.relation, row.score, row.time_score, row.geo_score, row.geo_basis = (
            c.relation, c.score, c.time_score, c.geo_score, c.geo_basis,
        )
        row.is_conflict = c.is_conflict and best is None and not user_selected
        row.is_selected = (best is not None and c.event.id == best.event.id) and not user_selected
    for ev_id, row in existing.items():
        if ev_id not in seen and row.decided_by != "user":
            db.delete(row)
    db.flush()

    rows = list(db.execute(select(CalendarMatch).where(CalendarMatch.logical_trip_id == trip.id)).scalars())
    conflict = next((m for m in rows if m.is_conflict), None)
    key = f"{ReviewKind.CALENDAR_CONFLICT}:{trip.id}"
    if conflict is not None and trip.state == LogicalState.ACTIVE:
        ev = db.get(CalendarEvent, conflict.calendar_event_id)
        review.upsert(
            db,
            vehicle_id=trip.vehicle_id,
            kind=ReviewKind.CALENDAR_CONFLICT,
            dedupe_key=key,
            title="Kalendern och fordonsdata stämmer inte överens",
            detail=(
                f"Bokningen {(ev.subject if ev else '') or '(utan rubrik)'!r} anger en annan plats än resans mål "
                f"({trip.end_address or 'okänd plats'}) vid samma tid."
            ),
            logical_trip_id=trip.id,
            data={"trip_id": str(trip.id), "calendar_event_id": str(conflict.calendar_event_id)},
        )
    else:
        review.resolve_by_key(db, trip.vehicle_id, key, by="system", resolution={"auto": True})
    return rows


def match_vehicle(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, since: datetime | None = None) -> int:
    """Matchar fordonets aktiva resor mot kalendern. Returnerar antal resor med vald träff."""
    cal = settings_svc.get_section(db, user_id, "calendar")
    abbreviations = settings_svc.get_section(db, user_id, "abbreviations")["map"]
    has_events = db.execute(
        select(CalendarEvent.id).where(CalendarEvent.user_id == user_id, CalendarEvent.is_demo.is_(vehicle.is_demo)).limit(1)
    ).first()
    has_matches = db.execute(
        select(CalendarMatch.id)
        .join(LogicalTrip, LogicalTrip.id == CalendarMatch.logical_trip_id)
        .where(LogicalTrip.vehicle_id == vehicle.id)
        .limit(1)
    ).first()
    if not has_events and not has_matches:
        return 0
    selected = 0
    for trip in seg.active_trips(db, vehicle.id):
        if trip.source == TripSource.GAP_FILL:
            continue
        if since is not None and trip.sort_time is not None and trip.sort_time < since:
            continue
        rows = match_trip(db, trip, user_id=user_id, is_demo=vehicle.is_demo, cal=cal, abbreviations=abbreviations)
        selected += 1 if any(m.is_selected for m in rows) else 0
    return selected


def set_user_choice(db: Session, trip: LogicalTrip, match: CalendarMatch, *, selected: bool) -> CalendarMatch:
    """Användaren väljer eller avvisar en kalenderträff. Valet bevaras vid omräkning."""
    if selected:
        for other in db.execute(
            select(CalendarMatch).where(CalendarMatch.logical_trip_id == trip.id, CalendarMatch.id != match.id)
        ).scalars():
            other.is_selected = False
    match.is_selected = selected
    match.is_conflict = False
    match.decided_by = "user"
    db.flush()
    review.resolve_by_key(
        db, trip.vehicle_id, f"{ReviewKind.CALENDAR_CONFLICT}:{trip.id}", by="user", resolution={"selected": selected}
    )
    return match
