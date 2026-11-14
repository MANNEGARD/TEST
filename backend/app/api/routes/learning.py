"""Inlärning: korrigeringar, mönster och manuella regler."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, owned_vehicle_ids
from app.api.errors import bad_request, not_found
from app.api.serializers import trip_out
from app.models import KnownLocation, LearningFeedback, LocationPattern, LogicalTrip
from app.models.enums import PatternStatus
from app.schemas.common import FeedbackOut, PatternOut, TripOut
from app.services import audit
from app.services.learning import feedback, patterns
from app.services.trips import segments as seg

router = APIRouter(tags=["Learning feedback"])


class PatternUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(suggested|approved|rejected)$")
    manual_category: str | None = Field(default=None, pattern="^(work|private)$")
    manual_purpose: str | None = Field(default=None, max_length=500)
    clear_manual_rule: bool = False


class RuleIn(BaseModel):
    origin_location_id: uuid.UUID | None = None
    destination_location_id: uuid.UUID
    category: str | None = Field(default=None, pattern="^(work|private)$")
    purpose: str | None = Field(default=None, max_length=500)


def _pattern_out(db, pats: list[LocationPattern]) -> list[PatternOut]:
    ids = {i for p in pats for i in (p.origin_location_id, p.destination_location_id) if i}
    names = {}
    if ids:
        names = {loc.id: loc.name for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(ids))).scalars()}
    out = []
    for p in pats:
        item = PatternOut.model_validate(p)
        item.origin_name = names.get(p.origin_location_id) if p.origin_location_id else None
        item.destination_name = names.get(p.destination_location_id)
        out.append(item)
    return out


@router.get("/learning/feedback", response_model=list[FeedbackOut], summary="Sparade korrigeringar och bekräftelser")
def list_feedback(principal: PrincipalDep, db: DbDep, limit: int = Query(default=100, ge=1, le=500),
                  only_corrections: bool = False) -> list[LearningFeedback]:
    q = select(LearningFeedback).where(LearningFeedback.user_id == principal.user.id)
    if only_corrections:
        q = q.where(LearningFeedback.kind == "correction")
    return list(db.execute(q.order_by(LearningFeedback.created_at.desc()).limit(limit)).scalars())


@router.post("/learning/feedback/{feedback_id}/undo", response_model=TripOut, summary="Ångra en korrigering")
def undo_feedback(feedback_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> TripOut:
    fb = db.get(LearningFeedback, feedback_id)
    if fb is None or fb.user_id != principal.user.id:
        raise not_found("Ändringen")
    trip = feedback.undo(db, fb, actor=principal.actor)
    db.commit()
    return trip_out(db, seg.load_trip(db, trip.id) or trip)


@router.get("/learning/patterns", response_model=list[PatternOut], summary="Inlärda mönster och manuella regler")
def list_patterns(
    principal: PrincipalDep, db: DbDep,
    pattern_type: str | None = Query(default=None, pattern="^(destination|route)$"),
    status: str | None = Query(default=None, pattern="^(suggested|approved|rejected)$"),
    location_id: uuid.UUID | None = None,
    min_count: int = Query(default=1, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[PatternOut]:
    q = select(LocationPattern).where(LocationPattern.owner_user_id == principal.user.id)
    if pattern_type:
        q = q.where(LocationPattern.pattern_type == pattern_type)
    if status:
        q = q.where(LocationPattern.status == status)
    if location_id:
        q = q.where((LocationPattern.destination_location_id == location_id) | (LocationPattern.origin_location_id == location_id))
    pats = [
        p for p in db.execute(q).scalars()
        if p.is_manual_rule or (p.confirmed_work + p.confirmed_private + p.imported_work + p.imported_private) >= min_count
    ]
    pats.sort(key=lambda p: -(p.confirmed_work + p.confirmed_private + p.imported_work + p.imported_private))
    return _pattern_out(db, pats[:limit])


@router.patch("/learning/patterns/{pattern_id}", response_model=PatternOut, summary="Godkänn/avvisa mönster eller sätt manuell regel")
def update_pattern(pattern_id: uuid.UUID, body: PatternUpdate, principal: WebPrincipalDep, db: DbDep) -> PatternOut:
    pat = db.get(LocationPattern, pattern_id)
    if pat is None or pat.owner_user_id != principal.user.id:
        raise not_found("Mönstret")
    if body.status:
        pat.status = body.status
    if body.clear_manual_rule:
        pat.is_manual_rule, pat.manual_category, pat.manual_purpose = False, None, None
    elif body.manual_category is not None or body.manual_purpose is not None:
        pat.is_manual_rule = True
        pat.manual_category = body.manual_category or pat.manual_category
        pat.manual_purpose = body.manual_purpose if body.manual_purpose is not None else pat.manual_purpose
        pat.status = PatternStatus.APPROVED
    audit.record(db, principal.actor, "pattern.update", "location_pattern", pat.id, after=body.model_dump(exclude_unset=True))
    db.flush()
    _reevaluate(db, principal)
    db.commit()
    return _pattern_out(db, [pat])[0]


@router.post("/learning/rules", response_model=PatternOut, status_code=201, summary="Skapa manuell regel för plats eller rutt")
def create_rule(body: RuleIn, principal: WebPrincipalDep, db: DbDep) -> PatternOut:
    if not body.category and not body.purpose:
        raise bad_request("Ange kategori och/eller syfte för regeln.")
    for loc_id in (body.origin_location_id, body.destination_location_id):
        if loc_id is not None:
            loc = db.get(KnownLocation, loc_id)
            if loc is None or loc.owner_user_id != principal.user.id:
                raise not_found("Platsen")
    pat = patterns.set_manual_rule(
        db, principal.user.id, origin_id=body.origin_location_id, dest_id=body.destination_location_id,
        category=body.category, purpose=body.purpose,
    )
    audit.record(db, principal.actor, "pattern.manual_rule", "location_pattern", pat.id, after=body.model_dump(mode="json"))
    _reevaluate(db, principal)
    db.commit()
    return _pattern_out(db, [pat])[0]


@router.delete("/learning/patterns/{pattern_id}/rule", status_code=204, summary="Ta bort manuell regel")
def delete_rule(pattern_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    pat = db.get(LocationPattern, pattern_id)
    if pat is None or pat.owner_user_id != principal.user.id:
        raise not_found("Mönstret")
    pat.is_manual_rule, pat.manual_category, pat.manual_purpose, pat.status = False, None, None, PatternStatus.SUGGESTED
    patterns.rebuild(db, principal.user.id)
    _reevaluate(db, principal)
    db.commit()
    return Response(status_code=204)


@router.post("/learning/rebuild", summary="Räkna om alla mönster från bekräftad historik")
def rebuild(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    count = patterns.rebuild(db, principal.user.id)
    _reevaluate(db, principal)
    db.commit()
    return {"patterns": count, **patterns.summarize_history(db, principal.user.id)}


def _reevaluate(db, principal) -> None:
    from app.models import Vehicle

    for vid in owned_vehicle_ids(db, principal.user.id):
        vehicle = db.get(Vehicle, vid)
        if vehicle is not None:
            feedback.after_decisions(db, user_id=principal.user.id, vehicle_id=vehicle.id, actor=principal.actor)


__all__ = ["router", "LogicalTrip"]
