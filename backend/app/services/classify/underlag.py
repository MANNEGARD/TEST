"""Fullständigt AI-underlag (``ai.context_level = "full"``, 1.3.0).

Ägaren har bett om att AI ska få allt som hjälper bedömningen: namn och gatuadress för start
och mål (även hemmet och privata platser), kartans namn och typ för okända ställen, hela
dagens resor, resan före och efter, historiken för samma mål och rutt, ägarens regler och
platsanteckningar, kalenderbokningarna (dagen, hela vistelsen vid resor till en annan ort och
de närmaste dagarna), arbetsgivaren och "Om mitt arbete".

Aldrig: koordinater, VIN, registreringsnummer, nycklar, tokens, lösenord, kalenderlänken,
HA-token och e-postadresser. Privata bokningar skickas bara som tid ("Privat bokning").

Allt är DATA, inte instruktioner: varje sträng tvättas (``_clean``) – styrtecken och osynliga
tecken bort, koder och lösenord i fritext bort, länkar blir "[länk]", ``<``/``>`` blir ``‹``/``›``
så att ingen text kan stänga ``<data>``, och texten kapas. Svaret valideras efteråt
(``purpose.check_generated`` mot ``document_texts``).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import CalendarEvent, KnownLocation, LocationPattern, LogicalTrip, Vehicle
from app.models.enums import (
    LOCATION_TYPE_LABELS_SV,
    ApprovalSource,
    Category,
    LocationType,
    LogicalState,
    ReviewStatus,
    TimeQuality,
    TripSource,
)
from app.services import settings as settings_svc
from app.services.calendar import calendar_text
from app.services.classify import calendar_context, lunch
from app.services.classify import signals as sig
from app.services.text_safety import sanitize_text
from app.services.trips import segments as seg
from app.services.trips import start_time

_WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
_REDACTED = "[borttaget]"
# Decimaltal med minst fyra decimaler (koordinater) – samma mönster som engine._COORD_RE.
_COORD_RE = re.compile(r"-?\d{1,3}[.,]\d{4,}")
_VIN_RE = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"\S{32,}")
_LINK_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Koder och lösenord i fritext: resten av raden efter nyckelordet tas bort.
_SECRET_RE = re.compile(
    r"(?i)\b(\w*kod|lösenord|password|passcode|pin|mötes-?id|meeting\s+id|konferens-?id)\b(\s*[:=]|\s+(?=\d)).*$"
)

LIMITS = {
    "name": 120, "address": 200, "purpose": 200, "subject": 200, "location": 200, "body": 1500, "note": 300,
    "about_work": 1000, "participant": 80,
}
MAX_DAY_TRIPS = 30
MAX_DAY_EVENTS = 20
MAX_HISTORY_PURPOSES = 8
MAX_STYLE_EXAMPLES = 8
PRIVATE_SUBJECT = "Privat bokning"
_OWNER = (None, ApprovalSource.USER, ApprovalSource.BULK)
_PRIVATE = ("personal", "private", "confidential")


# ----------------------------------------------------------------------------- tvätt


def _scrub(text: str) -> str:
    text = _LINK_RE.sub("[länk]", text)
    text = _EMAIL_RE.sub(_REDACTED, text)
    text = _VIN_RE.sub(_REDACTED, text)
    text = _COORD_RE.sub(_REDACTED, text)

    def _long(m: re.Match[str]) -> str:
        tok = m.group(0)
        return _REDACTED if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok) else tok

    text = _LONG_TOKEN_RE.sub(_long, text)
    return text.replace("<", "‹").replace(">", "›")


def _clean(value: Any, kind: str = "name", *, free_text: bool = False) -> str | None:
    """En sträng till underlaget: sanerad, utan koder/lösenord (fritext), länkar och koordinater."""
    if value is None:
        return None
    raw = str(value)
    if free_text:
        lines = [_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)} {_REDACTED}", line)
                 for line in raw.replace("\r", "\n").split("\n")]
        raw = "\n".join(lines)
    text = sanitize_text(_scrub(raw), limit=10_000)
    if not text:
        return None
    text = sanitize_text(text, limit=LIMITS.get(kind, 200))
    return text


def _redact_terms(value: Any, pattern: re.Pattern[str] | None) -> Any:
    if pattern is None:
        return value
    if isinstance(value, dict):
        return {k: _redact_terms(v, pattern) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_terms(v, pattern) for v in value]
    if isinstance(value, str):
        return pattern.sub(_REDACTED, value)
    return value


# ----------------------------------------------------------------------------- byggstenar


def _place(loc: KnownLocation | None, address: str | None) -> dict[str, Any]:
    from app.services.classify.engine import _name_is_address
    from app.services.places.normalize import parse_address

    if loc is None:
        return {
            "name": _clean(address), "address": _clean(address, "address"),
            "city": _clean(parse_address(address).city), "type": "Okänd plats", "confirmed": False,
            "company": None, "map_name": None, "map_type": None, "previous_visits": 0, "note": None,
        }
    confirmed = bool(loc.confirmed)
    unknown = not confirmed
    return {
        "name": _clean(loc.name or address),
        "address": _clean(loc.address or address, "address"),
        "city": _clean(loc.city or parse_address(loc.address or address).city),
        "type": LOCATION_TYPE_LABELS_SV.get(LocationType(loc.type), "Okänd") if confirmed else "Obekräftad plats",
        "confirmed": confirmed,
        "company": _clean(loc.company),
        "map_name": _clean(loc.name) if unknown and loc.name and not _name_is_address(loc) else None,
        "map_type": _clean(loc.osm_class, "name") if unknown and loc.osm_class else None,
        "previous_visits": int(loc.visit_count or 0),
        "note": _clean(loc.notes, "note", free_text=True),
    }


def _signal_text(s: Any) -> str | None:
    """Signalens text. En privat bokning nämns aldrig med rubrik (den används inte till ärendet)."""
    if s.kind == "calendar_private":
        return "Matchande kalenderbokning märkt som privat"
    return _clean(s.label, "subject")


def _status(t: LogicalTrip) -> str:
    if t.review_status == ReviewStatus.IMPORTED:
        return "importerad"
    if t.review_status == ReviewStatus.APPROVED:
        return "godkänd av dig" if t.approval_source in _OWNER else "godkänd automatiskt"
    if t.review_status in (ReviewStatus.PROPOSED, ReviewStatus.AUTO):
        return "förslag"
    return "ej avgjord"


def _confirmed_purpose(t: LogicalTrip) -> str | None:
    """Bara bekräftade ärenden (eller importerade) – aldrig ett obekräftat förslag."""
    if not t.purpose:
        return None
    if t.purpose_confirmed or t.review_status == ReviewStatus.IMPORTED:
        return _clean(t.purpose, "purpose")
    return None


def _label(t: LogicalTrip, locs: dict[uuid.UUID, KnownLocation], which: str) -> str | None:
    loc_id = t.start_location_id if which == "start" else t.end_location_id
    loc = locs.get(loc_id) if loc_id else None
    address = t.start_address if which == "start" else t.end_address
    return _clean((loc.name if loc is not None and loc.name else None) or address)


def _daytrip(t: LogicalTrip, locs: dict[uuid.UUID, KnownLocation], tz: Any, this_id: uuid.UUID) -> dict[str, Any]:
    started = start_time.known_start(t)
    end = t.ended_at.astimezone(tz).strftime("%H:%M") if t.ended_at else "?"
    begin = started.astimezone(tz).strftime("%H:%M") if started else ""
    category = t.category if t.category in (Category.WORK, Category.PRIVATE) else "undecided"
    return {
        "time": f"{begin}–{end}",
        "from": _label(t, locs, "start"),
        "to": _label(t, locs, "end"),
        "distance_km": round(t.distance_km or 0.0, 1),
        "category": category,
        "status": _status(t),
        "purpose": _confirmed_purpose(t),
        "this_trip": t.id == this_id,
    }


def _history(pat: LocationPattern | None) -> dict[str, Any] | None:
    if pat is None:
        return None
    total = pat.confirmed_work + pat.confirmed_private + pat.imported_work + pat.imported_private
    if total == 0 and not pat.purposes:
        return None
    purposes = []
    for p in (pat.purposes or [])[:MAX_HISTORY_PURPOSES]:
        text = _clean(p.get("text"), "purpose")
        if text:
            purposes.append({"text": text, "times": int(p.get("count", 0) or 0),
                             "confirmed_by_user": int(p.get("user_count", 0) or 0)})
    return {
        "trips": total,
        "work_confirmed": pat.confirmed_work,
        "private_confirmed": pat.confirmed_private,
        "work_imported": pat.imported_work,
        "private_imported": pat.imported_private,
        "last_visit": pat.last_seen_at.date().isoformat() if pat.last_seen_at else None,
        "purposes": purposes,
    }


def _rules(ctx: sig.TripContext) -> list[dict[str, Any]]:
    out = []
    for pat, scope in ((ctx.route_pattern, "rutten"), (ctx.dest_pattern, "målet")):
        if pat is None or not pat.is_manual_rule:
            continue
        category = pat.manual_category if pat.manual_category in (Category.WORK, Category.PRIVATE) else None
        out.append({
            "applies_to": scope,
            "category": category,
            "purpose": _clean(pat.manual_purpose, "purpose"),
            "ask_always": pat.manual_category == Category.UNDECIDED,
        })
    return out


def _is_private(ev: CalendarEvent) -> bool:
    return (ev.sensitivity or "").lower() in _PRIVATE


def _event(
    ev: CalendarEvent, ctx: sig.TripContext, *, work_domain: str | None, self_email: str | None,
    near: bool | None = None,
) -> dict[str, Any]:
    local_start = ev.start_at.astimezone(ctx.tz)
    local_end = ev.end_at.astimezone(ctx.tz)
    base: dict[str, Any] = {
        "date": local_start.date().isoformat(),
        "start": local_start.strftime("%H:%M"),
        "end": local_end.strftime("%H:%M"),
    }
    if _is_private(ev):
        # Privata bokningar: bara tiden – texten används aldrig till ärendet och skickas inte.
        return {**base, "subject": PRIVATE_SUBJECT, "location": None, "city": None, "body": None,
                "participants": [], "marked_private": True, "online": False, "near_destination": None,
                "matched_to_this_trip": None}
    body = calendar_text.clean_body(ev.body_preview)
    match = next(((m, e) for m, e in ctx.calendar_matches if e.id == ev.id), None)
    matched = None
    if match is not None:
        m = match[0]
        strength = None
        if not m.is_conflict:
            strength = "weak" if m.geo_basis in sig.WEAK_GEO_BASES else "strong"
        matched = {"relation": m.relation, "score": round(float(m.score or 0), 2), "strength": strength,
                   "basis": m.geo_basis, "conflicts_with_vehicle_data": bool(m.is_conflict)}
    return {
        **base,
        "subject": _clean(ev.subject, "subject"),
        "location": _clean(ev.location_name or ev.location_address, "location"),
        "city": _clean(ev.location_city),
        "body": _clean(body, "body", free_text=True) if body else None,
        "participants": [
            {"name": _clean(p["name"], "participant"), "relation": p["relation"], "company_domain": p["company_domain"]}
            for p in calendar_text.participants(ev.attendees, work_domain=work_domain, self_email=self_email)
        ],
        "marked_private": False,
        "online": calendar_context.is_online(ev),
        "near_destination": near,
        "matched_to_this_trip": matched,
    }


def _day_events(db: Session, ctx: sig.TripContext, is_demo: bool) -> list[CalendarEvent]:
    when = ctx.trip.ended_at or ctx.trip.sort_time
    if when is None:
        return []
    day = when.astimezone(ctx.tz).date()
    lo = datetime.combine(day, time.min, ctx.tz)
    hi = lo + timedelta(days=1)
    return list(db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == ctx.user_id,
            CalendarEvent.is_demo.is_(is_demo),
            CalendarEvent.removed_at.is_(None),
            CalendarEvent.is_cancelled.is_(False),
            CalendarEvent.end_at >= lo,
            CalendarEvent.start_at < hi,
        ).order_by(CalendarEvent.start_at).limit(MAX_DAY_EVENTS)
    ).scalars())


def _trip_doc(ctx: sig.TripContext, rules: sig.RuleResult) -> dict[str, Any]:
    from app.services.volvo.detection import collection_gap_of

    trip = ctx.trip
    tz = ctx.tz
    known = start_time.known_start(trip)
    if known is None:
        kind = "okänd"
    elif trip.start_time_quality == TimeQuality.REPORTED:
        kind = "rapporterad"
    elif trip.start_time_quality == TimeQuality.ESTIMATED:
        kind = "uppskattad"
    elif trip.start_time_quality == TimeQuality.EXACT:
        kind = "exakt"
    else:
        kind = "okänd"
    local_end = trip.ended_at.astimezone(tz) if trip.ended_at else None
    local_ref = (known or trip.ended_at or trip.sort_time)
    local_ref = local_ref.astimezone(tz) if local_ref else None
    gap = collection_gap_of(trip)
    return {
        "date": local_ref.date().isoformat() if local_ref else None,
        "weekday": _WEEKDAYS[local_ref.weekday()] if local_ref else None,
        "start_time": known.astimezone(tz).strftime("%H:%M") if known and kind != "okänd" else None,
        "start_time_kind": kind,
        "end_time": local_end.strftime("%H:%M") if local_end else None,
        "distance_km": round(trip.distance_km or 0.0, 1),
        "driving_minutes": round(trip.driving_seconds / 60) if trip.driving_seconds else None,
        "stops": max(0, (trip.segment_count or 1) - 1),
        "data_source": trip.source,
        "data_quality_flags": list(trip.quality_flags or []),
        "collection_gap": {
            "from": gap.start.astimezone(tz).strftime("%H:%M"),
            "to": gap.end.astimezone(tz).strftime("%H:%M"),
            "odometer_km": gap.odometer_km,
        } if gap is not None else None,
    }


def _lunch_doc(db: Session, ctx: sig.TripContext) -> dict[str, Any] | None:
    m = ctx.lunch
    if m is None:
        return None
    office = db.get(KnownLocation, m.office_id)
    place = db.get(KnownLocation, m.place_id)
    return {
        "status": m.status,
        "office": _clean(office.name if office else None),
        "place": _clean((place.name or place.address) if place else None),
        "stop_minutes": lunch.stop_text(m),
        "map_type": m.map_hint,
    }


def _owner_style(db: Session, user_id: uuid.UUID, abbreviations: dict[str, str]) -> dict[str, Any]:
    rows = db.execute(
        select(LogicalTrip.purpose)
        .join(Vehicle, Vehicle.id == LogicalTrip.vehicle_id)
        .where(
            Vehicle.owner_user_id == user_id,
            LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.category == Category.WORK,
            LogicalTrip.purpose.is_not(None),
            or_(
                LogicalTrip.review_status == ReviewStatus.IMPORTED,
                (LogicalTrip.review_status == ReviewStatus.APPROVED)
                & LogicalTrip.purpose_confirmed.is_(True)
                & (LogicalTrip.approval_source.is_(None)
                   | LogicalTrip.approval_source.in_((ApprovalSource.USER, ApprovalSource.BULK))),
            ),
        )
        .order_by(LogicalTrip.sort_time.desc())
        .limit(80)
    ).scalars()
    examples: list[str] = []
    for text in rows:
        clean = _clean(text, "purpose")
        if clean and clean not in examples:
            examples.append(clean)
        if len(examples) >= MAX_STYLE_EXAMPLES:
            break
    return {
        "examples": examples,
        "abbreviations": {(_clean(k) or ""): (_clean(v) or "") for k, v in (abbreviations or {}).items()},
    }


def _neighbours(db: Session, trip: LogicalTrip) -> tuple[LogicalTrip | None, LogicalTrip | None]:
    if trip.sort_time is None:
        return None, None
    base = select(LogicalTrip).where(
        LogicalTrip.vehicle_id == trip.vehicle_id, LogicalTrip.state == LogicalState.ACTIVE,
        LogicalTrip.id != trip.id, LogicalTrip.source != TripSource.GAP_FILL,
    )
    prev = db.execute(base.where(LogicalTrip.sort_time < trip.sort_time).order_by(LogicalTrip.sort_time.desc())
                      .limit(1)).scalar_one_or_none()
    nxt = db.execute(base.where(LogicalTrip.sort_time > trip.sort_time).order_by(LogicalTrip.sort_time.asc())
                     .limit(1)).scalar_one_or_none()
    return prev, nxt


def _day_trips(db: Session, trip: LogicalTrip, tz: Any) -> list[LogicalTrip]:
    when = trip.ended_at or trip.sort_time
    if when is None:
        return []
    day: date = when.astimezone(tz).date()
    lo = datetime.combine(day, time.min, tz)
    hi = lo + timedelta(days=1)
    return [
        t for t in seg.active_trips(db, trip.vehicle_id)
        if t.source != TripSource.GAP_FILL and t.sort_time is not None and lo <= t.sort_time < hi
    ][:MAX_DAY_TRIPS]


def _locations(db: Session, trips: list[LogicalTrip]) -> dict[uuid.UUID, KnownLocation]:
    ids = {i for t in trips for i in (t.start_location_id, t.end_location_id) if i}
    if not ids:
        return {}
    return {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(ids))).scalars()}


def _via(ctx: sig.TripContext, db: Session) -> list[dict[str, Any]]:
    raws = seg.ordered_raws(ctx.trip)
    out = []
    for raw in raws[:-1]:
        loc = db.get(KnownLocation, raw.end_location_id) if raw.end_location_id else None
        out.append(_place(loc, raw.end_address))
    return out


# ----------------------------------------------------------------------------- huvudfunktion


def build_full_document(
    db: Session,
    ctx: sig.TripContext,
    rules: sig.RuleResult,
    proposal: Any,
    *,
    user_id: uuid.UUID,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Det fullständiga underlaget för EN resa (fälten i byggkontraktet 6.A/3.A)."""
    from app.services.calendar import ics
    from app.services.classify import purpose as purpose_mod

    trip = ctx.trip
    tz = ctx.tz
    ai = cfg if cfg is not None else ctx.ai
    general = settings_svc.get_section(db, user_id, "general")
    cal = settings_svc.get_section(db, user_id, "calendar")
    work_domain = (cal.get("work_domain") or "").strip().lower() or None
    self_email = ics.self_email(db, user_id)
    vehicle = db.get(Vehicle, trip.vehicle_id)
    is_demo = bool(vehicle.is_demo) if vehicle is not None else False

    day = _day_trips(db, trip, tz)
    prev, nxt = _neighbours(db, trip)
    locs = _locations(db, [*day, *(t for t in (prev, nxt) if t is not None)])

    def ev_doc(ev: CalendarEvent, near: bool | None = None) -> dict[str, Any]:
        return _event(ev, ctx, work_domain=work_domain, self_email=self_email, near=near)

    stay = ctx.calendar_stay
    ahead = ctx.calendar_ahead
    doc: dict[str, Any] = {
        "level": "full",
        "owner": {
            "employer": _clean(general.get("employer_name")),
            "about_work": _clean(ai.get("about_work"), "about_work", free_text=True),
            "journal_scope": settings_svc.journal_scope(db, user_id),
            "home_is_workplace": bool(ctx.home_is_workplace),
        },
        "trip": _trip_doc(ctx, rules),
        "origin": _place(ctx.origin, trip.start_address),
        "destination": _place(ctx.destination, trip.end_address),
        "via": _via(ctx, db),
        "rule_engine": {
            "category": rules.category,
            "confidence": rules.confidence,
            "limits": [_clean(c) for c in rules.caps],
            "signals": [
                {"description": _signal_text(s), "direction": s.direction, "weight": round(s.weight, 2)}
                for s in rules.signals
            ],
        },
        "lunch_pattern": _lunch_doc(db, ctx),
        "day": [_daytrip(t, locs, tz, trip.id) for t in day],
        "previous_trip": _daytrip(prev, locs, tz, trip.id) if prev is not None else None,
        "next_trip": _daytrip(nxt, locs, tz, trip.id) if nxt is not None else None,
        "history_destination": _history(ctx.dest_pattern),
        "history_route": _history(ctx.route_pattern),
        "owner_rules": _rules(ctx),
        "calendar_day": [ev_doc(ev) for ev in _day_events(db, ctx, is_demo)],
        "calendar_stay": {
            "place": _clean(stay.place),
            "from": stay.start.isoformat(),
            "to": stay.end.isoformat() if stay.end else None,
            "basis": stay.basis,
            "events": [ev_doc(ev, near) for ev, near in stay.events],
        } if stay is not None and stay.events else None,
        "calendar_ahead": {
            "place": _clean(ahead.place),
            "days": [d.isoformat() for d in ahead.days],
            "events": [ev_doc(ev, True) for ev in ahead.events],
        } if ahead is not None and ahead.events else None,
        "calendar_strength": sig.calendar_strength(ctx),
        "owner_style": _owner_style(db, user_id, ctx.abbreviations),
        "previous_confirmed_purposes": [
            {"text": _clean(p["text"], "purpose"), "times": p.get("count", 0), "confirmed_by_user": p.get("user_count", 0)}
            for p in purpose_mod.candidate_purposes(ctx, limit=6)
        ],
        "rule_based_purpose": _clean(proposal.text, "purpose")
        if proposal is not None and proposal.source != "ai" else None,
        "tax": {"note": _clean(rules.tax_note, "body"), "car_benefit": bool(ctx.tax.get("car_benefit"))},
    }
    # Bilens identitet förekommer aldrig, var den än skulle ha hamnat (t.ex. i en anteckning).
    terms = [t for t in ((vehicle.vin if vehicle else None), (vehicle.registration_number if vehicle else None))
             if t and len(t.strip()) >= 3]
    pattern = re.compile("|".join(re.escape(t.strip()) for t in terms), re.IGNORECASE) if terms else None
    return _redact_terms(doc, pattern)


def document_texts(doc: dict[str, Any]) -> list[str]:
    """Alla strängar i underlaget (namnkontrollens belagda ord) – utom e-postdomänerna: ett
    företagsnamn får inte hittas på ur en domän."""
    out: list[str] = []

    def walk(value: Any, key: str | None = None) -> None:
        if key == "company_domain":
            return
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            out.append(value)

    walk(doc)
    return out


__all__ = ["PRIVATE_SUBJECT", "build_full_document", "document_texts"]
