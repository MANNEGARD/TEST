"""Lunchregeln (1.3.0): en kort tur från kontoret mitt på dagen och tillbaka är lunch.

Hela mönstret krävs – aldrig klockslaget ensamt (docs/ai-klassificering.md):

* utresan börjar vid ett av arbetsgivarens kontor (``signals.employer_office``) en arbetsdag
  (``automation.office_hours.days``, inte röd dag) och är kort (``max_distance_km`` och,
  när körtiden är känd, ``max_drive_minutes``),
* stoppet varar mellan ``min_stop_minutes`` och ``max_stop_minutes`` på samma ställe,
* återresan är lika kort tillbaka till SAMMA kontor, och allt ligger inom lunchfönstret,
* stället är inte en bekräftad kund, arbetsplats, hem eller hotell, har inget företag, ingen
  egen regel och ingen tidigare tjänsteresa som ägaren eller importen har avgjort, och kartan
  säger inte kontor/industri/lager; ingen kalenderbokning stämmer med resorna.

Modulen läser bara – autopiloten (regel L), regelmotorn (signalen ``lunch_pattern``) och
notiserna (ingen fråga medan ägaren äter) använder ``match`` och ``awaiting_return``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import CalendarMatch, KnownLocation, LocationPattern, LogicalTrip
from app.models.enums import ApprovalSource, LocationType, LogicalState, ReviewStatus, TripSource
from app.services import settings as settings_svc

RETURN_SLACK = timedelta(minutes=10)
_DECIDED = (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
_NEVER_PLACE_TYPES = (LocationType.CUSTOMER, LocationType.OFFICE, LocationType.REGULAR_WORKPLACE,
                      LocationType.HOME, LocationType.HOTEL)


@dataclass(frozen=True)
class LunchMatch:
    status: Literal["complete", "awaiting_return"]
    outbound_id: uuid.UUID
    return_id: uuid.UUID | None
    office_id: uuid.UUID
    place_id: uuid.UUID
    stop_low: float | None
    stop_high: float | None
    map_hint: Literal["restaurang", "kafé", "butik"] | None

    def related_to(self, trip_id: uuid.UUID) -> uuid.UUID | None:
        """Den andra resan i mönstret."""
        return self.return_id if trip_id == self.outbound_id else self.outbound_id


def config(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Lunchregelns inställningar plus arbetsdagarna, tidszonen och kalenderns lägsta poäng."""
    cfg = dict(settings_svc.get_section(db, user_id, "lunch"))
    hours = settings_svc.get_section(db, user_id, "automation").get("office_hours") or settings_svc.DEFAULT_OFFICE_HOURS
    cfg["days"] = set(hours.get("days") or [])
    cfg["tz"] = settings_svc.user_timezone(db, user_id)
    cfg["min_match_score"] = float(settings_svc.get_section(db, user_id, "calendar")["min_match_score"])
    return cfg


def office_ready(db: Session, user_id: uuid.UUID) -> bool:
    """Finns minst ett av arbetsgivarens kontor som bekräftad plats (eller tjänsteställeperiod)?"""
    from app.models import WorkplacePeriod

    if db.execute(
        select(KnownLocation.id).where(
            KnownLocation.owner_user_id == user_id,
            KnownLocation.confirmed.is_(True),
            KnownLocation.type.in_((LocationType.REGULAR_WORKPLACE, LocationType.OFFICE)),
            KnownLocation.merged_into_id.is_(None),
        ).limit(1)
    ).first():
        return True
    return bool(db.execute(
        select(WorkplacePeriod.id)
        .join(KnownLocation, KnownLocation.id == WorkplacePeriod.location_id)
        .where(WorkplacePeriod.user_id == user_id, KnownLocation.confirmed.is_(True))
        .limit(1)
    ).first())


# ----------------------------------------------------------------------------- byggstenar


def _local(when: datetime | None, cfg: dict[str, Any]) -> datetime | None:
    return when.astimezone(cfg["tz"]) if when is not None else None


def _hhmm(when: datetime | None, cfg: dict[str, Any]) -> str | None:
    local = _local(when, cfg)
    return local.strftime("%H:%M") if local is not None else None


def _workday(day: date, cfg: dict[str, Any]) -> bool:
    from app.services.classify.autopilot import is_swedish_holiday

    return day.weekday() in cfg["days"] and not is_swedish_holiday(day)


def _single(t: LogicalTrip | None) -> bool:
    """resa(t): aktiv, en enda delresa, inga mellanstopp och en känd sluttid."""
    return (
        t is not None and t.state == LogicalState.ACTIVE and t.segment_count == 1 and not (t.via or [])
        and t.ended_at is not None and t.source != TripSource.GAP_FILL
    )


def _short(t: LogicalTrip, cfg: dict[str, Any]) -> bool:
    """kort(t): sträckan och – när den är känd – körtiden (aldrig en starttid ur sträckan)."""
    from app.services.trips import start_time

    if t.distance_km is None or t.distance_km > float(cfg["max_distance_km"]):
        return False
    seconds: float | None = float(t.driving_seconds) if t.driving_seconds else None
    if seconds is None:
        started = start_time.known_start(t)
        if started is not None and t.ended_at is not None:
            seconds = (t.ended_at - started).total_seconds()
    return seconds is None or seconds <= float(cfg["max_drive_minutes"]) * 60


def _free(t: LogicalTrip) -> bool:
    """Inte avgjord av någon annan än lunchregeln."""
    if t.review_status not in _DECIDED:
        return True
    return t.review_status == ReviewStatus.APPROVED and t.approval_source == ApprovalSource.LUNCH


def _calendar(db: Session, t: LogicalTrip, cfg: dict[str, Any]) -> bool:
    """kalender(t): en vald träff som inte är en konflikt."""
    return bool(db.execute(
        select(CalendarMatch.id).where(
            CalendarMatch.logical_trip_id == t.id,
            CalendarMatch.is_selected.is_(True),
            CalendarMatch.is_conflict.is_(False),
            CalendarMatch.score >= cfg["min_match_score"],
        ).limit(1)
    ).first())


def _map_hint(loc: KnownLocation) -> Literal["restaurang", "kafé", "butik"] | None:
    cls = (loc.osm_class or "").lower()
    if cls in ("amenity=restaurant", "amenity=fast_food", "amenity=food_court"):
        return "restaurang"
    if cls == "amenity=cafe":
        return "kafé"
    if cls.startswith("shop="):
        return "butik"
    return None


def _place_ok(db: Session, origin_id: uuid.UUID, place: KnownLocation | None, user_id: uuid.UUID) -> bool:
    """ställe_ok(X): inget i underlaget säger att stället har med jobbet att göra."""
    if place is None or (place.confirmed and place.type in _NEVER_PLACE_TYPES):
        return False
    if place.type == LocationType.HOME:
        return False
    if place.confirmed and (place.company or "").strip():
        return False
    if place.assumption == "customer" or place.place_kind == "work":
        return False
    # Ägarens egen regel för målet eller rutten (oavsett kategori) går före lunchregeln.
    if db.execute(
        select(LocationPattern.id).where(
            LocationPattern.owner_user_id == user_id,
            LocationPattern.is_manual_rule.is_(True),
            LocationPattern.destination_location_id == place.id,
            or_(LocationPattern.origin_location_id.is_(None), LocationPattern.origin_location_id == origin_id),
        ).limit(1)
    ).first():
        return False
    # En tidigare tjänsteresa dit som ägaren eller importen har avgjort.
    from app.models import Vehicle

    owner_decided = db.execute(
        select(LogicalTrip.id)
        .join(Vehicle, Vehicle.id == LogicalTrip.vehicle_id)
        .where(
            Vehicle.owner_user_id == user_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.end_location_id == place.id,
            LogicalTrip.work_km > 0,
            or_(
                LogicalTrip.review_status == ReviewStatus.IMPORTED,
                (LogicalTrip.review_status == ReviewStatus.APPROVED)
                & (LogicalTrip.approval_source.is_(None)
                   | LogicalTrip.approval_source.in_((ApprovalSource.USER, ApprovalSource.BULK))),
            ),
        ).limit(1)
    ).first()
    return owner_decided is None


def _neighbour(db: Session, t: LogicalTrip, *, after: bool) -> LogicalTrip | None:
    if t.sort_time is None:
        return None
    q = select(LogicalTrip).where(
        LogicalTrip.vehicle_id == t.vehicle_id,
        LogicalTrip.state == LogicalState.ACTIVE,
        LogicalTrip.id != t.id,
        LogicalTrip.source != TripSource.GAP_FILL,
    )
    if after:
        q = q.where(LogicalTrip.sort_time > t.sort_time).order_by(LogicalTrip.sort_time.asc())
    else:
        q = q.where(LogicalTrip.sort_time < t.sort_time).order_by(LogicalTrip.sort_time.desc())
    return db.execute(q.limit(1)).scalar_one_or_none()


def _office(db: Session, loc_id: uuid.UUID | None, user_id: uuid.UUID, day: date) -> bool:
    from app.services.classify import signals as sig

    if loc_id is None:
        return False
    loc = db.get(KnownLocation, loc_id)
    ids, _home, with_periods = sig._workplace_ids(db, user_id, day)
    return sig.employer_office(loc, ids, with_periods)


def _outbound(db: Session, u: LogicalTrip, *, user_id: uuid.UUID, cfg: dict[str, Any]) -> KnownLocation | None:
    """Är ``u`` en utresa U (kontor → ställe X)? Returnerar X."""
    if not _single(u) or not _free(u) or u.start_location_id is None or u.end_location_id is None:
        return None
    if u.start_location_id == u.end_location_id:
        return None
    start = u.started_at or u.start_latest or u.ended_at
    local_end = _local(u.ended_at, cfg)
    if local_end is None or start is None:
        return None
    if not _workday(local_end.date(), cfg) or not _office(db, u.start_location_id, user_id, local_end.date()):
        return None
    if not _short(u, cfg):
        return None
    if (_hhmm(start, cfg) or "") < cfg["window_start"] or (_hhmm(u.ended_at, cfg) or "") >= cfg["window_end"]:
        return None
    place = db.get(KnownLocation, u.end_location_id)
    if not _place_ok(db, u.start_location_id, place, user_id) or _calendar(db, u, cfg):
        return None
    return place


def _return_stop(db: Session, u: LogicalTrip, r: LogicalTrip, *, cfg: dict[str, Any]) -> tuple[float, float] | None:
    """Är ``r`` återresan efter ``u``? Returnerar stoppets längd (låg, hög) i minuter."""
    from app.services.trips import merge
    from app.services.trips import segments as seg

    if not _single(r) or not _free(r):
        return None
    if r.start_location_id != u.end_location_id or r.end_location_id != u.start_location_id:
        return None
    if not _short(r, cfg):
        return None
    lu, lr = _local(u.ended_at, cfg), _local(r.ended_at, cfg)
    if lu is None or lr is None or lu.date() != lr.date() or lr.strftime("%H:%M") > cfg["window_end"]:
        return None
    if _calendar(db, r, cfg):
        return None
    u_raws, r_raws = seg.ordered_raws(u), seg.ordered_raws(r)
    if not u_raws or not r_raws:
        return None
    gap = merge.stop_gap(u_raws[-1], r_raws[0])
    if gap.minutes_low is None or gap.minutes_high is None:
        return None  # okänt stopp: aldrig lunch
    if gap.minutes_low < float(cfg["min_stop_minutes"]) or gap.minutes_high > float(cfg["max_stop_minutes"]):
        return None
    return gap.minutes_low, gap.minutes_high


def match(
    db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, cfg: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> LunchMatch | None:
    """Lunchmönstret för resan (som utresa eller återresa), eller None."""
    cfg = cfg if cfg is not None else config(db, user_id)
    if not cfg.get("enabled") or not office_ready(db, user_id):
        return None
    now = now or utcnow()
    place = _outbound(db, trip, user_id=user_id, cfg=cfg)
    if place is not None:
        nxt = _neighbour(db, trip, after=True)
        if nxt is not None:
            stop = _return_stop(db, trip, nxt, cfg=cfg)
            if stop is not None:
                return LunchMatch("complete", trip.id, nxt.id, trip.start_location_id, place.id,  # type: ignore[arg-type]
                                  stop[0], stop[1], _map_hint(place))
            return None
        deadline = trip.ended_at + timedelta(  # type: ignore[operator]
            minutes=float(cfg["max_stop_minutes"]) + float(cfg["max_drive_minutes"])
        ) + RETURN_SLACK
        if now <= deadline:
            return LunchMatch("awaiting_return", trip.id, None, trip.start_location_id, place.id,  # type: ignore[arg-type]
                              None, None, _map_hint(place))
        return None
    prev = _neighbour(db, trip, after=False)
    if prev is None:
        return None
    place = _outbound(db, prev, user_id=user_id, cfg=cfg)
    if place is None:
        return None
    stop = _return_stop(db, prev, trip, cfg=cfg)
    if stop is None:
        return None
    return LunchMatch("complete", prev.id, trip.id, prev.start_location_id, place.id,  # type: ignore[arg-type]
                      stop[0], stop[1], _map_hint(place))


def awaiting_return(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, now: datetime | None = None) -> bool:
    """Väntar regeln på återresan (ingen fråga om utresan medan ägaren äter)?"""
    m = match(db, trip, user_id=user_id, now=now)
    return m is not None and m.status == "awaiting_return"


def stop_text(m: LunchMatch) -> str | None:
    """"20–35" eller "25" (minuter) för underlaget."""
    if m.stop_low is None or m.stop_high is None:
        return None
    low, high = round(m.stop_low), round(m.stop_high)
    return str(low) if low == high else f"{low}–{high}"


def reason(m: LunchMatch) -> str:
    return "Lunch" + (f" ({m.map_hint})" if m.map_hint else "")


__all__ = ["LunchMatch", "RETURN_SLACK", "awaiting_return", "config", "match", "office_ready", "reason", "stop_text"]
