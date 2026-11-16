"""Föreslagna syften (resekommentarer) och kontroll mot påhittade uppgifter.

Regelbaserade förslag bygger enbart på belagda uppgifter: manuella regler,
matchande kalenderbokning, bekräftad historik och identifierade återresor.
Finns inget tillräckligt underlag sägs det rakt ut ("syfte behöver bekräftas").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import LogicalTrip
from app.models.enums import (
    SUBCATEGORY_LABELS_SV,
    Category,
    DecisionSource,
    LocationType,
    ReviewStatus,
    Subcategory,
)
from app.services.classify import textnorm
from app.services.classify.signals import RuleResult, TripContext
from app.services.learning import patterns as patterns_svc
from app.services.places import registry as places
from app.services.places.normalize import fold, parse_address

NEEDS_CONFIRMATION_SUFFIX = "syfte behöver bekräftas"
# Egen regel med fast text: gäller "när kalendern inte säger något annat" (standard, även NULL)
# eller "alltid" (location_patterns.manual_purpose_mode).
PURPOSE_MODE_UNLESS_CALENDAR = "unless_calendar"
PURPOSE_MODE_ALWAYS = "always"
PURPOSE_MODES = (PURPOSE_MODE_UNLESS_CALENDAR, PURPOSE_MODE_ALWAYS)
# Ett AI-ärende får vara högst så här långt (längre avvisas, kapas aldrig).
AI_PURPOSE_MAX = 80


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


def return_purpose(prior_purpose: str | None, abbreviations: dict[str, str] | None = None) -> str | None:
    """"Hemresa efter <utresans ärende>", eller None när utresans ärende inte duger som grund.

    Utresans ärende får inte självt vara en hemresa, hotellresa eller transport, inte vara
    för långt och inte vara platshållaren "syfte behöver bekräftas".
    """
    prior_norm = textnorm.normalize_purpose(prior_purpose, abbreviations)
    if (
        prior_norm is None
        or prior_norm.purpose_type in ("return_home", "hotel_transfer", "transit")
        or len(prior_norm.display) > 70
        or NEEDS_CONFIRMATION_SUFFIX in fold(prior_norm.display)
    ):
        return None
    base = textnorm.lower_first(textnorm.strip_trailing_period(prior_norm.display))
    return textnorm.sentence(f"Hemresa efter {base}")


def _manual_rule(ctx: TripContext) -> tuple[Any, PurposeProposal] | None:
    for pat in (ctx.route_pattern, ctx.dest_pattern):
        if pat is not None and pat.is_manual_rule and pat.manual_purpose:
            return pat, PurposeProposal(
                text=pat.manual_purpose,
                source="pattern",
                rationale="Manuell regel för platsen.",
                # "Fråga mig alltid" (kategori undecided): även syftet bekräftas av användaren.
                needs_confirmation=pat.manual_category == Category.UNDECIDED,
                based_on={"pattern_id": str(pat.id), "manual_rule": True,
                          "purpose_mode": pat.manual_purpose_mode or PURPOSE_MODE_UNLESS_CALENDAR},
            )
    return None


def calendar_proposal(ctx: TripContext, rules: RuleResult) -> PurposeProposal | None:
    """Regelmotorns kalenderförslag: rubriken från den valda bokningen (+ platsen).

    Stark träff (platsgrund) ger ``calendar: "strong"``; en träff där bara tiden stämmer ger
    ``calendar: "weak"`` och ``supported: False`` – och bara när resan av andra skäl är en
    tjänsteresa. Privata bokningar och onlinemöten används aldrig.
    """
    from app.services.classify import signals as sig

    selected = ctx.selected_match
    event = selected[1] if selected else None
    strength = sig.calendar_strength(ctx)
    if event is None or strength is None:
        return None
    if strength == "weak" and rules.category != Category.WORK:
        return None  # bokning utan platsuppgift ger syfte bara när resan av andra skäl är en tjänsteresa
    subject = textnorm.strip_trailing_period(event.subject or "")
    if not subject:
        return None
    phrase = _place_phrase(ctx)
    text = subject
    if phrase is not None and fold(phrase[1]) not in fold(subject):
        city = (ctx.destination.city if ctx.destination else None) or ""
        if not city or fold(city) not in fold(subject):
            text = f"{subject} {phrase[0]} {phrase[1]}"
    based_on: dict[str, Any] = {"calendar_event_id": str(event.id), "calendar": strength}
    if strength == "weak":
        based_on["supported"] = False
    return PurposeProposal(
        text=textnorm.sentence(text),
        source="rule",
        rationale="Rubriken från den matchande kalenderbokningen."
        if strength == "strong" else "Förslag att bekräfta – rubriken från en bokning vid samma tid, utan platsuppgift.",
        needs_confirmation=True,
        based_on=based_on,
    )


def is_calendar_based(proposal: PurposeProposal | None) -> bool:
    return proposal is not None and bool(
        proposal.based_on.get("calendar_event_id") or proposal.based_on.get("calendar_event_ids")
    )


def is_fixed(proposal: PurposeProposal | None) -> bool:
    """Ett fast regelförslag: inte "behöver bekräftas", inte ur kalendern och inte från AI."""
    return (
        proposal is not None
        and proposal.source != DecisionSource.AI
        and not proposal.based_on.get("insufficient")
        and not is_calendar_based(proposal)
    )


def propose(db: Session, ctx: TripContext, rules: RuleResult) -> PurposeProposal | None:
    """Regelbaserat syftesförslag i ärendeordningen (byggkontraktet 3.E). None när inget kan sägas.

    0. Ägarens egen regel med fast text i läget "alltid" (eller "fråga mig alltid").
    1. En stark kalenderträff (regelmotorns kalenderförslag; AI formulerar om i engine).
    2. Ägarens egen regel med fast text ("när kalendern inte säger något annat", standard).
    3. Återresa, hotell, dominerande bekräftat ärende.
    4. En svag kalenderträff (bara tiden stämmer): förslag utan stöd.
    5. Känd plats men otillräckligt underlag ("syfte behöver bekräftas").
    """
    if rules.category == Category.PRIVATE:
        return None  # privata resor behöver inget syfte

    manual = _manual_rule(ctx)
    if manual is not None and (
        manual[0].manual_purpose_mode == PURPOSE_MODE_ALWAYS or manual[0].manual_category == Category.UNDECIDED
    ):
        return manual[1]

    cal = calendar_proposal(ctx, rules)
    if cal is not None and cal.based_on.get("calendar") == "strong":
        return cal
    if manual is not None:
        return manual[1]

    fixed = _fixed_proposal(ctx, rules)
    if fixed is not None:
        return fixed
    if cal is not None:
        return cal
    return _insufficient(ctx, rules)


def _fixed_proposal(ctx: TripContext, rules: RuleResult) -> PurposeProposal | None:
    """Steg 3: återresa, hotell och dominerande bekräftat ärende."""
    # 3. Återresa efter identifierat tjänsteärende
    ret = next((s for s in rules.signals if s.kind == "return_trip" and s.direction == Category.WORK), None)
    if ret is not None and ctx.arrival_at_origin is not None:
        prior = ctx.arrival_at_origin
        trusted = prior.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED) and prior.purpose_confirmed
        city = _origin_city(ctx)
        return_text = return_purpose(prior.purpose, ctx.abbreviations) if trusted else None
        if return_text is not None:
            return PurposeProposal(
                text=return_text,
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

    return None


def _insufficient(ctx: TripContext, rules: RuleResult) -> PurposeProposal | None:
    """Känd arbetsplats utan tillräckliga uppgifter om syftet."""
    cands = candidate_purposes(ctx)
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
        if not m.is_conflict and not _private_event(ev):
            texts += _event_texts(ev)
    for ev in _context_events(ctx):
        texts += _event_texts(ev)
    for p in candidate_purposes(ctx, limit=12):
        texts.append(p["text"])
    if ctx.arrival_at_origin is not None and ctx.arrival_at_origin.purpose:
        texts.append(ctx.arrival_at_origin.purpose)
    for k, v in ctx.abbreviations.items():
        texts += [k, v]
    texts.append(str(ctx.ai.get("about_work") or ""))
    texts += extra_texts or []
    corpus: set[str] = set()
    for t in texts:
        for tok in textnorm.tokens(t):
            corpus.add(fold(tok))
    return corpus


def _private_event(ev: Any) -> bool:
    return (ev.sensitivity or "").lower() in ("personal", "private", "confidential")


def _event_texts(ev: Any) -> list[str]:
    """Bokningens belagda ord: rubrik, plats, ort, tvättad beskrivning och deltagarnas namn.

    Domäner räknas aldrig: ett företagsnamn får inte hittas på ur en e-postdomän.
    """
    from app.services.calendar import calendar_text

    out = [ev.subject or "", ev.location_name or "", ev.location_city or "",
           calendar_text.clean_body(ev.body_preview) or ""]
    for a in ev.attendees or []:
        if isinstance(a, dict) and a.get("name"):
            out.append(str(a["name"]))
    return out


def _context_events(ctx: TripContext) -> list[Any]:
    """Bokningar under vistelsen (på orten) och de närmaste dagarna – aldrig privata eller online."""
    out: list[Any] = []
    stay = getattr(ctx, "calendar_stay", None)
    if stay is not None:
        out += stay.near
    ahead = getattr(ctx, "calendar_ahead", None)
    if ahead is not None:
        out += ahead.usable
    return out


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


# Allmänna ord som får förekomma i ett genererat syfte utan att vara belagda i underlaget.
# Namn, orter och företag måste däremot alltid finnas i underlaget – oavsett skiftläge.
# Ärendetypen (kundbesök, möte, personalärende …) prövas separat mot underlaget, se
# ``unsupported_types``.
_FUNCTION_WORDS = frozenset(
    {
        "i", "på", "hos", "med", "och", "samt", "till", "från", "för", "av", "om", "vid", "efter", "inför", "under",
        "mellan", "via", "mot", "inom", "utan", "över", "en", "ett", "den", "det", "de", "som", "att", "är", "var",
        "har", "hade", "fick", "nya", "ny", "nytt", "ordinarie", "flera", "andra", "annat", "egen", "eget",
        "vår", "vårt", "våra", "min", "mitt", "mina", "dag", "dagen", "dagens", "samma", "tillbaka", "vidare", "hem",
        # Veckodagar och tidsord ("Rest till Helsingborg inför möte på måndag").
        "måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag", "veckan", "imorgon", "morgon",
        "kväll", "kvällen", "förmiddag", "eftermiddag", "flertalet",
    }
)
# Vanliga ord i körjournalssyften. De får också sättas samman och böjas
# ("Projektmöte", "Avstämningsmötet", "Säljmöte").
_COMMON_WORDS = frozenset(
    {
        "kund", "kunden", "kunder", "kundbesök", "kundmöte", "kundmöten", "besök", "besöket", "möte", "mötet",
        "möten", "resa", "resan", "resor", "rest", "åkt", "hemresa", "återresa", "tjänsteresa", "tjänsteärende",
        "ärende", "arbete", "arbetsplats", "kontor", "kontoret", "hotell", "hotellet", "övernattning", "dagsresa",
        "förflyttning", "fortsättning", "leverans", "hämtning", "service", "avstämning", "uppföljning", "genomgång",
        "planering", "workshop", "konferens", "seminarium", "utbildning", "kurs", "intervju", "rekrytering",
        "personalmöte", "ledningsgrupp", "ledningsgruppsmöte", "styrelsemöte", "kvartalsmöte", "verksamhetsmöte",
        "verksamhetsuppföljning", "platsbesök", "besiktning", "inventering", "upphandling", "internrevision",
        "revision", "upplärning", "introduktion", "lunch", "lunchmöte", "syfte", "behöver", "bekräftas",
        "jobbat", "arbetat", "driftat", "tankning", "laddning", "material", "offert", "avtal", "projekt",
        "sälj", "försäljning", "installation", "felsökning", "leverantör", "leverantörer", "leverantören",
        "montage", "montering", "visning", "reparation", "underhåll", "support", "demonstration", "information",
    }
)
_ALLOWED_WORDS = _FUNCTION_WORDS | _COMMON_WORDS
# Böjningsändelser som får läggas till ett belagt eller allmänt ord.
_ALLOWED_SUFFIXES = ("s", "en", "et", "n", "t", "er", "ar", "or", "na", "erna", "arna", "orna", "ens", "ets")


def _compound_supported(key: str, heads: set[str], depth: int = 0) -> bool:
    """Sammansättning/böjning: belagt eller allmänt ord + (foge-s) + allmänt ord eller ändelse."""
    if depth > 2:
        return False
    for head in heads:
        if len(key) <= len(head) or not key.startswith(head):
            continue
        rest = key[len(head):]
        if rest in _ALLOWED_SUFFIXES:
            return True
        for tail in (rest, rest[1:] if rest.startswith("s") else None):
            if not tail or len(tail) < 3:
                continue
            if tail in _COMMON_WORDS or _compound_supported(tail, _COMPOUND_HEADS, depth + 1):
                return True
    return False


# Allmänna ord som kan inleda en sammansättning (funktionsord gör det aldrig).
_COMPOUND_HEADS = {w for w in _COMMON_WORDS if len(w) >= 3}


def _word_supported(key: str, corpus: set[str]) -> bool:
    if key in corpus or key in _ALLOWED_WORDS:
        return True
    if any(c.isdigit() for c in key):
        return False  # siffror måste stå ordagrant i underlaget
    # Böjningsform eller sammansättning, t.ex. "Örebrokontoret", "Exempelbolagets" eller "Projektmöte".
    heads = {c for c in corpus if len(c) >= 4 and not any(ch.isdigit() for ch in c)} | _COMPOUND_HEADS
    return _compound_supported(key, heads)


def unsupported_words(text: str, corpus: set[str]) -> list[str]:
    """Alla ord (oavsett skiftläge) som varken är belagda i underlaget eller allmänna ord.

    Bindestrecks- och apostrofsammansättningar granskas del för del.
    """
    missing: list[str] = []
    for tok in textnorm.tokens(text):
        parts = [p for p in re.split(r"[-']", tok) if p]
        if all(_word_supported(fold(p), corpus) for p in parts):
            continue
        # Ett belagt bindestrecksord (t.ex. "Coca-Cola") godtas som helhet.
        if len(parts) > 1 and fold(tok) in corpus:
            continue
        missing.append(tok)
    return missing


# Ärendetyper som AI bara får ange när samma typ har stöd i underlaget.
_GATED_TYPES = frozenset(
    {
        Subcategory.CUSTOMER_VISIT,
        Subcategory.OFFICE_WORK,
        Subcategory.MEETING,
        Subcategory.TRAINING,
        Subcategory.PERSONNEL,
        Subcategory.OPERATIONS,
        Subcategory.HOTEL_TRANSFER,
    }
)
# Platstyp → ärendetyper som platsen i sig belägger.
_TYPES_BY_LOCATION = {
    LocationType.CUSTOMER: {Subcategory.CUSTOMER_VISIT},
    LocationType.OFFICE: {Subcategory.OFFICE_WORK, Subcategory.MEETING},
    LocationType.REGULAR_WORKPLACE: {Subcategory.OFFICE_WORK, Subcategory.MEETING},
    LocationType.HOTEL: {Subcategory.HOTEL_TRANSFER},
}
# Särskilda ärenden som inte följer av platstypen: de måste stå i underlaget
# (kalenderbokning, bekräftat syfte, regelmotorns förslag), t.ex. "Styrelsemöte".
_SPECIFIC_TERMS = (
    "styrelse", "ledningsgrupp", "kvartalsmöte", "verksamhetsmöte", "revision", "upphandling",
    "medarbetarsamtal", "avtackning", "personalmöte",
)


def _support_texts(ctx: TripContext, rule_proposal: PurposeProposal | None) -> list[str]:
    """Texter i underlaget som kan belägga en ärendetyp."""
    texts: list[str] = []
    for m, ev in ctx.calendar_matches:
        if not m.is_conflict and not _private_event(ev):
            texts.append(ev.subject or "")
            texts.append(ev.body_preview or "")
    for ev in _context_events(ctx):
        texts += [ev.subject or "", ev.body_preview or ""]
    for p in candidate_purposes(ctx, limit=12):
        texts.append(p["text"])
    prior = ctx.arrival_at_origin
    if prior is not None and prior.purpose and prior.purpose_confirmed:
        texts.append(prior.purpose)
    if rule_proposal is not None and rule_proposal.source != DecisionSource.AI:
        texts.append(rule_proposal.text)
    return [textnorm.expand_abbreviations(t, ctx.abbreviations) for t in texts if t]


def supported_types(
    ctx: TripContext, rules: RuleResult | None = None, rule_proposal: PurposeProposal | None = None
) -> set[str]:
    """Ärendetyper som har stöd i underlaget: vald/matchad kalenderbokning, tidigare bekräftat
    syfte för platsen eller rutten, platstypen eller regelmotorns eget förslag."""
    found: set[str] = set()
    for t in _support_texts(ctx, rule_proposal):
        found |= textnorm.purpose_types(t)
    for loc in (ctx.origin, ctx.destination):
        if loc is not None:
            found |= _TYPES_BY_LOCATION.get(loc.type, set())
    if rules is not None:
        if rules.subcategory:
            found.add(rules.subcategory)
        if any(s.kind == "hotel" for s in rules.signals):
            found.add(Subcategory.HOTEL_TRANSFER)
    return found


def unsupported_types(
    text: str, ctx: TripContext, rules: RuleResult | None = None, rule_proposal: PurposeProposal | None = None
) -> list[str]:
    """Ärendetyper/särskilda ärenden i ``text`` som saknar stöd i underlaget."""
    claimed = textnorm.purpose_types(text) & _GATED_TYPES
    missing: list[str] = []
    if claimed:
        have = supported_types(ctx, rules, rule_proposal)
        for kind in sorted(claimed - have):
            missing.append(f"ärendetyp utan stöd: {SUBCATEGORY_LABELS_SV.get(kind, kind)}")
    folded = fold(text)
    terms = [t for t in _SPECIFIC_TERMS if t in folded]
    if terms:
        support = fold(" ".join(_support_texts(ctx, rule_proposal)))
        missing += [f"ärende utan stöd: {t}" for t in terms if t not in support]
    return missing


def validate_generated(
    text: str | None,
    ctx: TripContext,
    rules: RuleResult | None = None,
    rule_proposal: PurposeProposal | None = None,
    extra_texts: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Godkänner ett AI-genererat syfte endast om

    1. varje ord är belagt i underlaget eller är ett allmänt ord (även sammansatt/böjt) –
       skiftläge spelar ingen roll, påhittade namn i gemener stoppas också, och
    2. syftets ärendetyp (kundbesök, möte, personalärende, utbildning …) har stöd i
       underlaget: en matchad kalenderbokning, ett tidigare bekräftat syfte för platsen
       eller rutten, platstypen eller regelmotorns eget förslag.
    """
    if not text or not text.strip():
        return False, []
    cleaned = text.strip()
    if len(cleaned) > 160 or "\n" in cleaned:
        return False, ["för långt eller flera rader"]
    lowered = fold(cleaned)
    if any(marker in lowered for marker in ("http://", "https://", "<", ">", "{", "}")):
        return False, ["otillåtet innehåll"]
    corpus = grounding_corpus(ctx, extra_texts)
    corpus |= {fold(w) for w in textnorm.GENERIC_WORDS}
    missing = unsupported_words(cleaned, corpus)
    missing += unsupported_types(cleaned, ctx, rules, rule_proposal)
    return (not missing), missing


@dataclass
class GeneratedCheck:
    """Utfallet av kontrollen av ett AI-ärende (byggkontraktet 3.F)."""

    ok: bool  # formen och namnen godkända – annars avvisas förslaget
    supported: bool  # ärendetypen har stöd och texten är mer än ett allmänt ord
    reasons: list[str] = field(default_factory=list)  # varför det avvisades
    unsupported: list[str] = field(default_factory=list)  # ärendetyper utan stöd
    names: list[str] = field(default_factory=list)  # ord utan stöd i underlaget (avvisar)


_BARE_WORDS = {"tjänsteresa", "arbete", "möte", "resa", "kundbesök", "besök", "tjänsteärende", "jobb"}


def check_generated(
    text: str | None,
    ctx: TripContext,
    *,
    rules: RuleResult | None = None,
    rule_proposal: PurposeProposal | None = None,
    extra_texts: list[str] | None = None,
) -> GeneratedCheck:
    """Formen (en rad, ≤ 80 tecken, inga länkar eller ``<>{}``) och namnen avvisar; en ärendetyp
    utan stöd eller en text som bara är ett allmänt ord behålls men får ``supported=False``."""
    if not text or not text.strip():
        return GeneratedCheck(False, False, ["tomt"])
    cleaned = text.strip()
    if len(cleaned) > AI_PURPOSE_MAX or "\n" in cleaned or "\r" in cleaned:
        return GeneratedCheck(False, False, ["för långt eller flera rader"])
    lowered = fold(cleaned)
    if any(marker in lowered for marker in ("http://", "https://", "www.", "<", ">", "{", "}")):
        return GeneratedCheck(False, False, ["otillåtet innehåll"])
    corpus = grounding_corpus(ctx, extra_texts)
    corpus |= {fold(w) for w in textnorm.GENERIC_WORDS}
    missing = unsupported_words(cleaned, corpus)
    if missing:
        return GeneratedCheck(False, False, ["namn utan stöd i underlaget"], names=missing)
    types = unsupported_types(cleaned, ctx, rules, rule_proposal)
    words = [fold(t) for t in textnorm.tokens(textnorm.strip_trailing_period(cleaned))]
    bare = bool(words) and all(w in _BARE_WORDS or w in _FUNCTION_WORDS for w in words)
    return GeneratedCheck(True, not types and not bare, [], types + (["bara ett allmänt ord"] if bare else []))


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
    "supported_types",
    "unsupported_tokens",
    "unsupported_types",
    "unsupported_words",
    "validate_generated",
    "places",
]
