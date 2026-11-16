"""Kalenderbokningens text och deltagare inför AI-underlaget (1.3.0).

Beskrivningen lagras som den kom (upp till 4 000 tecken, radbrytningar kvar) och tvättas
först när den ska skickas: möteslänkar, inringningsuppgifter, mötes-ID, lösenord och
signaturer tas bort (``clean_body``). Deltagarna märks som kollega, extern eller okänd
utifrån ägarens jobbdomän (``calendar.work_domain``); e-postadresserna själva lämnar
aldrig servern – bara namnet, märkningen och, för externa, domänen som ledtråd till företaget.

Allt här är data från en kalender som någon annan kan ha skrivit i. Ingenting loggas.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.services.places.normalize import fold
from app.services.text_safety import sanitize_text

BODY_STORE_LIMIT = 4000
BODY_SEND_LIMIT = 1500
NAME_LIMIT = 80
MAX_ATTENDEES = 50
MAX_PARTICIPANTS_SENT = 15

# Allmänna e-postdomäner säger ingenting om vilket företag en deltagare kommer från.
GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.se", "live.com", "live.se", "msn.com",
    "icloud.com", "me.com", "yahoo.com", "yahoo.se", "telia.com", "bredband.net", "comhem.se", "spray.se",
    "protonmail.com", "proton.me",
})

# Ett mötesblock (Teams/Zoom/Webex) börjar med en av de här raderna och pågår till slutet
# av stycket (tom rad) eller texten.
_BLOCK_START = re.compile(
    r"(microsoft teams|join zoom meeting|anslut till mötet|anslut till ett microsoft teams|"
    r"join the meeting|join microsoft teams|klicka här för att ansluta|webex|^_{5,}|^-{5,}\s*$)",
    re.IGNORECASE,
)
# Enskilda rader som alltid tas bort.
_LINE_DROP = re.compile(
    r"(mötes-?id|meeting id|konferens-?id|lösenord|password|passcode|lösenkod|\bkod\s*:|\bpin\s*[:=]|"
    r"dial-?in|ring in|ringa in|telefonkonferens|phone conference|tap to join)",
    re.IGNORECASE,
)
_PHONE_LINE = re.compile(r"^\s*(tel[.:]?\s*)?\+\d[\d\s,#*()-]{5,}")
_LINK = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_MAILTO = re.compile(r"\bmailto:\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b")
# Signaturen och allt efter den kapas.
_SIGNATURE = re.compile(r"^(--\s*|med vänliga hälsningar|vänliga hälsningar|mvh\b|best regards|kind regards)",
                        re.IGNORECASE)
_ONLINE_HINTS = ("teams.microsoft", "microsoft teams", "zoom.us", "join zoom", "webex", "meet.google",
                 "anslut till mötet", "skype")


def store_body(value: Any) -> str | None:
    """Beskrivningen som den lagras: styrtecken bort, radbrytningar kvar, högst 4 000 tecken."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [sanitize_text(line, limit=BODY_STORE_LIMIT) or "" for line in text.split("\n")]
    out = "\n".join(lines).strip()
    # Många tomma rader i rad blir en.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out[:BODY_STORE_LIMIT].rstrip() or None


def clean_body(text: str | None) -> str | None:
    """Beskrivningen utan mötesblock, inringning, koder, länkar och signatur (används vid sändning)."""
    if not text:
        return None
    kept: list[str] = []
    in_block = False
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if _SIGNATURE.match(line):
            break
        if not line:
            in_block = False
            kept.append("")
            continue
        if in_block:
            continue
        if _BLOCK_START.search(line):
            in_block = True
            continue
        if _LINE_DROP.search(line) or _PHONE_LINE.match(line):
            continue
        line = _MAILTO.sub("", line)
        line = _LINK.sub("", line)
        line = _EMAIL.sub("", line)
        line = " ".join(line.split())
        if line:
            kept.append(line)
    out = "\n".join(kept).strip()
    out = re.sub(r"\n{2,}", "\n", out)
    return out or None


def looks_online(*texts: str | None) -> bool:
    """Säger texten att mötet är ett onlinemöte (Teams, Zoom, Webex …)?"""
    folded = " ".join(fold(t or "") for t in texts)
    return any(h in folded for h in _ONLINE_HINTS)


def domain_of(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    return domain or None


def is_generic(domain: str | None) -> bool:
    return bool(domain) and domain in GENERIC_DOMAINS


def same_domain(domain: str | None, work_domain: str | None) -> bool:
    """Samma domän som ägarens jobbdomän, eller en underdomän till den."""
    if not domain or not work_domain:
        return False
    work = work_domain.strip().lower()
    return domain == work or domain.endswith("." + work)


def parse_person(value: Any, params: dict[str, Any] | None = None) -> dict[str, str] | None:
    """En deltagare ur ICS (``mailto:``-adress och parametern CN)."""
    raw = str(value or "").strip()
    email = raw[7:] if raw.lower().startswith("mailto:") else raw
    email = email.strip().lower()
    if "@" not in email:
        email = ""
    name = None
    if params:
        cn = params.get("CN")
        if isinstance(cn, (list, tuple)):
            cn = cn[0] if cn else None
        if cn:
            name = str(cn).strip().strip('"').strip()
    if not name and email:
        name = email.split("@", 1)[0]
    name = sanitize_text(name, limit=NAME_LIMIT)
    if not name and not email:
        return None
    return {"name": name or "", "email": email[:254]}


def participants(
    attendees: list[dict[str, Any]] | None, *, work_domain: str | None, self_email: str | None
) -> list[dict[str, Any]]:
    """Deltagarna som de skickas: namn, kollega/extern/okänd och den externa domänen.

    Ägaren själv tas bort. E-postadresserna följer aldrig med.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    me = (self_email or "").strip().lower()
    for a in attendees or []:
        if not isinstance(a, dict):
            continue
        email = str(a.get("email") or "").strip().lower()
        if me and email == me:
            continue
        name = sanitize_text(a.get("name"), limit=NAME_LIMIT)
        if not name:
            continue
        key = email or fold(name)
        if key in seen:
            continue
        seen.add(key)
        domain = domain_of(email)
        if not work_domain or not domain:
            relation = "okänd"
        elif same_domain(domain, work_domain):
            relation = "kollega"
        else:
            relation = "extern"
        company = domain if relation == "extern" and not is_generic(domain) else None
        out.append({"name": name, "relation": relation, "company_domain": company})
        if len(out) >= MAX_PARTICIPANTS_SENT:
            break
    return out


def external_domains(attendees: list[dict[str, Any]] | None, work_domain: str | None) -> list[str]:
    """Externa, icke-allmänna domäner bland deltagarna (ledtråd till företaget)."""
    out: list[str] = []
    for a in attendees or []:
        domain = domain_of(str((a or {}).get("email") or ""))
        if not domain or is_generic(domain) or same_domain(domain, work_domain):
            continue
        if domain not in out:
            out.append(domain)
    return out


_COMPANY_FORMS = ("aktiebolag", "ab", "hb", "kb")


def _company_key(text: str | None) -> str:
    words = [w for w in re.split(r"[\s,.]+", fold(text or "")) if w]
    while words and words[-1] in _COMPANY_FORMS:
        words.pop()
    while words and words[0] in _COMPANY_FORMS:
        words.pop(0)
    return "".join(words).replace("-", "")


def domain_matches(domain: str | None, *names: str | None) -> bool:
    """Stämmer domänens första led med ett företags- eller platsnamn?

    ``exempelakeri.se`` ↔ "Exempel Åkeri AB" är en träff. Allmänna e-postdomäner är aldrig det.
    """
    if not domain or is_generic(domain):
        return False
    first = domain.lower()
    if first.startswith("www."):
        first = first[4:]
    first = fold(first.split(".", 1)[0]).replace("-", "")
    if len(first) < 3:
        return False
    for name in names:
        key = _company_key(name)
        if not key:
            continue
        if key == first:
            return True
        shorter, longer = sorted((key, first), key=len)
        if len(shorter) >= 5 and longer.startswith(shorter):
            return True
    return False


def learn_owner(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """(ägarens egen adress, ägarens jobbdomän) ur bokningarnas deltagare.

    Egen adress = den som förekommer i flest bokningar med deltagare, om den finns i minst
    hälften av dem. Domänen = den egna adressens domän, annars vanligaste arrangörsdomänen,
    annars vanligaste deltagardomänen – aldrig en allmän domän.
    """
    with_people = [e.get("attendees") or [] for e in events if e.get("attendees")]
    if not with_people:
        return None, None
    seen_in: Counter[str] = Counter()
    organizers: Counter[str] = Counter()
    everyone: Counter[str] = Counter()
    for people in with_people:
        emails = {str(p.get("email") or "").lower() for p in people if p.get("email")}
        for email in emails:
            seen_in[email] += 1
        for p in people:
            domain = domain_of(str(p.get("email") or ""))
            if not domain or is_generic(domain):
                continue
            everyone[domain] += 1
            if p.get("role") == "organizer":
                organizers[domain] += 1
    self_email = None
    if seen_in:
        email, count = seen_in.most_common(1)[0]
        if count * 2 >= len(with_people):
            self_email = email
    domain = domain_of(self_email)
    if not domain or is_generic(domain):
        domain = None
        if organizers:
            domain = organizers.most_common(1)[0][0]
        elif everyone:
            domain = everyone.most_common(1)[0][0]
    return self_email, domain


__all__ = [
    "BODY_SEND_LIMIT", "BODY_STORE_LIMIT", "GENERIC_DOMAINS", "clean_body", "domain_matches", "domain_of",
    "external_domains", "is_generic", "learn_owner", "looks_online", "parse_person", "participants",
    "same_domain", "store_body",
]
