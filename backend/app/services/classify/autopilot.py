"""Självgående läge: regeltabellen (A.2), platsantagandet (A.3) och ägarens rättelser.

Autopiloten gäller bara live-resor som skapats efter ``automation.active_since``, som är
aktiva och som ingen ännu har godkänt (``eligible``). ``decide`` prövar reglerna i
byggkontraktets ordning och skriver ingenting; ``apply`` genomför beslutet.

* Spärr 1 (ingen automatik): mätarställning saknas, flera körningar kan ha skett, resan
  blandar privat och tjänst, kalendern säger emot fordonsdata eller ägarens regel
  "fråga alltid".
* Spärr 2 (aldrig automatisk Arbete): någon signal pekar på Privat, motstridiga signaler,
  rutten bostad ↔ ordinarie tjänsteställe, eller målet är hemmet (utom T), en privat
  plats, tjänstestället eller ett hotell (utom egen regel R).
* Reglerna P, R, H, T och A; allt annat blir EN fråga.

Automatiskt godkända resor bär ``approval_source`` och räknas aldrig som ägarbekräftad
historik. Importerade och manuellt inlagda resor rörs aldrig. Platsantagandet (A) och
"Stämmer"/företagsnamnet byggs av S1d.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    PatternStatus,
    ReviewStatus,
    TripSource,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.text_safety import sanitize_text

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

# Kategorikälla på resan: ägarens egna regler räknas som "rule", det inlärda som "pattern".
# Aldrig "user" eller "ha", så att avstämningen mot importen beter sig som förut.
_RULE_SOURCES = frozenset({ApprovalSource.RULE, ApprovalSource.COMMUTE, ApprovalSource.PRIVATE_PLACE})


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
    }


def _evaluate(db: Session, trip: LogicalTrip, user_id: uuid.UUID, cfg: dict[str, Any]) -> tuple[Any, Any]:
    """Regelmotorns underlag (TripContext och RuleResult) för resan, utan AI."""
    from app.services.classify import signals as sig

    ctx = sig.build_context(
        db, trip, user_id=user_id, tz=cfg["tz"], tax=cfg["tax"], ai=cfg["ai"], abbreviations=cfg["abbreviations"]
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
    """Spärr 2, signaldelen: något pekar på Privat, eller resan är en arbetsresa."""
    if any(s.direction == Category.PRIVATE for s in rules.signals):
        return True
    if "Motstridiga signaler" in (rules.caps or []):
        return True
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
    db: Session, trip: LogicalTrip, ctx: Any, *, user_id: uuid.UUID, work_allowed: bool
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
    purpose = sanitize_text(rule.manual_purpose)
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
    db: Session, trip: LogicalTrip, ctx: Any, *, user_id: uuid.UUID, cfg: dict[str, Any], work_allowed: bool
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
    return AutoDecision(
        "approve", category=category, purpose=purpose, source=ApprovalSource.HISTORY,
        pattern_id=ctx.route_pattern.id if ctx.route_pattern is not None else None,
        reason=f"Som de {HISTORY_MIN} senaste gångerna",
    )


def _return_rule(trip: LogicalTrip, ctx: Any, *, cfg: dict[str, Any], private_block: bool) -> AutoDecision | None:
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
    return AutoDecision(
        "approve", category=Category.WORK, purpose=sanitize_text(purpose),
        source=ApprovalSource.ASSUMED if assumed else ApprovalSource.RETURN_TRIP,
        related_trip_id=prev.id, reason=purpose.rstrip(". "),
        veto_until=veto,
    )


def _assumption_rule(
    db: Session, trip: LogicalTrip, ctx: Any, rules: Any, *, user_id: uuid.UUID, cfg: dict[str, Any]
) -> AutoDecision | None:
    """A · "Antaget kundbesök" (A.3). Byggs av S1d; tills dess gäller den aldrig."""
    return None


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

    private_block = _private_block(ctx, rules)
    dest_kind = _destination_kind(ctx)

    decision = (
        _commute_rule(trip, ctx, cfg)
        or _private_place_rule(ctx)
        or _own_rule(
            db, trip, ctx, user_id=user_id,
            # Hotell stoppar inte ägarens egen regel.
            work_allowed=not private_block and dest_kind in (None, "hotel"),
        )
        or _history_rule(
            db, trip, ctx, user_id=user_id, cfg=cfg, work_allowed=not private_block and dest_kind is None
        )
        or _return_rule(trip, ctx, cfg=cfg, private_block=private_block)
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
        if trip.purpose != purpose or not trip.purpose_confirmed:
            db.execute(
                update(TripPurpose)
                .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
                .values(is_current=False)
            )
            db.add(
                TripPurpose(
                    logical_trip_id=trip.id, text=purpose, source=DecisionSource.PATTERN,
                    rationale=d.reason or "Godkänd automatiskt.", needs_confirmation=False,
                    based_on={"autopilot": d.source}, created_by=actor.label,
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
        return _approve(db, trip, d, actor=actor)
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
        elif d.outcome != "approve":
            out[d.outcome] = out.get(d.outcome, 0) + 1
    db.flush()
    return out


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


def purpose_candidates(db: Session, trip: LogicalTrip, *, user_id: uuid.UUID, limit: int = 2) -> list[str]:
    """Ärendeknappar: ägarens tidigare bekräftade ärenden för rutten, sedan för målet."""
    from app.services.classify.purpose import NEEDS_CONFIRMATION_SUFFIX
    from app.services.learning import patterns as patterns_svc
    from app.services.places.normalize import fold

    route, dest = patterns_svc.get_patterns(db, user_id, trip.start_location_id, trip.end_location_id)
    out: list[str] = []
    seen: set[str] = set()
    for pat in (route, dest):
        items = [p for p in patterns_svc.top_purposes(pat, limit=12) if p.get("category") == Category.WORK]
        items.sort(key=lambda p: (-p.get("user_count", 0), -p.get("count", 0)))
        for p in items:
            text = sanitize_text(p.get("text"))
            if not text or NEEDS_CONFIRMATION_SUFFIX in fold(text):
                continue
            key = p.get("key") or fold(text)
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out[:limit]


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
    """
    source = before.get("approval_source")
    if source not in AUTO_APPROVAL_SOURCES:
        return None
    corrected = before.get("category") != trip.category or (before.get("purpose") or None) != (trip.purpose or None)
    if not corrected:
        return None
    if source == ApprovalSource.RULE:
        pat = _set_ask_always(db, (before.get("approval_basis") or {}).get("pattern_id"), actor)
        if pat is None:
            return None
        dest = db.get(KnownLocation, pat.destination_location_id)
        return f"Regeln för {_label(dest)} frågar dig nu i stället."
    if source == ApprovalSource.ASSUMED:
        return _on_assumed_correction(db, trip, before=before, actor=actor)
    return None


def _on_assumed_correction(db: Session, trip: LogicalTrip, *, before: dict[str, Any], actor: audit.Actor) -> str | None:
    """A.3 (b): "Fel" på en antagen resa. Byggs av S1d."""
    return None


def confirm_assumption(db: Session, trip: LogicalTrip, *, actor: audit.Actor) -> None:
    """"Stämmer" på en antagen resa. (Stubbe: gör inget.)"""
    return None


def set_company(db: Session, trip: LogicalTrip, text: str, *, actor: audit.Actor) -> None:
    """Företagsnamnet för platsen. (Stubbe: gör inget.)"""
    return None


def classify_place(osm_category: str | None, osm_type: str | None) -> str:
    """Platsens art ur kartans typ (tabellen i A.3). (Stubbe: alltid ``unclear``.)"""
    return "unclear"
