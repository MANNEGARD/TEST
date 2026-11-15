"""Notistexter på svenska.

Sekretess: gatuadresser förekommer aldrig. Hemmet visas som "Hemmet" och privata
platser som "Privat plats". När ``hide_locations_on_lock_screen`` är på innehåller
låsskärmstexten inga platsnamn alls – detaljerna visas först i den inloggade appen.
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo
from typing import Any

from sqlalchemy.orm import Session

from app.models import KnownLocation, LogicalTrip
from app.models.enums import Category
from app.services.ha import actions as A
from app.services.places import registry as places
from app.services.places.normalize import address_key, parse_address

APP_TITLE = "SMGD DriveLog"
MAX_PURPOSE_BUTTONS = 3
MAX_BUTTON_TITLE = 60

_POSTCODE_RE = re.compile(r"^(?:SE-?)?\d{3}\s?\d{2}\s+", re.IGNORECASE)


def fmt_mil(km: float | None) -> str:
    """Kilometer → svenska mil med decimalkomma: 42.0 → "4,2 mil"."""
    return f"{(km or 0) / 10:.1f}".replace(".", ",") + " mil"


def fmt_minutes(low: float | None, high: float | None, exact: bool = True) -> str:
    if low is None:
        return "okänt"
    if exact or high is None or round(high) == round(low):
        return f"{round(low)} minuters"
    return f"mellan {round(low)} och {round(high)} minuters"


def safe_label(loc: KnownLocation | None, address: str | None) -> str:
    """Platsetikett som aldrig är en gatuadress."""
    label = places.public_label(loc, address)
    candidates = {address_key(address)}
    if loc is not None:
        candidates.add(address_key(loc.address))
    if address_key(label) in candidates and address_key(label):
        city = (loc.city if loc is not None else None) or parse_address(address).city
        label = city or "okänd plats"
    # Postnummer ("702 10 Örebro") är onödigt exakt – visa bara orten.
    return _POSTCODE_RE.sub("", label).strip() or "okänd plats"


def trip_labels(db: Session, trip: LogicalTrip) -> tuple[str, str]:
    origin = db.get(KnownLocation, trip.start_location_id) if trip.start_location_id else None
    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    return safe_label(origin, trip.start_address), safe_label(dest, trip.end_address)


def _q_path(public_id: str) -> str:
    return f"/m/q/{public_id}"


def classify(db: Session, trip: LogicalTrip, public_id: str, *, hide_locations: bool) -> dict[str, Any]:
    origin, dest = trip_labels(db, trip)
    route = f"{origin} → {dest}"
    distance = fmt_mil(trip.distance_km)
    question = "Var detta arbete eller privat?"
    if trip.category in (Category.WORK, Category.PRIVATE):
        question += f" Förslag: {'Arbete' if trip.category == Category.WORK else 'Privat'}."
    detail = f"Ny resa: {route}. {distance}. {question}"
    message = f"Ny resa: {distance}. {question}" if hide_locations else detail
    return {
        "title": "Ny resa",
        "message": message,
        "actions": [
            {"key": A.WORK, "title": "Arbete", "icon": "sfsymbols:briefcase"},
            {"key": A.PRIVATE, "title": "Privat", "icon": "sfsymbols:house"},
            {"key": A.REVIEW, "title": "Granska", "uri": _q_path(public_id)},
            {"key": A.LATER, "title": "Senare"},
        ],
        "context": {"detail_message": detail, "route_label": route, "distance_km": trip.distance_km},
    }


def purpose(
    db: Session, trip: LogicalTrip, public_id: str, *, hide_locations: bool, previous: list[str]
) -> dict[str, Any]:
    _, dest = trip_labels(db, trip)
    detail = f"Du har en resa till {dest}. Vad var syftet?"
    message = f"Du har en tjänsteresa på {fmt_mil(trip.distance_km)} utan syfte. Vad var syftet?" if hide_locations else detail
    acts: list[dict[str, Any]] = []
    for i, text in enumerate(previous[:MAX_PURPOSE_BUTTONS]):
        title = text if len(text) <= MAX_BUTTON_TITLE else text[: MAX_BUTTON_TITLE - 1] + "…"
        acts.append({"key": f"{A.PURPOSE_PICK}{i}", "title": title, "purpose": text})
    acts.append({"key": A.PURPOSE_TEXT, "title": "Ange nytt syfte", "text_input": True, "placeholder": "Syfte med resan"})
    acts.append({"key": A.OPEN, "title": "Öppna körjournal", "uri": _q_path(public_id)})
    return {
        "title": "Vad var syftet?",
        "message": message,
        "actions": acts,
        "context": {"detail_message": detail, "destination_label": dest, "distance_km": trip.distance_km},
    }


def merge(public_id: str, minutes_low: float | None, minutes_high: float | None, exact: bool) -> dict[str, Any]:
    if minutes_low is None:
        message = "Två resor med okänt stillestånd emellan. Ska de slås samman?"
    else:
        message = f"Två resor med {fmt_minutes(minutes_low, minutes_high, exact)} stillestånd. Ska de slås samman?"
    return {
        "title": "Slå samman resor?",
        "message": message,
        "actions": [
            {"key": A.MERGE_YES, "title": "Ja"},
            {"key": A.MERGE_NO, "title": "Nej"},
            {"key": A.REVIEW, "title": "Granska", "uri": _q_path(public_id)},
            {"key": A.LATER, "title": "Senare"},
        ],
        "context": {"detail_message": message},
    }


def digest(count: int) -> dict[str, Any]:
    noun = "resa behöver" if count == 1 else "resor behöver"
    return {
        "title": APP_TITLE,
        "message": f"{count} {noun} granskas i körjournalen.",
        "actions": [
            {"key": A.OPEN, "title": "Öppna granskning", "uri": "/review"},
            {"key": A.LATER, "title": "Senare"},
        ],
        "context": {"count": count},
    }


def weekly(summary: dict[str, Any]) -> dict[str, Any]:
    review = summary["review_count"]
    review_text = "1 resa som behöver granskas" if review == 1 else f"{review} resor som behöver granskas"
    message = (
        f"Veckans körjournal: {fmt_mil(summary['work_km'])} i tjänsten, "
        f"{fmt_mil(summary['private_km'])} privat och {review_text}."
    )
    return {
        "title": "Veckosammanställning",
        "message": message,
        "actions": [{"key": A.OPEN, "title": "Öppna körjournal", "uri": "/review"}],
        "context": {"summary": summary},
    }


def daily(summary: dict[str, Any]) -> dict[str, Any]:
    trips = summary["trips"]
    review = summary["review_count"]
    trips_text = "1 resa" if trips == 1 else f"{trips} resor"
    review_text = "1 resa behöver granskas" if review == 1 else f"{review} resor behöver granskas"
    message = f"Dagens körjournal: {trips_text}, {fmt_mil(summary['total_km'])}. {review_text}."
    return {
        "title": "Daglig avstämning",
        "message": message,
        "actions": [{"key": A.OPEN, "title": "Öppna granskning", "uri": "/review"}],
        "context": {"summary": summary},
    }


def test(public_id: str) -> dict[str, Any]:
    return {
        "title": APP_TITLE,
        "message": "Testnotis från SMGD DriveLog. Tryck på Fungerar för att bekräfta att svaren når fram.",
        "actions": [
            {"key": A.TEST_OK, "title": "Fungerar"},
            {"key": A.OPEN, "title": "Öppna", "uri": _q_path(public_id)},
        ],
        "context": {},
    }


# ----------------------------------------------------------------------------- självgående läge (C.4)

WEEKDAYS_SV = ("mån", "tis", "ons", "tors", "fre", "lör", "sön")
MONTHS_SV = (
    "januari", "februari", "mars", "april", "maj", "juni",
    "juli", "augusti", "september", "oktober", "november", "december",
)
# Flaggor som betyder att resan kan vara flera körningar (spärr 1 i A.2).
MULTI_FLAGS = frozenset({"possible_missed_trips", "collection_gap", "detour_or_multiple_trips"})
MULTI_TEXT = "Kan vara flera körningar."


def _button(prefix: str, text: str) -> str:
    title = f"{prefix}{text}"
    return title if len(title) <= MAX_BUTTON_TITLE else title[: MAX_BUTTON_TITLE - 1] + "…"


def _same_text(a: str | None, b: str | None) -> bool:
    return bool(address_key(a)) and address_key(a) == address_key(b)


def place_label(loc: KnownLocation | None, address: str | None) -> str:
    """Platsens namn i notiser (E.3), aldrig med husnummer.

    Hemmet blir "Hemmet" och privata platser "Privat plats". En bekräftad plats visas med
    sitt namn. En obekräftad plats visas som "POI-namn, ort" eller "gata utan nummer, ort".
    Hela adressen visas bara i den inloggade appen.
    """
    if loc is not None and loc.type in ("home", "private"):
        return places.public_label(loc, address)
    city_raw = (loc.city if loc is not None and loc.city else None) or parse_address(
        (loc.address if loc is not None and loc.address else None) or address
    ).city
    city = _POSTCODE_RE.sub("", city_raw or "").strip() or None
    if loc is not None and loc.confirmed and loc.name and not _same_text(loc.name, loc.address) \
            and not _same_text(loc.name, address):
        return _POSTCODE_RE.sub("", loc.name).strip() or "okänd plats"
    name = None
    if loc is not None:
        # Kartans namn (POI) sparas som company eller name; en adress räknas aldrig som namn.
        for candidate in (loc.company, loc.name):
            if candidate and not _same_text(candidate, loc.address) and not _same_text(candidate, address) \
                    and parse_address(candidate).number is None:
                name = candidate.strip()
                break
    if name is None:
        street = parse_address((loc.address if loc is not None and loc.address else None) or address).street
        if street and street != city_raw:
            name = street
    if name and city and city.lower() not in name.lower():
        return f"{name}, {city}"
    return name or city or "okänd plats"


def place_labels(db: Session, trip: LogicalTrip) -> tuple[str, str]:
    origin = db.get(KnownLocation, trip.start_location_id) if trip.start_location_id else None
    dest = db.get(KnownLocation, trip.end_location_id) if trip.end_location_id else None
    return place_label(origin, trip.start_address), place_label(dest, trip.end_address)


def fmt_day(when: datetime | None, tz: tzinfo) -> str:
    """"tis 15/9"."""
    if when is None:
        return "okänd dag"
    local = when.astimezone(tz)
    return f"{WEEKDAYS_SV[local.weekday()]} {local.day}/{local.month}"


def fmt_trip_times(trip: LogicalTrip, tz: tzinfo) -> str:
    """"08:10–08:52", "≈ 07:35–08:52" eller "–08:52" när starttiden är okänd."""
    from app.services.trips import start_time

    start = start_time.display(trip, tz)
    end = ""
    if trip.ended_at is not None:
        end = trip.ended_at.astimezone(tz).strftime("%H:%M")
        if trip.end_time_quality == "estimated":
            end = f"≈ {end}"
    return f"{start}–{end}" if start else f"–{end}"


def fmt_when_local(when: datetime, now: datetime, tz: tzinfo) -> str:
    """"i dag 14:05", "i morgon 14:05" eller "tis 15/9 14:05"."""
    local, today = when.astimezone(tz), now.astimezone(tz).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"i dag {clock}"
    if (local.date() - today).days == 1:
        return f"i morgon {clock}"
    return f"{fmt_day(when, tz)} {clock}"


def month_name(month: str) -> str:
    """"2026-10" → "Oktober"."""
    try:
        return MONTHS_SV[int(month[5:7]) - 1].capitalize()
    except (ValueError, IndexError):
        return month


def is_multi(trip: LogicalTrip) -> bool:
    return bool(set(trip.quality_flags or []) & MULTI_FLAGS)


def classify_v2(
    db: Session, trip: LogicalTrip, *, tz: tzinfo, hide_locations: bool, suggestion: str | None,
    remember_work: bool, remember_private: bool,
) -> dict[str, Any]:
    """"Ny resa" i självgående läge: Tjänst med ärende eller Privat i samma tryck."""
    origin, dest = place_labels(db, trip)
    day, distance = fmt_day(trip.ended_at or trip.sort_time, tz), fmt_mil(trip.distance_km)
    multi = is_multi(trip)
    tail = "Tjänst eller privat?" + (f" {MULTI_TEXT}" if multi else "")
    detail = f"{day} {fmt_trip_times(trip, tz)} · {origin} → {dest} · {distance}. {tail}"
    message = f"{day} · {distance}. {tail}" if hide_locations else detail
    acts: list[dict[str, Any]] = []
    if suggestion and not multi:
        acts.append({"key": f"{A.WORK_PICK}0", "title": _button("Tjänst: ", suggestion), "purpose": suggestion})
        if remember_work:
            acts.append({"key": f"{A.WORK_ALWAYS}0", "title": _button("Alltid: ", suggestion), "purpose": suggestion})
    acts.append({"key": A.WORK_TEXT, "title": "Tjänst – skriv ärende", "text_input": True, "placeholder": "Ärende"})
    acts.append({"key": A.PRIVATE, "title": "Privat"})
    if remember_private:
        acts.append({"key": A.PRIVATE_ALWAYS, "title": "Alltid privat här"})
    acts.append({"key": A.LATER, "title": "Senare"})
    return {
        "title": "Ny resa",
        "message": message,
        "actions": acts[: A.MAX_ACTIONS],
        "context": {"detail_message": detail, "route_label": f"{origin} → {dest}", "distance_km": trip.distance_km,
                    "self_driving": True, "multi": multi},
    }


def purpose_v2(
    db: Session, trip: LogicalTrip, *, tz: tzinfo, hide_locations: bool, candidates: list[str], remember: bool,
) -> dict[str, Any]:
    """"Vilket ärende?" för en tjänsteresa utan ärende."""
    _, dest = place_labels(db, trip)
    day, distance = fmt_day(trip.ended_at or trip.sort_time, tz), fmt_mil(trip.distance_km)
    detail = f"{dest}, {distance}"
    message = f"{day}, {distance}" if hide_locations else detail
    acts: list[dict[str, Any]] = []
    for i, text in enumerate(candidates[:2]):
        acts.append({"key": f"{A.PURPOSE_PICK}{i}", "title": _button("", text), "purpose": text})
    if candidates and remember:
        acts.append({"key": f"{A.PURPOSE_ALWAYS}0", "title": _button("Alltid: ", candidates[0]), "purpose": candidates[0]})
    acts.append({"key": A.PURPOSE_TEXT, "title": "Skriv ärende", "text_input": True, "placeholder": "Ärende"})
    acts.append({"key": A.PRIVATE, "title": "Privat"})
    acts.append({"key": A.LATER, "title": "Senare"})
    return {
        "title": "Vilket ärende?",
        "message": message,
        "actions": acts[: A.MAX_ACTIONS],
        "context": {"detail_message": detail, "destination_label": dest, "distance_km": trip.distance_km,
                    "self_driving": True},
    }


def company(db: Session, trip: LogicalTrip, *, tz: tzinfo, hide_locations: bool) -> dict[str, Any]:
    """"Vilket företag var det?" när kartan inte har något namn för en arbetsplats."""
    _, dest = place_labels(db, trip)
    when = trip.ended_at or trip.sort_time
    day, distance = fmt_day(when, tz), fmt_mil(trip.distance_km)
    clock = when.astimezone(tz).strftime("%H:%M") if when else ""
    detail = f"{day} {clock} · {dest} · {distance}".replace("  ", " ")
    message = f"{day} · {distance}" if hide_locations else detail
    return {
        "title": "Vilket företag var det?",
        "message": message,
        "actions": [
            {"key": A.COMPANY_TEXT, "title": "Skriv företagets namn", "text_input": True, "placeholder": "Företag"},
            {"key": A.PRIVATE, "title": "Privat"},
            {"key": A.LATER, "title": "Senare"},
        ],
        "context": {"detail_message": detail, "destination_label": dest, "distance_km": trip.distance_km,
                    "self_driving": True},
    }


def assumed(trip: LogicalTrip, *, tz: tzinfo, now: datetime, veto_until: datetime | None) -> dict[str, Any]:
    """"Antaget: Kundbesök Dagab" med vetotiden. Inga platser utöver ärendet."""
    purpose_text = (trip.purpose or "Kundbesök").strip()
    day, distance = fmt_day(trip.ended_at or trip.sort_time, tz), fmt_mil(trip.distance_km)
    when = fmt_when_local(veto_until, now, tz) if veto_until is not None else "snart"
    message = f"{day} {distance} · Räknas som tjänst {when} om du inte säger emot"
    return {
        "title": _button("Antaget: ", purpose_text),
        "message": message,
        "actions": [
            {"key": A.OK, "title": "Stämmer"},
            {"key": A.PRIVATE, "title": "Fel – privat"},
            {"key": A.WORK_TEXT, "title": "Annat ärende", "text_input": True, "placeholder": "Ärende"},
        ],
        "context": {"detail_message": message, "distance_km": trip.distance_km,
                    "veto_until": veto_until.isoformat() if veto_until else None},
    }


def alert(title: str, message: str) -> dict[str, Any]:
    return {
        "title": f"DriveLog: {title}",
        "message": message,
        "actions": [{"key": A.OPEN, "title": "Öppna", "uri": "/settings"}],
        "context": {"detail_message": message},
    }


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _amount(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{round(number):,}".replace(",", " ") + " kr"


def month_waiting(month: str, blockers: list[Any], notes: list[Any]) -> dict[str, Any]:
    """"Oktober väntar": vad som väntar, utan platser."""
    texts = [b.text for b in blockers if getattr(b, "code", "") != "month_not_over" and getattr(b, "text", "")]
    message = " · ".join(texts) if texts else "Underlaget är inte klart än."
    missing_start = _note_count(notes, "missing_start_time")
    if missing_start:
        message += f". {_count(missing_start, 'resa saknar', 'resor saknar')} starttid."
    return {
        "title": f"{month_name(month)} väntar",
        "message": message,
        "actions": [{"key": A.OPEN, "title": "Öppna", "uri": "/export"}],
        "context": {"detail_message": message, "month": month},
    }


def _note_count(notes: list[Any], code: str) -> int:
    for n in notes:
        if getattr(n, "code", None) == code:
            return int(getattr(n, "count", 0) or len(getattr(n, "trip_ids", []) or []))
    return 0


def month_ready(
    month: str, *, summary: dict[str, Any] | None, notes: list[Any], export_id: Any | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """"Oktober klart: 23 tjänsteresor, 412 mil, 10 300 kr, varav 3 antagna" med länken."""
    name = month_name(month)
    assumed_n = _note_count(notes, "assumed")
    missing_start = _note_count(notes, "missing_start_time")
    acts: list[dict[str, Any]] = []
    if export_id is None:
        title, message = f"{name}: inga tjänsteresor", "Inget underlag behövs för månaden."
        acts.append({"key": A.OPEN, "title": "Öppna", "uri": "/export"})
    else:
        summary = summary or {}
        trips_n = int(summary.get("trips") or 0)
        parts = [_count(trips_n, "tjänsteresa", "tjänsteresor")]
        if summary.get("mil") is not None:
            parts.append(f"{round(float(summary['mil'])):,}".replace(",", " ") + " mil")
        amount = _amount(summary.get("amount")) if summary.get("amount") is not None else None
        if amount:
            parts.append(amount)
        if previous is not None:
            title = f"{name} uppdaterad"
            d_trips = trips_n - int(previous.get("trips") or 0)
            d_km = round(float(summary.get("km") or 0) - float(previous.get("km") or 0))
            message = f"{d_trips:+d} {'resa' if abs(d_trips) == 1 else 'resor'}, {d_km:+d} km"
        else:
            title = f"{name} klart"
            message = ", ".join(parts)
        if assumed_n:
            message += f", varav {assumed_n} {'rad antagen' if assumed_n == 1 else 'rader antagna'} – se dem innan du skickar"
        message += "."
        acts.append({"key": A.OPEN, "title": "Öppna underlaget", "uri": f"{A.EXPORT_LINK_PREFIX}{export_id}"})
    if missing_start:
        message += f" {_count(missing_start, 'resa saknar', 'resor saknar')} starttid."
    if assumed_n:
        acts.append({"key": A.OPEN_SECOND, "title": "Se antagna", "uri": f"/m/month/{month}"})
    return {"title": title, "message": message, "actions": acts,
            "context": {"detail_message": message, "month": month}}
