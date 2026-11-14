"""Förslag ur importerad historik: platstyper och namn som användaren får godkänna.

Inget här ändrar hur resor klassas förrän användaren bekräftat platsen – förslagen
lagras bara på den obekräftade platsen (suggested_type/suggested_name).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnownLocation, LogicalTrip, Vehicle
from app.models.enums import LOCATION_TYPE_LABELS_SV, Category, LocationType, LogicalState, TripSource
from app.services import settings as settings_svc
from app.services.classify import textnorm
from app.services.places.normalize import fold

_COMPANY = re.compile(r"(?:kundbesök|besök|möte)\s+(?:på|hos)\s+(.+)$", re.IGNORECASE)


def _company_from_purpose(text: str) -> str | None:
    m = _COMPANY.search(text.strip().rstrip("."))
    if not m:
        return None
    name = m.group(1).strip()
    # Ta med ledande egennamn/förkortningar, stanna vid första vanliga ord.
    words = []
    for w in name.split():
        if w[:1].isupper() or w.isupper() or (words and w.lower() in ("och", "&", "ab")):
            words.append(w)
        else:
            break
    out = " ".join(words).strip(" ,")
    return out if 2 <= len(out) <= 60 else None


def refresh(db: Session, user_id: uuid.UUID) -> int:
    """Räknar om förslag för obekräftade platser. Returnerar antal platser med förslag."""
    tz: ZoneInfo = settings_svc.user_timezone(db, user_id)
    abbreviations = settings_svc.get_section(db, user_id, "abbreviations")["map"]
    vehicle_ids = [v for (v,) in db.execute(
        select(Vehicle.id).where(Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(False))
    ).all()]
    locs = {
        loc.id: loc
        for loc in db.execute(
            select(KnownLocation).where(KnownLocation.owner_user_id == user_id, KnownLocation.merged_into_id.is_(None))
        ).scalars()
    }
    if not vehicle_ids or not locs:
        return 0
    trips = list(
        db.execute(
            select(LogicalTrip).where(
                LogicalTrip.vehicle_id.in_(vehicle_ids), LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.source != TripSource.GAP_FILL, LogicalTrip.sort_time.is_not(None),
            ).order_by(LogicalTrip.vehicle_id, LogicalTrip.sort_time)
        ).scalars()
    )
    first_departure: Counter = Counter()
    last_arrival: Counter = Counter()
    overnight: Counter = Counter()
    days: set[tuple[uuid.UUID, Any]] = set()
    purposes_by_dest: dict[uuid.UUID, list[textnorm.NormalizedPurpose]] = defaultdict(list)
    arrivals: Counter = Counter()

    by_day: dict[tuple[uuid.UUID, Any], list[LogicalTrip]] = defaultdict(list)
    for t in trips:
        by_day[(t.vehicle_id, t.sort_time.astimezone(tz).date())].append(t)  # type: ignore[union-attr]
        if t.end_location_id:
            arrivals[t.end_location_id] += 1
            if t.purpose and t.purpose_confirmed and t.category == Category.WORK:
                norm = textnorm.normalize_purpose(t.purpose, abbreviations)
                if norm:
                    purposes_by_dest[t.end_location_id].append(norm)
    for key, day_trips in by_day.items():
        days.add(key)
        if day_trips[0].start_location_id:
            first_departure[day_trips[0].start_location_id] += 1
        if day_trips[-1].end_location_id:
            last_arrival[day_trips[-1].end_location_id] += 1
    for a, b in zip(trips, trips[1:], strict=False):
        if a.vehicle_id != b.vehicle_id or not a.end_location_id or a.end_location_id != b.start_location_id:
            continue
        if a.ended_at and b.started_at:
            da, dbb = a.ended_at.astimezone(tz).date(), b.started_at.astimezone(tz).date()
            if dbb > da and (dbb - da).days <= 2:
                overnight[a.end_location_id] += 1

    n_days = max(1, len(days))
    home_id = None
    if first_departure:
        cand, n = first_departure.most_common(1)[0]
        if n / n_days >= 0.3 and last_arrival.get(cand, 0) / n_days >= 0.3:
            home_id = cand

    count = 0
    for loc_id, loc in locs.items():
        if loc.confirmed:
            continue
        s_type = s_name = reason = None
        norms = purposes_by_dest.get(loc_id, [])
        types = Counter(n.purpose_type for n in norms)
        texts = " ".join(fold(n.display) for n in norms)
        if loc_id == home_id:
            s_type, s_name = LocationType.HOME, "Hemmet"
            reason = (f"Dagens första resa startar här {first_departure[loc_id]} av {n_days} dagar och "
                      f"dagens sista resa slutar här {last_arrival.get(loc_id, 0)} dagar.")
        elif overnight.get(loc_id, 0) >= 1 and ("hotell" in texts or "hotel" in texts):
            s_type = LocationType.HOTEL
            reason = f"Bilen har stått här över natten {overnight[loc_id]} gånger och kommentarerna nämner hotell."
        elif types.get("customer_visit", 0) >= 1 and types["customer_visit"] >= len(norms) / 2:
            s_type = LocationType.CUSTOMER
            companies = Counter(c for c in (_company_from_purpose(n.display) for n in norms) if c)
            if companies:
                s_name = companies.most_common(1)[0][0]
            reason = f"{types['customer_visit']} av {len(norms)} bekräftade syften hit är kundbesök."
        elif arrivals.get(loc_id, 0) >= 5 and ("kontoret" in texts or "kontor" in texts):
            s_type = LocationType.OFFICE
            reason = f"{arrivals[loc_id]} resor hit och kommentarerna nämner kontoret."
        if s_type is not None:
            count += 1
            reason = f"Förslag: {LOCATION_TYPE_LABELS_SV[s_type]}. {reason}"
        loc.suggested_type, loc.suggested_name, loc.suggestion_reason = s_type, s_name, reason
    db.flush()
    return count


def location_stats(db: Session, user_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
    """Besöksstatistik per plats: vanligaste klassning och bekräftade syften."""
    vehicle_ids = [v for (v,) in db.execute(select(Vehicle.id).where(Vehicle.owner_user_id == user_id)).all()]
    out: dict[uuid.UUID, dict[str, Any]] = defaultdict(
        lambda: {"arrivals": 0, "work": 0, "private": 0, "undecided": 0, "km": 0.0, "purposes": Counter()}
    )
    if not vehicle_ids:
        return {}
    for t in db.execute(
        select(LogicalTrip).where(
            LogicalTrip.vehicle_id.in_(vehicle_ids), LogicalTrip.state == LogicalState.ACTIVE,
            LogicalTrip.end_location_id.is_not(None),
        )
    ).scalars():
        s = out[t.end_location_id]
        s["arrivals"] += 1
        s[t.category if t.category in ("work", "private") else "undecided"] += 1
        s["km"] += t.distance_km
        if t.purpose and t.purpose_confirmed:
            s["purposes"][t.purpose.strip()] += 1
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for loc_id, s in out.items():
        decided = {k: s[k] for k in ("work", "private") if s[k]}
        result[loc_id] = {
            "arrivals": s["arrivals"],
            "work": s["work"],
            "private": s["private"],
            "undecided": s["undecided"],
            "km": round(s["km"], 1),
            "most_common_category": max(decided, key=decided.get) if decided else None,  # type: ignore[arg-type]
            "top_purposes": [{"text": text, "count": n} for text, n in s["purposes"].most_common(5)],
        }
    return result
