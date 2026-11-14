"""Föreslagna syften (resekommentarer) och kontroll mot påhittade uppgifter.

Regelbaserade förslag bygger enbart på belagda uppgifter: manuella regler,
matchande kalenderbokning, bekräftad historik och identifierade återresor.
Finns inget tillräckligt underlag sägs det rakt ut ("syfte behöver bekräftas").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import LogicalTrip
from app.models.enums import Category, LocationType, ReviewStatus
from app.services.classify import textnorm
from app.services.classify.signals import RuleResult, TripContext
from app.services.learning import patterns as patterns_svc
from app.services.places import registry as places
from app.services.places.normalize import fold, parse_address

NEEDS_CONFIRMATION_SUFFIX = "syfte behöver bekräftas"


@dataclass
class PurposeProposal:
    text: str
    source: str  # rule | pattern | ai
    rationale: str
    needs_confirmation: bool
    based_on: dict[str, Any] = field(default_factory=dict)


def _place_phrase(ctx: TripContext) -> tuple[str, str] | None:
    """(preposition, namn) för destinationen – endast belagda namn."""
    d = ctx.destination
    if d is not None and d.confirmed and d.name:
        if d.type == LocationType.CUSTOMER:
            return "hos", d.company or d.name
        return "på", d.name
    city = (d.city if d is not None else None) or parse_address(ctx.trip.end_address).city
    if city:
        return "i", city
    return None


def _origin_city(ctx: TripContext) -> str | None:
    o = ctx.origin
    return (o.city if o is not None else None) or parse_address(ctx.trip.start_address).city


def candidate_purposes(ctx: TripContext, limit: int = 5) -> list[dict[str, Any]]:
    """Tidigare bekräftade syften för rutten/destinationen (användarens egna först)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for pat in (ctx.route_pattern, ctx.dest_pattern):
        for p in patterns_svc.top_purposes(pat, limit=limit * 2):
            if p["key"] in seen:
                continue
            seen.add(p["key"])
            out.append(p)
    out.sort(key=lambda p: (-p.get("user_count", 0), -p.get("count", 0)))
    return out[:limit]


def propose(db: Session, ctx: TripContext, rules: RuleResult) -> PurposeProposal | None:
    """Regelbaserat syftesförslag. Returnerar None när inget kan sägas."""
    if rules.category == Category.PRIVATE:
        return None  # privata resor behöver inget syfte

    # 1. Manuell regel
    for pat in (ctx.route_pattern, ctx.dest_pattern):
        if pat is not None and pat.is_manual_rule and pat.manual_purpose:
            return PurposeProposal(
                text=pat.manual_purpose,
                source="pattern",
                rationale="Manuell regel för platsen.",
                needs_confirmation=False,
                based_on={"pattern_id": str(pat.id), "manual_rule": True},
            )

    # 2. Matchande kalenderbokning – ger kontext, användaren bekräftar.
    event = ctx.selected_event
    if event is not None and (event.sensitivity or "").lower() not in ("personal", "private"):
        subject = textnorm.strip_trailing_period(event.subject or "")
        if subject:
            phrase = _place_phrase(ctx)
            text = subject
            if phrase is not None and fold(phrase[1]) not in fold(subject):
                city = (ctx.destination.city if ctx.destination else None) or ""
                if not city or fold(city) not in fold(subject):
                    text = f"{subject} {phrase[0]} {phrase[1]}"
            return PurposeProposal(
                text=textnorm.sentence(text),
                source="rule",
                rationale="Rubriken från den matchande Outlook-bokningen.",
                needs_confirmation=True,
                based_on={"calendar_event_id": str(event.id)},
            )

    # 3. Återresa efter identifierat tjänsteärende
    ret = next((s for s in rules.signals if s.kind == "return_trip" and s.direction == Category.WORK), None)
    if ret is not None and ctx.arrival_at_origin is not None:
        prior = ctx.arrival_at_origin
        prior_norm = textnorm.normalize_purpose(prior.purpose, ctx.abbreviations)
        trusted = prior.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED) and prior.purpose_confirmed
        city = _origin_city(ctx)
        if (
            trusted
            and prior_norm is not None
            and prior_norm.purpose_type not in ("return_home", "hotel_transfer", "transit")
            and len(prior_norm.display) <= 70
            and NEEDS_CONFIRMATION_SUFFIX not in fold(prior_norm.display)
        ):
            base = textnorm.lower_first(textnorm.strip_trailing_period(prior_norm.display))
            return PurposeProposal(
                text=textnorm.sentence(f"Hemresa efter {base}"),
                source="rule",
                rationale="Återresa efter den bekräftade tjänsteresan dit.",
                needs_confirmation=True,
                based_on={"related_trip_id": str(prior.id)},
            )
        if city:
            return PurposeProposal(
                text=textnorm.sentence(f"Hemresa från {city}"),
                source="rule",
                rationale="Återresa efter tjänsteärende; ursprungsresans syfte är inte bekräftat.",
                needs_confirmation=True,
                based_on={"related_trip_id": str(prior.id)},
            )

    # 4. Hotell under tjänsteresa
    if any(s.kind == "hotel" for s in rules.signals):
        standard = next(
            (p for p in candidate_purposes(ctx, limit=8) if p.get("type") == "hotel_transfer"), None
        )
        return PurposeProposal(
            text=standard["text"] if standard else "Rest mellan kontor och hotell.",
            source="pattern" if standard else "rule",
            rationale="Resa till eller från hotell under en pågående tjänsteresa.",
            needs_confirmation=True,
            based_on={"hotel": True},
        )

    # 5. Dominerande bekräftat syfte för rutten/destinationen
    cands = candidate_purposes(ctx)
    if cands:
        total = sum(p.get("count", 0) for p in cands)
        top = cands[0]
        dominant = top.get("count", 0) >= 3 and total > 0 and top["count"] / total >= 0.6
        if dominant:
            return PurposeProposal(
                text=top["text"],
                source="pattern",
                rationale=f"Samma syfte har bekräftats {top['count']} gånger för den här resan.",
                needs_confirmation=True,
                based_on={"purpose_key": top["key"], "count": top["count"]},
            )

    # 6. Känd arbetsplats utan tillräckliga uppgifter om syftet
    if rules.category == Category.WORK or cands:
        phrase = _place_phrase(ctx)
        if phrase is not None:
            return PurposeProposal(
                text=f"Besök {phrase[0]} {phrase[1]} – {NEEDS_CONFIRMATION_SUFFIX}.",
                source="rule",
                rationale="Platsen är känd men underlaget räcker inte för att avgöra syftet.",
                needs_confirmation=True,
                based_on={"insufficient": True, "alternatives": [p["text"] for p in cands[:3]]},
            )
    return None


# --------------------------------------------------------------------- fabriceringsskydd


def grounding_corpus(ctx: TripContext, extra_texts: list[str] | None = None) -> set[str]:
    """Alla ord som är belagda i underlaget för en resa (platsnamn, orter, företag,
    kalenderrubriker, tidigare bekräftade syften, egna förkortningar)."""
    texts: list[str] = []
    for loc in (ctx.origin, ctx.destination):
        if loc is not None:
            texts += [loc.name or "", loc.city or "", loc.company or ""]
            if ctx.ai.get("send_street_addresses"):
                texts.append(loc.address or "")
    for addr in (ctx.trip.start_address, ctx.trip.end_address):
        city = parse_address(addr).city
        if city:
            texts.append(city)
    for m, ev in ctx.calendar_matches:
        if not m.is_conflict:
            texts.append(ev.subject or "")
            texts.append(ev.location_name or "")
    for p in candidate_purposes(ctx, limit=12):
        texts.append(p["text"])
    if ctx.arrival_at_origin is not None and ctx.arrival_at_origin.purpose:
        texts.append(ctx.arrival_at_origin.purpose)
    for k, v in ctx.abbreviations.items():
        texts += [k, v]
    texts += extra_texts or []
    corpus: set[str] = set()
    for t in texts:
        for tok in textnorm.tokens(t):
            corpus.add(fold(tok))
    return corpus


def unsupported_tokens(text: str, corpus: set[str]) -> list[str]:
    """Egennamn/förkortningar/siffror i ``text`` som saknar stöd i underlaget."""
    missing = []
    for tok in textnorm.named_tokens(text):
        key = fold(tok)
        if key in corpus:
            continue
        # Tillåt böjningsformer av belagda ord (t.ex. "Örebrokontoret" ↔ "Örebro" + "kontoret").
        if len(key) >= 5 and any(len(c) >= 4 and (key.startswith(c) or c.startswith(key)) for c in corpus):
            continue
        missing.append(tok)
    return missing


def validate_generated(text: str | None, ctx: TripContext) -> tuple[bool, list[str]]:
    """Godkänner ett AI-genererat syfte endast om alla namn finns i underlaget."""
    if not text or not text.strip():
        return False, []
    cleaned = text.strip()
    if len(cleaned) > 160 or "\n" in cleaned:
        return False, ["för långt eller flera rader"]
    lowered = fold(cleaned)
    if any(marker in lowered for marker in ("http://", "https://", "<", ">", "{", "}")):
        return False, ["otillåtet innehåll"]
    missing = unsupported_tokens(cleaned, grounding_corpus(ctx))
    return (not missing), missing


def similar_trips(db: Session, trip: LogicalTrip, limit: int = 5) -> list[LogicalTrip]:
    """Tidigare liknande resor (samma rutt, i andra hand samma destination)."""
    from sqlalchemy import select

    from app.models.enums import LogicalState

    if trip.end_location_id is None:
        return []
    base = select(LogicalTrip).where(
        LogicalTrip.vehicle_id == trip.vehicle_id,
        LogicalTrip.state == LogicalState.ACTIVE,
        LogicalTrip.id != trip.id,
        LogicalTrip.end_location_id == trip.end_location_id,
    )
    if trip.sort_time is not None:
        base = base.where(LogicalTrip.sort_time < trip.sort_time)
    rows: list[LogicalTrip] = []
    if trip.start_location_id is not None:
        rows = list(
            db.execute(
                base.where(LogicalTrip.start_location_id == trip.start_location_id)
                .order_by(LogicalTrip.sort_time.desc())
                .limit(limit)
            ).scalars()
        )
    if len(rows) < limit:
        seen = {r.id for r in rows}
        for r in db.execute(base.order_by(LogicalTrip.sort_time.desc()).limit(limit * 2)).scalars():
            if r.id not in seen:
                rows.append(r)
                seen.add(r.id)
            if len(rows) >= limit:
                break
    return rows


__all__ = [
    "PurposeProposal",
    "candidate_purposes",
    "grounding_corpus",
    "propose",
    "similar_trips",
    "unsupported_tokens",
    "validate_generated",
    "places",
]
