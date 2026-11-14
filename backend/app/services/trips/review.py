"""Granskningsärenden (inkorgen i granskningscentret)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import ReviewItem
from app.models.enums import ReviewKind, ReviewState


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


def trip_key(kind: str, trip_id: uuid.UUID) -> str:
    return f"{kind}:{trip_id}"


def merge_key(first_raw_id: uuid.UUID, second_raw_id: uuid.UUID) -> str:
    return f"{ReviewKind.MERGE}:{first_raw_id}:{second_raw_id}"
