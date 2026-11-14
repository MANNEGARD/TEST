"""Notistexter på svenska.

Sekretess: gatuadresser förekommer aldrig. Hemmet visas som "Hemmet" och privata
platser som "Privat plats". När ``hide_locations_on_lock_screen`` är på innehåller
låsskärmstexten inga platsnamn alls – detaljerna visas först i den inloggade appen.
"""

from __future__ import annotations

import re
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
