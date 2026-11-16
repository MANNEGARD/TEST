"""Kalenderlänk (ICS): tolkning, synk, status och schemat.

En hemlig kalenderlänk från Outlook / Microsoft 365 (delad eller publicerad kalender) är en
kalenderkälla utan inloggning och utan appregistrering. Händelserna lagras i samma tabell som
Graph-händelserna (``CalendarEvent``) med ``calendar_id = "ics"``, så att matchningen,
klassningen, granskningen och resedetaljen fungerar oförändrat.

Två källor kan finnas samtidigt: Graph-koden rör aldrig ``calendar_id = "ics"`` och den här
modulen rör bara sina egna händelser. En förekomst som Graph redan har (samma ``ical_uid``
och samma start, eller samma start, slut och rubrik) hoppas över, så att samma möte inte finns
två gånger.

Allt innehåll i filen är opålitlig indata – aldrig instruktioner. Bara fälten i
``_event_values`` läses, all text tvättas med ``text_safety.sanitize_text`` och kapas, och
taken (filstorlek, antal händelser, upprepningssteg, förekomster) hindrar en elak fil från att
dra minne eller processor. Länken själv finns bara krypterad i ``secrets_encrypted``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import CalendarEvent, CalendarMatch, IntegrationConnection
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import integrations
from app.services import settings as settings_svc
from app.services.calendar import ics_fetch
from app.services.calendar.ics_fetch import IcsError
from app.services.text_safety import sanitize_text

logger = logging.getLogger(__name__)

ICS_CALENDAR_ID = "ics"
PROVIDER = IntegrationProvider.CALENDAR_ICS
RUN_KIND = "calendar_ics_sync"

MAX_VEVENTS = 25_000
MAX_SERIES_STEPS = 200_000
MAX_TOTAL_STEPS = 1_000_000
# Uppskattade förekomster inom fönstret för alla upprepade serier tillsammans. Varje förekomst
# blir en kopia av händelsen i minnet, så taket hindrar en fil med många täta serier från att
# dra minne även när stegen ryms i budgeten.
MAX_WINDOW_ESTIMATE = 20_000
MAX_OCCURRENCES = 5_000
# Hårt tak under själva expansionen (förekomster som byggs i minnet), oavsett uppskattningen.
MAX_EXPANDED = 20_000
# Tidsbudget (sekunder väggtid) för expansionen. Prövas mellan serierna, så en enskild serie
# kan dra över något; stegtaken ovan håller varje serie liten.
EXPAND_SECONDS = 15.0

MSG_NOT_ICS = "Filen bakom länken är ingen kalender (ICS)."
MSG_TOO_MANY = "Kalendern innehåller för många händelser (högst 25 000)."
MSG_NOT_CONFIGURED = "Ingen kalenderlänk är sparad."

TIP_BUSY_ONLY = (
    "Länken visar bara ledig/upptagen-tid, så DriveLog kan inte föreslå ärenden. Dela kalendern i Outlook "
    "med Kan visa rubriker och platser och klistra in den nya länken."
)
TIP_EMPTY = "Länken innehåller inga händelser i synkfönstret. Kontrollera att du har delat rätt kalender."
TIP_NO_LOCATION = (
    "Ingen händelse i länken har någon plats. DriveLog kan då bara jämföra tider, vilket ger svagare förslag. "
    "Välj Kan visa rubriker och platser när du delar kalendern."
)
TIP_TRUNCATED = (
    "Kalendern har fler händelser än DriveLog läser i synkfönstret (högst 5 000). De senare hoppades över."
)

_BUSY_WORDS = frozenset({
    "busy", "upptagen", "tentative", "preliminär", "preliminärt", "free", "ledig", "out of office",
    "frånvarande", "working elsewhere", "arbetar någon annanstans",
})
_BUSY_STATUS = {
    "FREE": "free", "TENTATIVE": "tentative", "BUSY": "busy", "OOF": "oof", "WORKINGELSEWHERE": "workingElsewhere",
}
_FAST_FREQ = {"SECONDLY", "MINUTELY", "HOURLY"}
_PERIOD_DAYS = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 28, "YEARLY": 365}
_ORDINAL_DAY = re.compile(r"^\s*[+-]?\d")


def _err(message: str, code: str = "ics_invalid") -> IcsError:
    return IcsError(message, code)


# ----------------------------------------------------------------------------- tolkning


@dataclass
class ParseResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    total_vevents: int = 0
    skipped: int = 0
    truncated: bool = False
    busy_only: bool = False
    with_location: int = 0
    occurrences: int = 0  # ej inställda förekomster i fönstret (före ev. busy_only-rensning)


def _as_utc(value: datetime | date, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=tz)  # flytande tid (eller okänd TZID) i ägarens tidszon
        return value.astimezone(UTC)
    return datetime(value.year, value.month, value.day, tzinfo=tz).astimezone(UTC)


def _decoded(component: Any, name: str) -> Any:
    prop = component.get(name)
    if prop is None or isinstance(prop, list):
        return None
    try:
        value = prop.dt
    except Exception:  # trasigt värde (icalendar ger vBroken som kastar vid åtkomst)
        return None
    return value if isinstance(value, (datetime, date)) else None


def _text(component: Any, name: str, limit: int) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return sanitize_text(str(value), limit)


def _flag(component: Any, name: str) -> str:
    return (_text(component, name, 40) or "").upper()


def _rrules(component: Any) -> list[Any]:
    value = component.get("RRULE")
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _valid_start(component: Any) -> bool:
    start = _decoded(component, "DTSTART")
    if start is None:
        return False
    if component.get("DTEND") is not None and _decoded(component, "DTEND") is None:
        return False
    duration = component.get("DURATION")
    if duration is not None:
        try:
            ok = isinstance(duration.dt, timedelta)
        except Exception:
            ok = False
        if not ok:
            return False
    return True


def _per_period(rule: Any, freq: str) -> int:
    """Övre gräns för antalet kandidater per period (det upprepningen faktiskt räknar fram).

    BYDAY utan ordningstal betyder varje sådan veckodag i perioden: cirka 5 per månad och
    53 per år. Med ordningstal (1MO, -1FR) är det en per period. BYMONTH, BYMONTHDAY m.fl.
    begränsar i DAILY och WEEKLY men utökar i MONTHLY och YEARLY."""
    def n(part: str) -> int:
        return len(rule.get(part) or [])

    plain = ordinal = 0
    for value in rule.get("BYDAY") or []:
        if _ORDINAL_DAY.match(str(value)):
            ordinal += 1
        else:
            plain += 1
    if freq == "DAILY":
        return 1
    if freq == "WEEKLY":
        return max(1, plain + ordinal)
    if freq == "MONTHLY":
        return max(1, n("BYMONTHDAY") or plain * 5 + ordinal)
    if n("BYYEARDAY"):
        return n("BYYEARDAY")
    if n("BYWEEKNO"):
        return n("BYWEEKNO") * max(1, plain + ordinal)
    months = n("BYMONTH")
    if n("BYMONTHDAY"):
        return (months or 12) * n("BYMONTHDAY")
    if plain or ordinal:
        return months * (plain * 5 + ordinal) if months else plain * 53 + ordinal
    return max(1, months)


_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _ints(rule: Any, part: str) -> list[int]:
    return [int(v) for v in rule.get(part) or []]


def _yeardays_possible(months: frozenset[int], monthdays: frozenset[int], yeardays: frozenset[int]) -> bool:
    """Finns någon dag (vanligt år eller skottår) som uppfyller BYYEARDAY, BYMONTH och BYMONTHDAY?
    Går igenom BYYEARDAY-värdena, inte årets dagar, så att kostnaden följer regelns längd."""
    for year, year_len in ((2023, 365), (2024, 366)):
        for yd in yeardays:
            if not 1 <= abs(yd) <= year_len:
                continue
            d = date(year, 1, 1) + timedelta(days=(yd - 1 if yd > 0 else year_len + yd))
            if months and d.month not in months:
                continue
            last = (date(year + d.month // 12, d.month % 12 + 1, 1) - timedelta(days=1)).day
            if monthdays and d.day not in monthdays and d.day - last - 1 not in monthdays:
                continue
            return True
    return False


def _weeks_possible(months: frozenset[int], weeks: list[int]) -> bool:
    """Kan någon av veckorna (ISO-veckonummer) ligga i någon av månaderna? Generöst räknat:
    vecka n börjar mellan 29 december och 4 januari plus 7·(n−1) dagar."""
    if not any(1 <= abs(w) <= 53 for w in weeks):
        return False
    if not months:
        return True
    ref = date(2023, 1, 1)
    for w in weeks:
        if not 1 <= abs(w) <= 53:
            continue
        first = -4 + 7 * (w - 1) if w > 0 else 365 + 4 - 7 * -w - 7
        for offset in range(first, first + 15):
            if (ref + timedelta(days=offset)).month in months:  # även dagar i grannåren
                return True
    return False


def _never_occurs(rule: Any, freq: str, per: int) -> bool:
    """Sant för en regel som aldrig kan ge någon förekomst (30 februari, BYSETPOS=6 bland fem
    måndagar …). Biblioteket letar då efter nästa förekomst ända till år 9999, vilket tar
    sekunder per serie – en billig väg att dra processor."""
    months = frozenset(_ints(rule, "BYMONTH"))
    monthdays = frozenset(_ints(rule, "BYMONTHDAY"))
    yeardays = frozenset(_ints(rule, "BYYEARDAY"))
    if months and not any(1 <= m <= 12 for m in months):
        return True
    if monthdays and not any(1 <= abs(d) <= _MONTH_DAYS[m - 1] for d in monthdays
                             for m in (months or range(1, 13)) if 1 <= m <= 12):
        return True
    if yeardays and not _yeardays_possible(months, monthdays, yeardays):
        return True
    weeks = _ints(rule, "BYWEEKNO")
    if weeks and freq == "YEARLY" and not _weeks_possible(months, weeks):
        return True
    ordinals = [int(m.group(0)) for v in rule.get("BYDAY") or [] if (m := _ORDINAL_DAY.match(str(v)))]
    plain = len(rule.get("BYDAY") or []) - len(ordinals)
    if ordinals and not plain:
        limit = 5 if freq == "MONTHLY" or (freq == "YEARLY" and months) else 53
        if not any(1 <= abs(n) <= limit for n in ordinals):
            return True
    setpos = _ints(rule, "BYSETPOS")
    return bool(setpos) and not any(1 <= abs(n) <= per for n in setpos)


def _series_cost(component: Any, window_start: datetime, window_end: datetime, tz: ZoneInfo) -> tuple[int, int] | None:
    """Uppskattade (steg från seriens start, förekomster i fönstret), eller None för en serie
    som inte ska expanderas (för tät, oläslig eller slut före fönstret)."""
    start = _as_utc(_decoded(component, "DTSTART"), tz)
    steps_total = window_total = 0
    for rule in _rrules(component):
        if not isinstance(rule, icalendar.vRecur):
            return None
        try:
            freq = str((rule.get("FREQ") or [""])[0]).upper()
            interval = max(1, int((rule.get("INTERVAL") or [1])[0]))
            count = rule.get("COUNT")
            count_n = max(0, int(count[0])) if count else None
            until = rule.get("UNTIL")
            until_v = until[0] if until else None
            setpos = len(rule.get("BYSETPOS") or [])
        except (TypeError, ValueError, IndexError, AttributeError):
            return None
        if freq in _FAST_FREQ or any(rule.get(k) for k in ("BYSECOND", "BYMINUTE", "BYHOUR")):
            return None
        if freq not in _PERIOD_DAYS:
            return None
        if isinstance(until_v, (datetime, date)) and _as_utc(until_v, tz) < window_start:
            return None
        try:
            per = _per_period(rule, freq)  # kandidater per period (kostnaden)
            if _never_occurs(rule, freq, per):
                return None
        except (TypeError, ValueError, AttributeError):
            return None
        hits = min(per, setpos) if setpos else per  # förekomster per period (högst)
        period = timedelta(days=_PERIOD_DAYS[freq]) * interval
        span = window_end - start
        steps = max(1, math.ceil(span / period)) * per if span > timedelta(0) else per
        window_len = window_end - max(start, window_start)
        in_window = max(1, math.ceil(window_len / period) + 1) * hits if window_len > timedelta(0) else 0
        if count_n is not None:
            periods_needed = math.ceil(count_n / hits)
            steps = min(steps, periods_needed * per)
            in_window = min(in_window, count_n)
            # COUNT förekomster tar minst så många perioder. Tar de slut före fönstret räknas
            # serien inte mot fönsterbudgeten (den expanderas ändå; MAX_EXPANDED är det hårda taket).
            if start < window_start and periods_needed < (window_start - start) / period:
                in_window = 0
        steps_total += steps
        window_total += in_window
    return steps_total, window_total


def _rdate_count(component: Any) -> int | None:
    """Antal RDATE-värden (0 utan RDATE), eller None när de inte går att läsa."""
    value = component.get("RDATE")
    if value is None:
        return 0
    try:
        return sum(len(v.dts) for v in (value if isinstance(value, list) else [value]))
    except Exception:
        return None


def _components(root: Any) -> Any:
    """Alla underkomponenter, utan rekursion (en elak fil kan vara djupt nästlad)."""
    stack = list(root.subcomponents)
    while stack:
        comp = stack.pop()
        yield comp
        stack.extend(comp.subcomponents)


def _drop_nested_events(root: Any) -> None:
    """Biblioteket letar VEVENT på alla nivåer; bara de på översta nivån har prövats."""
    stack = [root]
    while stack:
        comp = stack.pop()
        comp.subcomponents = [c for c in comp.subcomponents if c.name != "VEVENT"]
        stack.extend(comp.subcomponents)


def _split_duplicate_uids(cal: Any) -> dict[str, str]:
    """Två VEVENT med samma UID utan RECURRENCE-ID (otillåtet, men det förekommer i
    sammanslagna kalendrar) slår biblioteket ihop till en. En dubblett med annan start får en
    egen UID före expansionen; den ursprungliga återställs i ``_event_values``. Returnerar
    {tvättad ny UID: tvättad ursprunglig UID}."""
    raw_uids = {str(c.get("UID")) for c in cal.subcomponents if c.name == "VEVENT" and c.get("UID") is not None}
    cores: dict[str, set[bytes]] = {}
    aliases: dict[str, str] = {}
    for comp in cal.subcomponents:
        if comp.name != "VEVENT" or comp.get("RECURRENCE-ID") is not None or comp.get("UID") is None:
            continue
        uid = str(comp.get("UID"))
        dtstart = comp.get("DTSTART")
        try:
            start = dtstart.to_ical() if dtstart is not None and not isinstance(dtstart, list) else b""
        except Exception:  # trasigt värde: prövas och räknas bort i _prefilter
            start = b""
        start = start if isinstance(start, bytes) else str(start).encode()
        starts = cores.setdefault(uid, set())
        if not starts or start in starts:
            starts.add(start)  # första, eller exakt samma händelse igen (den sista vinner som förut)
            continue
        starts.add(start)
        n = len(starts)
        new_uid = f"{uid}#{n}"
        while new_uid in raw_uids:
            n += 1
            new_uid = f"{uid}#{n}"
        raw_uids.add(new_uid)
        comp["UID"] = icalendar.vText(new_uid)
        original, renamed = sanitize_text(uid, 400), sanitize_text(new_uid, 400)
        if original and renamed:
            aliases[renamed] = original
    return aliases


def _prefilter(
    cal: Any, window_start: datetime, window_end: datetime, tz: ZoneInfo
) -> tuple[int, set[str], dict[str, str]]:
    """Tar bort trasiga och för dyra komponenter före expansionen. Returnerar (antal bortagna,
    UID för upprepade serier, UID-alias för dubbletter)."""
    skipped = 0
    total_steps = total_window = 0
    recurring: set[str] = set()
    kept: list[Any] = []
    aliases = _split_duplicate_uids(cal)
    for comp in cal.subcomponents:
        if comp.name != "VEVENT":
            if comp.name == "VTIMEZONE":  # bara tidszoner behövs; VTODO, VJOURNAL m.m. tas bort
                _drop_nested_events(comp)
                kept.append(comp)
            continue
        _drop_nested_events(comp)
        if not _valid_start(comp):
            skipped += 1
            continue
        uid = _text(comp, "UID", 400)
        uid = aliases.get(uid, uid) if uid else uid
        has_rrule = comp.get("RRULE") is not None
        rdates = _rdate_count(comp)
        if comp.get("RECURRENCE-ID") is not None or has_rrule or rdates:
            if uid:
                recurring.add(uid)
        if comp.get("RECURRENCE-ID") is not None or not (has_rrule or rdates):
            kept.append(comp)  # ändrade förekomster och enstaka händelser behålls alltid
            continue
        try:
            cost = _series_cost(comp, window_start, window_end, tz) if has_rrule else (0, 0)
        except (TypeError, ValueError, OverflowError, AttributeError):
            cost = None
        if cost is None or rdates is None:
            skipped += 1
            continue
        steps, in_window = cost[0] + rdates, cost[1] + rdates  # varje RDATE kan bli en förekomst
        if (steps > MAX_SERIES_STEPS or total_steps + steps > MAX_TOTAL_STEPS
                or total_window + in_window > MAX_WINDOW_ESTIMATE):
            skipped += 1
            continue
        total_steps += steps
        total_window += in_window
        kept.append(comp)
    cal.subcomponents = kept
    return skipped, recurring, aliases


def _collect(
    query: Any, start: datetime, end: datetime, out: list[Any], deadline: float
) -> tuple[int, bool]:
    """Lägger förekomsterna i [start, end] i ``out``, serie för serie och förekomst för
    förekomst, och slutar vid ``MAX_EXPANDED`` eller när tidsbudgeten är slut. Returnerar
    (antal trasiga serier, kapat)."""
    failed = 0
    for series in query.series:
        if time.monotonic() > deadline:
            return failed, True
        try:
            for occurrence in series.between(start, end):
                if len(out) >= MAX_EXPANDED:
                    return failed, True
                out.append(occurrence.as_component(query.keep_recurrence_attributes))
        except Exception:  # biblioteket kan kasta många slags fel på en trasig serie
            failed += 1
    return failed, False


def _expand(cal: Any, start: datetime, end: datetime) -> tuple[list[Any], int, bool]:
    """Förekomsterna i [start, end], antal trasiga serier och om taket nåddes. Går kalendern
    inte att dela upp i serier prövas varje UID för sig, så att en trasig händelse inte stoppar
    resten."""
    out: list[Any] = []
    deadline = time.monotonic() + EXPAND_SECONDS
    query: Any = None
    try:
        query = recurring_ical_events.of(cal, skip_bad_series=True)
    except Exception:  # noqa: S110 – biblioteket kan kasta många slags fel på trasig indata
        query = None
    if query is not None:
        failed, capped = _collect(query, start, end, out, deadline)
        return out, failed, capped
    timezones = [c for c in cal.subcomponents if c.name != "VEVENT"]
    groups: dict[str, list[Any]] = {}
    for i, comp in enumerate(c for c in cal.subcomponents if c.name == "VEVENT"):
        groups.setdefault(str(comp.get("UID") or f"#{i}"), []).append(comp)
    skipped = 0
    for comps in groups.values():
        part = icalendar.Calendar()
        part.subcomponents = [*timezones, *comps]
        try:
            part_query = recurring_ical_events.of(part, skip_bad_series=True)
        except Exception:
            skipped += 1
            continue
        failed, capped = _collect(part_query, start, end, out, deadline)
        skipped += failed
        if capped:
            return out, skipped, True
    return out, skipped, False


def _occ_key(value: datetime | date, tz: ZoneInfo) -> str:
    if isinstance(value, datetime):
        return _as_utc(value, tz).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _event_values(
    comp: Any, tz: ZoneInfo, include_body: bool, recurring: set[str], aliases: dict[str, str] | None = None
) -> dict[str, Any] | None:
    raw_start = _decoded(comp, "DTSTART")
    if raw_start is None:
        return None
    raw_end = _decoded(comp, "DTEND")
    all_day = not isinstance(raw_start, datetime) or _flag(comp, "X-MICROSOFT-CDO-ALLDAYEVENT") == "TRUE"
    start_at = _as_utc(raw_start, tz)
    if raw_end is not None:
        end_at = _as_utc(raw_end, tz)
    elif not isinstance(raw_start, datetime):
        end_at = start_at + timedelta(days=1)
    else:
        end_at = start_at
    end_at = max(end_at, start_at)
    uid = _text(comp, "UID", 400)
    if uid and aliases:
        uid = aliases.get(uid, uid)
    summary = _text(comp, "SUMMARY", 500)
    if uid:
        key = uid
    else:
        raw_text = comp.get("DTSTART").to_ical().decode("utf-8", "replace") if comp.get("DTSTART") else ""
        key = "nouid:" + _sha(f"{raw_text}|{summary or ''}")
    occ = _decoded(comp, "RECURRENCE-ID") or raw_start
    busy = _flag(comp, "X-MICROSOFT-CDO-BUSYSTATUS")
    if busy in _BUSY_STATUS:
        show_as = _BUSY_STATUS[busy]
    elif _flag(comp, "TRANSP") == "TRANSPARENT":
        show_as = "free"
    else:
        show_as = "busy"
    is_online = comp.get("X-MICROSOFT-SKYPETEAMSMEETINGURL") is not None
    return {
        "external_id": f"{_sha(key)[:40]}:{_occ_key(occ, tz)}",
        "ical_uid": uid,
        "subject": summary or "",
        "start_at": start_at,
        "end_at": end_at,
        "is_all_day": all_day,
        "is_cancelled": _flag(comp, "STATUS") == "CANCELLED",
        "show_as": show_as,
        "sensitivity": "private" if _flag(comp, "CLASS") in ("PRIVATE", "CONFIDENTIAL") else "normal",
        "is_online": is_online,
        "online_provider": "teamsForBusiness" if is_online else None,
        "location_name": _text(comp, "LOCATION", 500),
        "location_address": None,
        "location_city": None,
        "latitude": None,
        "longitude": None,
        "body_preview": _text(comp, "DESCRIPTION", 255) if include_body else None,
        "series_master_id": uid if uid and uid in recurring else None,
    }


def _is_busy_text(values: dict[str, Any]) -> bool:
    if values["location_name"]:
        return False
    subject = (values["subject"] or "").strip().casefold()
    return not subject or subject in _BUSY_WORDS


def parse(
    data: bytes, *, window_start: datetime, window_end: datetime, tz: ZoneInfo, include_body: bool
) -> ParseResult:
    """Tolkar en ICS-fil och returnerar förekomsterna i fönstret som värden för ``CalendarEvent``."""
    raw = data.lstrip(b"\xef\xbb\xbf").lstrip()
    if not raw[:15].upper().startswith(b"BEGIN:VCALENDAR"):
        raise _err(MSG_NOT_ICS)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:  # äldre export i Windows-1252/Latin-1 (Göteborg, inte G�teborg)
        text = raw.decode("cp1252", errors="replace")
    cal: Any = None
    try:
        cal = icalendar.Calendar.from_ical(text)
    except Exception:  # noqa: S110 – allt som inte går att läsa är "ingen kalender"
        cal = None
    if cal is None or isinstance(cal, list) or getattr(cal, "name", "") != "VCALENDAR":
        raise _err(MSG_NOT_ICS)  # utanför except: inget __context__
    result = ParseResult()
    result.total_vevents = sum(1 for c in _components(cal) if c.name == "VEVENT")
    if result.total_vevents > MAX_VEVENTS:
        raise _err(MSG_TOO_MANY)
    expanded: tuple[int, set[str], dict[str, str], list[Any], int, bool] | None = None
    try:
        skipped, recurring, aliases = _prefilter(cal, window_start, window_end, tz)
        found, failed, capped = _expand(cal, window_start - timedelta(days=1), window_end + timedelta(days=1))
        expanded = (skipped, recurring, aliases, found, failed, capped)
    except Exception:  # noqa: S110
        expanded = None
    if expanded is None:
        raise _err(MSG_NOT_ICS)
    result.skipped, recurring, aliases, found, failed, capped = expanded
    result.skipped += failed
    result.truncated = capped
    occurrences: list[dict[str, Any]] = []
    for comp in found:
        try:
            values = _event_values(comp, tz, include_body, recurring, aliases)
        except Exception:
            values = None
        if values is None:
            result.skipped += 1
            continue
        if values["end_at"] >= window_start and values["start_at"] <= window_end:
            occurrences.append(values)
    occurrences.sort(key=lambda v: (v["start_at"], v["external_id"]))
    if len(occurrences) > MAX_OCCURRENCES:
        occurrences = occurrences[:MAX_OCCURRENCES]
        result.truncated = True
    by_id: dict[str, dict[str, Any]] = {}
    for values in occurrences:
        by_id[values["external_id"]] = values  # samma förekomst två gånger: den sista vinner
    events = sorted(by_id.values(), key=lambda v: (v["start_at"], v["external_id"]))
    active = [v for v in events if not v["is_cancelled"]]
    result.occurrences = len(active)
    result.with_location = sum(1 for v in active if v["location_name"])
    result.busy_only = bool(events) and all(_is_busy_text(v) for v in events)
    result.events = [] if result.busy_only else events
    return result


# ----------------------------------------------------------------------------- synk


def _window(db: Session, user_id: uuid.UUID, now: datetime) -> tuple[datetime, datetime, bool, ZoneInfo]:
    cal = settings_svc.get_section(db, user_id, "calendar")
    start = now - timedelta(days=int(cal["sync_days_back"]))
    end = now + timedelta(days=int(cal["sync_days_forward"]))
    return start, end, bool(cal["store_body_preview"]), settings_svc.user_timezone(db, user_id)


def _fetch_and_parse(
    db: Session, user_id: uuid.UUID, url: str, now: datetime, transport: httpx.BaseTransport | None
) -> tuple[ParseResult, datetime, datetime]:
    start, end, include_body, tz = _window(db, user_id, now)
    data = ics_fetch.fetch(url, transport=transport)
    parsed = parse(data, window_start=start, window_end=end, tz=tz, include_body=include_body)
    return parsed, start, end


def _subject_key(subject: str | None) -> str:
    return " ".join((sanitize_text(subject or "", 500) or "").split()).casefold()


def _graph_duplicates(db: Session, user_id: uuid.UUID, start: datetime, end: datetime) -> set[tuple[Any, ...]]:
    """Nycklar för mötena som Graph redan har: (UID, start) och (start, slut, rubrik).

    Graph ger varje förekomst i en upprepad serie ett eget iCalUId medan ICS-filen har seriens
    gemensamma UID, så för upprepade möten jämförs tid och rubrik i stället."""
    rows = db.execute(
        select(CalendarEvent.ical_uid, CalendarEvent.start_at, CalendarEvent.end_at, CalendarEvent.subject).where(
            CalendarEvent.user_id == user_id,
            CalendarEvent.is_demo.is_(False),
            CalendarEvent.removed_at.is_(None),
            CalendarEvent.calendar_id.not_in((ICS_CALENDAR_ID, "demo")),
            CalendarEvent.end_at >= start - timedelta(days=1),
            CalendarEvent.start_at <= end + timedelta(days=1),
        )
    ).all()
    keys: set[tuple[Any, ...]] = set()
    for uid, at, until, subject in rows:
        if uid:
            keys.add(("uid", uid, _norm(at)))
        if subject_key := _subject_key(subject):
            keys.add(("slot", _norm(at), _norm(until), subject_key))
    return keys


def _is_graph_duplicate(values: dict[str, Any], dupes: set[tuple[Any, ...]]) -> bool:
    if values["ical_uid"] and ("uid", values["ical_uid"], values["start_at"]) in dupes:
        return True
    subject_key = _subject_key(values["subject"])
    return bool(subject_key) and ("slot", values["start_at"], values["end_at"], subject_key) in dupes


def _norm(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _upsert(
    db: Session, user_id: uuid.UUID, conn: IntegrationConnection, parsed: ParseResult,
    start: datetime, end: datetime, now: datetime,
) -> dict[str, int]:
    stats = {"created": 0, "updated": 0, "removed": 0, "skipped": parsed.skipped}
    existing = {
        e.external_id: e
        for e in db.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == user_id,
                CalendarEvent.calendar_id == ICS_CALENDAR_ID,
                CalendarEvent.end_at >= start,
                CalendarEvent.start_at <= end,
            )
        ).scalars()
    }
    dupes = _graph_duplicates(db, user_id, start, end)
    seen: set[str] = set()
    for values in parsed.events:
        if _is_graph_duplicate(values, dupes):
            stats["skipped"] += 1
            continue
        seen.add(values["external_id"])
        ev = existing.get(values["external_id"])
        if ev is None:
            ev = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.user_id == user_id,
                    CalendarEvent.calendar_id == ICS_CALENDAR_ID,
                    CalendarEvent.external_id == values["external_id"],
                )
            ).scalar_one_or_none()
        if ev is None:
            ev = CalendarEvent(
                user_id=user_id, connection_id=conn.id, calendar_id=ICS_CALENDAR_ID, is_demo=False, **values
            )
            db.add(ev)
            stats["created"] += 1
        else:
            for k, v in values.items():
                setattr(ev, k, v)
            ev.connection_id = conn.id
            ev.removed_at = None
            stats["updated"] += 1
        ev.fetched_at = now
    for ext_id, ev in existing.items():
        if ext_id not in seen and ev.removed_at is None:
            ev.removed_at = now  # borttagen ur länken (eller flyttad ur fönstret)
            stats["removed"] += 1
    db.flush()
    return stats


def _summary(parsed: ParseResult) -> dict[str, Any]:
    return {
        "events": parsed.occurrences,
        "with_location": parsed.with_location,
        "busy_only": parsed.busy_only,
        "skipped": parsed.skipped,
        "truncated": parsed.truncated,
    }


def _apply(
    db: Session, user_id: uuid.UUID, conn: IntegrationConnection, parsed: ParseResult,
    start: datetime, end: datetime, now: datetime, run: Any,
) -> dict[str, Any]:
    stats = _upsert(db, user_id, conn, parsed, start, end, now)
    integrations.mark_success(conn, now)
    conn.status = IntegrationStatus.CONNECTED
    summary = _summary(parsed)
    summary["skipped"] = stats["skipped"]
    conn.capabilities = summary
    integrations.finish_run(run, "ok", stats={**stats, "events": summary["events"]})
    db.flush()
    return {
        "status": "ok",
        "created": stats["created"],
        "updated": stats["updated"],
        "removed": stats["removed"],
        "events": summary["events"],
        "skipped": stats["skipped"],
        "truncated": summary["truncated"],
        "tip": tip_for(conn, settings_svc.get_section(db, user_id, "calendar")),
    }


def _link(conn: IntegrationConnection | None) -> str | None:
    url = integrations.secrets(conn).get("url") if conn is not None else None
    return url if isinstance(url, str) and url else None


def save_link(
    db: Session, user_id: uuid.UUID, raw_url: str, *, now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Prövar länken (hämtar och tolkar) och sparar den krypterad först när det gick."""
    now = now or utcnow()
    url = ics_fetch.normalize_link(raw_url)
    parsed, start, end = _fetch_and_parse(db, user_id, url, now, transport)
    conn = integrations.get_or_create(db, user_id, PROVIDER)
    integrations.set_secrets(conn, {"url": url}, merge=False)
    integrations.update_config(conn, {"host": ics_fetch.host_of(url)})
    conn.status = IntegrationStatus.CONNECTED
    run = integrations.start_run(db, conn, RUN_KIND)
    _apply(db, user_id, conn, parsed, start, end, now, run)
    return status(db, user_id)


def sync_user(
    db: Session, user_id: uuid.UUID, *, now: datetime | None = None, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Hämtar länken och uppdaterar lagrade händelser. Kastar ``IcsError`` (felet sparas)."""
    now = now or utcnow()
    conn = integrations.get(db, user_id, PROVIDER)
    url = _link(conn)
    if conn is None or url is None:
        raise IcsError(MSG_NOT_CONFIGURED, "not_configured")
    failure: IcsError | None = None
    try:
        parsed, start, end = _fetch_and_parse(db, user_id, url, now, transport)
    except IcsError as exc:
        failure = IcsError(exc.message, exc.code)
    run = integrations.start_run(db, conn, RUN_KIND)
    if failure is not None:
        integrations.mark_error(conn, failure.message)
        integrations.finish_run(run, "error", error=failure.message)
        db.flush()
        raise failure
    return _apply(db, user_id, conn, parsed, start, end, now, run)


def remove_link(db: Session, user_id: uuid.UUID) -> bool:
    """Raderar länken, dess händelser och kalenderträffarna på dem. Returnerar om något fanns."""
    conn = integrations.get(db, user_id, PROVIDER)
    if conn is None or (_link(conn) is None and conn.status == IntegrationStatus.NOT_CONFIGURED):
        return False
    ids = select(CalendarEvent.id).where(
        CalendarEvent.user_id == user_id, CalendarEvent.calendar_id == ICS_CALENDAR_ID
    )
    db.execute(delete(CalendarMatch).where(CalendarMatch.calendar_event_id.in_(ids)))
    db.execute(
        delete(CalendarEvent).where(CalendarEvent.user_id == user_id, CalendarEvent.calendar_id == ICS_CALENDAR_ID)
    )
    db.expire_all()
    conn = integrations.get(db, user_id, PROVIDER)
    if conn is not None:
        integrations.disconnect(db, conn)
    db.flush()
    return True


def stored_events(db: Session, user_id: uuid.UUID) -> int:
    return int(db.execute(
        select(func.count(CalendarEvent.id)).where(
            CalendarEvent.user_id == user_id, CalendarEvent.calendar_id == ICS_CALENDAR_ID,
            CalendarEvent.is_demo.is_(False), CalendarEvent.removed_at.is_(None),
        )
    ).scalar_one())


def tip_for(conn: IntegrationConnection | None, cal_settings: dict[str, Any] | None = None) -> str | None:
    """Ett råd till ägaren utifrån senaste lyckade synk, eller None."""
    if conn is None or _link(conn) is None or conn.last_success_at is None:
        return None
    caps = conn.capabilities or {}
    if not caps:
        return None
    if caps.get("busy_only"):
        return TIP_BUSY_ONLY
    if not caps.get("events"):
        return TIP_EMPTY
    if not caps.get("with_location"):
        return TIP_NO_LOCATION
    if caps.get("truncated"):
        return TIP_TRUNCATED
    return None


def status(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Säker vy för API/UI – aldrig länken, bara att den finns och värdnamnet."""
    conn = integrations.get(db, user_id, PROVIDER)
    cal = settings_svc.get_section(db, user_id, "calendar")
    has_link = _link(conn) is not None
    caps = (conn.capabilities or {}) if conn is not None else {}
    with_location = caps.get("with_location") if has_link else None
    host = (conn.config or {}).get("host") if conn is not None and has_link else None
    return {
        "provider": PROVIDER.value,
        "status": str(conn.status) if conn is not None else IntegrationStatus.NOT_CONFIGURED.value,
        "has_link": has_link,
        "host": host if isinstance(host, str) and host else None,
        "last_sync_at": conn.last_sync_at if conn is not None else None,
        "last_success_at": conn.last_success_at if conn is not None else None,
        "last_error": conn.last_error if conn is not None else None,
        "last_error_at": conn.last_error_at if conn is not None else None,
        "stored_events": stored_events(db, user_id),
        "events_with_location": int(with_location) if isinstance(with_location, int) else None,
        "tip": tip_for(conn, cal),
        "stores_body_preview": bool(cal["store_body_preview"]),
        "sync_days_back": int(cal["sync_days_back"]),
        "sync_days_forward": int(cal["sync_days_forward"]),
    }


# ----------------------------------------------------------------------------- schemat


def sync_all_due(db: Session, *, now: datetime | None = None, min_interval_minutes: int = 30) -> dict[str, int]:
    """Synkar alla sparade kalenderlänkar som inte synkats nyligen (anropas av schemat).

    Samma regler som ``sync.sync_all_due``: status ERROR försöks igen glesare, och efter en
    lyckad synk matchas resorna och klassas om.
    """
    from app.services.calendar import sync

    now = now or utcnow()
    done = failed = 0
    conns = db.execute(
        select(IntegrationConnection).where(
            IntegrationConnection.provider == PROVIDER,
            IntegrationConnection.status.in_((IntegrationStatus.CONNECTED, IntegrationStatus.ERROR)),
            IntegrationConnection.secrets_encrypted.is_not(None),
        )
    ).scalars().all()
    for conn in conns:
        interval = timedelta(minutes=min_interval_minutes)
        if conn.status == IntegrationStatus.ERROR:
            interval = max(interval, sync.ERROR_RETRY_INTERVAL)
        if conn.last_sync_at and now - _norm(conn.last_sync_at) < interval:
            continue
        user_id = conn.user_id
        try:
            sync_user(db, user_id, now=now)
            db.commit()
        except IcsError as exc:
            logger.warning("Kalenderlänken kunde inte synkas: %s", exc.message)
            failed += 1
            db.commit()
            continue
        done += 1
        sync.rematch_after_sync(db, user_id, now)
    return {"synced": done, "failed": failed}


__all__ = [
    "ICS_CALENDAR_ID", "PROVIDER", "IcsError", "ParseResult", "parse", "remove_link", "save_link", "status",
    "sync_all_due", "sync_user", "tip_for",
]
