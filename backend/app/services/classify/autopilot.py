"""Självgående läge: regeltabellen (A.2), platsantagandet (A.3) och ägarens rättelser.

Autopiloten gäller bara live-resor som skapats efter ``automation.active_since``, som är
aktiva och som ingen ännu har godkänt (``eligible``). ``decide`` prövar reglerna i
byggkontraktets ordning och skriver ingenting; ``apply`` genomför beslutet.

* Spärr 1 (ingen automatik): mätarställning saknas, flera körningar kan ha skett, resan
  blandar privat och tjänst, kalendern säger emot fordonsdata eller ägarens regel
  "fråga alltid".
* Spärr 2 (aldrig automatisk Arbete): någon signal pekar på Privat, motstridiga signaler,
  rutten bostad ↔ ordinarie tjänsteställe (utom när ägaren valt att resor hem ↔ kontor är
  tjänst, regel K), eller målet är hemmet (utom T), en privat plats, tjänstestället eller ett
  hotell (utom egen regel R).
* Reglerna i ordning (1.3.0): väntan på adressen → spärr 1 → lunchväntan → P → P → R → K
  (hem ↔ kontor) → L (lunch) → kvällsärende under tjänsteresa (fråga) → H → T → A; allt annat
  blir EN fråga.
* Ärendet ur kalendern (ägarens beslut A): godkänns resan automatiskt som Arbete av R, K, H
  eller T och kalendern ger starkt stöd, blir ärendet kalenderns (AI-formulerat när det klarat
  namnkontrollen) och märks ``from_calendar`` i månadens genomgång.

Automatiskt godkända resor bär ``approval_source`` och räknas aldrig som ägarbekräftad
historik. Importerade och manuellt inlagda resor rörs aldrig. Platsantagandet (A) och
"Stämmer"/företagsnamnet byggs av S1d.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    KnownLocation,
    LearningFeedback,
    LocationPattern,
    LogicalTrip,
    TripClassification,
    TripPurpose,
    User,
    Vehicle,
)
from app.models.enums import (
    AUTO_APPROVAL_SOURCES,
    ApprovalSource,
    Category,
    DecisionSource,
    GeocodeStatus,
    LocationType,
    LogicalState,
    MonthCloseState,
    PatternStatus,
    PlaceAssumption,
    PlaceKind,
    ReviewStatus,
    TripSource,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.text_safety import sanitize_text
from app.services.trips.segments import TripError

HISTORY_MIN = 3
# Beslutet väntar på adressen, men högst så här länge efter att platsen skapades (A.1).
WAIT_FOR_ADDRESS = timedelta(minutes=30)
# Sträckan får avvika så här mycket från ruttens median (H) eller utresans (T).
DISTANCE_TOLERANCE = 0.15
# "Alltid" för ett tjänsteärende kräver att hela texten ryms här.
REMEMBER_MAX_PURPOSE = 40

LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
# Flaggor som betyder att resan kan vara flera körningar (spärr 1).
MULTI_TRIP_FLAGS = frozenset({"possible_missed_trips", "collection_gap", "detour_or_multiple_trips"})
MULTI_TRIP_TEXT = "Kan vara flera körningar"
COMMUTE_NOTE = "Resa mellan bostad och arbetsplats"
# Platsantagandet (A.3).
ASSUMED_REASON = "Antaget kundbesök"
ASSUMED_PURPOSE_PREFIX = "Kundbesök"
COMPANY_QUESTION = "Vilket företag var det?"
# Företagsnamnet ryms med god marginal i ett ärende ("Kundbesök <namn>").
COMPANY_MAX = 100
# Platstyper som aldrig antas vara en kund (även om kartan säger arbetsplats).
_NEVER_ASSUMED_TYPES = frozenset({
    LocationType.HOME, LocationType.PRIVATE, LocationType.REGULAR_WORKPLACE, LocationType.OFFICE, LocationType.HOTEL,
})

# Kategorikälla på resan: ägarens egna regler räknas som "rule", det inlärda som "pattern".
# Aldrig "user" eller "ha", så att avstämningen mot importen beter sig som förut.
_RULE_SOURCES = frozenset({
    ApprovalSource.RULE, ApprovalSource.COMMUTE, ApprovalSource.PRIVATE_PLACE, ApprovalSource.HOME_OFFICE,
    ApprovalSource.LUNCH,
})
LUNCH_WAIT_TEXT = "Lunch? Väntar på återresan"
EVENING_TEXT = "Kvällsärende under tjänsteresa"
EVENING_MAX_KM = 15.0


@dataclass(frozen=True)
class AutoDecision:
    outcome: Literal["approve", "ask", "ask_purpose", "ask_company", "wait", "skip"]
    category: str | None = None
    purpose: str | None = None
    source: str | None = None  # enums.ApprovalSource
    pattern_id: uuid.UUID | None = None
    related_trip_id: uuid.UUID | None = None
    reason: str = ""  # "Din regel för Acme AB", "Som de 3 senaste gångerna" …
    candidates: tuple[str, ...] = ()  # ärendeknappar
    note: str | None = None  # anteckning på resan (P: "Resa mellan bostad och arbetsplats")
    veto_until: str | None = None  # ISO-tid; antagna resor räknas först när vetotiden gått
    calendar_event_ids: tuple[str, ...] = ()  # ärendet kommer ur kalendern (beslut A)


SKIP = AutoDecision("skip")


def is_active(db: Session, user_id: uuid.UUID) -> bool:
    """Sant när självgående läge är valt och har en starttidpunkt (``active_since``)."""
    return (
        settings_svc.automation_mode(db, user_id) == settings_svc.MODE_SELF_DRIVING
        and settings_svc.active_since(db, user_id) is not None
    )


def ensure_active_since(db: Session, user_id: uuid.UUID | None = None, now: datetime | None = None) -> None:
    """Sätter ``automation.active_since`` för användare i självgående läge som saknar den.

    Utan ``user_id`` gäller det alla aktiva användare (första steget i ``tick``). En redan
    satt tidpunkt ändras aldrig, så resor från före uppgraderingen rörs aldrig och ingen
    omräkning görs. I läget ``manual`` sätts ingenting.
    """
    now = now or utcnow()
    if user_id is not None:
        user_ids = [user_id]
    else:
        user_ids = list(db.execute(select(User.id).where(User.is_active.is_(True))).scalars())
    for uid in user_ids:
        if settings_svc.automation_mode(db, uid) != settings_svc.MODE_SELF_DRIVING:
            continue
        if settings_svc.get_section(db, uid, "automation").get("active_since"):
            continue
        value = now.isoformat(timespec="seconds")
        settings_svc.update_section(db, uid, "automation", {"active_since": value}, allow_read_only=True)
        audit.record(db, audit.SYSTEM, "settings.automation_active_since", "settings", "automation",
                     after={"active_since": value}, user_id=uid)
    db.flush()


# ----------------------------------------------------------------------------- hjälpfunktioner


def _raws(trip: LogicalTrip) -> list[Any]:
    return [s.raw_trip for s in trip.segments if s.raw_trip is not None]


def is_live(trip: LogicalTrip) -> bool:
    """Alla delresor kommer från bilen (Volvo eller demo)."""
    raws = _raws(trip)
    return bool(raws) and all(r.source in LIVE_SOURCES for r in raws)


def eligible(trip: LogicalTrip, *, active_since: datetime | None) -> bool:
    """Live-resa, aktiv, ej godkänd och skapad efter ``active_since``.

    Den tidigaste ``RawTrip.created_at`` i resan räknas, så en resa som delvis fanns före
    uppgraderingen aldrig omfattas.
    """
    if active_since is None or trip.state != LogicalState.ACTIVE:
        return False
    if trip.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED):
        return False
    raws = _raws(trip)
    if not raws or any(r.source not in LIVE_SOURCES for r in raws):
        return False
    created = [r.created_at for r in raws]
    if any(c is None for c in created):
        return False
    return min(created) >= active_since


def _is_home(loc: KnownLocation | None) -> bool:
    return loc is not None and loc.confirmed and loc.type == LocationType.HOME


def _is_private(loc: KnownLocation | None) -> bool:
    return loc is not None and loc.confirmed and loc.type == LocationType.PRIVATE


def _typed(loc: KnownLocation | None, *types: str) -> bool:
    return loc is not None and loc.confirmed and loc.type in types


def _label(loc: KnownLocation | None, address: str | None = None) -> str:
    """Platsens namn i skälet: "Hemmet", "Privat plats", platsens namn eller orten.

    En obekräftad plats vars namn bara är gatuadressen visas med ort, aldrig med husnummer.
    """
    from app.services.classify.engine import _name_is_address
    from app.services.places import registry as places

    if loc is not None and loc.type not in (LocationType.HOME, LocationType.PRIVATE):
        if loc.name and (loc.confirmed or not _name_is_address(loc)):
            return loc.name
    return places.public_label(loc, address)


def config(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    """Inställningarna som besluten bygger på, hämtade en gång per körning."""
    return {
        "now": now or utcnow(),
        "active_since": settings_svc.active_since(db, user_id) if is_active(db, user_id) else None,
        "automation": settings_svc.get_section(db, user_id, "automation"),
        "tax": settings_svc.get_section(db, user_id, "tax"),
        "ai": settings_svc.get_section(db, user_id, "ai"),
        "scope": settings_svc.journal_scope(db, user_id),
        "tz": settings_svc.user_timezone(db, user_id),
        "abbreviations": settings_svc.get_section(db, user_id, "abbreviations")["map"],
        "employer": settings_svc.get_section(db, user_id, "general").get("employer_name") or "",
        "opts": _context_options(db, user_id, now),
    }


def _context_options(db: Session, user_id: uuid.UUID, now: datetime | None) -> dict[str, Any]:
    from app.services.classify import signals as sig

    return sig.context_options(db, user_id, now=now)


def _evaluate(db: Session, trip: LogicalTrip, user_id: uuid.UUID, cfg: dict[str, Any]) -> tuple[Any, Any]:
    """Regelmotorns underlag (TripContext och RuleResult) för resan, utan AI."""
    from app.services.classify import signals as sig

    ctx = sig.build_context(
        db, trip, user_id=user_id, tz=cfg["tz"], tax=cfg["tax"], ai=cfg["ai"], abbreviations=cfg["abbreviations"],
        opts=cfg.get("opts"),
    )
    return ctx, sig.evaluate(ctx)


def _within(value: float, reference: float) -> bool:
    if reference <= 0:
        return False
    return abs(value - reference) <= DISTANCE_TOLERANCE * reference


def _local_day(when: datetime | None, cfg: dict[str, Any]) -> Any:
    return when.astimezone(cfg["tz"]).date() if when is not None else None


# ----------------------------------------------------------------------------- spärrar


def _hard_block(trip: LogicalTrip, rules: Any) -> str | None:
    """Spärr 1: ingen automatik alls. Returnerar skälet, eller None."""
    from app.services.classify import signals as sig

    flags = set(trip.quality_flags or [])
    if "missing_odometer" in flags or trip.odometer_start is None or trip.odometer_end is None:
        return "Mätarställning saknas"
    if flags & MULTI_TRIP_FLAGS:
        return MULTI_TRIP_TEXT
    if trip.has_mixed_categories:
        return "Resan blandar privat och tjänst"
    if rules.calendar_conflict:
        return "Kalendern säger emot bilens uppgifter"
    if any(s.kind == sig.ASK_ALWAYS_KIND for s in rules.signals):
        return "Din regel: fråga alltid"
    return None


def _commute_direct(ctx: Any) -> bool:
    """Bostad ↔ ordinarie tjänsteställe, åt endera hållet (prövas på platserna)."""
    return (_is_home(ctx.origin) and ctx.dest_is_regular_workplace) or (
        ctx.origin_is_regular_workplace and _is_home(ctx.destination)
    )


def _private_block(ctx: Any, rules: Any) -> bool:
    """Spärr 2, signaldelen: något pekar på Privat, eller resan är en arbetsresa.

    Har ägaren valt att resor hem ↔ kontor är tjänsteresor räknas arbetsresan inte som privat.
    """
    if any(s.direction == Category.PRIVATE for s in rules.signals):
        return True
    if "Motstridiga signaler" in (rules.caps or []):
        return True
    if getattr(ctx, "home_office_is_work", False):
        return False
    return _commute_direct(ctx) or bool(ctx.commute_via_stop)


def _destination_kind(ctx: Any) -> str | None:
    """Spärr 2, målets art: home | private | workplace | hotel, annars None."""
    d = ctx.destination
    if _is_home(d):
        return "home"
    if _is_private(d):
        return "private"
    if ctx.dest_is_regular_workplace:
        return "workplace"
    if _typed(d, LocationType.HOTEL):
        return "hotel"
    return None


# ----------------------------------------------------------------------------- reglerna


def _commute_rule(trip: LogicalTrip, ctx: Any, cfg: dict[str, Any]) -> AutoDecision | None:
    """P · "Resa till arbetet": bostad ↔ ordinarie tjänsteställe utan mellanstopp."""
    if not _commute_direct(ctx) or trip.segment_count > 1 or (trip.via or []):
        return None
    if not bool(cfg["tax"].get("commute_rule_enabled", True)) or ctx.home_is_workplace:
        return None
    if getattr(ctx, "home_office_is_work", False):
        return None  # ägarens val: hem ↔ kontor är tjänst (regel K)
    if cfg["scope"] != settings_svc.SCOPE_BUSINESS_ONLY:
        return None
    return AutoDecision(
        "approve", category=Category.PRIVATE, source=ApprovalSource.COMMUTE, reason="Resa till arbetet",
        note=COMMUTE_NOTE,
    )


def _private_place_rule(ctx: Any) -> AutoDecision | None:
    """P · "Privat plats": start och mål är hemmet eller bekräftade privata platser."""
    o, d = ctx.origin, ctx.destination
    if (_is_home(o) or _is_private(o)) and (_is_home(d) or _is_private(d)):
        return AutoDecision("approve", category=Category.PRIVATE, source=ApprovalSource.PRIVATE_PLACE,
                            reason="Privat plats")
    return None


def _own_rule(
    db: Session, trip: LogicalTrip, ctx: Any, *, user_id: uuid.UUID, work_allowed: bool, rules: Any = None
) -> AutoDecision | None:
    """R · "Din regel för X": ägarens regel för rutten, annars för målet."""
    rule: LocationPattern | None = None
    for pat in (ctx.route_pattern, ctx.dest_pattern):
        if pat is not None and pat.is_manual_rule and pat.manual_category in (Category.WORK, Category.PRIVATE):
            rule = pat
            break
    if rule is None:
        return None
    reason = f"Din regel för {_label(ctx.destination, trip.end_address)}"
    if rule.manual_category == Category.PRIVATE:
        return AutoDecision("approve", category=Category.PRIVATE, source=ApprovalSource.RULE,
                            pattern_id=rule.id, reason=reason)
    if not work_allowed:
        # Regeln säger Arbete men spärr 2 gäller: resan ger en fråga.
        return AutoDecision("ask", reason=reason,
                            candidates=tuple(purpose_candidates(db, trip, user_id=user_id)))
    from app.services.classify.purpose import PURPOSE_MODE_ALWAYS

    purpose = sanitize_text(rule.manual_purpose)
    if not (purpose and rule.manual_purpose_mode == PURPOSE_MODE_ALWAYS):
        cal = _calendar_purpose(db, trip, ctx, rules)
        if cal is not None:
            # Regeln avgör kategorin, kalendern ärendet ("när kalendern inte säger något annat").
            return AutoDecision("approve", category=Category.WORK, purpose=cal[0], source=ApprovalSource.RULE,
                                pattern_id=rule.id, reason=reason, calendar_event_ids=cal[1])
    if purpose:
        return AutoDecision("approve", category=Category.WORK, purpose=purpose, source=ApprovalSource.RULE,
                            pattern_id=rule.id, reason=reason)
    return AutoDecision("ask_purpose", category=Category.WORK, pattern_id=rule.id, reason=reason,
                        candidates=tuple(purpose_candidates(db, trip, user_id=user_id)))


def _owner_confirmed(t: LogicalTrip) -> bool:
    """Ägarens eget godkännande (inte massgodkännande och inte automatiken)."""
    return t.review_status == ReviewStatus.APPROVED and t.approval_source in (None, ApprovalSource.USER)


def _destination_corrected(db: Session, user_id: uuid.UUID, dest_id: uuid.UUID) -> bool:
    """Har ägaren någon gång rättat kategorin för en resa till målet (ej ångrat)?"""
    rows = db.execute(
        select(LearningFeedback.original_category, LearningFeedback.new_category).where(
            LearningFeedback.user_id == user_id,
            LearningFeedback.destination_location_id == dest_id,
            LearningFeedback.kind == "correction",
            LearningFeedback.undone_at.is_(None),
        )
    ).all()
    return any(
        orig in (Category.WORK, Category.PRIVATE) and new != orig for orig, new in rows
    )


def _history_rule(
    db: Session, trip: LogicalTrip, ctx: Any, *, user_id: uuid.UUID, cfg: dict[str, Any], work_allowed: bool,
    rules: Any = None,
) -> AutoDecision | None:
    """H · "Som de 3 senaste gångerna": bara ruttens egen, ägarbekräftade historik."""
    from app.services.classify.textnorm import normalize_purpose

    if trip.start_location_id is None or trip.end_location_id is None or trip.sort_time is None:
        return None
    route = list(
        db.execute(
            select(LogicalTrip)
            .join(Vehicle, Vehicle.id == LogicalTrip.vehicle_id)
            .where(
                Vehicle.owner_user_id == user_id,
                LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.start_location_id == trip.start_location_id,
                LogicalTrip.end_location_id == trip.end_location_id,
                LogicalTrip.id != trip.id,
            )
            .order_by(LogicalTrip.sort_time.desc())
        ).scalars()
    )
    history = [
        t for t in route
        if _owner_confirmed(t) and t.sort_time is not None and t.sort_time < trip.sort_time
        and not t.has_mixed_categories and t.category in (Category.WORK, Category.PRIVATE)
    ]
    if len(history) < HISTORY_MIN:
        return None
    latest = history[:HISTORY_MIN]
    category = latest[0].category
    if any(t.category != category for t in latest):
        return None
    opposite = Category.PRIVATE if category == Category.WORK else Category.WORK
    # Motbevis: en avgjord resa på rutten med motsatt kategori, oavsett vem som avgjorde den.
    for t in route:
        if t.review_status not in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED):
            continue
        if t.category == opposite or t.has_mixed_categories:
            return None
    if _destination_corrected(db, user_id, trip.end_location_id):
        return None
    same = [t for t in history if t.category == category]
    purpose: str | None = None
    if category == Category.WORK:
        if not work_allowed:
            return None
        keys = []
        for t in latest:
            norm = normalize_purpose(t.purpose, cfg["abbreviations"]) if t.purpose_confirmed else None
            if norm is None:
                return None
            keys.append(norm.key)
        if len(set(keys)) != 1:
            return None
        weekend = [_local_day(t.started_at or t.sort_time, cfg).weekday() >= 5 for t in same]
        weekends = sum(weekend)
        weekdays = len(weekend) - weekends
        if weekends == weekdays:
            return None  # ingen dagtyp dominerar
        trip_weekend = _local_day(trip.started_at or trip.sort_time, cfg).weekday() >= 5
        if trip_weekend != (weekends > weekdays):
            return None
        median = statistics.median(t.distance_km for t in same)
        if not _within(trip.distance_km, median):
            return None
        from app.services.classify.purpose import NEEDS_CONFIRMATION_SUFFIX
        from app.services.places.normalize import fold

        purpose = sanitize_text(latest[0].purpose)
        if not purpose or NEEDS_CONFIRMATION_SUFFIX in fold(purpose):
            return None
    cal = _calendar_purpose(db, trip, ctx, rules) if category == Category.WORK else None
    return AutoDecision(
        "approve", category=category, purpose=cal[0] if cal else purpose, source=ApprovalSource.HISTORY,
        pattern_id=ctx.route_pattern.id if ctx.route_pattern is not None else None,
        reason=f"Som de {HISTORY_MIN} senaste gångerna", calendar_event_ids=cal[1] if cal else (),
    )


def _return_rule(
    trip: LogicalTrip, ctx: Any, *, cfg: dict[str, Any], private_block: bool, db: Session | None = None,
    rules: Any = None,
) -> AutoDecision | None:
    """T · "Hemresa efter …": samma dag hem efter en godkänd tjänsteresa dit."""
    from app.services.classify.purpose import return_purpose

    prev = ctx.previous
    if prev is None or private_block or not _is_home(ctx.destination) or ctx.origin_is_regular_workplace:
        return None
    if trip.start_location_id is None or prev.end_location_id != trip.start_location_id:
        return None
    if prev.review_status != ReviewStatus.APPROVED or prev.category != Category.WORK or prev.has_mixed_categories:
        return None
    if not (prev.purpose and prev.purpose_confirmed):
        return None
    if _local_day(prev.ended_at or prev.sort_time, cfg) != _local_day(trip.started_at or trip.sort_time, cfg):
        return None
    if not _within(trip.distance_km, prev.distance_km):
        return None
    purpose = return_purpose(prev.purpose, cfg["abbreviations"])
    if purpose is None:
        return None
    assumed = prev.approval_source == ApprovalSource.ASSUMED
    veto = (prev.approval_basis or {}).get("veto_until") if assumed else None
    cal = _calendar_purpose(db, trip, ctx, rules) if (db is not None and not assumed) else None
    return AutoDecision(
        "approve", category=Category.WORK, purpose=cal[0] if cal else sanitize_text(purpose),
        source=ApprovalSource.ASSUMED if assumed else ApprovalSource.RETURN_TRIP,
        related_trip_id=prev.id, reason=purpose.rstrip(". "),
        veto_until=veto, calendar_event_ids=cal[1] if cal else (),
    )


def _home_office_rule(
    db: Session, trip: LogicalTrip, ctx: Any, rules: Any, *, user_id: uuid.UUID, cfg: dict[str, Any]
) -> AutoDecision | None:
    """K · "Hem ↔ kontor i {ort}" (ägarens beslut B): resor mellan hemmet och vilket som helst
    av arbetsgivarens kontor är tjänsteresor och godkänns automatiskt – oavsett tid på dygnet
    (beslut I). Övriga spärrar gäller: spärr 1 prövas före, och en signal som pekar på Privat
    (t.ex. en privat plats) ger en fråga i stället."""
    from app.services.classify import signals as sig
    from app.services.classify.purpose import is_fixed, propose

    if not sig.home_office(ctx):
        return None
    if any(s.direction == Category.PRIVATE for s in rules.signals):
        return None
    outbound = _is_home(ctx.origin)
    office = ctx.destination if outbound else ctx.origin
    city = sanitize_text((office.city if office is not None else None) or None) or ""
    where = city or sanitize_text(office.name if office is not None else None) or "kontoret"
    reason = f"Hem ↔ kontor i {where}"
    cal = _calendar_purpose(db, trip, ctx, rules)
    if cal is not None:
        return AutoDecision("approve", category=Category.WORK, purpose=cal[0], source=ApprovalSource.HOME_OFFICE,
                            reason=reason, calendar_event_ids=cal[1])
    purpose: str | None = None
    proposal = propose(db, ctx, rules)
    if proposal is not None and is_fixed(proposal):
        purpose = sanitize_text(proposal.text)  # ägarens regel eller historik för rutten
    if purpose is None:
        current = _current_purpose(db, trip)
        based = (current.based_on or {}) if current is not None else {}
        if current is not None and current.source == DecisionSource.AI and based.get("supported") is True:
            purpose = sanitize_text(current.text)
    if purpose is None:
        purpose = f"Resa till kontoret i {where}" if outbound else f"Hemresa från kontoret i {where}"
    return AutoDecision("approve", category=Category.WORK, purpose=purpose, source=ApprovalSource.HOME_OFFICE,
                        reason=reason)


def _lunch_rule(ctx: Any) -> AutoDecision | None:
    """L · "Lunch": hela lunchmönstret är klart (utresa, kort stopp, återresa till samma kontor)."""
    from app.services.classify import lunch

    m = getattr(ctx, "lunch", None)
    if m is None or m.status != "complete":
        return None
    return AutoDecision("approve", category=Category.PRIVATE, source=ApprovalSource.LUNCH, reason=lunch.reason(m),
                        related_trip_id=m.related_to(ctx.trip.id))


def _evening_rule(trip: LogicalTrip, ctx: Any, cfg: dict[str, Any]) -> AutoDecision | None:
    """Kvällsärende under tjänsteresa (beslut H): en kort tur från eller till hotellet utanför
    kontorstid ger alltid en fråga – ingen automatisk privat och ingen automatisk tjänst."""
    o, d = ctx.origin, ctx.destination
    at_hotel = _typed(o, LocationType.HOTEL) or _typed(d, LocationType.HOTEL)
    if not at_hotel or (trip.distance_km or 0) > EVENING_MAX_KM:
        return None
    other = d if _typed(o, LocationType.HOTEL) else o
    if other is not None and (_typed(other, LocationType.CUSTOMER) or (
        other is d and ctx.dest_is_employer_office) or (other is o and ctx.origin_is_employer_office)):
        return None
    if _in_working_hours(trip.ended_at or trip.sort_time, cfg):
        return None
    return AutoDecision("ask", reason=EVENING_TEXT, candidates=())


def _in_working_hours(when: datetime | None, cfg: dict[str, Any]) -> bool:
    """Inom ``automation.office_hours`` (dag och klockslag), utan hänsyn till röda dagar."""
    if when is None:
        return True
    hours = (cfg.get("automation") or {}).get("office_hours") or settings_svc.DEFAULT_OFFICE_HOURS
    local = when.astimezone(cfg["tz"])
    if local.weekday() not in set(hours.get("days") or []):
        return False
    hhmm = local.strftime("%H:%M")
    return str(hours.get("start", "07:00")) <= hhmm < str(hours.get("end", "18:00"))


def _poi_name(loc: KnownLocation | None) -> str | None:
    """Platsens namn ur kartan (eller företaget), aldrig gatuadressen."""
    from app.services.classify.engine import _name_is_address

    if loc is None:
        return None
    company = sanitize_text(loc.company, limit=COMPANY_MAX)
    if company:
        return company
    if loc.name and not _name_is_address(loc):
        return sanitize_text(loc.name, limit=COMPANY_MAX)
    return None


def _place_kind(loc: KnownLocation) -> str | None:
    """Platsens art. Kartans typ prövas igen, så att platser som sparats före en ändring i
    tabellen (t.ex. office=government, som inte längre är "work") följer tabellen."""
    from app.services.places import geocode

    if loc.place_kind == PlaceKind.WORK and loc.osm_class and "=" in loc.osm_class:
        cat, _, typ = loc.osm_class.partition("=")
        return geocode.classify_place(cat, typ)
    return loc.place_kind


def _assumable_place(loc: KnownLocation | None) -> bool:
    """Kan platsen över huvud taget antas vara en kund? (obekräftad, ej återkallad, rätt art)"""
    return (
        loc is not None
        and not loc.confirmed
        and loc.assumption != PlaceAssumption.REVOKED
        and _place_kind(loc) in (PlaceKind.WORK, PlaceKind.UNCLEAR)
        and loc.type not in _NEVER_ASSUMED_TYPES
    )


WORKPLACE_NEAR_M = 1000.0
WORKPLACE_QUESTION = "Kan vara din arbetsplats"


def _is_employer_place(db: Session, loc: KnownLocation, *, user_id: uuid.UUID, cfg: dict[str, Any]) -> bool:
    """Bär platsen arbetsgivarens namn, eller ligger den inom 1 km från ett tjänsteställe?
    Då kan det vara arbetsplatsen (en annan byggnad eller parkering) och inte en kund."""
    from app.models import WorkplacePeriod
    from app.services.places.normalize import fold, haversine_m

    employer = fold(cfg.get("employer") or "")
    if len(employer) >= 3:
        for text in (loc.company, loc.name):
            folded = fold(text or "")
            if folded and (folded == employer or employer in folded):
                return True
    if loc.latitude is None or loc.longitude is None:
        return False
    period_ids = {
        i for (i,) in db.execute(
            select(WorkplacePeriod.location_id).where(
                WorkplacePeriod.user_id == user_id, WorkplacePeriod.location_id.is_not(None)
            )
        ).all()
    }
    cond = KnownLocation.type == LocationType.REGULAR_WORKPLACE
    if period_ids:
        cond = cond | KnownLocation.id.in_(period_ids)
    for wp in db.execute(
        select(KnownLocation).where(
            KnownLocation.owner_user_id == user_id, KnownLocation.id != loc.id,
            KnownLocation.latitude.is_not(None), KnownLocation.longitude.is_not(None), cond,
        )
    ).scalars():
        if haversine_m(loc.latitude, loc.longitude, wp.latitude, wp.longitude) <= WORKPLACE_NEAR_M:
            return True
    return False


def assumed_purpose_text(name: str) -> str:
    return f"{ASSUMED_PURPOSE_PREFIX} {name}"


def _ai_says_work(db: Session, trip: LogicalTrip) -> bool:
    """AI-utlåtandet (A.3): den aktuella klassningen har ``ai_assessment`` med riktningen
    Arbete och ingen ``ai_disagreement``."""
    from app.models import TripEvidence
    from app.services.classify.engine import _current_classification

    cls = _current_classification(db, trip.id)
    if cls is None or cls.source != DecisionSource.AI:
        return False
    rows = db.execute(
        select(TripEvidence.kind, TripEvidence.direction).where(
            TripEvidence.logical_trip_id == trip.id, TripEvidence.classification_id == cls.id
        )
    ).all()
    kinds = {(k, d) for k, d in rows}
    if any(k == "ai_disagreement" for k, _d in kinds):
        return False
    return ("ai_assessment", Category.WORK) in kinds


def _ai_purpose(trip: LogicalTrip, ctx: Any, rules: Any) -> str | None:
    """AI:ns ärende, men bara när det har stöd (``based_on.supported``) och fortfarande klarar
    ``validate_generated``."""
    from app.services.classify import purpose as purpose_mod

    if trip.purpose_source != DecisionSource.AI or not trip.purpose:
        return None
    current = _current_purpose(db_of(trip), trip)
    if current is None or (current.based_on or {}).get("supported") is not True:
        return None  # en AI-text utan stöd blir aldrig grund för ett automatiskt godkännande
    text = sanitize_text(trip.purpose)
    if not text:
        return None
    ok, _missing = purpose_mod.validate_generated(text, ctx, rules=rules)
    return text if ok else None


def db_of(obj: Any) -> Session:
    from sqlalchemy.orm import object_session

    session = object_session(obj)
    assert session is not None
    return session


def _current_purpose(db: Session, trip: LogicalTrip) -> TripPurpose | None:
    return db.execute(
        select(TripPurpose)
        .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
        .order_by(TripPurpose.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _calendar_purpose(db: Session, trip: LogicalTrip, ctx: Any, rules: Any) -> tuple[str, tuple[str, ...]] | None:
    """Ärendet ur kalendern när kalendern ger starkt stöd (beslut A).

    I första hand AI:ns text för resan (som redan klarat namnkontrollen när den sparades),
    annars regelmotorns kalenderförslag (bokningens rubrik). Privata bokningar och onlinemöten
    används aldrig. Returnerar (ärende, bokningarnas id) eller None.
    """
    from app.services.classify import purpose as purpose_mod
    from app.services.classify import signals as sig

    if ctx is None or sig.calendar_support(ctx) != "strong":
        return None
    current = _current_purpose(db, trip)
    based = (current.based_on or {}) if current is not None else {}
    if (
        current is not None and current.source == DecisionSource.AI and based.get("calendar") == "strong"
        and current.text and not based.get("fallback")
    ):
        text = sanitize_text(current.text)
        if text:
            return text, tuple(str(i) for i in based.get("calendar_event_ids") or [])
    if rules is None:
        return None
    proposal = purpose_mod.calendar_proposal(ctx, rules)
    if proposal is not None and proposal.based_on.get("calendar") == "strong":
        text = sanitize_text(proposal.text)
        if text:
            return text, (str(proposal.based_on["calendar_event_id"]),)
    return None


def _easter(year: int) -> date:
    """Påskdagen (gregoriansk kalender, Meeus/Jones/Butchers algoritm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def is_swedish_holiday(day: date) -> bool:
    """Svenska röda dagar och aftnar som brukar vara lediga på vardagar."""
    fixed = {(1, 1), (1, 6), (5, 1), (6, 6), (12, 24), (12, 25), (12, 26), (12, 31)}
    if (day.month, day.day) in fixed:
        return True
    easter = _easter(day.year)
    if day in (easter - timedelta(days=2), easter + timedelta(days=1), easter + timedelta(days=39)):
        return True
    # Midsommarafton: fredagen mellan 19 och 25 juni.
    return day.month == 6 and 19 <= day.day <= 25 and day.weekday() == 4


def _in_office_hours(when: datetime | None, cfg: dict[str, Any]) -> bool:
    """Ligger tidpunkten inom kontorstiden (``automation.office_hours``)?"""
    if when is None:
        return False
    hours = (cfg.get("automation") or {}).get("office_hours") or settings_svc.DEFAULT_OFFICE_HOURS
    local = when.astimezone(cfg["tz"])
    if local.weekday() not in set(hours.get("days") or []):
        return False
    if is_swedish_holiday(local.date()):
        return False  # röd dag eller afton: en fråga i stället för ett antagande
    hhmm = local.strftime("%H:%M")
    return str(hours.get("start", "07:00")) <= hhmm < str(hours.get("end", "18:00"))


def _assumption_rule(
    db: Session, trip: LogicalTrip, ctx: Any, rules: Any, *, user_id: uuid.UUID, cfg: dict[str, Any]
) -> AutoDecision | None:
    """A · "Antaget kundbesök" (A.3).

    Anropas bara när spärr 1 och 2 är fria och varken P, R, H eller T har avgjort resan.
    Målet ska vara en obekräftad plats som kartan kallar arbetsplats (``place_kind=work``)
    med namn, eller ``unclear`` med namn där AI-leverantören bedömt resan som tjänst.
    Utanför kontorstid, eller med vetotiden 0, blir antagandet bara en snabbknapp i en fråga.
    """
    dest = ctx.destination
    if not _assumable_place(dest):
        return None
    name = _poi_name(dest)
    if _is_employer_place(db, dest, user_id=user_id, cfg=cfg):
        # Arbetsgivarens namn eller nära tjänstestället: aldrig ett antagande, en vanlig fråga.
        return AutoDecision("ask", reason=WORKPLACE_QUESTION,
                            candidates=tuple(purpose_candidates(db, trip, user_id=user_id)))
    if _place_kind(dest) == PlaceKind.WORK:
        if not name:
            return AutoDecision("ask_company", reason=COMPANY_QUESTION)
        purpose = assumed_purpose_text(name)
    else:
        if not name or not _ai_says_work(db, trip):
            return None
        purpose = _ai_purpose(trip, ctx, rules) or assumed_purpose_text(name)

    others = [c for c in purpose_candidates(db, trip, user_id=user_id) if c != purpose]
    candidates = tuple([purpose, *others][:2])
    if not _in_office_hours(trip.ended_at or trip.sort_time, cfg):
        # Tidpunkten används aldrig som tecken på privat – bara som skäl att fråga.
        return AutoDecision("ask", reason="Utanför kontorstid", candidates=candidates)
    veto_hours = int((cfg.get("automation") or {}).get("assumption_veto_hours") or 0)
    if veto_hours <= 0:
        return AutoDecision("ask", reason="Fråga mig först", candidates=candidates)
    veto_until = (cfg["now"] + timedelta(hours=veto_hours)).isoformat(timespec="seconds")
    return AutoDecision(
        "approve", category=Category.WORK, purpose=purpose, source=ApprovalSource.ASSUMED,
        reason=ASSUMED_REASON, veto_until=veto_until,
    )


def decide(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, ctx: Any, rules: Any, cfg: Any) -> AutoDecision:
    """Beslut enligt regeltabellen (A.2); skriver inget.

    Ger ``wait`` när målets adress fortfarande slås upp och platsen är yngre än 30 minuter.
    """
    cfg = cfg if cfg is not None else config(db, user_id)
    if not eligible(trip, active_since=cfg["active_since"]):
        return SKIP
    if ctx is None or rules is None:
        ctx, rules = _evaluate(db, trip, user_id, cfg)
    dest = ctx.destination
    if (
        dest is not None
        and dest.geocode_status == GeocodeStatus.PENDING
        and dest.created_at is not None
        and cfg["now"] - dest.created_at < WAIT_FOR_ADDRESS
    ):
        return AutoDecision("wait", reason="Adressen slås upp")

    blocked = _hard_block(trip, rules)
    if blocked is not None:
        multi = blocked == MULTI_TRIP_TEXT
        cands = () if multi else tuple(purpose_candidates(db, trip, user_id=user_id))
        return AutoDecision("ask", reason=blocked, candidates=cands)
    lunch_match = getattr(ctx, "lunch", None)
    if lunch_match is not None and lunch_match.status == "awaiting_return":
        # Ingen fråga om utresan medan ägaren äter: beslutet tas när återresan är klar.
        return AutoDecision("wait", reason=LUNCH_WAIT_TEXT)

    private_block = _private_block(ctx, rules)
    dest_kind = _destination_kind(ctx)

    decision = (
        _commute_rule(trip, ctx, cfg)
        or _private_place_rule(ctx)
        or _own_rule(
            db, trip, ctx, user_id=user_id,
            # Hotell stoppar inte ägarens egen regel.
            work_allowed=not private_block and dest_kind in (None, "hotel"), rules=rules,
        )
        or _home_office_rule(db, trip, ctx, rules, user_id=user_id, cfg=cfg)
        or _lunch_rule(ctx)
        or _evening_rule(trip, ctx, cfg)
        or _history_rule(
            db, trip, ctx, user_id=user_id, cfg=cfg, work_allowed=not private_block and dest_kind is None,
            rules=rules,
        )
        or _return_rule(trip, ctx, cfg=cfg, private_block=private_block, db=db, rules=rules)
    )
    if decision is None and not private_block and dest_kind is None:
        decision = _assumption_rule(db, trip, ctx, rules, user_id=user_id, cfg=cfg)
    if decision is not None:
        return decision
    return AutoDecision("ask", candidates=tuple(purpose_candidates(db, trip, user_id=user_id)))


# ----------------------------------------------------------------------------- genomförande


def _approve(db: Session, trip: LogicalTrip, d: AutoDecision, *, actor: audit.Actor) -> bool:
    from app.services.trips import segments as seg

    if d.category not in (Category.WORK, Category.PRIVATE) or d.source not in AUTO_APPROVAL_SOURCES:
        return False
    purpose = sanitize_text(d.purpose) if d.category == Category.WORK else None
    if d.category == Category.WORK and not purpose:
        return False  # en tjänsteresa godkänns aldrig utan ärende
    now = utcnow()
    before = {"category": trip.category, "review_status": trip.review_status, "purpose": trip.purpose}
    category_source = DecisionSource.RULE if d.source in _RULE_SOURCES else DecisionSource.PATTERN

    trip.category = d.category
    trip.category_source = category_source
    trip.review_status = ReviewStatus.APPROVED
    trip.tax_note = None
    if purpose:
        if trip.purpose != purpose or not trip.purpose_confirmed or d.calendar_event_ids:
            db.execute(
                update(TripPurpose)
                .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
                .values(is_current=False)
            )
            based_on: dict[str, Any] = {"autopilot": d.source}
            if d.calendar_event_ids:
                # Ärendet kommer ur kalendern (beslut A): syns i månadens genomgång och kan rättas.
                based_on.update({"from_calendar": True, "calendar_event_ids": list(d.calendar_event_ids)})
            db.add(
                TripPurpose(
                    logical_trip_id=trip.id, text=purpose, source=DecisionSource.PATTERN,
                    rationale=("Ärende ur kalendern. " if d.calendar_event_ids else "")
                    + (d.reason or "Godkänd automatiskt."),
                    needs_confirmation=False, based_on=based_on, created_by=actor.label,
                )
            )
        trip.purpose = purpose
        trip.purpose_source = DecisionSource.PATTERN
        trip.purpose_confirmed = True
    elif d.category == Category.PRIVATE and not trip.purpose_confirmed:
        trip.purpose, trip.purpose_source = None, None  # privata resor behöver inget ärende
    if d.note and not (trip.notes or "").strip():
        trip.notes = d.note
    trip.approval_source = d.source
    trip.approval_basis = {
        "pattern_id": str(d.pattern_id) if d.pattern_id else None,
        "related_trip_id": str(d.related_trip_id) if d.related_trip_id else None,
        "reason": d.reason,
        "veto_until": d.veto_until,
    }
    if d.calendar_event_ids:
        trip.approval_basis["from_calendar"] = True
    trip.auto_approved_at = now
    trip.updated_by = actor.label
    seg.refresh_logical(db, trip)

    db.execute(
        update(TripClassification)
        .where(TripClassification.logical_trip_id == trip.id, TripClassification.is_current.is_(True))
        .values(is_current=False)
    )
    db.add(
        TripClassification(
            logical_trip_id=trip.id,
            source=category_source,
            category=trip.category,
            subcategory=trip.subcategory,
            confidence=None,
            requires_review=False,
            source_quality="limited",
            signals={"autopilot": d.source, "reason": d.reason},
            created_by=actor.label,
        )
    )
    audit.record(
        db, actor, "trip.auto_approved", "logical_trip", trip.id, before=before,
        after={"category": trip.category, "purpose": trip.purpose, "approval_source": d.source,
               "reason": d.reason},
    )
    db.flush()
    return True


def apply(db: Session, trip: LogicalTrip, d: AutoDecision, *, actor: audit.Actor) -> bool:
    """Genomför ett beslut. Returnerar True när resan ändrades.

    * approve: godkänd med kategori, ärende och ``approval_source``; ingen LearningFeedback.
    * ask/ask_company: står resan som AUTO blir den PROPOSED, så att frågan ställs.
    * ask_purpose: Arbete, PROPOSED (ärendet frågas efter).
    """
    from app.services.trips import segments as seg

    if trip.state != LogicalState.ACTIVE or trip.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED):
        return False
    if d.outcome == "approve":
        approved = _approve(db, trip, d, actor=actor)
        if approved and d.source == ApprovalSource.ASSUMED and d.related_trip_id is None:
            _after_assumption(db, trip)
        return approved
    if d.outcome in ("ask", "ask_company"):
        if trip.review_status == ReviewStatus.AUTO:
            trip.review_status = ReviewStatus.PROPOSED
            trip.updated_by = actor.label
            db.flush()
            return True
        return False
    if d.outcome == "ask_purpose":
        if trip.category == Category.WORK and trip.review_status == ReviewStatus.PROPOSED:
            return False
        trip.category = Category.WORK
        trip.category_source = DecisionSource.RULE
        trip.review_status = ReviewStatus.PROPOSED
        trip.updated_by = actor.label
        seg.refresh_logical(db, trip)
        db.flush()
        return True
    return False


def run_vehicle(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, actor: audit.Actor) -> dict[str, int]:
    """decide + apply för alla resor som omfattas, i tidsordning (så att T ser utresan)."""
    from app.services.trips import segments as seg

    if not is_active(db, user_id):
        return {}
    cfg = config(db, user_id)
    out: dict[str, int] = {}
    for trip in seg.active_trips(db, vehicle.id):
        if not eligible(trip, active_since=cfg["active_since"]):
            continue
        d = decide(db, trip, user_id=user_id, ctx=None, rules=None, cfg=cfg)
        if apply(db, trip, d, actor=actor) and d.outcome == "approve":
            out["approved"] = out.get("approved", 0) + 1
            if d.source == ApprovalSource.LUNCH:
                _cancel_questions(db, trip.id)  # t.ex. frågan om utresan när återresan kom sent
        elif d.outcome != "approve":
            out[d.outcome] = out.get(d.outcome, 0) + 1
    db.flush()
    return out


def _cancel_questions(db: Session, trip_id: uuid.UUID) -> None:
    try:
        from app.services.ha import notifier

        notifier.cancel_for_trip(db, trip_id)
    except Exception:  # notiser får aldrig stoppa ett beslut
        import logging

        logging.getLogger(__name__).exception("Kunde inte avbryta frågan om resan")


def reconsider(db: Session, *, user_id: uuid.UUID, trip_ids: list[uuid.UUID], actor: audit.Actor) -> None:
    """Prövar resorna igen när adressen blivit klar: ny klassning (AI tillåten), beslut
    och granskningsärenden."""
    from app.services.classify import engine
    from app.services.trips import pipeline
    from app.services.trips import segments as seg

    if not is_active(db, user_id):
        return None
    provider = engine.get_provider(db, user_id)
    cache: dict[str, Any] = {}
    vehicles: dict[uuid.UUID, Vehicle] = {}
    for trip_id in trip_ids:
        trip = seg.load_trip(db, trip_id)
        if trip is None or trip.state != LogicalState.ACTIVE:
            continue
        vehicle = db.get(Vehicle, trip.vehicle_id)
        if vehicle is None or vehicle.owner_user_id != user_id:
            continue
        engine.classify_trip(db, trip, user_id=user_id, actor=actor, provider=provider, use_llm=True,
                             settings_cache=cache)
        vehicles[vehicle.id] = vehicle
    for vehicle in vehicles.values():
        run_vehicle(db, user_id=user_id, vehicle=vehicle, actor=actor)
        for trip in seg.active_trips(db, vehicle.id):
            pipeline.sync_trip_review_items(db, trip)
    db.flush()
    return None


# ----------------------------------------------------------------------------- frågor och regler


def purpose_candidate_items(
    db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, limit: int = 2
) -> list[tuple[str, str]]:
    """Ärendeknappar med ursprung, i ordningen i byggkontraktet 3.E.

    1. resans aktuella ärende ur kalendern ("calendar"),
    2. platsantagandet "Kundbesök <namn>" ("assumed"),
    3. ägarens tidigare bekräftade ärenden för rutten, sedan målet ("history"),
    4. AI:ns fria förslag ("ai") – bara när punkt 3 inte gav något.
    Alla tvättas och kapas till 80 tecken.
    """
    from app.services.classify.purpose import NEEDS_CONFIRMATION_SUFFIX
    from app.services.learning import patterns as patterns_svc
    from app.services.places.normalize import fold

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(text: str | None, origin: str, key: str | None = None) -> None:
        clean = sanitize_text(text, limit=80)
        if not clean or NEEDS_CONFIRMATION_SUFFIX in fold(clean):
            return
        k = key or fold(clean)
        if k in seen or fold(clean) in seen:
            return
        seen.add(k)
        seen.add(fold(clean))
        out.append((clean, origin))

    current = _current_purpose(db, trip) if trip.purpose else None
    based = (current.based_on or {}) if current is not None else {}
    if current is not None and not trip.purpose_confirmed and (
        based.get("calendar_event_id") or based.get("calendar_event_ids")
    ):
        add(current.text, "calendar")
    # Platsantagandet som snabbknapp (A.3): "Tjänst: Kundbesök <namn>", t.ex. utanför
    # kontorstid eller med vetotiden 0.
    place = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    name = _poi_name(place) if _assumable_place(place) else None
    if name and (place.place_kind == PlaceKind.WORK or _ai_says_work(db, trip)):  # type: ignore[union-attr]
        add(assumed_purpose_text(name), "assumed")
    route, dest = patterns_svc.get_patterns(db, user_id, trip.start_location_id, trip.end_location_id)
    history = 0
    for pat in (route, dest):
        items = [p for p in patterns_svc.top_purposes(pat, limit=12) if p.get("category") == Category.WORK]
        items.sort(key=lambda p: (-p.get("user_count", 0), -p.get("count", 0)))
        for p in items:
            before = len(out)
            add(p.get("text"), "history", p.get("key"))
            history += len(out) - before
    if history == 0 and current is not None and current.source == DecisionSource.AI and not trip.purpose_confirmed \
            and not (based.get("calendar_event_id") or based.get("calendar_event_ids")):
        add(current.text, "ai")
    return out[:limit]


def candidate_origin(db: Session, trip: LogicalTrip, text: str) -> str:
    """Varifrån en ärendeknapp kommer: "calendar" eller "ai" när texten är resans obekräftade
    förslag ur kalendern eller från AI, annars "history" (ägarens bekräftade ärende eller
    platsantagandet)."""
    if trip.purpose_confirmed or not trip.purpose:
        return "history"
    current = _current_purpose(db, trip)
    if current is None or sanitize_text(current.text, limit=80) != sanitize_text(text, limit=80):
        return "history"
    based = current.based_on or {}
    if based.get("calendar_event_id") or based.get("calendar_event_ids"):
        return "calendar"
    if current.source == DecisionSource.AI:
        return "ai"
    return "history"


def purpose_candidates(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, limit: int = 2) -> list[str]:
    """Ärendeknappar (texterna ur ``purpose_candidate_items``)."""
    return [text for text, _origin in purpose_candidate_items(db, trip, user_id=user_id, limit=limit)]


def _remember_blocked(loc: KnownLocation | None, db: Session, user_id: uuid.UUID) -> bool:
    """"Alltid" erbjuds inte för hemmet, privata platser, tjänstestället eller hotell."""
    from app.services.classify import signals as sig

    if loc is None:
        return True
    if loc.type in (LocationType.HOME, LocationType.PRIVATE, LocationType.REGULAR_WORKPLACE, LocationType.HOTEL):
        return True
    _ids, _home, with_periods = sig._workplace_ids(db, user_id, None)
    return loc.id in with_periods


def _confirmed_purpose_here(db: Session, trip: LogicalTrip, text: str, abbreviations: dict[str, str]) -> bool:
    """Har ägaren tidigare bekräftat just det här ärendet för platsen?"""
    from app.services.classify.textnorm import normalize_purpose

    wanted = normalize_purpose(text, abbreviations)
    if wanted is None:
        return False
    rows = db.execute(
        select(LogicalTrip).where(
            LogicalTrip.vehicle_id == trip.vehicle_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.end_location_id == trip.end_location_id,
            LogicalTrip.id != trip.id,
            LogicalTrip.category == Category.WORK,
            LogicalTrip.purpose_confirmed.is_(True),
            LogicalTrip.review_status.in_((ReviewStatus.APPROVED, ReviewStatus.IMPORTED)),
        )
    ).scalars()
    for t in rows:
        if t.review_status == ReviewStatus.APPROVED and not _owner_confirmed(t):
            continue  # automatiken räknas aldrig som ägarens bekräftelse
        norm = normalize_purpose(t.purpose, abbreviations)
        if norm is not None and norm.key == wanted.key:
            return True
    return False


def can_remember(db: Session, trip: LogicalTrip, text: str | None) -> bool:
    """Om "Alltid" får erbjudas för resan.

    Målet ska finnas och inte vara hemmet, en privat plats, det ordinarie tjänstestället
    eller ett hotell. För Arbete (``text`` satt) ska hela texten rymmas i 40 tecken och
    vara ett ärende som ägaren tidigare bekräftat för platsen, eller "Kundbesök <företag>".
    """
    if trip.end_location_id is None:
        return False
    vehicle = db.get(Vehicle, trip.vehicle_id)
    if vehicle is None:
        return False
    dest = db.get(KnownLocation, trip.end_location_id)
    if _remember_blocked(dest, db, vehicle.owner_user_id):
        return False
    if text is None:
        return True
    clean = sanitize_text(text, limit=10_000)
    if not clean or len(clean) > REMEMBER_MAX_PURPOSE:
        return False
    company = sanitize_text(dest.company) if dest is not None else None
    if company and clean.casefold() == f"kundbesök {company}".casefold():
        return True
    abbreviations = settings_svc.get_section(db, vehicle.owner_user_id, "abbreviations")["map"]
    return _confirmed_purpose_here(db, trip, clean, abbreviations)


def remember_always(
    db: Session, *, user_id: uuid.UUID, trip: LogicalTrip, category: str, purpose: str | None, actor: audit.Actor
) -> LocationPattern:
    """Sparar ägarens egen regel för målet ("Alltid …"); kräver ``can_remember``.

    "Alltid privat här" gör dessutom målet till en bekräftad privat plats, så att resor
    därifrån och hem blir privata (P).
    """
    from app.services.learning import patterns as patterns_svc

    vehicle = db.get(Vehicle, trip.vehicle_id)
    if vehicle is None or vehicle.owner_user_id != user_id:
        raise ValueError("Det går inte att spara en regel för den här resan.")
    if category not in (Category.WORK, Category.PRIVATE):
        raise ValueError("Det går inte att spara en regel för den här resan.")
    text = sanitize_text(purpose) if category == Category.WORK else None
    if category == Category.WORK and not text:
        raise ValueError("Ett tjänsteärende behövs för regeln.")
    if not can_remember(db, trip, text):
        raise ValueError("Det går inte att spara en regel för den här resan.")
    dest = db.get(KnownLocation, trip.end_location_id)
    assert dest is not None
    pat = patterns_svc.set_manual_rule(
        db, user_id, origin_id=None, dest_id=dest.id, category=category, purpose=text
    )
    pat.manual_purpose_mode = None  # "Alltid: …" – kalendern går före regelns text (standard)
    if category == Category.PRIVATE:
        dest.type = LocationType.PRIVATE
        dest.confirmed = True
    audit.record(
        db, actor, "pattern.remember_always", "location_pattern", pat.id,
        after={"category": category, "purpose": text, "location_id": str(dest.id)}, user_id=user_id,
    )
    db.flush()
    # Väntande resor till samma mål avgörs direkt av den nya regeln.
    run_vehicle(db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM)
    from app.services.trips import pipeline
    from app.services.trips import segments as seg

    for t in seg.active_trips(db, vehicle.id):
        pipeline.sync_trip_review_items(db, t)
    db.flush()
    return pat


def _set_ask_always(db: Session, pattern_id: Any, actor: audit.Actor) -> LocationPattern | None:
    try:
        pid = uuid.UUID(str(pattern_id))
    except (TypeError, ValueError):
        return None
    pat = db.get(LocationPattern, pid)
    if pat is None or not pat.is_manual_rule:
        return None
    before = {"category": pat.manual_category, "purpose": pat.manual_purpose}
    pat.is_manual_rule = True
    pat.manual_category = Category.UNDECIDED  # "fråga mig alltid"
    pat.manual_purpose = None
    pat.status = PatternStatus.APPROVED
    audit.record(db, actor, "pattern.ask_always", "location_pattern", pat.id, before=before,
                 after={"category": Category.UNDECIDED}, user_id=pat.owner_user_id)
    db.flush()
    return pat


def on_owner_decision(db: Session, trip: LogicalTrip, *, before: dict[str, Any], actor: audit.Actor) -> str | None:
    """Följder av ägarens beslut på en automatisk resa (A.2). Returnerar text till ägaren.

    * R-resa som rättas: regeln blir "fråga alltid".
    * H-resa: rättelsen räknas i historiken, så H gäller inte längre för rutten.
    * Antagen resa: A.3 (b) (S1d).
    * En godkänd tjänsteresa som får annan kategori eller annat ärende, oavsett vem som
      godkände den: hemresor som T godkänt efter den ("Hemresa efter …") öppnas igen och
      får en fråga, eftersom deras ärende byggde på utresan.
    """
    source = before.get("approval_source")
    corrected = before.get("category") != trip.category or (before.get("purpose") or None) != (trip.purpose or None)
    if not corrected:
        return None
    text: str | None = None
    if source == ApprovalSource.RULE:
        pat = _set_ask_always(db, (before.get("approval_basis") or {}).get("pattern_id"), actor)
        if pat is not None:
            dest = db.get(KnownLocation, pat.destination_location_id)
            text = f"Regeln för {_label(dest)} frågar dig nu i stället."
    elif source == ApprovalSource.ASSUMED:
        text = _on_assumed_correction(db, trip, before=before, actor=actor)
    if before.get("review_status") == ReviewStatus.APPROVED and before.get("category") == Category.WORK:
        if _reopen_return_trips(db, trip, actor=actor):
            extra = "Hemresan efter den frågas om igen."
            text = f"{text} {extra}" if text else extra
    return text


def _reopen(db: Session, t: LogicalTrip, *, actor: audit.Actor, user_id: uuid.UUID, action: str, reason: str) -> None:
    """Öppnar en automatiskt godkänd resa igen (``proposed``, ``approval_source`` NULL) med en
    fråga. Ligger den i en månad som redan har ett automatiskt underlag blir månaden
    ``stale``; resan försvinner då ur avtrycket och filen ersätts när frågan är besvarad."""
    from app.services.trips import pipeline

    mc = _exported_month(db, t.vehicle_id, _trip_month(db, t, user_id))
    if mc is not None and mc.state != MonthCloseState.STALE:
        mc.state = MonthCloseState.STALE
        audit.record(db, actor, "month_close.stale", "month_close", mc.id,
                     after={"month": mc.month, "reason": reason}, user_id=user_id)
    old = {"review_status": t.review_status, "approval_source": t.approval_source}
    t.review_status = ReviewStatus.PROPOSED
    t.approval_source = None
    t.approval_basis = None
    t.auto_approved_at = None
    t.purpose_confirmed = False
    t.updated_by = actor.label
    audit.record(db, actor, action, "logical_trip", t.id, before=old,
                 after={"review_status": ReviewStatus.PROPOSED, "approval_source": None}, user_id=user_id)
    db.flush()
    pipeline.sync_trip_review_items(db, t)


def _reopen_return_trips(db: Session, trip: LogicalTrip, *, actor: audit.Actor) -> int:
    """Hemresor (T, även antagna) som fortfarande bygger på ``trip`` öppnas igen."""
    vehicle = db.get(Vehicle, trip.vehicle_id)
    if vehicle is None:
        return 0
    count = 0
    for t in db.execute(
        select(LogicalTrip).where(
            LogicalTrip.vehicle_id == trip.vehicle_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.review_status == ReviewStatus.APPROVED,
            LogicalTrip.approval_source.in_((ApprovalSource.RETURN_TRIP, ApprovalSource.ASSUMED)),
            LogicalTrip.id != trip.id,
        )
    ).scalars():
        if str((t.approval_basis or {}).get("related_trip_id") or "") != str(trip.id):
            continue
        _reopen(db, t, actor=actor, user_id=vehicle.owner_user_id, action="trip.return_trip_reopened",
                reason="outbound_corrected")
        count += 1
    db.flush()
    return count


class AssumptionError(TripError, ValueError):
    """Antagandet eller företagsnamnet kan inte sparas. Är ett TripError, så att svaret
    från telefonen blir "invalid" med texten, och ett ValueError som remember_always."""

    def __init__(self, message: str):
        super().__init__(message, "assumption")


def _after_assumption(db: Session, trip: LogicalTrip) -> None:
    """Första antagandet för en plats: platsen blir "antagen kund" och notisen köas
    (``notices.queue_assumed`` skickar bara första gången per plats)."""
    from app.services.ha import notices

    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    if dest is None:
        return
    if dest.assumption is None:
        dest.assumption = PlaceAssumption.CUSTOMER
    vehicle = db.get(Vehicle, trip.vehicle_id)
    if vehicle is None:
        return
    db.flush()
    try:
        notices.queue_assumed(db, user_id=vehicle.owner_user_id, trip=trip, now=utcnow())
    except Exception:  # notisen får aldrig stoppa beslutet
        import logging

        logging.getLogger(__name__).exception("Kunde inte köa notisen om antagandet")


def _trip_month(db: Session, trip: LogicalTrip, user_id: uuid.UUID) -> str | None:
    when = trip.ended_at or trip.sort_time
    if when is None:
        return None
    local = when.astimezone(settings_svc.user_timezone(db, user_id))
    return f"{local.year:04d}-{local.month:02d}"


def _exported_month(db: Session, vehicle_id: uuid.UUID, month: str | None) -> Any:
    """Månadsskiftets rad när månaden redan har ett automatiskt underlag, annars None."""
    from app.models import MonthClose

    if month is None:
        return None
    mc = db.execute(
        select(MonthClose).where(MonthClose.vehicle_id == vehicle_id, MonthClose.month == month)
    ).scalar_one_or_none()
    if mc is not None and mc.state in (MonthCloseState.EXPORTED, MonthCloseState.STALE):
        return mc
    return None


def _on_assumed_correction(db: Session, trip: LogicalTrip, *, before: dict[str, Any], actor: audit.Actor) -> str | None:
    """A.3 (b): "Fel" på en antagen resa.

    Platsens antagande återkallas (``assumption="revoked"``). Övriga antagna resor dit, och
    antagna hemresor efter dem, öppnas igen (``proposed``, ``approval_source`` NULL) och får
    frågor, också i en månad som redan har ett automatiskt underlag. Den månaden blir
    ``stale``, och filen ersätts när frågorna är besvarade. Byter ägaren bara ärende
    ("Annat ärende") återkallas inget.
    """

    if trip.category == before.get("category"):
        return None
    basis = before.get("approval_basis") or {}
    dest_id = trip.end_location_id
    if basis.get("related_trip_id"):
        # En antagen hemresa (T) som rättas: antagandet gäller utresans mål.
        try:
            outbound = db.get(LogicalTrip, uuid.UUID(str(basis["related_trip_id"])))
        except (TypeError, ValueError):
            outbound = None
        dest_id = outbound.end_location_id if outbound is not None else None
    dest = db.get(KnownLocation, dest_id) if dest_id else None
    vehicle = db.get(Vehicle, trip.vehicle_id)
    if dest is None or vehicle is None:
        return None
    user_id = vehicle.owner_user_id
    before_assumption = dest.assumption
    dest.assumption = PlaceAssumption.REVOKED
    audit.record(db, actor, "location.assumption_revoked", "known_location", dest.id,
                 before={"assumption": before_assumption}, after={"assumption": PlaceAssumption.REVOKED},
                 user_id=user_id)

    assumed = list(
        db.execute(
            select(LogicalTrip)
            .join(Vehicle, Vehicle.id == LogicalTrip.vehicle_id)
            .where(
                Vehicle.owner_user_id == user_id,
                LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.approval_source == ApprovalSource.ASSUMED,
                LogicalTrip.id != trip.id,
            )
            .order_by(LogicalTrip.sort_time)
        ).scalars()
    )
    affected = {t.id for t in assumed if t.end_location_id == dest.id}
    wrong = affected | {trip.id}
    # Antagna hemresor efter en felaktig utresa är lika fel.
    for t in assumed:
        related = (t.approval_basis or {}).get("related_trip_id")
        if related and related in {str(i) for i in wrong}:
            affected.add(t.id)

    reopened = 0
    for t in assumed:
        if t.id not in affected:
            continue
        _reopen(db, t, actor=actor, user_id=user_id, action="trip.assumption_reopened", reason="assumption_revoked")
        reopened += 1
    db.flush()
    text = f"{_label(dest)} räknas inte längre som kund."
    if reopened == 1:
        text += " 1 annan antagen resa frågas om igen."
    elif reopened > 1:
        text += f" {reopened} andra antagna resor frågas om igen."
    return text


def _make_customer(
    db: Session, trip: LogicalTrip, name: str, *, actor: audit.Actor, action: str
) -> tuple[KnownLocation, LocationPattern, uuid.UUID]:
    """Regel (c): platsen blir bekräftad kund med en egen regel "Kundbesök <namn>"."""
    from app.services.classify.engine import _name_is_address
    from app.services.learning import patterns as patterns_svc

    if trip.state != LogicalState.ACTIVE:
        raise AssumptionError("Resan är inte aktiv.")
    vehicle = db.get(Vehicle, trip.vehicle_id)
    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    if vehicle is None or dest is None:
        raise AssumptionError("Resan har ingen plats att spara företaget på.")
    if dest.type in _NEVER_ASSUMED_TYPES and dest.confirmed:
        raise AssumptionError("Platsen kan inte bli en kund.")
    user_id = vehicle.owner_user_id
    before = {"type": dest.type, "confirmed": dest.confirmed, "company": dest.company, "assumption": dest.assumption}
    dest.company = name
    if _name_is_address(dest):
        dest.name = name
    dest.type = LocationType.CUSTOMER
    dest.confirmed = True
    dest.assumption = PlaceAssumption.CUSTOMER
    purpose = assumed_purpose_text(name)
    pat = patterns_svc.set_manual_rule(
        db, user_id, origin_id=None, dest_id=dest.id, category=Category.WORK, purpose=purpose
    )
    pat.manual_purpose_mode = None
    audit.record(db, actor, action, "known_location", dest.id, before=before,
                 after={"type": dest.type, "confirmed": True, "company": name, "rule_id": str(pat.id)},
                 user_id=user_id)
    db.flush()
    return dest, pat, user_id


def confirm_assumption(db: Session, trip: LogicalTrip, *, actor: audit.Actor) -> None:
    """"Stämmer" på en antagen resa (A.3 c).

    Platsen blir bekräftad kund (``type=customer``, ``confirmed=True``, ``company``) med en
    egen regel "Kundbesök <namn>", så att R gäller framöver. Resan blir ägarens beslut.
    """
    from app.services.learning import feedback

    if trip.approval_source != ApprovalSource.ASSUMED or trip.category != Category.WORK:
        raise AssumptionError("Resan är inte längre antagen.")
    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    basis = trip.approval_basis or {}
    if basis.get("related_trip_id"):
        raise AssumptionError("Svara på resan till kunden i stället.")
    name = _poi_name(dest)
    if dest is None or not name:
        raise AssumptionError("Platsen saknar namn.")
    _dest, _pat, user_id = _make_customer(db, trip, name, actor=actor, action="location.assumption_confirmed")
    feedback.apply_decision(
        db, trip, user_id=user_id, actor=actor, channel="ha" if actor.type == "ha" else "web",
        category=Category.WORK, purpose=trip.purpose, approve=True,
    )


def set_company(db: Session, trip: LogicalTrip, text: str, *, actor: audit.Actor) -> None:
    """Företagsnamnet för platsen ("Vilket företag var det?", A.3).

    Texten tvättas. Platsen blir bekräftad kund med regeln "Kundbesök <namn>" och resan
    godkänns som tjänsteresa med det ärendet.
    """
    from app.services.learning import feedback

    name = sanitize_text(text, limit=COMPANY_MAX)
    if not name:
        raise AssumptionError("Skriv företagets namn.")
    _dest, _pat, user_id = _make_customer(db, trip, name, actor=actor, action="location.company_set")
    feedback.apply_decision(
        db, trip, user_id=user_id, actor=actor, channel="ha" if actor.type == "ha" else "web",
        category=Category.WORK, purpose=assumed_purpose_text(name), approve=True,
    )


def classify_place(osm_category: str | None, osm_type: str | None) -> str:
    """Platsens art ur kartans typ (tabellen i A.3): ``work``, ``never`` eller ``unclear``."""
    from app.services.places import geocode

    return geocode.classify_place(osm_category, osm_type)


# ----------------------------------------------------------------------------- ärendet ur kalendern


def reject_calendar_purpose(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, actor: audit.Actor) -> None:
    """"Ärendet stämmer inte" (beslut A): kalenderträffen avvisas och ärendet blir regelns.

    Bokningarna som ärendet byggde på väljs inte igen för resan (``matching.set_user_choice``),
    ärendet sätts till regelns eller historikens förslag utan kalendern (3.E steg 2–3) eller
    töms, och blir obekräftat så att ett ärende om syftet öppnas. Kategorin och godkännandet
    står kvar. Ett ärende som ägaren själv har skrivit rörs aldrig.
    """
    from app.models import CalendarMatch
    from app.services.calendar import matching
    from app.services.classify import purpose as purpose_mod

    if trip.state != LogicalState.ACTIVE:
        raise TripError("Resan är inte aktiv.", "not_active")
    current = _current_purpose(db, trip)
    based = (current.based_on or {}) if current is not None else {}
    ids = [str(i) for i in (based.get("calendar_event_ids") or [])]
    if based.get("calendar_event_id"):
        ids.append(str(based["calendar_event_id"]))
    if current is None or not ids or trip.purpose_source == DecisionSource.USER:
        raise TripError("Resans ärende kommer inte ur kalendern.", "not_from_calendar")
    before = {"purpose": trip.purpose, "purpose_confirmed": trip.purpose_confirmed, "calendar_event_ids": ids}
    for m in db.execute(select(CalendarMatch).where(CalendarMatch.logical_trip_id == trip.id)).scalars():
        if str(m.calendar_event_id) in ids:
            matching.set_user_choice(db, trip, m, selected=False)
    db.flush()
    cfg = config(db, user_id)
    ctx, rules = _evaluate(db, trip, user_id, cfg)
    proposal = purpose_mod.propose(db, ctx, rules)
    if proposal is not None and purpose_mod.is_calendar_based(proposal):
        proposal = None  # (en annan bokning) – ägaren har just sagt att kalendern inte stämmer
    db.execute(
        update(TripPurpose)
        .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
        .values(is_current=False)
    )
    if proposal is not None and trip.work_km > 0:
        db.add(TripPurpose(
            logical_trip_id=trip.id, text=proposal.text, source=proposal.source,
            rationale="Ärendet ur kalendern stämde inte. " + proposal.rationale, needs_confirmation=True,
            based_on={**proposal.based_on, "calendar_rejected": True}, created_by=actor.label,
        ))
        trip.purpose, trip.purpose_source = proposal.text, proposal.source
    else:
        trip.purpose, trip.purpose_source = None, None
    trip.purpose_confirmed = False
    trip.updated_by = actor.label
    if isinstance(trip.approval_basis, dict) and trip.approval_basis.get("from_calendar"):
        trip.approval_basis = {**trip.approval_basis, "from_calendar": None}
    audit.record(db, actor, "trip.calendar_purpose_rejected", "logical_trip", trip.id, before=before,
                 after={"purpose": trip.purpose, "purpose_confirmed": False}, user_id=user_id)
    db.flush()
