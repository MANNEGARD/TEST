"""Normalisering av adresser och geografiska hjälpfunktioner."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_NUM = re.compile(r"^(?P<street>.*?)[\s]+(?P<number>\d+[\w\-]*)$")
_LEADING_NUM = re.compile(r"^(?P<number>\d+[\w\-]*)\s+(?P<street>.+)$")


@dataclass(frozen=True)
class ParsedAddress:
    raw: str
    street: str | None
    number: str | None
    city: str | None

    @property
    def street_key(self) -> str | None:
        if not self.street or not self.city:
            return None
        return f"{fold(self.street)}|{fold(self.city)}"

    @property
    def number_int(self) -> int | None:
        if not self.number:
            return None
        m = re.match(r"\d+", self.number)
        return int(m.group()) if m else None


def clean(text: str | None) -> str:
    """Trimmar och komprimerar blanksteg utan att ändra innehållet i övrigt."""
    if text is None:
        return ""
    return _WS.sub(" ", str(text)).strip()


def fold(text: str | None) -> str:
    """Skiftlägesokänslig jämförelsenyckel (å/ä/ö bevaras, övriga diakriter tas bort)."""
    s = clean(text).casefold()
    out = []
    for ch in unicodedata.normalize("NFC", s):
        if ch in "åäö":
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
    return "".join(out)


def address_key(address: str | None) -> str:
    """Stabil nyckel för en adress: 'Transportgatan 4,  Örebro' → 'transportgatan 4, örebro'."""
    s = fold(address)
    s = re.sub(r"\s*,\s*", ", ", s)
    return s.strip(" ,")


def parse_address(address: str | None) -> ParsedAddress:
    raw = clean(address)
    if not raw:
        return ParsedAddress(raw="", street=None, number=None, city=None)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    city = parts[-1] if len(parts) >= 2 else None
    street_part = parts[0] if parts else raw
    street, number = street_part, None
    m = _NUM.match(street_part)
    if m:
        street, number = m.group("street"), m.group("number")
    else:
        m2 = _LEADING_NUM.match(street_part)
        if m2:
            street, number = m2.group("street"), m2.group("number")
    return ParsedAddress(raw=raw, street=street.strip() or None, number=number, city=city)


def city_of(address: str | None) -> str | None:
    return parse_address(address).city


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def valid_coordinates(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    return not (abs(lat) < 1e-9 and abs(lon) < 1e-9)
