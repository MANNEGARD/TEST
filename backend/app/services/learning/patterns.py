"""Inlärda mönster per destination och rutt.

Mönstren räknas alltid om från grunden ur källdata:

* ``confirmed_*`` – resor som användaren själv godkänt eller korrigerat.
* ``imported_*``  – källmärkt klassning ur importerad körjournal. Eftersom importerna
                    ofta är ett urval av enbart tjänsteresor väger dessa lätt och kan
                    aldrig ensamma ge automatisk klassning.

AI:ns egna obekräftade förslag räknas aldrig in – en felaktig gissning kan därför
inte förstärka sig själv. Eftersom allt räknas om försvinner effekten av en
korrigering helt när den ångras.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LearningFeedback, LocationPattern, LogicalTrip, Vehicle
from app.models.enums import Category, LogicalState, PatternStatus, ReviewStatus
from app.services import settings as settings_svc
from app.services.classify.textnorm import normalize_purpose

MAX_PURPOSES = 12


def dest_key(dest_id: uuid.UUID) -> str:
    return f"dest:{dest_id}"


def route_key(origin_id: uuid.UUID, dest_id: uuid.UUID) -> str:
    return f"route:{origin_id}:{dest_id}"


@dataclass
class _Acc:
    pattern_type: str
    origin_id: uuid.UUID | None
    dest_id: uuid.UUID
    confirmed_work: int = 0
    confirmed_private: int = 0
    imported_work: int = 0
    imported_private: int = 0
    corrections_to_private: int = 0
    corrections_to_work: int = 0
    purposes: dict[str, dict[str, Any]] = field(default_factory=dict)
    weekdays: Counter = field(default_factory=Counter)
    hours: Counter = field(default_factory=Counter)
    last_seen: datetime | None = None


def rebuild(db: Session, user_id: uuid.UUID) -> int:
    """Räknar om alla mönster för användaren. Returnerar antal mönster."""
    tz: ZoneInfo = settings_svc.user_timezone(db, user_id)
    abbreviations = settings_svc.get_section(db, user_id, "abbreviations")["map"]
    vehicle_ids = [v for (v,) in db.execute(select(Vehicle.id).where(Vehicle.owner_user_id == user_id)).all()]
    accs: dict[str, _Acc] = {}

    def acc_for(key: str, ptype: str, origin: uuid.UUID | None, dest: uuid.UUID) -> _Acc:
        if key not in accs:
            accs[key] = _Acc(ptype, origin, dest)
        return accs[key]

    if vehicle_ids:
        trips = db.execute(
            select(LogicalTrip).where(
                LogicalTrip.vehicle_id.in_(vehicle_ids),
                LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.end_location_id.is_not(None),
                LogicalTrip.review_status.in_((ReviewStatus.APPROVED, ReviewStatus.IMPORTED)),
                LogicalTrip.category.in_((Category.WORK, Category.PRIVATE)),
            )
        ).scalars()
        for t in trips:
            if t.has_mixed_categories or t.undecided_km > 0.05:
                continue  # blandade resor säger inget entydigt om destinationen
            targets = [acc_for(dest_key(t.end_location_id), "destination", None, t.end_location_id)]
            if t.start_location_id is not None:
                targets.append(
                    acc_for(
                        route_key(t.start_location_id, t.end_location_id), "route", t.start_location_id, t.end_location_id
                    )
                )
            confirmed = t.review_status == ReviewStatus.APPROVED
            norm = normalize_purpose(t.purpose, abbreviations) if (t.purpose and t.purpose_confirmed) else None
            for a in targets:
                if confirmed and t.category == Category.WORK:
                    a.confirmed_work += 1
                elif confirmed:
                    a.confirmed_private += 1
                elif t.category == Category.WORK:
                    a.imported_work += 1
                else:
                    a.imported_private += 1
                when = t.started_at or t.ended_at
                if when is not None:
                    local = when.astimezone(tz)
                    a.weekdays[str(local.weekday())] += 1
                    a.hours[str(local.hour)] += 1
                    if a.last_seen is None or when > a.last_seen:
                        a.last_seen = when
                if norm is not None:
                    p = a.purposes.setdefault(
                        norm.key,
                        {
                            "key": norm.key,
                            "text": norm.display,
                            "type": norm.purpose_type,
                            "count": 0,
                            "user_count": 0,
                            "category": t.category,
                            "last_used": None,
                        },
                    )
                    p["count"] += 1
                    if confirmed:
                        p["user_count"] += 1
                    stamp = when.isoformat() if when else None
                    if stamp and (p["last_used"] is None or stamp > p["last_used"]):
                        p["last_used"] = stamp
                        p["text"] = norm.display  # senaste stavningen visas

        # Korrigeringar (ej ångrade) där användaren bytte kategori.
        fb_rows = db.execute(
            select(LearningFeedback).where(
                LearningFeedback.user_id == user_id,
                LearningFeedback.undone_at.is_(None),
                LearningFeedback.kind == "correction",
                LearningFeedback.destination_location_id.is_not(None),
            )
        ).scalars()
        for fb in fb_rows:
            if fb.new_category == fb.original_category or fb.original_category in (None, Category.UNDECIDED):
                continue
            keys = [dest_key(fb.destination_location_id)]
            if fb.origin_location_id is not None:
                keys.append(route_key(fb.origin_location_id, fb.destination_location_id))
            for k in keys:
                a = accs.get(k)
                if a is None:
                    continue
                if fb.new_category == Category.PRIVATE:
                    a.corrections_to_private += 1
                elif fb.new_category == Category.WORK:
                    a.corrections_to_work += 1

    existing = {
        p.pattern_key: p
        for p in db.execute(select(LocationPattern).where(LocationPattern.owner_user_id == user_id)).scalars()
    }
    for key, a in accs.items():
        pat = existing.get(key)
        if pat is None:
            pat = LocationPattern(
                owner_user_id=user_id,
                pattern_type=a.pattern_type,
                pattern_key=key,
                origin_location_id=a.origin_id,
                destination_location_id=a.dest_id,
                status=PatternStatus.SUGGESTED,
            )
            db.add(pat)
        pat.confirmed_work, pat.confirmed_private = a.confirmed_work, a.confirmed_private
        pat.imported_work, pat.imported_private = a.imported_work, a.imported_private
        pat.corrections_to_private, pat.corrections_to_work = a.corrections_to_private, a.corrections_to_work
        purposes = sorted(
            a.purposes.values(), key=lambda p: (-p["user_count"], -p["count"], p["last_used"] or ""), reverse=False
        )
        pat.purposes = purposes[:MAX_PURPOSES]
        pat.weekday_hist = dict(a.weekdays)
        pat.hour_hist = dict(a.hours)
        pat.last_seen_at = a.last_seen
    for key, pat in existing.items():
        if key not in accs:
            if pat.is_manual_rule:
                pat.confirmed_work = pat.confirmed_private = pat.imported_work = pat.imported_private = 0
                pat.corrections_to_private = pat.corrections_to_work = 0
                pat.purposes, pat.weekday_hist, pat.hour_hist = [], {}, {}
            else:
                db.delete(pat)
    db.flush()
    return len(accs)


def get_patterns(
    db: Session, user_id: uuid.UUID, origin_id: uuid.UUID | None, dest_id: uuid.UUID | None
) -> tuple[LocationPattern | None, LocationPattern | None]:
    """Returnerar (ruttmönster, destinationsmönster). Mönster som användaren avvisat används inte."""
    if dest_id is None:
        return None, None
    keys = [dest_key(dest_id)]
    if origin_id is not None:
        keys.append(route_key(origin_id, dest_id))
    rows = {
        p.pattern_key: p
        for p in db.execute(
            select(LocationPattern).where(
                LocationPattern.owner_user_id == user_id,
                LocationPattern.pattern_key.in_(keys),
                LocationPattern.status != PatternStatus.REJECTED,
            )
        ).scalars()
    }
    route = rows.get(route_key(origin_id, dest_id)) if origin_id is not None else None
    return route, rows.get(dest_key(dest_id))


def set_manual_rule(
    db: Session,
    user_id: uuid.UUID,
    *,
    origin_id: uuid.UUID | None,
    dest_id: uuid.UUID,
    category: str | None,
    purpose: str | None,
) -> LocationPattern:
    key = route_key(origin_id, dest_id) if origin_id else dest_key(dest_id)
    pat = db.execute(
        select(LocationPattern).where(LocationPattern.owner_user_id == user_id, LocationPattern.pattern_key == key)
    ).scalar_one_or_none()
    if pat is None:
        pat = LocationPattern(
            owner_user_id=user_id,
            pattern_type="route" if origin_id else "destination",
            pattern_key=key,
            origin_location_id=origin_id,
            destination_location_id=dest_id,
        )
        db.add(pat)
    pat.is_manual_rule = bool(category or purpose)
    pat.manual_category = category
    pat.manual_purpose = purpose
    pat.status = PatternStatus.APPROVED if pat.is_manual_rule else PatternStatus.SUGGESTED
    db.flush()
    return pat


def top_purposes(pattern: LocationPattern | None, limit: int = 5) -> list[dict[str, Any]]:
    if pattern is None:
        return []
    return list(pattern.purposes or [])[:limit]


def summarize_history(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Översikt för sidan Platser och AI-minne."""
    pats = list(db.execute(select(LocationPattern).where(LocationPattern.owner_user_id == user_id)).scalars())
    by_type: dict[str, int] = defaultdict(int)
    for p in pats:
        by_type[p.pattern_type] += 1
    return {
        "patterns": len(pats),
        "by_type": dict(by_type),
        "manual_rules": sum(1 for p in pats if p.is_manual_rule),
        "suggested": sum(1 for p in pats if p.status == PatternStatus.SUGGESTED),
    }
