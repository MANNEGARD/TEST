"""Sammanslagning, uppdelning och fördelning av delsträckor."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DbDep, PrincipalDep, get_owned_trip, get_vehicle, owned_vehicle_ids
from app.api.errors import bad_request, not_found
from app.api.serializers import trip_out, trips_out
from app.models import LogicalTrip, ReviewItem, TripMerge, TripSegment
from app.models.enums import ReviewKind, ReviewState
from app.schemas.common import TripOut
from app.services import audit
from app.services import settings as settings_svc
from app.services.trips import merge, pipeline
from app.services.trips import segments as seg

router = APIRouter(tags=["Trip merging"])


class MergeRequest(BaseModel):
    trip_ids: list[uuid.UUID] = Field(min_length=2, max_length=50)
    acknowledge_gap: bool = False


class SplitRequest(BaseModel):
    after_index: int = Field(ge=0, description="Dela efter delresa med detta index (0-baserat)")


class Part(BaseModel):
    distance_km: float = Field(gt=0)
    category: str = Field(pattern="^(work|private|undecided)$")
    note: str | None = Field(default=None, max_length=300)


class AllocateRequest(BaseModel):
    raw_trip_id: uuid.UUID
    parts: list[Part] = Field(min_length=1, max_length=10)


class MergeAnswer(BaseModel):
    accept: bool


def _tolerance(db, user_id: uuid.UUID) -> float:
    return float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])


def _sync(db, *trips: LogicalTrip) -> None:
    for t in trips:
        pipeline.sync_trip_review_items(db, t)


@router.post("/trips/merge", response_model=TripOut, summary="Slå samman valda resor manuellt")
def merge_trips(body: MergeRequest, principal: PrincipalDep, db: DbDep) -> TripOut:
    owned = set(owned_vehicle_ids(db, principal.user.id))
    trips = list(
        db.execute(
            select(LogicalTrip).where(LogicalTrip.id.in_(body.trip_ids), LogicalTrip.vehicle_id.in_(owned))
            .options(selectinload(LogicalTrip.segments).selectinload(TripSegment.raw_trip))
        ).scalars()
    )
    if len(trips) != len(set(body.trip_ids)):
        raise not_found("En eller flera resor")
    merged = merge.merge_trips(
        db, trips, trigger="manual", actor=principal.actor, acknowledge_gap=body.acknowledge_gap,
        odometer_tolerance=_tolerance(db, principal.user.id),
    )
    _sync(db, merged)
    db.commit()
    return trip_out(db, get_owned_trip(db, principal, merged.id))


@router.post("/trips/{trip_id}/unmerge", response_model=list[TripOut], summary="Ångra en sammanslagning")
def unmerge(trip_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> list[TripOut]:
    trip = get_owned_trip(db, principal, trip_id)
    restored = merge.dissolve(db, trip, actor=principal.actor)
    restored = [seg.load_trip(db, t.id) or t for t in restored]
    _sync(db, *restored)
    db.commit()
    return trips_out(db, restored)


@router.post("/trips/{trip_id}/split", response_model=list[TripOut], summary="Dela en sammanslagen resa i två")
def split(trip_id: uuid.UUID, body: SplitRequest, principal: PrincipalDep, db: DbDep) -> list[TripOut]:
    trip = get_owned_trip(db, principal, trip_id)
    left, right = merge.split_trip(db, trip, after_index=body.after_index, actor=principal.actor)
    left, right = seg.load_trip(db, left.id) or left, seg.load_trip(db, right.id) or right
    _sync(db, left, right)
    db.commit()
    return trips_out(db, [left, right])


def _allocation(trip: LogicalTrip, raw_trip_id: uuid.UUID) -> list[dict[str, Any]]:
    """Delresans nuvarande delsträckor med effektiv kategori (för ändringshistoriken)."""
    return [
        {"distance_km": s.distance_km, "category": seg.segment_category(trip, s), "note": s.note}
        for s in sorted(trip.segments, key=lambda s: s.part)
        if s.raw_trip_id == raw_trip_id
    ]


@router.post("/trips/{trip_id}/allocate", response_model=TripOut,
             summary="Fördela en delresa på privat och tjänst (delsträckor)")
def allocate(trip_id: uuid.UUID, body: AllocateRequest, principal: PrincipalDep, db: DbDep) -> TripOut:
    trip = get_owned_trip(db, principal, trip_id)
    before = _allocation(trip, body.raw_trip_id)
    seg.allocate_raw_trip(db, trip, body.raw_trip_id, [p.model_dump() for p in body.parts], principal.actor.label)
    audit.record(db, principal.actor, "trip.allocate", "logical_trip", trip.id,
                 before={"raw_trip_id": str(body.raw_trip_id), "parts": before},
                 after={"raw_trip_id": str(body.raw_trip_id), "parts": [p.model_dump() for p in body.parts]})
    _sync(db, trip)
    db.commit()
    return trip_out(db, get_owned_trip(db, principal, trip_id))


@router.delete("/trips/{trip_id}/allocate/{raw_trip_id}", response_model=TripOut, summary="Ta bort fördelningen av en delresa")
def clear_allocation(trip_id: uuid.UUID, raw_trip_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> TripOut:
    trip = get_owned_trip(db, principal, trip_id)
    before = _allocation(trip, raw_trip_id)
    seg.clear_allocation(db, trip, raw_trip_id, principal.actor.label)
    audit.record(db, principal.actor, "trip.allocation_cleared", "logical_trip", trip.id,
                 before={"raw_trip_id": str(raw_trip_id), "parts": before},
                 after={"raw_trip_id": str(raw_trip_id), "parts": _allocation(trip, raw_trip_id)})
    _sync(db, trip)
    db.commit()
    return trip_out(db, get_owned_trip(db, principal, trip_id))


@router.get("/merges", summary="Logg över sammanslagningar och uppdelningar")
def list_operations(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None,
                    limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    return [
        {"id": str(op.id), "operation": op.operation, "trigger": op.trigger, "result_trip_ids": op.result_trip_ids,
         "source_trip_ids": op.source_trip_ids, "details": op.details, "created_by": op.created_by,
         "created_at": op.created_at, "undone_at": op.undone_at}
        for op in db.execute(
            select(TripMerge).where(TripMerge.vehicle_id == vehicle.id).order_by(TripMerge.created_at.desc()).limit(limit)
        ).scalars()
    ]


@router.post("/merges/{operation_id}/undo", response_model=list[TripOut], summary="Ångra en loggad åtgärd")
def undo_operation(operation_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> list[TripOut]:
    op = db.get(TripMerge, operation_id)
    if op is None or op.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Åtgärden")
    result = merge.undo_operation(db, op, actor=principal.actor)
    result = [seg.load_trip(db, t.id) or t for t in result]
    _sync(db, *result)
    db.commit()
    return trips_out(db, result)


@router.post("/merge-questions/{item_id}/answer", summary="Besvara en fråga om sammanslagning (regel B)")
def answer_question(item_id: uuid.UUID, body: MergeAnswer, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    item = db.get(ReviewItem, item_id)
    if item is None or item.kind != ReviewKind.MERGE or item.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Frågan")
    if item.status not in (ReviewState.OPEN, ReviewState.SNOOZED):
        raise bad_request("Frågan är redan besvarad.", "already_answered")
    try:
        first_id, second_id = (uuid.UUID(str(item.data[k])) for k in ("first_raw_trip_id", "second_raw_trip_id"))
    except (KeyError, TypeError, ValueError):
        raise bad_request("Frågan saknar uppgift om vilka resor den gäller.", "invalid_question") from None
    merged = merge.answer_question(
        db, vehicle_id=item.vehicle_id,
        first_raw_trip_id=first_id,
        second_raw_trip_id=second_id,
        accept=body.accept, actor=principal.actor, odometer_tolerance=_tolerance(db, principal.user.id),
    )
    if merged is not None:
        merged = seg.load_trip(db, merged.id) or merged
        _sync(db, merged)
    try:
        from app.services.ha import notifier

        cancel = getattr(notifier, "cancel_for_review_item", None)
        if cancel is not None:
            cancel(db, item.id)
    except Exception:  # noqa: S110
        pass
    db.commit()
    return {"merged": merged is not None, "trip": trip_out(db, merged).model_dump(mode="json") if merged else None}
