"""Granskningsärenden (inkorgen i granskningscentret)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import LogicalTrip, RawTrip, ReviewItem, TripSegment
from app.models.enums import LogicalState, ReviewKind, ReviewState


def upsert(
    db: Session,
    *,
    vehicle_id: uuid.UUID,
    kind: str,
    dedupe_key: str,
    title: str,
    detail: str | None = None,
    logical_trip_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
    reopen: bool = False,
    reopen_reason: str | None = None,
) -> tuple[ReviewItem, bool]:
    """Skapar ärendet om det saknas. Returnerar (ärende, skapades_nu).

    Ett ärende som användaren redan avgjort eller avfärdat öppnas inte igen om inte
    ``reopen`` anges – samma fråga ska inte ställas två gånger. Med ``reopen_reason``
    öppnas ärendet igen bara om systemet självt stängde det med just den orsaken
    (``resolution["reason"]``), aldrig om användaren har avgjort eller avfärdat det.
    """
    item = db.execute(
        select(ReviewItem).where(ReviewItem.vehicle_id == vehicle_id, ReviewItem.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if item is None:
        item = ReviewItem(
            vehicle_id=vehicle_id,
            kind=kind,
            dedupe_key=dedupe_key,
            title=title,
            detail=detail,
            logical_trip_id=logical_trip_id,
            data=data or {},
        )
        db.add(item)
        db.flush()
        return item, True
    if item.status in (ReviewState.OPEN, ReviewState.SNOOZED):
        item.title, item.detail = title, detail
        item.logical_trip_id = logical_trip_id
        item.data = data or item.data
    elif reopen or (
        reopen_reason is not None
        and item.status == ReviewState.RESOLVED
        and (item.resolution or {}).get("reason") == reopen_reason
    ):
        item.status = ReviewState.OPEN
        item.title, item.detail = title, detail
        item.logical_trip_id = logical_trip_id
        item.data = data or item.data
        item.resolved_at = None
        item.resolved_by = None
        item.resolution = {}
    db.flush()
    return item, False


def resolve(
    db: Session,
    item: ReviewItem,
    *,
    by: str,
    resolution: dict[str, Any] | None = None,
    dismissed: bool = False,
) -> ReviewItem:
    if item.status in (ReviewState.RESOLVED, ReviewState.DISMISSED):
        return item
    item.status = ReviewState.DISMISSED if dismissed else ReviewState.RESOLVED
    item.resolved_at = utcnow()
    item.resolved_by = by
    item.resolution = resolution or {}
    db.flush()
    return item


def resolve_by_key(db: Session, vehicle_id: uuid.UUID, dedupe_key: str, *, by: str, resolution: dict[str, Any]) -> None:
    item = db.execute(
        select(ReviewItem).where(ReviewItem.vehicle_id == vehicle_id, ReviewItem.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if item is not None:
        resolve(db, item, by=by, resolution=resolution)


def resolve_for_trip(
    db: Session,
    trip_id: uuid.UUID,
    kinds: tuple[str, ...],
    *,
    by: str,
    resolution: dict[str, Any] | None = None,
) -> int:
    items = (
        db.execute(
            select(ReviewItem).where(
                ReviewItem.logical_trip_id == trip_id,
                ReviewItem.kind.in_(kinds),
                ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)),
            )
        )
        .scalars()
        .all()
    )
    for item in items:
        resolve(db, item, by=by, resolution=resolution or {})
    return len(items)


def snooze(db: Session, item: ReviewItem, until: datetime) -> ReviewItem:
    item.status = ReviewState.SNOOZED
    item.snoozed_until = until
    db.flush()
    return item


def wake_snoozed(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    items = (
        db.execute(
            select(ReviewItem).where(ReviewItem.status == ReviewState.SNOOZED, ReviewItem.snoozed_until <= now)
        )
        .scalars()
        .all()
    )
    for item in items:
        item.status = ReviewState.OPEN
        item.snoozed_until = None
    db.flush()
    return len(items)


# Orsak när ett ärende stängs för att journalen bara omfattar tjänsteresor (business_only).
SCOPE_REASON = "journal_scope"
# Orsak när ett ärende stängs för att resan det gäller inte längre finns i journalen.
INACTIVE_REASON = "trip_inactive"

# Orsak när ett ärende stängs för att villkoret som skapade det inte längre gäller
# (t.ex. ett överlapp i mätarställningen som bara var avrundning).
NO_LONGER_REASON = "no_longer_applies"
OVERLAP_GONE_NOTE = (
    "Stängt automatiskt: resorna överlappar inte mer än avrundningen till hela kilometer "
    "(eller så har resorna ändrats)."
)


def close_stale(
    db: Session,
    vehicle_id: uuid.UUID,
    *,
    prefixes: tuple[str, ...],
    keep: set[str],
    data_type: str | None,
    note: str,
    when: Callable[[ReviewItem], bool] | None = None,
) -> int:
    """Stänger öppna (och uppskjutna) ärenden vars villkor inte längre är uppfyllt.

    Gäller ärenden med nyckel som börjar med något av ``prefixes`` och ``data["type"]`` lika
    med ``data_type`` (``None`` = vilken typ som helst), vars nyckel inte finns i ``keep`` (de som fortfarande gäller). Ärenden
    som ägaren har avgjort eller avfärdat rörs inte. Lösningen visar varför ärendet stängdes.
    """
    items = (
        db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle_id,
                ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)),
                or_(*(ReviewItem.dedupe_key.startswith(p, autoescape=True) for p in prefixes)),
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for item in items:
        if item.dedupe_key in keep or (when and not when(item)):
            continue
        if data_type is not None and (item.data or {}).get("type") != data_type:
            continue
        item.snoozed_until = None
        resolve(db, item, by="system", resolution={"auto": True, "reason": NO_LONGER_REASON, "note": note})
        closed += 1
    return closed


# Ärenden som gäller en viss journalresa och saknar mening när resan inte längre är aktiv.
_PER_TRIP_KINDS = (
    ReviewKind.CATEGORY,
    ReviewKind.PURPOSE,
    ReviewKind.TAX_UNCERTAIN,
    ReviewKind.MIXED_ALLOCATION,
    ReviewKind.MISSING_ODOMETER,
    ReviewKind.DATA_GAP,
)


def close_orphaned(db: Session, vehicle_id: uuid.UUID, *, by: str = "system") -> int:
    """Stänger öppna ärenden som gäller resor som inte längre finns i journalen.

    * Resebundna ärenden (kategori, syfte, mätarställning …) för resor som inte är aktiva.
    * Övriga ärenden för makulerade resor (ångrad import, borttagen manuell resa).
    * Sammanslagningsfrågor där någon av de fysiska resorna är makulerad eller saknar
      aktiv journalresa – frågan kan då inte längre besvaras.
    """
    items = (
        db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle_id, ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED))
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for item in items:
        stale = False
        if item.kind == ReviewKind.ANOMALY:
            # Avvikelser avgörs av stats.anomalies.close_noise via råresorna, som körs direkt
            # efter: en ångrad uppdelning makulerar delresan men råresan finns kvar i en aktiv resa.
            continue
        if item.kind == ReviewKind.MERGE:
            data = item.data or {}
            for key in ("first_raw_trip_id", "second_raw_trip_id"):
                try:
                    raw_id = uuid.UUID(str(data.get(key)))
                except ValueError:
                    continue
                raw = db.get(RawTrip, raw_id)
                active = db.execute(
                    select(LogicalTrip.id)
                    .join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
                    .where(TripSegment.raw_trip_id == raw_id, LogicalTrip.state == LogicalState.ACTIVE)
                    .limit(1)
                ).first()
                if raw is None or raw.voided_at is not None or active is None:
                    stale = True
        elif item.logical_trip_id is not None:
            trip = db.get(LogicalTrip, item.logical_trip_id)
            if trip is None or trip.state == LogicalState.VOIDED:
                stale = True
            elif trip.state != LogicalState.ACTIVE and item.kind in _PER_TRIP_KINDS:
                stale = True
        if stale:
            resolve(db, item, by=by, resolution={"auto": True, "reason": INACTIVE_REASON})
            closed += 1
    return closed


def trip_key(kind: str, trip_id: uuid.UUID) -> str:
    return f"{kind}:{trip_id}"


def merge_key(first_raw_id: uuid.UUID, second_raw_id: uuid.UUID) -> str:
    return f"{ReviewKind.MERGE}:{first_raw_id}:{second_raw_id}"
