"""Klassificeringsmotorn: regler först, LLM som rådgivare, skyddsräcken sist.

Flöde per resa:

1. Regelmotorn (``signals.evaluate``) ger kategori, beslutsindikator och signaler.
2. Ett regelbaserat syfte föreslås (``purpose.propose``).
3. Om en AI-leverantör är ansluten och underlaget är otydligt får den signalerna och
   ett avidentifierat underlag. Svaret kan aldrig ensamt ge automatisk klassning,
   aldrig åsidosätta skatte- eller privatregler, och ett genererat syfte godkänns
   bara om alla namn finns i underlaget.
4. Automationsnivån bestäms av trösklarna (hög / medel / låg) och strikt läge.

Resor som användaren redan avgjort eller som har källmärkt importerad klassning
rörs inte; för dem föreslås endast ett saknat syfte.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import KnownLocation, LogicalTrip, TripClassification, TripEvidence, TripPurpose, Vehicle
from app.models.enums import (
    LOCATION_TYPE_LABELS_SV,
    Category,
    DecisionSource,
    IntegrationProvider,
    LocationType,
    LogicalState,
    ReviewStatus,
    TripSource,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.classify import llm as llm_mod
from app.services.classify import purpose as purpose_mod
from app.services.classify import signals as sig
from app.services.classify import textnorm
from app.services.classify.purpose import PurposeProposal
from app.services.classify.signals import ENGINE_VERSION, RuleResult, Signal, TripContext
from app.services.places.normalize import parse_address
from app.services.trips import segments as seg

logger = logging.getLogger(__name__)

_WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_DECIDED = (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
LLM_MAX_CONFIDENCE = 0.7  # ett rent AI-förslag kan som mest ge "begär godkännande"


@dataclass
class Assessment:
    trip_id: uuid.UUID
    category: str
    confidence: float
    requires_review: bool
    review_status: str
    subcategory: str | None
    purpose: PurposeProposal | None
    signals: list[Signal]
    tax_note: str | None
    source: str  # rule | ai
    provider: str | None = None
    model: str | None = None
    caps: list[str] = field(default_factory=list)
    suggested_category: str | None = None  # modellens lutning även när resan lämnas oavgjord
    llm: dict[str, Any] | None = None
    changed: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Strukturerat resultat enligt specifikationens exempel."""
        return {
            "category": {"work": "Arbete", "private": "Privat"}.get(self.category, "Ej avgjord"),
            "category_code": self.category,
            "suggested_category": self.suggested_category,
            "confidence": round(self.confidence, 2),
            "purpose": self.purpose.text if self.purpose else None,
            "purpose_rationale": self.purpose.rationale if self.purpose else None,
            "evidence": [s.label for s in self.signals if s.direction != "neutral" or s.kind.startswith("calendar")],
            "requires_review": self.requires_review,
            "review_status": self.review_status,
            "source_quality": "verified" if not self.caps else "limited",
            "caps": self.caps,
            "source": self.source,
            "model": self.model,
        }


# ----------------------------------------------------------------------------- AI-underlag


def _safe(text: str | None, limit: int = 120) -> str | None:
    if text is None:
        return None
    cleaned = _CTRL.sub(" ", str(text)).strip()
    return cleaned[:limit] if cleaned else None


_REDACTED = "[borttaget]"
# Gatuadress med nummer, t.ex. "Storgatan 5" eller "Västra vägen 12B".
_STREET_RE = re.compile(
    r"\b[0-9A-Za-zÀ-ÖØ-öø-ÿ-]*(?:gatan|gata|vägen|väg|gränd|gränden|stigen|backen|allén|allé|torget|torg|leden|"
    r"plan|platsen|stråket|kajen|esplanaden|parken)\s+\d+\s*[A-Za-z]?\b",
    re.IGNORECASE,
)
# Decimaltal med minst fyra decimaler (koordinater).
_COORD_RE = re.compile(r"-?\d{1,3}[.,]\d{4,}")
_GENERIC_LABELS = {"hemmet", "hem", "bostaden", "privat plats", "privat", "okänd plats", "kontoret", "kontor"}
_PRIVATE_SENSITIVITY = ("personal", "private")

# Regelsignaler vars UI-text bara består av fasta formuleringar och siffror.
_SAFE_SIGNAL_KINDS = {
    "manual_rule", "confirmed_history", "imported_history", "user_corrections", "private_place", "home_is_workplace",
    "commute", "regular_workplace", "refuel_stop", "return_trip", "hotel", "continuation", "unknown_destination",
}
# Sanerade beskrivningar för signaler vars UI-text kan innehålla platsnamn, adresser eller kalenderrubriker.
_LLM_SIGNAL_TEXT = {
    "unconfirmed_destination": "Destinationen är inte bekräftad i platsregistret",
    "known_customer": "Destinationen är en känd kund",
    "known_office": "Destinationen är ett känt kontor",
    "calendar_match": "Matchande kalenderbokning vid resans tid",
    "calendar_context": "Kalenderbokning vid resans tid utan platsuppgift",
    "calendar_private": "Matchande kalenderbokning märkt som privat",
    "calendar_conflict": "Kalenderbokning på annan plats vid samma tid",
}


def _is_private_event(ev) -> bool:
    return (ev.sensitivity or "").lower() in _PRIVATE_SENSITIVITY


def _street_terms(address: str | None) -> list[str]:
    """Gatuadressdelar av en adress (hela adressen, gata + nummer, gatunamn)."""
    if not address:
        return []
    parsed = parse_address(address)
    if parsed.city is None and parsed.number is None:
        return []  # bara ett ortnamn eller liknande – ingen gatuadress
    out = [address]
    if parsed.street and (parsed.city or parsed.number) and parsed.street != parsed.city:
        out.append(parsed.street)
        if parsed.number:
            out.append(f"{parsed.street} {parsed.number}")
    return out


def _name_is_address(loc) -> bool:
    """Platsens namn är (en del av) gatuadressen – t.ex. standardnamnet för en ny plats."""
    name = (loc.name or "").strip()
    if not name:
        return True
    if loc.address:
        if name.casefold() == loc.address.strip().casefold():
            return True
        street = parse_address(loc.address).street
        if street and parse_address(loc.address).city and street.casefold() in name.casefold():
            return True
    return bool(_STREET_RE.search(name))


def _place_doc(loc, address: str | None, send_street: bool) -> dict[str, Any]:
    if loc is None:
        return {"label": parse_address(address).city or "okänd plats", "type": "Okänd", "known": False}
    if loc.confirmed and loc.type == LocationType.HOME:
        return {"label": "Hemmet", "type": "Hem", "known": True}
    if loc.confirmed and loc.type == LocationType.PRIVATE:
        return {"label": "Privat plats", "type": "Privat destination", "known": True}
    city = _safe(loc.city) or parse_address(address).city
    if loc.confirmed and not _name_is_address(loc):
        label = _safe(loc.name)
    else:
        label = city or "okänd plats"  # obekräftad plats eller namn som bara är adressen: endast ort
    doc: dict[str, Any] = {
        "label": label,
        "type": LOCATION_TYPE_LABELS_SV.get(LocationType(loc.type), "Okänd") if loc.confirmed else "Obekräftad plats",
        "city": city,
        "known": bool(loc.confirmed),
        "previous_visits": loc.visit_count,
    }
    if loc.confirmed and loc.company:
        doc["company"] = _safe(loc.company)
    if send_street and loc.address:
        doc["address"] = _safe(loc.address)
    return doc


def _llm_signal_text(s: Signal) -> str:
    if s.kind in _SAFE_SIGNAL_KINDS:
        return s.label
    return _LLM_SIGNAL_TEXT.get(s.kind, f"Regelsignal: {s.kind}")


def _redaction_terms(ctx: TripContext, send_street: bool, send_subjects: bool, private_terms: list[str]) -> list[str]:
    """Texter som aldrig får förekomma i underlaget, var de än skulle hamna."""
    terms: list[str] = list(private_terms)
    for loc in (ctx.origin, ctx.destination):
        if loc is None:
            continue
        if loc.type in (LocationType.HOME, LocationType.PRIVATE):
            terms += [loc.name or "", *_street_terms(loc.address)]
        elif not send_street:
            terms += _street_terms(loc.address)
            if _name_is_address(loc):
                terms.append(loc.name or "")
    if not send_street:
        terms += _street_terms(ctx.trip.start_address) + _street_terms(ctx.trip.end_address)
    for _m, ev in ctx.calendar_matches:
        if not send_subjects or _is_private_event(ev):
            terms.append(ev.subject or "")
        if _is_private_event(ev):
            terms.append(ev.location_name or "")
        if not send_street:
            terms += _street_terms(ev.location_address)
            if ev.location_name and _STREET_RE.search(ev.location_name):
                terms.append(ev.location_name)
    out = {t.strip() for t in terms if t and len(t.strip()) >= 3 and t.strip().casefold() not in _GENERIC_LABELS}
    return sorted(out, key=len, reverse=True)


def _redact(value: Any, pattern: re.Pattern[str] | None, send_street: bool) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v, pattern, send_street) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, pattern, send_street) for v in value]
    if isinstance(value, str):
        text = pattern.sub(_REDACTED, value) if pattern is not None else value
        if not send_street:
            text = _STREET_RE.sub(_REDACTED, text)
        return _COORD_RE.sub(_REDACTED, text)
    return value


def private_location_terms(db: Session, user_id: uuid.UUID) -> list[str]:
    """Namn och adresser för användarens hem och privata platser (skickas aldrig till AI)."""
    rows = db.execute(
        select(KnownLocation.name, KnownLocation.address).where(
            KnownLocation.owner_user_id == user_id,
            KnownLocation.type.in_((LocationType.HOME, LocationType.PRIVATE)),
        )
    ).all()
    terms: list[str] = []
    for name, address in rows:
        terms += [name or "", *_street_terms(address)]
    return terms


def build_llm_document(
    ctx: TripContext,
    rules: RuleResult,
    proposal: PurposeProposal | None,
    *,
    private_terms: list[str] | None = None,
) -> dict[str, Any]:
    """Minimalt, avidentifierat underlag till AI-leverantören.

    Allt som skickas byggs här. Signalbeskrivningarna är särskilda, sanerade texter (inte
    gränssnittets etiketter), och hela dokumentet saneras till sist: kalenderrubriker när
    de inte får skickas, gatuadresser när de inte får skickas samt alltid hemadress,
    privata platsnamn, privata kalenderbokningar och koordinater.
    """
    trip = ctx.trip
    ai = ctx.ai
    local_start = ctx.local_start
    local_end = trip.ended_at.astimezone(ctx.tz) if trip.ended_at else None
    send_street = bool(ai.get("send_street_addresses"))
    send_subjects = bool(ai.get("send_calendar_subjects", True))
    rule_purpose = proposal.text if proposal else None
    if proposal is not None and proposal.based_on.get("calendar_event_id") and not send_subjects:
        rule_purpose = None  # förslaget bygger på kalenderrubriken
    doc: dict[str, Any] = {
        "trip": {
            "weekday": _WEEKDAYS[local_start.weekday()] if local_start else None,
            "start_time": local_start.strftime("%H:%M") if (local_start and trip.started_at) else None,
            "end_time": local_end.strftime("%H:%M") if local_end else None,
            "distance_km": round(trip.distance_km, 1),
            "stops": max(0, trip.segment_count - 1),
            "data_source": trip.source,
            "data_quality_flags": list(trip.quality_flags or []),
        },
        "origin": _place_doc(ctx.origin, trip.start_address, send_street),
        "destination": _place_doc(ctx.destination, trip.end_address, send_street),
        "rule_engine": {
            "category": rules.category,
            "confidence": rules.confidence,
            "limits": rules.caps,
            "signals": [
                {"description": _llm_signal_text(s), "direction": s.direction, "weight": round(s.weight, 2)}
                for s in rules.signals
            ],
        },
        "previous_confirmed_purposes": [
            {"text": _safe(p["text"]), "times": p.get("count", 0), "confirmed_by_user": p.get("user_count", 0)}
            for p in purpose_mod.candidate_purposes(ctx, limit=6)
        ],
        "rule_based_purpose": rule_purpose,
        "tax": {"note": rules.tax_note, "car_benefit": bool(ctx.tax.get("car_benefit"))},
    }
    if send_subjects:
        events = []
        for m, ev in ctx.calendar_matches[:3]:
            private_event = _is_private_event(ev)
            item: dict[str, Any] = {
                "subject": None if private_event else _safe(ev.subject),
                "marked_private": private_event,
                "start": ev.start_at.astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M"),
                "end": ev.end_at.astimezone(ctx.tz).strftime("%H:%M"),
                "city": _safe(ev.location_city),
                "online_meeting": ev.is_online,
                "relation_to_trip": m.relation,
                "match_score": round(m.score, 2),
                "conflicts_with_vehicle_data": m.is_conflict,
            }
            if send_street and not private_event:
                item["location"] = _safe(ev.location_name)
            if ai.get("send_calendar_body") and ev.body_preview and not private_event:
                item["description_excerpt"] = _safe(ev.body_preview, 300)
            events.append(item)
        doc["calendar"] = events
    prior = ctx.arrival_at_origin
    if prior is not None:
        doc["trip_that_arrived_at_origin"] = {
            "category": prior.category,
            "confirmed": prior.review_status in _DECIDED,
            "purpose": _safe(prior.purpose) if prior.purpose_confirmed else None,
        }
    terms = _redaction_terms(ctx, send_street, send_subjects, private_terms or [])
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE) if terms else None
    return _redact(doc, pattern, send_street)


def _should_call_llm(rules: RuleResult, proposal: PurposeProposal | None, ctx: TripContext) -> bool:
    if rules.tax_note:
        return False  # skattemässig bedömning avgörs av användaren, inte av AI
    has_material = any(s.direction != "neutral" for s in rules.signals) or bool(ctx.calendar_matches)
    if not has_material:
        return False  # inget underlag – en LLM kan bara gissa
    if rules.category == Category.UNDECIDED:
        return True
    if rules.category == Category.WORK and ctx.ai.get("use_llm_for_purpose", True):
        if proposal is None or proposal.based_on.get("insufficient") or proposal.based_on.get("calendar_event_id"):
            return True
    return False


def _inputs_hash(rules: RuleResult, proposal: PurposeProposal | None, ai: dict[str, Any], provider_id: str) -> str:
    basis = {
        "v": ENGINE_VERSION,
        "category": rules.category,
        "confidence": rules.confidence,
        "review": rules.requires_review,
        "tax": rules.tax_note,
        "signals": sorted(
            (s.kind, s.direction, round(s.weight, 3), s.ref_id or "", s.label) for s in rules.signals
        ),
        "purpose": [proposal.text, proposal.source, proposal.needs_confirmation] if proposal else None,
        "thresholds": [ai.get("auto_threshold"), ai.get("review_threshold"), ai.get("strict_mode")],
        "provider": provider_id,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()


def _automation_level(category: str, confidence: float, requires_review: bool, ai: dict[str, Any]) -> tuple[str, str]:
    """Returnerar (kategori att sätta på resan, granskningsstatus)."""
    auto_t, review_t = float(ai["auto_threshold"]), float(ai["review_threshold"])
    if category == Category.UNDECIDED or confidence < review_t:
        return Category.UNDECIDED, ReviewStatus.NEEDS_REVIEW  # låg säkerhet → obligatorisk granskning
    if ai.get("strict_mode") or requires_review or confidence < auto_t:
        return category, ReviewStatus.PROPOSED  # medelhög säkerhet → begär godkännande
    return category, ReviewStatus.AUTO  # hög säkerhet → automatisk preliminär klassning


# ----------------------------------------------------------------------------- leverantör


def get_provider(db: Session, user_id: uuid.UUID) -> llm_mod.LLMProvider | None:
    ai = settings_svc.get_section(db, user_id, "ai")
    if ai["provider"] == "none":
        return None
    conn = integrations.get(db, user_id, IntegrationProvider.AI)
    api_key = integrations.secrets(conn).get(f"{ai['provider']}_api_key")
    if not api_key:
        return None
    try:
        return llm_mod.build_provider(ai["provider"], api_key, ai.get("model") or None)
    except Exception:  # felaktig konfiguration ska aldrig stoppa regelmotorn
        logger.exception("Kunde inte initiera AI-leverantören")
        return None


def _report_llm_outcome(db: Session, user_id: uuid.UUID, error: llm_mod.LLMError | None) -> None:
    conn = integrations.get(db, user_id, IntegrationProvider.AI)
    if conn is None:
        return
    if error is None:
        integrations.mark_success(conn)
    else:
        integrations.mark_error(conn, str(error), needs_reauth=error.auth)


# ----------------------------------------------------------------------------- huvudflöde


@dataclass
class _Prepared:
    ai: dict[str, Any]
    ctx: TripContext
    rules: RuleResult
    proposal: PurposeProposal | None
    inputs_hash: str


def _prepare(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    provider: llm_mod.LLMProvider | None,
    use_llm: bool,
    settings_cache: dict[str, Any] | None,
) -> _Prepared:
    """Regelmotorns resultat och underlagets hash – utan att anropa AI-leverantören."""
    cache = settings_cache if settings_cache is not None else {}
    if "ai" not in cache:
        cache["ai"] = settings_svc.get_section(db, user_id, "ai")
        cache["tax"] = settings_svc.get_section(db, user_id, "tax")
        cache["tz"] = settings_svc.user_timezone(db, user_id)
        cache["abbr"] = settings_svc.get_section(db, user_id, "abbreviations")["map"]
    ai = cache["ai"]
    ctx = sig.build_context(
        db, trip, user_id=user_id, tz=cache["tz"], tax=cache["tax"], ai=ai, abbreviations=cache["abbr"]
    )
    rules = sig.evaluate(ctx)
    proposal = purpose_mod.propose(db, ctx, rules)
    provider_id = f"{provider.name}:{provider.model}" if (provider is not None and use_llm) else "none"
    return _Prepared(ai, ctx, rules, proposal, _inputs_hash(rules, proposal, ai, provider_id))


def assess_trip(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    provider: llm_mod.LLMProvider | None = None,
    use_llm: bool = True,
    settings_cache: dict[str, Any] | None = None,
) -> tuple[Assessment, TripContext, RuleResult, str]:
    """Beräknar en bedömning utan att spara den."""
    cache = settings_cache if settings_cache is not None else {}
    prep = _prepare(db, trip, user_id=user_id, provider=provider, use_llm=use_llm, settings_cache=cache)
    assessment = _assess(db, trip, prep, user_id=user_id, provider=provider if use_llm else None, cache=cache)
    return assessment, prep.ctx, prep.rules, prep.inputs_hash


def _assess(
    db: Session,
    trip: LogicalTrip,
    prep: _Prepared,
    *,
    user_id: uuid.UUID,
    provider: llm_mod.LLMProvider | None,
    cache: dict[str, Any],
) -> Assessment:
    ai, ctx, rules, proposal = prep.ai, prep.ctx, prep.rules, prep.proposal
    category, confidence, requires_review = rules.category, rules.confidence, rules.requires_review
    signals = list(rules.signals)
    source, model, llm_info = DecisionSource.RULE, None, None
    suggested = rules.category if rules.category != Category.UNDECIDED else None

    if provider is not None and _should_call_llm(rules, proposal, ctx):
        if "private_terms" not in cache:
            cache["private_terms"] = private_location_terms(db, user_id)
        try:
            result = provider.assess(build_llm_document(ctx, rules, proposal, private_terms=cache["private_terms"]))
            _report_llm_outcome(db, user_id, None)
        except llm_mod.LLMError as exc:
            logger.warning("AI-leverantören svarade med fel: %s", exc)
            _report_llm_outcome(db, user_id, exc)
            result = None
            signals.append(Signal("ai_unavailable", "AI-leverantören kunde inte användas – endast regler tillämpades", "neutral"))
        if result is not None:
            data = result.data
            source, model = DecisionSource.AI, result.model
            llm_info = {"provider": result.provider, "model": result.model, "usage": result.usage, "raw": data}
            llm_cat = data["category"]
            if rules.category == Category.UNDECIDED and llm_cat != Category.UNDECIDED:
                # Reglerna räckte inte; AI:ns förslag kräver alltid godkännande.
                category, confidence, requires_review = llm_cat, min(data["confidence"], LLM_MAX_CONFIDENCE), True
                suggested = llm_cat
                signals.append(
                    Signal(
                        "ai_assessment",
                        f"AI-bedömning: {'Arbete' if llm_cat == Category.WORK else 'Privat'} (kräver godkännande)",
                        llm_cat,
                        0.0,
                        data={"evidence": data["evidence"]},
                    )
                )
            elif llm_cat not in (Category.UNDECIDED, rules.category):
                requires_review = True
                confidence = min(confidence, 0.55)
                signals.append(
                    Signal("ai_disagreement", "AI-bedömningen avviker från reglerna – granska resan", "neutral",
                           data={"ai_category": llm_cat, "evidence": data["evidence"]})
                )
            elif llm_cat == rules.category:
                signals.append(
                    Signal("ai_assessment", "AI-bedömningen stämmer med reglerna", "neutral",
                           data={"evidence": data["evidence"]})
                )
            requires_review = requires_review or (data["requires_review"] and rules.category == Category.UNDECIDED)

            # Genererat syfte: endast om alla namn är belagda i underlaget.
            if category == Category.WORK and data["purpose"]:
                keep_rule = proposal is not None and not (
                    proposal.based_on.get("insufficient") or proposal.based_on.get("calendar_event_id")
                )
                if not keep_rule:
                    ok, missing = purpose_mod.validate_generated(data["purpose"], ctx)
                    if ok:
                        proposal = PurposeProposal(
                            text=textnorm.sentence(data["purpose"]),
                            source=DecisionSource.AI,
                            rationale="AI-förslag utifrån: " + "; ".join(data["evidence"][:4]) if data["evidence"] else "AI-förslag.",
                            needs_confirmation=True,
                            based_on={"ai": True, "model": result.model},
                        )
                    else:
                        signals.append(
                            Signal(
                                "ai_purpose_rejected",
                                "AI-förslag till syfte avvisades: innehöll uppgifter utan stöd i underlaget",
                                "neutral",
                                data={"unsupported": missing},
                            )
                        )

    trip_category, review_status = _automation_level(category, confidence, requires_review, ai)
    assessment = Assessment(
        trip_id=trip.id,
        category=trip_category,
        confidence=round(confidence, 3),
        requires_review=requires_review or review_status != ReviewStatus.AUTO,
        review_status=review_status,
        subcategory=rules.subcategory if trip_category != Category.UNDECIDED else None,
        purpose=proposal if trip_category != Category.PRIVATE else None,
        signals=signals,
        tax_note=rules.tax_note,
        source=source,
        provider=llm_info["provider"] if llm_info else None,
        model=model,
        caps=rules.caps,
        suggested_category=suggested,
        llm=llm_info,
    )
    return assessment


def _current_classification(db: Session, trip_id: uuid.UUID) -> TripClassification | None:
    return db.execute(
        select(TripClassification)
        .where(TripClassification.logical_trip_id == trip_id, TripClassification.is_current.is_(True))
        .order_by(TripClassification.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def classify_trip(
    db: Session,
    trip: LogicalTrip,
    *,
    user_id: uuid.UUID,
    actor: Actor,
    provider: llm_mod.LLMProvider | None = None,
    use_llm: bool = True,
    force: bool = False,
    settings_cache: dict[str, Any] | None = None,
) -> Assessment | None:
    """Bedömer och sparar förslaget. Returnerar None om inget ändrats sedan senast."""
    if trip.state != LogicalState.ACTIVE or trip.source == TripSource.GAP_FILL:
        return None
    decided = trip.review_status in _DECIDED
    if decided and not (trip.work_km > 0 and (not trip.purpose or not trip.purpose.strip())):
        return None  # användarbeslut och källmärkt import rörs inte

    cache = settings_cache if settings_cache is not None else {}
    active_provider = provider if use_llm else None
    prep = _prepare(db, trip, user_id=user_id, provider=provider, use_llm=use_llm, settings_cache=cache)
    rules, inputs_hash = prep.rules, prep.inputs_hash
    current = _current_classification(db, trip.id)
    stored = (current.signals or {}) if current is not None else {}
    if decided:
        unchanged = stored.get("purpose_inputs_hash") == inputs_hash
    else:
        unchanged = (
            current is not None
            and current.source in (DecisionSource.RULE, DecisionSource.AI)
            and stored.get("inputs_hash") == inputs_hash
        )
    # Ett misslyckat AI-anrop prövas igen vid nästa körning, men skrivs inte om i onödan.
    retry_llm = unchanged and bool(stored.get("llm_failed")) and active_provider is not None
    if not force and unchanged and not retry_llm:
        # Underlaget är oförändrat: varken AI-anrop eller databasskrivning.
        assessment = _assess(db, trip, prep, user_id=user_id, provider=None, cache=cache)
        assessment.changed = False
        return assessment

    assessment = _assess(db, trip, prep, user_id=user_id, provider=active_provider, cache=cache)
    llm_failed = any(s.kind == "ai_unavailable" for s in assessment.signals)
    if not force and retry_llm and llm_failed:
        assessment.changed = False
        return assessment

    proposal = assessment.purpose
    if decided:
        # Endast syftesförslag – kategorin är redan avgjord av användaren/källan.
        if proposal is not None and trip.category == Category.WORK:
            _store_purpose(db, trip, proposal, actor)
        if current is not None:
            # Markering så att samma underlag inte skickas till AI-leverantören igen.
            current.signals = {**stored, "purpose_inputs_hash": inputs_hash, "llm_failed": llm_failed}
        return assessment

    db.execute(
        update(TripClassification)
        .where(TripClassification.logical_trip_id == trip.id, TripClassification.is_current.is_(True))
        .values(is_current=False)
    )
    row = TripClassification(
        logical_trip_id=trip.id,
        source=assessment.source,
        category=assessment.category,
        subcategory=assessment.subcategory,
        confidence=assessment.confidence,
        requires_review=assessment.requires_review,
        source_quality="verified" if not assessment.caps else "limited",
        provider=assessment.provider,
        model=assessment.model,
        engine_version=ENGINE_VERSION,
        signals={
            "inputs_hash": inputs_hash,
            "llm_failed": llm_failed,
            "work_score": rules.work_score,
            "private_score": rules.private_score,
            "caps": assessment.caps,
            "suggested_category": assessment.suggested_category,
            "result": assessment.as_dict(),
            "llm": {k: v for k, v in (assessment.llm or {}).items() if k != "raw"} or None,
        },
        created_by=actor.label,
    )
    db.add(row)
    db.flush()
    db.execute(delete(TripEvidence).where(TripEvidence.logical_trip_id == trip.id))
    for s in assessment.signals:
        db.add(
            TripEvidence(
                logical_trip_id=trip.id,
                classification_id=row.id,
                kind=s.kind,
                label=s.label[:300],
                direction=s.direction,
                weight=round(s.weight, 3),
                ref_type=s.ref_type,
                ref_id=s.ref_id,
                data=s.data,
            )
        )

    trip.category = assessment.category
    trip.subcategory = assessment.subcategory
    trip.category_source = assessment.source
    trip.confidence = assessment.confidence
    trip.review_status = assessment.review_status
    trip.tax_note = assessment.tax_note
    trip.updated_by = actor.label
    if proposal is not None:
        _store_purpose(db, trip, proposal, actor)
    elif not trip.purpose_confirmed and trip.purpose_source in (DecisionSource.RULE, DecisionSource.AI, DecisionSource.PATTERN):
        trip.purpose, trip.purpose_source = None, None  # tidigare förslag gäller inte längre
        db.execute(
            update(TripPurpose)
            .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
            .values(is_current=False)
        )
    seg.refresh_logical(db, trip)
    db.flush()
    return assessment


def _store_purpose(db: Session, trip: LogicalTrip, proposal: PurposeProposal, actor: Actor) -> None:
    """Sparar ett syftesförslag. Skriver aldrig över ett syfte som användaren bekräftat."""
    if trip.purpose_confirmed and trip.purpose:
        return
    if trip.purpose == proposal.text and trip.purpose_source == proposal.source:
        return
    db.execute(
        update(TripPurpose)
        .where(TripPurpose.logical_trip_id == trip.id, TripPurpose.is_current.is_(True))
        .values(is_current=False)
    )
    db.add(
        TripPurpose(
            logical_trip_id=trip.id,
            text=proposal.text,
            source=proposal.source,
            rationale=proposal.rationale,
            based_on=proposal.based_on,
            needs_confirmation=proposal.needs_confirmation,
            created_by=actor.label,
        )
    )
    trip.purpose = proposal.text
    trip.purpose_source = proposal.source
    trip.purpose_confirmed = not proposal.needs_confirmation


def classify_pending(
    db: Session,
    *,
    user_id: uuid.UUID,
    vehicle: Vehicle,
    actor: Actor,
    provider: llm_mod.LLMProvider | None | str = "auto",
    use_llm: bool = True,
    limit: int | None = None,
) -> int:
    """Bedömer alla resor som inte är avgjorda. Returnerar antal ändrade bedömningar."""
    if provider == "auto":
        provider = get_provider(db, user_id) if use_llm else None
    cache: dict[str, Any] = {}
    changed = 0
    trips = seg.active_trips(db, vehicle.id)
    for trip in trips:
        result = classify_trip(
            db,
            trip,
            user_id=user_id,
            actor=actor,
            provider=provider,  # type: ignore[arg-type]
            use_llm=use_llm,
            settings_cache=cache,
        )
        if result is not None and result.changed:
            changed += 1
            if limit is not None and changed >= limit:
                break
    db.flush()
    return changed
