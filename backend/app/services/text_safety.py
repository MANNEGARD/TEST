"""Tvätt av text som kommer utifrån (svar från telefonen, kartans svar, regeltexter).

All sådan text kan hamna i ett ärende, en adress eller i underlaget och behandlas som
opålitlig indata: styrtecken, osynliga formattecken (bidi-tecken, nollbredd) och
rad-/styckeavgränsare tas bort, blanksteg slås ihop och texten kapas.
"""

from __future__ import annotations

import unicodedata

MAX_TEXT = 200

# Cc = styrtecken, Cf = formattecken (bl.a. U+202E och U+200B), Zl/Zp = U+2028/U+2029.
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def sanitize_text(value: str | None, limit: int = MAX_TEXT) -> str | None:
    """Ren, kort text på en rad, eller None när inget återstår.

    Tabb och radbrytning är styrtecken men skiljer ord åt, så de blir blanksteg i stället
    för att försvinna.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = unicodedata.normalize("NFC", value)
    chars: list[str] = []
    for ch in text:
        if ch in "\t\n\r\v\f":
            chars.append(" ")
        elif unicodedata.category(ch) in _REMOVED_CATEGORIES:
            continue
        else:
            chars.append(ch)
    cleaned = " ".join("".join(chars).split())
    if limit is not None and limit >= 0:
        cleaned = cleaned[:limit].rstrip()
    return cleaned or None


# Bindetecknen U+200C/U+200D behövs i sammansatta emoji och i vissa skriftsystem och lämnas kvar.
_KEPT_FORMAT = frozenset({"\u200c", "\u200d"})


def strip_invisible(value: str | None) -> str | None:
    """Tar bort osynliga styr- och formattecken (bidi, nollbredd) men lämnar texten i övrigt orörd.

    Till skillnad från ``sanitize_text`` slås rader och blanksteg inte ihop och texten kapas inte:
    radbrytning, vagnretur och tabb är kvar. Rad- och styckeavgränsare (U+2028/U+2029) blir
    radbrytningar så att orden inte flyter ihop.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    chars: list[str] = []
    for ch in value:
        if ch in "\t\n\r":
            chars.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Zl", "Zp"):
            chars.append("\n")
        elif cat == "Cc" or (cat == "Cf" and ch not in _KEPT_FORMAT):
            continue
        else:
            chars.append(ch)
    return "".join(chars)
