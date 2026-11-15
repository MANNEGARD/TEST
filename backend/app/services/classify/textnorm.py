"""Normaliserad representation av resekommentarer.

Originalkommentaren ändras aldrig. Här härleds en jämförelsenyckel, en grov
syftestyp och de egennamn som förekommer – underlag för mönster och för kontrollen
att AI-genererade syften inte innehåller påhittade uppgifter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import Subcategory
from app.services.places.normalize import clean, fold

_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:[-'][0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*")

# (nyckelord i fold()-form, typ). Ordningen avgör vid flera träffar.
_TYPE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("hemresa", "hem från", "resa hem", "åkt hem", "hem efter"), Subcategory.RETURN_HOME),
    (("hotell", "hotel"), Subcategory.HOTEL_TRANSFER),
    (("kundbesök", "kundmöte", "kundmöten", "upphandling", "besök hos", "möte hos"), Subcategory.CUSTOMER_VISIT),
    (
        ("intervju", "rekrytering", "anställning", "tackat av", "avtackning", "personalmöte", "apt", "medarbetarsamtal"),
        Subcategory.PERSONNEL,
    ),
    (("upplärning", "utbildning", "intro", "ykb", "kurs", "utbildningsråd"), Subcategory.TRAINING),
    (
        ("rycka in", "köra ", "kört ", "chaufför", "lastbil", "släp", "byta av", "täcker för"),
        Subcategory.OPERATIONS,
    ),
    (
        ("möte", "ledningsgrupp", "styrelse", "kvartalsmöte", "internrevision", "uppföljning", "revision"),
        Subcategory.MEETING,
    ),
    (("jobbat", "driftat", "arbetat", "kontoret", "kontor"), Subcategory.OFFICE_WORK),
    (("resa till", "rest till", "resa ", "rest ", "vidare från", "tillbaka till", "åkt till"), Subcategory.TRANSIT),
)

# Vanliga ord som inleder en mening med versal utan att vara egennamn.
_GENERIC_CAPITALIZED = {
    "kundbesök", "möte", "hemresa", "resa", "besök", "arbete", "jobbat", "driftat", "arbetat", "upplärning",
    "utbildning", "intervju", "personalmöte", "verksamhetsuppföljning", "verksamhetsmöte", "ledningsgruppsmöte",
    "kvartalsmöte", "styrelsemöte", "från", "till", "tillbaka", "vidare", "rest", "åkt", "varit", "hämtat", "köpt",
    "fått", "haft", "täcker", "tackat", "visat", "flertalet", "upphandling", "internrevision", "utbildningsråd",
    "hem", "i", "på", "hos", "med", "och", "samt", "efter", "inför", "under", "mellan", "kontoret", "kontor",
    "hotell", "hotellet", "syfte", "behöver", "bekräftas", "tjänsteresa", "tjänsteärende", "privat", "ärende",
    "fortsättning", "återresa", "tankning", "laddning", "lunch", "kund", "kunden", "leverans", "service",
    "möten", "kundmöte", "kundmöten", "avstämning", "uppföljning", "genomgång", "planering", "workshop",
    "konferens", "seminarium", "rekrytering", "anställningsintervju", "platsbesök", "besiktning", "inventering",
    "resor", "förflyttning", "dagsresa", "övernattning", "ordinarie", "arbetsplats", "bostaden", "hemmet",
}
GENERIC_WORDS = frozenset(_GENERIC_CAPITALIZED)


@dataclass(frozen=True)
class NormalizedPurpose:
    original: str
    key: str  # jämförelsenyckel (förkortningar expanderade, gemener, komprimerade blanksteg)
    display: str  # original med komprimerade blanksteg
    purpose_type: str
    entities: tuple[str, ...]  # egennamn/förkortningar som förekommer


def tokens(text: str | None) -> list[str]:
    return _TOKEN.findall(text or "")


def expand_abbreviations(text: str, abbreviations: dict[str, str]) -> str:
    if not abbreviations:
        return text
    lookup = {fold(k): v for k, v in abbreviations.items()}

    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        return lookup.get(fold(word), word)

    return _TOKEN.sub(repl, text)


def purpose_type(text: str | None) -> str:
    s = " " + fold(text) + " "
    for keywords, kind in _TYPE_RULES:
        if any(k in s for k in keywords):
            return kind
    return Subcategory.OTHER


def purpose_types(text: str | None) -> set[str]:
    """Alla ärendetyper som texten anger (``purpose_type`` ger bara den första)."""
    s = " " + fold(text) + " "
    found = {kind for keywords, kind in _TYPE_RULES if any(k in s for k in keywords)}
    # "kund", "kunden", "kunder" … påstår ett kundärende även utan ordet "kundbesök".
    if any(fold(t).startswith("kund") for t in tokens(text)):
        found.add(Subcategory.CUSTOMER_VISIT)
    return found


def named_tokens(text: str | None) -> list[str]:
    """Egennamnsliknande ord: versal inne i meningen, versalord, ord med siffror.

    Meningens första ord räknas bara om det inte är ett vanligt ord.
    """
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\s[–—-]\s", text or ""):
        words = tokens(sentence)
        for i, w in enumerate(words):
            is_upper = len(w) >= 2 and w.isupper()
            has_digit = any(c.isdigit() for c in w)
            capitalized = w[0].isupper()
            if has_digit or is_upper:
                out.append(w)
            elif capitalized and (i > 0 or fold(w) not in _GENERIC_CAPITALIZED):
                if fold(w) not in _GENERIC_CAPITALIZED:
                    out.append(w)
    return out


def normalize_purpose(text: str | None, abbreviations: dict[str, str] | None = None) -> NormalizedPurpose | None:
    if text is None or not str(text).strip():
        return None
    original = str(text)
    display = clean(original)
    expanded = expand_abbreviations(display, abbreviations or {})
    key = re.sub(r"[.\s]+$", "", fold(expanded))
    return NormalizedPurpose(
        original=original,
        key=key,
        display=display,
        purpose_type=purpose_type(expanded),
        entities=tuple(dict.fromkeys(named_tokens(display))),
    )


def sentence(text: str) -> str:
    """Kort körjournalsmening: inledande versal, ingen dubbel punkt."""
    t = clean(text)
    if not t:
        return t
    t = t[0].upper() + t[1:]
    return t if t[-1] in ".!?" else t + "."


def lower_first(text: str) -> str:
    """Gör första bokstaven liten om ordet inte är ett egennamn eller en förkortning."""
    t = clean(text)
    if not t:
        return t
    first = tokens(t)[:1]
    if first and (first[0].isupper() and len(first[0]) > 1):
        return t  # förkortning, t.ex. "YKB"
    if first and fold(first[0]) not in _GENERIC_CAPITALIZED:
        return t  # sannolikt egennamn
    return t[0].lower() + t[1:]


def strip_trailing_period(text: str) -> str:
    return re.sub(r"[.\s]+$", "", clean(text))
