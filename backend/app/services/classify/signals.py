"""Deterministisk regelmotor: mätbara signaler per resa.

Varje signal har riktning (arbete/privat/neutral), vikt och en svensk förklaring.
Tidpunkt och veckodag används aldrig som privatindikator – kvällar och helger är
vanliga tjänsteresetider i det historiska underlaget.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CalendarEvent,
    CalendarMatch,
    KnownLocation,
    LocationPattern,
    LogicalTrip,
    WorkplacePeriod,
)
from app.models.enums import (
    LOCATION_TYPE_LABELS_SV,
    Category,
    LocationType,
    LogicalState,
    ReviewStatus,
    Subcategory,
)
from app.services.learning import patterns as patterns_svc
from app.services.places import registry as places

ENGINE_VERSION = "1.0"
RETURN_LOOKBACK_DAYS = 14
_TRUSTED = (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)


@dataclass
class Signal:
    kind: str
    label: str
    direction: str  # work | private | neutral
    weight: float = 0.0
    ref_type: str | None = None
    ref_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "direction": self.direction,
            "weight": round(self.weight, 3),
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "data": self.data,
        }


@dataclass
class TripContext:
    trip: LogicalTrip
    user_id: uuid.UUID
    tz: ZoneInfo
    origin: KnownLocation | None
    destination: KnownLocation | None
    route_pattern: LocationPattern | None
    dest_pattern: LocationPattern | None
    previous: LogicalTrip | None  # föregående aktiva resa (i tid)
    arrival_at_origin: LogicalTrip | None  # senaste resa som slutade där denna börjar
    calendar_matches: list[tuple[CalendarMatch, CalendarEvent]]
    tax: dict[str, Any]
    ai: dict[str, Any]
    abbreviations: dict[str, str]
    dest_is_regular_workplace: bool = False
    origin_is_regular_workplace: bool = False
    home_is_workplace: bool = False

    @property
    def local_start(self) -> datetime | None:
        when = self.trip.started_at or self.trip.sort_time
        return when.astimezone(self.tz) if when else None

    @property
    def selected_event(self) -> CalendarEvent | None:
        for m, ev in self.calendar_matches:
            if m.is_selected and not m.is_conflict:
                return ev
        return None


def _workplace_ids(db: Session, user_id: uuid.UUID, on: date | None) -> tuple[set[uuid.UUID], bool]:
    """Tjänsteställen som gäller ett visst datum, samt om bostaden är tjänsteställe då."""
    ids: set[uuid.UUID] = set()
    home = False
    for wp in db.execute(select(WorkplacePeriod).where(WorkplacePeriod.user_id == user_id)).scalars():
        if on is not None:
            if wp.valid_from and on < wp.valid_from:
                continue
            if wp.valid_to and on > wp.valid_to:
                continue
        if wp.kind == "home":
            home = True
        elif wp.location_id:
            ids.add(wp.location_id)
    return ids, home


def build_context(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    tz: ZoneInfo,
    tax: dict[str, Any],
    ai: dict[str, Any],
    abbreviations: dict[str, str],
) -> TripContext:
    origin = db.get(KnownLocation, trip.start_location_id) if trip.start_location_id else None
    destination = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    route, dest = patterns_svc.get_patterns(db, user_id, trip.start_location_id, trip.end_location_id)

    previous = None
    arrival = None
    if trip.sort_time is not None:
        previous = db.execute(
            select(LogicalTrip)
            .where(
                LogicalTrip.vehicle_id == trip.vehicle_id,
                LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.sort_time < trip.sort_time,
                LogicalTrip.id != trip.id,
            )
            .order_by(LogicalTrip.sort_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        if trip.start_location_id is not None:
            arrival = db.execute(
                select(LogicalTrip)
                .where(
                    LogicalTrip.vehicle_id == trip.vehicle_id,
                    LogicalTrip.state == LogicalState.ACTIVE,
                    LogicalTrip.end_location_id == trip.start_location_id,
                    LogicalTrip.sort_time < trip.sort_time,
                    LogicalTrip.sort_time >= trip.sort_time - timedelta(days=RETURN_LOOKBACK_DAYS),
                    LogicalTrip.id != trip.id,
                )
                .order_by(LogicalTrip.sort_time.desc())
                .limit(1)
            ).scalar_one_or_none()

    matches = list(
        db.execute(
            select(CalendarMatch, CalendarEvent)
            .join(CalendarEvent, CalendarEvent.id == CalendarMatch.calendar_event_id)
            .where(CalendarMatch.logical_trip_id == trip.id)
            .order_by(CalendarMatch.score.desc())
        ).all()
    )
    local_day = None
    when = trip.started_at or trip.sort_time
    if when is not None:
        local_day = when.astimezone(tz).date()
    wp_ids, wp_home = _workplace_ids(db, user_id, local_day)
    ctx = TripContext(
        trip=trip,
        user_id=user_id,
        tz=tz,
        origin=origin,
        destination=destination,
        route_pattern=route,
        dest_pattern=dest,
        previous=previous,
        arrival_at_origin=arrival,
        calendar_matches=[(m, e) for m, e in matches],
        tax=tax,
        ai=ai,
        abbreviations=abbreviations,
    )
    ctx.home_is_workplace = bool(tax.get("home_is_workplace")) or wp_home
    ctx.dest_is_regular_workplace = bool(
        destination is not None
        and (destination.id in wp_ids or (destination.type == LocationType.REGULAR_WORKPLACE and destination.confirmed))
    )
    ctx.origin_is_regular_workplace = bool(
        origin is not None and (origin.id in wp_ids or (origin.type == LocationType.REGULAR_WORKPLACE and origin.confirmed))
    )
    return ctx


def _is_home(loc: KnownLocation | None) -> bool:
    return loc is not None and loc.confirmed and loc.type == LocationType.HOME


def _typed(loc: KnownLocation | None, *types: str) -> bool:
    return loc is not None and loc.confirmed and loc.type in types


def _pattern_signals(ctx: TripContext) -> list[Signal]:
    out: list[Signal] = []
    route, dest = ctx.route_pattern, ctx.dest_pattern

    # Manuell regel (rutt går före destination).
    for pat, scope in ((route, "rutten"), (dest, "destinationen")):
        if pat is not None and pat.is_manual_rule and pat.manual_category in (Category.WORK, Category.PRIVATE):
            out.append(
                Signal(
                    "manual_rule",
                    f"Manuell regel för {scope}: {'Arbete' if pat.manual_category == Category.WORK else 'Privat'}",
                    pat.manual_category,
                    0.9,
                    "location_pattern",
                    str(pat.id),
                )
            )
            return out

    # Användarbekräftad historik – ruttmönstret används om det har underlag, annars destinationen.
    chosen, factor, scope = None, 1.0, "rutten"
    if route is not None and (route.confirmed_work + route.confirmed_private) >= 2:
        chosen = route
    elif dest is not None and (dest.confirmed_work + dest.confirmed_private) >= 1:
        chosen, factor, scope = dest, 0.7, "destinationen"
    elif route is not None and (route.confirmed_work + route.confirmed_private) >= 1:
        chosen = route
    if chosen is not None:
        cw, cp = chosen.confirmed_work, chosen.confirmed_private
        total = cw + cp
        ratio = (cw + 0.5) / (total + 1)
        strength = min(1.0, total / 5)
        weight = 0.6 * strength * abs(2 * ratio - 1) * factor
        if weight >= 0.03:
            direction = Category.WORK if ratio > 0.5 else Category.PRIVATE
            out.append(
                Signal(
                    "confirmed_history",
                    f"Tidigare bekräftade resor för {scope}: {cw} arbete, {cp} privat",
                    direction,
                    weight,
                    "location_pattern",
                    str(chosen.id),
                    {"confirmed_work": cw, "confirmed_private": cp, "scope": scope},
                )
            )

    # Importerad historik: positivt referensmaterial men ett urval → låg, begränsad vikt.
    imp = route if (route is not None and (route.imported_work + route.imported_private) > 0) else dest
    if imp is not None and (imp.imported_work + imp.imported_private) > 0:
        iw, ip = imp.imported_work, imp.imported_private
        if ip == 0:
            out.append(
                Signal(
                    "imported_history",
                    f"{iw} tidigare importerade tjänsteresor hit (importerat urval – väger lätt)",
                    Category.WORK,
                    min(0.3, 0.1 + 0.04 * iw),
                    "location_pattern",
                    str(imp.id),
                    {"imported_work": iw, "imported_private": ip, "selection_biased": True},
                )
            )
        else:
            ratio = (iw + 0.5) / (iw + ip + 1)
            weight = 0.4 * min(1.0, (iw + ip) / 5) * abs(2 * ratio - 1)
            if weight >= 0.03:
                out.append(
                    Signal(
                        "imported_history",
                        f"Importerad historik: {iw} arbete, {ip} privat",
                        Category.WORK if ratio > 0.5 else Category.PRIVATE,
                        weight,
                        "location_pattern",
                        str(imp.id),
                        {"imported_work": iw, "imported_private": ip},
                    )
                )

    # Tidigare korrigeringar minskar tilliten till den äldre regeln.
    corr = route if route is not None and (route.corrections_to_private + route.corrections_to_work) else dest
    if corr is not None:
        if corr.corrections_to_private > corr.corrections_to_work:
            out.append(
                Signal(
                    "user_corrections",
                    f"Du har ändrat {corr.corrections_to_private} liknande resor till Privat",
                    Category.PRIVATE,
                    0.15 * min(3, corr.corrections_to_private),
                    "location_pattern",
                    str(corr.id),
                )
            )
        elif corr.corrections_to_work > corr.corrections_to_private:
            out.append(
                Signal(
                    "user_corrections",
                    f"Du har ändrat {corr.corrections_to_work} liknande resor till Arbete",
                    Category.WORK,
                    0.15 * min(3, corr.corrections_to_work),
                    "location_pattern",
                    str(corr.id),
                )
            )
    return out


def _location_signals(ctx: TripContext) -> tuple[list[Signal], str | None, str | None]:
    """Returnerar (signaler, skattenotering, föreslagen underkategori)."""
    out: list[Signal] = []
    tax_note: str | None = None
    sub: str | None = None
    o, d = ctx.origin, ctx.destination
    commute_rule = bool(ctx.tax.get("commute_rule_enabled", True))

    if d is None:
        out.append(Signal("unknown_destination", "Destinationen är inte en känd plats", "neutral"))
    elif not d.confirmed:
        out.append(
            Signal(
                "unconfirmed_destination",
                f"Platsen {places.display_name(d)!r} är inte bekräftad i platsregistret",
                "neutral",
                ref_type="known_location",
                ref_id=str(d.id),
            )
        )

    if _typed(d, LocationType.PRIVATE):
        out.append(
            Signal("private_place", "Destinationen är markerad som privat plats", Category.PRIVATE, 0.9,
                   "known_location", str(d.id))  # type: ignore[union-attr]
        )
        sub = Subcategory.PRIVATE_ERRAND
    elif _typed(o, LocationType.PRIVATE) and _is_home(d):
        out.append(
            Signal("private_place", "Resan startar vid en privat plats och går hem", Category.PRIVATE, 0.7,
                   "known_location", str(o.id))  # type: ignore[union-attr]
        )
        sub = Subcategory.PRIVATE_ERRAND

    # Bostad ↔ ordinarie tjänsteställe: skattemässig bedömning enligt konfigurationen.
    commute_out = _is_home(o) and ctx.dest_is_regular_workplace
    commute_back = ctx.origin_is_regular_workplace and _is_home(d)
    if commute_out or commute_back:
        if ctx.home_is_workplace:
            out.append(
                Signal(
                    "home_is_workplace",
                    "Bostaden är konfigurerad som tjänsteställe – resan till arbetsplatsen är tjänsteresa",
                    Category.WORK,
                    0.5,
                )
            )
            sub = Subcategory.OFFICE_WORK
        elif commute_rule:
            out.append(
                Signal(
                    "commute",
                    "Resa mellan bostad och ordinarie tjänsteställe (räknas normalt som privat resa)",
                    Category.PRIVATE,
                    0.6,
                    data={"tax_rule": "arbetsresa"},
                )
            )
            tax_note = (
                "Resan går mellan bostaden och det ordinarie tjänstestället. Sådana resor räknas normalt "
                "som privata (arbetsresor). Bekräfta klassningen."
            )
            sub = Subcategory.COMMUTE
    elif ctx.dest_is_regular_workplace:
        out.append(
            Signal("regular_workplace", "Destinationen är ordinarie tjänsteställe (resan utgår inte från bostaden)",
                   Category.WORK, 0.4, "known_location", str(d.id))  # type: ignore[union-attr]
        )
        sub = Subcategory.OFFICE_WORK
    elif _typed(d, LocationType.CUSTOMER):
        out.append(
            Signal("known_customer", f"Känd kund: {places.display_name(d)}", Category.WORK, 0.55,
                   "known_location", str(d.id))  # type: ignore[union-attr]
        )
        sub = Subcategory.CUSTOMER_VISIT
    elif _typed(d, LocationType.OFFICE):
        out.append(
            Signal("known_office", f"Känt kontor: {places.display_name(d)}", Category.WORK, 0.5,
                   "known_location", str(d.id))  # type: ignore[union-attr]
        )
        sub = Subcategory.OFFICE_WORK
    elif _typed(d, LocationType.FUEL_STATION, LocationType.CHARGING_STATION):
        out.append(
            Signal("refuel_stop", f"{LOCATION_TYPE_LABELS_SV[LocationType(d.type)]} – avgörs av omgivande resor",  # type: ignore[union-attr]
                   "neutral", ref_type="known_location", ref_id=str(d.id))  # type: ignore[union-attr]
        )
    return out, tax_note, sub


def _journey_signals(ctx: TripContext, already_commute: bool) -> tuple[list[Signal], str | None]:
    """Återresor, hotell och fortsättningsresor (även över flera dagar)."""
    out: list[Signal] = []
    sub: str | None = None
    arr, d, o = ctx.arrival_at_origin, ctx.destination, ctx.origin
    if arr is None or already_commute:
        return out, sub
    trusted = arr.review_status in _TRUSTED
    arr_work = arr.category == Category.WORK and not arr.has_mixed_categories
    arr_private = arr.category == Category.PRIVATE and not arr.has_mixed_categories
    data = {"related_trip_id": str(arr.id), "trusted": trusted}

    if _is_home(d):
        if arr_work:
            out.append(
                Signal(
                    "return_trip",
                    "Återresa hem efter " + ("bekräftat" if trusted else "preliminärt klassat") + " tjänsteärende",
                    Category.WORK,
                    0.55 if trusted else 0.3,
                    "logical_trip",
                    str(arr.id),
                    data,
                )
            )
            sub = Subcategory.RETURN_HOME
        elif arr_private:
            out.append(
                Signal("return_trip", "Återresa hem efter privat ärende", Category.PRIVATE, 0.5 if trusted else 0.3,
                       "logical_trip", str(arr.id), data)
            )
            sub = Subcategory.PRIVATE_ERRAND
    elif _typed(d, LocationType.HOTEL):
        if arr_work:
            out.append(
                Signal("hotel", "Resa till hotell under pågående tjänsteresa", Category.WORK, 0.45 if trusted else 0.25,
                       "logical_trip", str(arr.id), data)
            )
            sub = Subcategory.HOTEL_TRANSFER
    elif _typed(o, LocationType.HOTEL):
        if arr_work:
            out.append(
                Signal("hotel", "Resa från hotell under pågående tjänsteresa", Category.WORK, 0.35 if trusted else 0.2,
                       "logical_trip", str(arr.id), data)
            )
            sub = Subcategory.HOTEL_TRANSFER
    elif arr_work and not _is_home(o):
        within = arr.ended_at and ctx.trip.sort_time and (ctx.trip.sort_time - arr.ended_at) <= timedelta(hours=12)
        if within:
            out.append(
                Signal("continuation", "Fortsättning efter tjänsteärende samma dag", Category.WORK,
                       0.2 if trusted else 0.1, "logical_trip", str(arr.id), data)
            )
    return out, sub


def _calendar_signals(ctx: TripContext) -> tuple[list[Signal], bool]:
    out: list[Signal] = []
    conflict = False
    for m, ev in ctx.calendar_matches:
        subject = (ev.subject or "(utan rubrik)")[:80]
        if m.is_conflict:
            conflict = True
            out.append(
                Signal(
                    "calendar_conflict",
                    f"Kalenderbokning på annan plats samtidigt: {subject}",
                    "neutral",
                    0.0,
                    "calendar_event",
                    str(ev.id),
                    {"score": m.score},
                )
            )
        elif m.is_selected:
            private_event = (ev.sensitivity or "").lower() in ("personal", "private")
            if private_event:
                out.append(
                    Signal("calendar_private", f"Matchande privat kalenderbokning: {subject}", Category.PRIVATE, 0.35,
                           "calendar_event", str(ev.id), {"score": m.score, "relation": m.relation})
                )
            else:
                out.append(
                    Signal(
                        "calendar_match",
                        f"Matchande Outlook-möte: {subject}",
                        Category.WORK,
                        0.25 + 0.35 * max(0.0, min(1.0, m.score)),
                        "calendar_event",
                        str(ev.id),
                        {"score": m.score, "relation": m.relation, "geo_basis": m.geo_basis},
                    )
                )
    return out, conflict


@dataclass
class RuleResult:
    signals: list[Signal]
    category: str
    confidence: float
    requires_review: bool
    subcategory: str | None
    tax_note: str | None
    work_score: float
    private_score: float
    caps: list[str]
    calendar_conflict: bool


def _saturate(weights: list[float]) -> float:
    acc = 1.0
    for w in weights:
        acc *= 1.0 - max(0.0, min(0.99, w))
    return 1.0 - acc


def evaluate(ctx: TripContext) -> RuleResult:
    """Kombinerar signalerna till ett förslag. Confidence är en beslutsindikator
    (0–1), inte en statistisk sannolikhet."""
    loc_signals, tax_note, sub = _location_signals(ctx)
    is_commute = any(s.kind == "commute" for s in loc_signals)
    pat_signals = _pattern_signals(ctx)
    jour_signals, jour_sub = _journey_signals(ctx, is_commute)
    cal_signals, conflict = _calendar_signals(ctx)
    signals = loc_signals + pat_signals + jour_signals + cal_signals
    sub = sub or jour_sub

    manual = next((s for s in signals if s.kind == "manual_rule"), None)
    work = _saturate([s.weight for s in signals if s.direction == Category.WORK])
    private = _saturate([s.weight for s in signals if s.direction == Category.PRIVATE])
    margin = work - private
    caps: list[str] = []
    requires_review = False

    if max(work, private) < 0.2:
        category, confidence = Category.UNDECIDED, 0.3
        requires_review = True
    elif margin > 0.15:
        category, confidence = Category.WORK, min(0.99, 0.5 + 0.5 * margin)
    elif margin < -0.15:
        category, confidence = Category.PRIVATE, min(0.99, 0.5 - 0.5 * margin)
    else:
        category, confidence = Category.UNDECIDED, 0.4
        requires_review = True

    if work > 0.4 and private > 0.4:
        requires_review = True
        caps.append("Motstridiga signaler")
        confidence = min(confidence, 0.6)

    strong_kinds = {"manual_rule", "confirmed_history", "known_customer", "known_office", "regular_workplace",
                    "calendar_match", "private_place", "home_is_workplace", "return_trip", "hotel", "commute",
                    "calendar_private", "user_corrections"}
    if category != Category.UNDECIDED and not any(
        s.kind in strong_kinds and s.direction == category for s in signals
    ):
        caps.append("Endast importerad historik som stöd")
        confidence = min(confidence, 0.75)

    flags = set(ctx.trip.quality_flags or [])
    if flags & {"possible_missed_trips", "start_time_uncertain", "missing_odometer"}:
        caps.append("Osäker datakvalitet")
        confidence = min(confidence, 0.7)

    if tax_note and not manual:
        requires_review = bool(ctx.tax.get("require_approval_when_uncertain", True)) or requires_review
        caps.append("Skattemässig bedömning krävs")
    if manual:
        tax_note = None
    if conflict:
        requires_review = True
        caps.append("Konflikt mellan kalender och fordonsdata")
        confidence = min(confidence, 0.6)
    if ctx.trip.has_mixed_categories:
        requires_review = True

    return RuleResult(
        signals=signals,
        category=category,
        confidence=round(confidence, 3),
        requires_review=requires_review,
        subcategory=sub,
        tax_note=tax_note,
        work_score=round(work, 3),
        private_score=round(private, 3),
        caps=caps,
        calendar_conflict=conflict,
    )
