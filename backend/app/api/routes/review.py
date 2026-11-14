"""Granskningscentret: ärenden och dataluckor."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, get_vehicle, owned_vehicle_ids
from app.api.errors import bad_request, not_found
from app.api.serializers import trip_out, trips_out
from app.db import utcnow
from app.models import DataGap, LogicalTrip, ReviewItem
from app.models.enums import REVIEW_KIND_LABELS_SV, GapStatus, LogicalState, ReviewKind, ReviewState
from app.schemas.common import GapOut, ReviewItemOut, TripOut
from app.services import audit
from app.services import settings as settings_svc
from app.services.trips import gaps, pipeline, review
from app.services.trips import segments as seg

router = APIRouter(tags=["Review"])


class SnoozeRequest(BaseModel):
    minutes: int = Field(default=24 * 60, ge=5, le=60 * 24 * 60)


class GapResolve(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _owned_item(db, principal, item_id: uuid.UUID) -> ReviewItem:
    item = db.get(ReviewItem, item_id)
    if item is None or item.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Ärendet")
    return item


def _owned_gap(db, principal, gap_id: uuid.UUID) -> DataGap:
    gap = db.get(DataGap, gap_id)
    if gap is None or gap.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Luckan")
    return gap


@router.get("/review", summary="Ärenden i granskningscentret")
def list_items(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    kind: list[str] | None = Query(default=None),
    status: str = Query(default="open", pattern="^(open|resolved|dismissed|snoozed|all)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    review.wake_snoozed(db)
    base = select(ReviewItem).where(ReviewItem.vehicle_id == vehicle.id)
    if status != "all":
        base = base.where(ReviewItem.status == status)
    counts = Counter(
        k for (k,) in db.execute(
            select(ReviewItem.kind).where(ReviewItem.vehicle_id == vehicle.id, ReviewItem.status == ReviewState.OPEN)
        ).all()
    )
    if kind:
        base = base.where(ReviewItem.kind.in_(kind))
    items = list(db.execute(base.order_by(ReviewItem.created_at.desc()).offset(offset).limit(limit)).scalars())
    trip_ids = {i.logical_trip_id for i in items if i.logical_trip_id}
    for i in items:  # sammanslagningsfrågor refererar två resor
        for key in ("first_trip_id", "second_trip_id"):
            if i.kind == ReviewKind.MERGE and i.data.get(key):
                trip_ids.add(uuid.UUID(i.data[key]))
    trips: dict[uuid.UUID, TripOut] = {}
    if trip_ids:
        rows = db.execute(select(LogicalTrip).where(LogicalTrip.id.in_(trip_ids))).scalars().all()
        trips = {t.id: t for t in trips_out(db, rows)}
    out = []
    for i in items:
        item = ReviewItemOut.model_validate(i)
        item.trip = trips.get(i.logical_trip_id) if i.logical_trip_id else None
        payload = item.model_dump(mode="json")
        payload["kind_label"] = REVIEW_KIND_LABELS_SV.get(ReviewKind(i.kind), i.kind) if i.kind in set(ReviewKind) else i.kind
        if i.kind == ReviewKind.MERGE:
            payload["first_trip"] = (trips[uuid.UUID(i.data["first_trip_id"])].model_dump(mode="json")
                                     if i.data.get("first_trip_id") and uuid.UUID(i.data["first_trip_id"]) in trips else None)
            payload["second_trip"] = (trips[uuid.UUID(i.data["second_trip_id"])].model_dump(mode="json")
                                      if i.data.get("second_trip_id") and uuid.UUID(i.data["second_trip_id"]) in trips else None)
        out.append(payload)
    db.commit()
    return {
        "items": out,
        "open_counts": {k: counts.get(k, 0) for k in ReviewKind},
        "open_total": sum(counts.values()),
        "kind_labels": {k: v for k, v in REVIEW_KIND_LABELS_SV.items()},
    }


@router.post("/review/{item_id}/dismiss", response_model=ReviewItemOut, summary="Avfärda ett ärende")
def dismiss(item_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> ReviewItem:
    item = _owned_item(db, principal, item_id)
    review.resolve(db, item, by=principal.actor.label, resolution={"dismissed": True}, dismissed=True)
    audit.record(db, principal.actor, "review.dismiss", "review_item", item.id, after={"kind": item.kind})
    db.commit()
    return item


@router.post("/review/{item_id}/resolve", response_model=ReviewItemOut, summary="Markera ett ärende som hanterat")
def resolve(item_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> ReviewItem:
    item = _owned_item(db, principal, item_id)
    if item.kind in (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION, ReviewKind.MERGE):
        raise bad_request("Det här ärendet stängs automatiskt när resan har åtgärdats.", "resolve_via_trip")
    review.resolve(db, item, by=principal.actor.label, resolution={"manual": True})
    db.commit()
    return item


@router.post("/review/{item_id}/snooze", response_model=ReviewItemOut, summary="Skjut upp ett ärende")
def snooze(item_id: uuid.UUID, body: SnoozeRequest, principal: PrincipalDep, db: DbDep) -> ReviewItem:
    item = _owned_item(db, principal, item_id)
    if item.status not in (ReviewState.OPEN, ReviewState.SNOOZED):
        raise bad_request("Ärendet är redan stängt.")
    review.snooze(db, item, utcnow() + timedelta(minutes=body.minutes))
    db.commit()
    return item


@router.post("/review/{item_id}/reopen", response_model=ReviewItemOut, summary="Öppna ett avfärdat ärende igen")
def reopen(item_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> ReviewItem:
    item = _owned_item(db, principal, item_id)
    item.status, item.resolved_at, item.resolved_by, item.resolution, item.snoozed_until = ReviewState.OPEN, None, None, {}, None
    db.commit()
    return item


@router.post("/review/refresh", summary="Kör regler, klassificering och ärendegenerering på nytt")
def refresh(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    result = pipeline.after_trips_changed(
        db, user_id=principal.user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False, origin="manual"
    )
    db.commit()
    return {k: v for k, v in result.items() if isinstance(v, (int, float, str))}


@router.get("/gaps", summary="Kilometer i mätarställningen mellan de journalförda resorna")
def list_gaps(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None,
              only_open: bool = False) -> dict[str, Any]:
    """Luckor i mätarställningen.

    ``expected`` är sant när endast tjänsteresor redovisas (``scope`` = ``business_only``):
    luckorna är då privat körning som inte ska åtgärdas, och listan kan döljas.
    """
    vehicle = get_vehicle(db, principal, vehicle_id)
    scope = settings_svc.journal_scope(db, principal.user.id)
    expected = scope == settings_svc.SCOPE_BUSINESS_ONLY
    rows = gaps.list_gaps(db, vehicle.id, only_open=only_open)
    open_rows = [g for g in rows if g.status == GapStatus.OPEN]
    return {
        "scope": scope,
        "expected": expected,
        "label": "Övrig körning (redovisas inte)" if expected else "Dataluckor",
        "note": (
            "Körning mellan de redovisade tjänsteresorna. Den är privat och behöver inte åtgärdas."
            if expected
            else "Kilometer i mätarställningen som saknar journalpost."
        ),
        "items": [GapOut.model_validate(g).model_dump(mode="json") for g in rows],
        "open_count": len(open_rows),
        "open_km": round(sum(g.km for g in open_rows), 1),
        "resolved_private_km": round(sum(g.km for g in rows if g.status == GapStatus.RESOLVED_PRIVATE), 1),
    }


@router.post("/gaps/{gap_id}/resolve-private", response_model=TripOut,
             summary="Intyga att luckan var privat körning (skapar sammanfattande privatpost)")
def resolve_gap_private(gap_id: uuid.UUID, body: GapResolve, principal: PrincipalDep, db: DbDep) -> TripOut:
    gap = _owned_gap(db, principal, gap_id)
    trip = gaps.resolve_as_private(db, gap, actor=principal.actor, note=body.note)
    gaps.recompute(db, gap.vehicle_id, float(settings_svc.get_section(db, principal.user.id, "merge")["odometer_tolerance_km"]))
    db.commit()
    return trip_out(db, seg.load_trip(db, trip.id) or trip)


@router.post("/gaps/{gap_id}/ignore", response_model=GapOut, summary="Lämna luckan oredovisad (visas som oavstämd i exporten)")
def ignore_gap(gap_id: uuid.UUID, body: GapResolve, principal: PrincipalDep, db: DbDep) -> DataGap:
    gap = _owned_gap(db, principal, gap_id)
    gaps.ignore(db, gap, actor=principal.actor, note=body.note)
    db.commit()
    return gap


@router.post("/gaps/{gap_id}/reopen", response_model=GapOut, summary="Ångra hanteringen av en lucka")
def reopen_gap(gap_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> DataGap:
    gap = _owned_gap(db, principal, gap_id)
    gaps.reopen(db, gap, actor=principal.actor)
    gaps.recompute(db, gap.vehicle_id, float(settings_svc.get_section(db, principal.user.id, "merge")["odometer_tolerance_km"]))
    db.commit()
    return gap


__all__ = ["router", "LogicalState", "datetime"]
