"""Normaliserad representation av resekommentarer.

Originalkommentaren ändras aldrig. Här härleds en jämförelsenyckel, en grov
syftestyp och de egennamn som förekommer – underlag för mönster och för kontrollen
att AI-genererade syften inte innehåller påhittade uppgifter.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.models.enums import Subcategory
from app.services.places.normalize import clean, fold

_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:[-'][0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*")
# Alla bokstäver och siffror i alla skriftsystem (namnspärren för AI-ärendet, se ``words``).
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*")

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


def words(text: str | None) -> list[str]:
    """Ord i alla skriftsystem (kyrilliska, grekiska, Ł …), till skillnad från ``tokens``."""
    return _WORD.findall(text or "")


def compat(text: str) -> str:
    """Kompatibilitetsform (NFKC): helbreddstecken och liknande blir vanliga tecken."""
    return unicodedata.normalize("NFKC", text)


def strip_hidden(text: str) -> str:
    """Tar bort styrtecken och osynliga formattecken (bidi, nollbredd) ur en text från AI:n.

    Radbrytning, vagnretur och tabb lämnas (formkontrollen avvisar flera rader), och rad- och
    styckeavgränsare (U+2028/U+2029) blir radbrytningar. Synlig text ändras inte.
    """
    out: list[str] = []
    for ch in text:
        if ch in "\t\n\r":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Zl", "Zp"):
            out.append("\n")
        elif cat not in ("Cc", "Cf"):
            out.append(ch)
    return "".join(out)


def vehicle_pattern(terms: list[str | None]) -> re.Pattern[str] | None:
    """Mönster för bilens VIN och registreringsnummer i alla skrivformer.

    Den sparade strängen som den står (som förut) och dessutom tecken för tecken med valfria
    blanksteg, bindestreck eller punkter emellan ("ABC 123", "ABC-123"), avgränsad så att den
    inte träffar mitt i ett längre ord. Texten jämförs i kompatibilitetsform (``redact``).
    """
    parts: list[str] = []
    for term in terms:
        raw = (term or "").strip()
        bare = re.sub(r"[^0-9A-Za-z]", "", compat(raw))
        if len(raw) >= 3:
            parts.append(re.escape(raw))
        if len(bare) >= 3:
            parts.append(r"(?<![0-9A-Za-z])" + r"[\s\-.]*".join(re.escape(c) for c in bare) + r"(?![0-9A-Za-z])")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def redact(text: str, pattern: re.Pattern[str] | None, replacement: str) -> str:
    """Ersätter träffar av ``pattern``, prövat i kompatibilitetsform (så att även helbreddstecken
    träffar). En text utan träff lämnas orörd."""
    if pattern is None:
        return text
    normalized = compat(text)
    return pattern.sub(replacement, normalized) if pattern.search(normalized) else text


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
