"""Körjournalen: lista, detaljer, ändringar, massredigering och manuella resor."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, get_owned_trip, get_vehicle, owned_vehicle_ids
from app.api.errors import bad_request, not_found
from app.api.serializers import trip_out, trips_out
from app.jobs import enqueue
from app.models import (
    AuditEvent,
    BusinessJourney,
    CalendarEvent,
    CalendarMatch,
    Driver,
    ImportedFile,
    ImportRow,
    KnownLocation,
    LogicalTrip,
    RawTrip,
    TripClassification,
    TripEvidence,
    TripMerge,
    TripPurpose,
    TripSegment,
    VehicleObservation,
)
from app.models.enums import (
    Category,
    DecisionSource,
    DistanceQuality,
    LogicalState,
    PositionSource,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.schemas.common import AuditOut, FeedbackOut, LocationBrief, TripListOut, TripOut
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import engine as classify_engine
from app.services.classify import purpose as purpose_mod
from app.services.classify import signals as sig
from app.services.learning import feedback
from app.services.places import registry as places
from app.services.trips import pipeline
from app.services.trips import segments as seg

router = APIRouter(tags=["Trips"])

SORT_FIELDS = {
    "date": LogicalTrip.sort_time,
    "distance": LogicalTrip.distance_km,
    "category": LogicalTrip.category,
    "status": LogicalTrip.review_status,
    "start": LogicalTrip.start_address,
    "end": LogicalTrip.end_address,
    "odometer": LogicalTrip.odometer_start,
    "purpose": LogicalTrip.purpose,
}


class TripUpdate(BaseModel):
    category: str | None = Field(default=None, pattern="^(work|private|undecided)$")
    purpose: str | None = Field(default=None, max_length=500)
    subcategory: str | None = Field(default=None, max_length=40)
    contacts: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=2000)
    approve: bool = False
    is_favorite: bool | None = None
    driver_id: uuid.UUID | None = None


class BulkUpdate(BaseModel):
    trip_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, pattern="^(work|private|undecided)$")
    purpose: str | None = Field(default=None, max_length=500)
    approve: bool = False
    is_favorite: bool | None = None


class ManualTrip(BaseModel):
    vehicle_id: uuid.UUID | None = None
    started_at: datetime
    ended_at: datetime
    start_address: str = Field(min_length=1, max_length=300)
    end_address: str = Field(min_length=1, max_length=300)
    distance_km: float = Field(gt=0, le=5000)
    odometer_start: float | None = Field(default=None, ge=0)
    odometer_end: float | None = Field(default=None, ge=0)
    category: str = Field(default="undecided", pattern="^(work|private|undecided)$")
    purpose: str | None = Field(default=None, max_length=500)


def _local_range(tz: ZoneInfo, start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    lo = datetime.combine(start, time.min, tz) if start else None
    hi = datetime.combine(end + timedelta(days=1), time.min, tz) if end else None
    return lo, hi


def filtered_query(
    db, principal, *, vehicle_id, start, end, category, review_status, source, q, needs_review, favorite,
    journey_id, location_id, min_km, max_km,
):
    vehicle = get_vehicle(db, principal, vehicle_id)
    tz = settings_svc.user_timezone(db, principal.user.id)
    lo, hi = _local_range(tz, start, end)
    stmt = select(LogicalTrip).where(LogicalTrip.vehicle_id == vehicle.id, LogicalTrip.state == LogicalState.ACTIVE)
    if lo is not None:
        stmt = stmt.where(LogicalTrip.sort_time >= lo)
    if hi is not None:
        stmt = stmt.where(LogicalTrip.sort_time < hi)
    if category:
        stmt = stmt.where(LogicalTrip.category.in_(category))
    if review_status:
        stmt = stmt.where(LogicalTrip.review_status.in_(review_status))
    if source:
        stmt = stmt.where(LogicalTrip.source.in_(source))
    if needs_review is True:
        stmt = stmt.where(
            LogicalTrip.review_status.in_((ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED, ReviewStatus.AUTO))
        )
    elif needs_review is False:
        stmt = stmt.where(LogicalTrip.review_status.in_((ReviewStatus.APPROVED, ReviewStatus.IMPORTED)))
    if favorite is not None:
        stmt = stmt.where(LogicalTrip.is_favorite.is_(favorite))
    if journey_id is not None:
        stmt = stmt.where(LogicalTrip.journey_id == journey_id)
    if location_id is not None:
        stmt = stmt.where(or_(LogicalTrip.start_location_id == location_id, LogicalTrip.end_location_id == location_id))
    if min_km is not None:
        stmt = stmt.where(LogicalTrip.distance_km >= min_km)
    if max_km is not None:
        stmt = stmt.where(LogicalTrip.distance_km <= max_km)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(LogicalTrip.start_address).like(like),
                func.lower(LogicalTrip.end_address).like(like),
                func.lower(LogicalTrip.purpose).like(like),
                func.lower(LogicalTrip.contacts).like(like),
                func.lower(LogicalTrip.notes).like(like),
            )
        )
    return stmt, vehicle


@router.get("/trips", response_model=TripListOut, summary="Körjournal med filter, sökning och sortering")
def list_trips(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    start: date | None = Query(default=None, alias="from", description="Lokalt datum (inklusive)"),
    end: date | None = Query(default=None, alias="to", description="Lokalt datum (inklusive)"),
    category: list[str] | None = Query(default=None),
    review_status: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    needs_review: bool | None = None,
    favorite: bool | None = None,
    journey_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    min_km: float | None = Query(default=None, ge=0),
    max_km: float | None = Query(default=None, ge=0),
    sort: str = Query(default="date", pattern="^(date|distance|category|status|start|end|odometer|purpose)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> TripListOut:
    stmt, _ = filtered_query(
        db, principal, vehicle_id=vehicle_id, start=start, end=end, category=category, review_status=review_status,
        source=source, q=q, needs_review=needs_review, favorite=favorite, journey_id=journey_id,
        location_id=location_id, min_km=min_km, max_km=max_km,
    )
    sub = stmt.subquery()
    total, km, work, private, undecided = db.execute(
        select(
            func.count(), func.coalesce(func.sum(sub.c.distance_km), 0), func.coalesce(func.sum(sub.c.work_km), 0),
            func.coalesce(func.sum(sub.c.private_km), 0), func.coalesce(func.sum(sub.c.undecided_km), 0),
        ).select_from(sub)
    ).one()
    col = SORT_FIELDS[sort]
    ordering = col.asc().nulls_last() if order == "asc" else col.desc().nulls_last()
    rows = list(
        db.execute(
            stmt.order_by(ordering, LogicalTrip.id).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
    )
    return TripListOut(
        items=trips_out(db, rows),
        total=total,
        page=page,
        page_size=page_size,
        totals={
            "distance_km": round(float(km), 1),
            "work_km": round(float(work), 1),
            "private_km": round(float(private), 1),
            "undecided_km": round(float(undecided), 1),
        },
    )


def _raw_out(db, raw: RawTrip) -> dict[str, Any]:
    origin: dict[str, Any] | None = None
    if raw.import_row_id:
        row = db.get(ImportRow, raw.import_row_id)
        if row is not None:
            f = db.get(ImportedFile, row.file_id)
            origin = {"file": f.filename if f else None, "row_number": row.row_number, "format": f.format if f else None,
                      "batch_id": str(f.batch_id) if f else None}
    return {
        "id": str(raw.id),
        "source": raw.source,
        "started_at": raw.started_at,
        "ended_at": raw.ended_at,
        "start_time_quality": raw.start_time_quality,
        "end_time_quality": raw.end_time_quality,
        "start_earliest": raw.start_earliest,
        "start_latest": raw.start_latest,
        "odometer_start": raw.odometer_start,
        "odometer_end": raw.odometer_end,
        "odometer_source": raw.odometer_source,
        "distance_km": raw.distance_km,
        "distance_quality": raw.distance_quality,
        "duration_seconds": raw.duration_seconds,
        "start_address": raw.start_address,
        "end_address": raw.end_address,
        "start_position": {"lat": raw.start_lat, "lon": raw.start_lon} if raw.start_lat is not None else None,
        "end_position": {"lat": raw.end_lat, "lon": raw.end_lon} if raw.end_lat is not None else None,
        "position_source": raw.position_source,
        "fuel_liters": raw.fuel_liters,
        "fuel_quality": raw.fuel_quality,
        "energy_kwh": raw.energy_kwh,
        "energy_quality": raw.energy_quality,
        "regen_kwh": raw.regen_kwh,
        "original_category": raw.original_category,
        "original_title": raw.original_title,
        "original_notes": raw.original_notes,
        "is_complete": raw.is_complete,
        "possible_missed_trips": raw.possible_missed_trips,
        "quality_flags": raw.quality_flags or [],
        "import_origin": origin,
    }


@router.get("/trips/{trip_id}", summary="Resedetaljer med underlag, originalresor och historik")
def trip_detail(trip_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    trip = get_owned_trip(db, principal, trip_id)
    user_id = principal.user.id
    raws = seg.ordered_raws(trip)

    segments = [
        {"id": str(s.id), "raw_trip_id": str(s.raw_trip_id), "seq": s.seq, "part": s.part,
         "distance_km": s.distance_km, "category": seg.segment_category(trip, s),
         "category_override": s.category_override, "note": s.note}
        for s in sorted(trip.segments, key=lambda s: (s.seq, s.part))
    ]
    classification = db.execute(
        select(TripClassification).where(TripClassification.logical_trip_id == trip.id)
        .order_by(TripClassification.created_at.desc())
    ).scalars().all()
    current = next((c for c in classification if c.is_current), None)
    proposal = next((c for c in classification if c.source in (DecisionSource.RULE, DecisionSource.AI)), None)
    evidence = db.execute(
        select(TripEvidence).where(TripEvidence.logical_trip_id == trip.id).order_by(TripEvidence.weight.desc())
    ).scalars().all()
    purposes = db.execute(
        select(TripPurpose).where(TripPurpose.logical_trip_id == trip.id).order_by(TripPurpose.created_at.desc()).limit(10)
    ).scalars().all()
    matches = db.execute(
        select(CalendarMatch, CalendarEvent).join(CalendarEvent, CalendarEvent.id == CalendarMatch.calendar_event_id)
        .where(CalendarMatch.logical_trip_id == trip.id).order_by(CalendarMatch.score.desc())
    ).all()

    # Faktiska positionsobservationer (inte en körd rutt).
    obs_ids = [uuid.UUID(i) for r in raws for i in (r.observation_ids or [])]
    observations: list[dict[str, Any]] = []
    if obs_ids:
        for o in db.execute(
            select(VehicleObservation).where(VehicleObservation.id.in_(obs_ids)).order_by(VehicleObservation.fetched_at)
        ).scalars():
            if o.latitude is not None and o.longitude is not None:
                observations.append({"lat": o.latitude, "lon": o.longitude, "reported_at": o.position_reported_at,
                                     "fetched_at": o.fetched_at, "odometer_km": o.odometer_km})
    loc_ids = {i for r in raws for i in (r.start_location_id, r.end_location_id) if i}
    locs = {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(loc_ids))).scalars()} if loc_ids else {}

    def point(raw: RawTrip, which: str) -> dict[str, Any]:
        loc = locs.get(getattr(raw, f"{which}_location_id"))
        lat, lon = getattr(raw, f"{which}_lat"), getattr(raw, f"{which}_lon")
        basis = "vehicle_position" if lat is not None else None
        if lat is None and loc is not None and loc.latitude is not None:
            lat, lon, basis = loc.latitude, loc.longitude, "known_location"
        return {
            "address": getattr(raw, f"{which}_address"),
            "location": LocationBrief.model_validate(loc).model_dump(mode="json") if loc else None,
            "lat": lat, "lon": lon, "coordinate_basis": basis,
            "at": raw.started_at if which == "start" else raw.ended_at,
        }

    stops = [point(raws[0], "start")] + [point(r, "end") for r in raws] if raws else []

    sources = db.execute(
        select(LogicalTrip).where(LogicalTrip.merged_into_id == trip.id).order_by(LogicalTrip.sort_time)
    ).scalars().all()
    merge_ops = [
        {"id": str(op.id), "operation": op.operation, "trigger": op.trigger, "created_at": op.created_at,
         "created_by": op.created_by, "undone_at": op.undone_at, "details": op.details}
        for op in db.execute(
            select(TripMerge).where(TripMerge.vehicle_id == trip.vehicle_id).order_by(TripMerge.created_at.desc()).limit(200)
        ).scalars()
        if str(trip.id) in (op.result_trip_ids or []) or str(trip.id) in (op.source_trip_ids or [])
    ]
    history = db.execute(
        select(AuditEvent).where(AuditEvent.entity_type == "logical_trip", AuditEvent.entity_id == str(trip.id))
        .order_by(AuditEvent.at.desc()).limit(50)
    ).scalars().all()
    journey = db.get(BusinessJourney, trip.journey_id) if trip.journey_id else None

    tz = settings_svc.user_timezone(db, user_id)
    ctx = sig.build_context(
        db, trip, user_id=user_id, tz=tz, tax=settings_svc.get_section(db, user_id, "tax"),
        ai=settings_svc.get_section(db, user_id, "ai"),
        abbreviations=settings_svc.get_section(db, user_id, "abbreviations")["map"],
    )
    similar = purpose_mod.similar_trips(db, trip, limit=6)

    return {
        "trip": trip_out(db, trip).model_dump(mode="json"),
        "segments": segments,
        "raw_trips": [_raw_out(db, r) for r in raws],
        "map": {
            "stops": stops,
            "observations": observations,
            # Endast start, stopp och mål är kända. En linje mellan punkterna är INTE den körda vägen.
            "route_is_actual": False,
            "route_note": "Kartan visar kända punkter. Linjen mellan dem är inte den faktiskt körda vägen.",
        },
        "assessment": (proposal.signals or {}).get("result") if proposal else None,
        "assessment_meta": {
            "source": proposal.source, "model": proposal.model, "created_at": proposal.created_at,
            "confidence": proposal.confidence, "engine_version": proposal.engine_version,
        } if proposal else None,
        "decision": {"source": current.source, "category": current.category, "created_at": current.created_at,
                     "created_by": current.created_by} if current else None,
        "evidence": [
            {"kind": e.kind, "label": e.label, "direction": e.direction, "weight": e.weight, "ref_type": e.ref_type,
             "ref_id": e.ref_id, "data": e.data}
            for e in evidence
        ],
        "purpose_history": [
            {"text": p.text, "source": p.source, "rationale": p.rationale, "needs_confirmation": p.needs_confirmation,
             "is_current": p.is_current, "created_at": p.created_at}
            for p in purposes
        ],
        "purpose_suggestions": [
            {"text": p["text"], "count": p.get("count", 0), "user_count": p.get("user_count", 0), "type": p.get("type")}
            for p in purpose_mod.candidate_purposes(ctx, limit=6)
        ],
        "calendar_matches": [
            {"id": str(m.id), "event_id": str(ev.id), "subject": ev.subject, "start_at": ev.start_at, "end_at": ev.end_at,
             "location": ev.location_name, "is_online": ev.is_online, "relation": m.relation, "score": m.score,
             "time_score": m.time_score, "geo_score": m.geo_score, "geo_basis": m.geo_basis,
             "is_selected": m.is_selected, "is_conflict": m.is_conflict, "decided_by": m.decided_by}
            for m, ev in matches
        ],
        "similar_trips": [t.model_dump(mode="json") for t in trips_out(db, similar)],
        "merged_from": [t.model_dump(mode="json") for t in trips_out(db, sources)],
        "merge_operations": merge_ops,
        "journey": {"id": str(journey.id), "name": journey.name, "status": journey.status, "purpose": journey.purpose,
                    "started_at": journey.started_at, "ended_at": journey.ended_at} if journey else None,
        "history": [AuditOut.model_validate(h).model_dump(mode="json") for h in history],
        "feedback": [FeedbackOut.model_validate(f).model_dump(mode="json") for f in feedback.history(db, trip.id)],
    }


def _cancel_notifications(db, trip_id: uuid.UUID) -> None:
    try:
        from app.services.ha import notifier

        cancel = getattr(notifier, "cancel_for_trip", None)
        if cancel is not None:
            cancel(db, trip_id)
    except Exception:  # notiser får aldrig stoppa en ändring i journalen  # noqa: S110
        pass


@router.patch("/trips/{trip_id}", response_model=TripOut, summary="Ändra kategori, syfte m.m. (lär systemet)")
def update_trip(trip_id: uuid.UUID, body: TripUpdate, principal: PrincipalDep, db: DbDep) -> TripOut:
    trip = get_owned_trip(db, principal, trip_id)
    data = body.model_dump(exclude_unset=True)
    if "driver_id" in data:
        if body.driver_id is not None:
            driver = db.get(Driver, body.driver_id)
            if driver is None or driver.owner_user_id != principal.user.id:
                raise not_found("Föraren")
        trip.driver_id = body.driver_id
    if body.is_favorite is not None:
        trip.is_favorite = body.is_favorite
    if any(k in data for k in ("category", "purpose", "subcategory", "contacts", "notes")) or body.approve:
        feedback.apply_decision(
            db, trip, user_id=principal.user.id, actor=principal.actor,
            channel="web",
            category=body.category, purpose=body.purpose, subcategory=body.subcategory,
            contacts=body.contacts, notes=body.notes, approve=body.approve,
        )
        # Frågor som blivit inaktuella (klassning godkänd, syfte bekräftat) avbryts.
        _cancel_notifications(db, trip.id)
    db.commit()
    enqueue("classify.pending", user_id=str(principal.user.id), vehicle_id=str(trip.vehicle_id))
    return trip_out(db, get_owned_trip(db, principal, trip_id))


@router.post("/trips/{trip_id}/approve", response_model=TripOut, summary="Godkänn föreslagen klassning")
def approve_trip(trip_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> TripOut:
    trip = get_owned_trip(db, principal, trip_id)
    feedback.apply_decision(db, trip, user_id=principal.user.id, actor=principal.actor, approve=True)
    _cancel_notifications(db, trip.id)
    db.commit()
    return trip_out(db, get_owned_trip(db, principal, trip_id))


@router.post("/trips/{trip_id}/classify", summary="Kör klassificeringen på nytt för resan", tags=["Classification"])
def reclassify(trip_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    trip = get_owned_trip(db, principal, trip_id)
    provider = classify_engine.get_provider(db, principal.user.id)
    result = classify_engine.classify_trip(
        db, trip, user_id=principal.user.id, actor=principal.actor, provider=provider, force=True
    )
    pipeline.sync_trip_review_items(db, trip)
    db.commit()
    return {
        "changed": bool(result and result.changed),
        "assessment": result.as_dict() if result else None,
        "trip": trip_out(db, get_owned_trip(db, principal, trip_id)).model_dump(mode="json"),
        "note": None if result else "Resan är redan avgjord av dig eller av importerad källa och bedöms inte om.",
    }


@router.post("/trips/bulk", summary="Massredigering av valda resor")
def bulk_update(body: BulkUpdate, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    owned = set(owned_vehicle_ids(db, principal.user.id))
    trips = list(
        db.execute(
            select(LogicalTrip).where(LogicalTrip.id.in_(body.trip_ids), LogicalTrip.vehicle_id.in_(owned))
            .options(selectinload(LogicalTrip.segments).selectinload(TripSegment.raw_trip))
        ).scalars()
    )
    if len(trips) != len(set(body.trip_ids)):
        raise not_found("En eller flera resor")
    if body.is_favorite is not None:
        for t in trips:
            t.is_favorite = body.is_favorite
    done, errors = 0, []
    if body.category is not None or body.purpose is not None or body.approve:
        done, errors = feedback.bulk_apply(
            db, trips, user_id=principal.user.id, actor=principal.actor, category=body.category,
            purpose=body.purpose, approve=body.approve,
        )
        for t in trips:
            _cancel_notifications(db, t.id)
    else:
        done = len(trips)
    db.commit()
    return {"updated": done, "errors": errors}


@router.get("/trips/{trip_id}/history", response_model=list[AuditOut], summary="Ändringshistorik för en resa")
def trip_history(trip_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> list[AuditEvent]:
    trip = get_owned_trip(db, principal, trip_id)
    return list(
        db.execute(
            select(AuditEvent).where(AuditEvent.entity_type == "logical_trip", AuditEvent.entity_id == str(trip.id))
            .order_by(AuditEvent.at.desc()).limit(200)
        ).scalars()
    )


@router.post("/trips/manual", response_model=TripOut, status_code=201, summary="Lägg in en resa manuellt")
def create_manual_trip(body: ManualTrip, principal: WebPrincipalDep, db: DbDep) -> TripOut:
    vehicle = get_vehicle(db, principal, body.vehicle_id)
    if body.started_at.tzinfo is None or body.ended_at.tzinfo is None:
        raise bad_request("Tiderna måste ange tidszon.")
    if body.ended_at < body.started_at:
        raise bad_request("Sluttiden ligger före starttiden.")
    if (body.odometer_start is None) != (body.odometer_end is None):
        raise bad_request("Ange både mätarställning start och slut, eller ingen av dem.")
    if body.odometer_start is not None and body.odometer_end < body.odometer_start:  # type: ignore[operator]
        raise bad_request("Mätarställning slut är lägre än mätarställning start.")
    s_loc = places.resolve_address(db, principal.user.id, body.start_address, created_from="user", is_demo=vehicle.is_demo)
    e_loc = places.resolve_address(db, principal.user.id, body.end_address, created_from="user", is_demo=vehicle.is_demo)
    has_odo = body.odometer_start is not None
    raw = RawTrip(
        vehicle_id=vehicle.id,
        source=TripSource.MANUAL,
        source_key=f"manual:{uuid.uuid4().hex}",
        started_at=body.started_at,
        ended_at=body.ended_at,
        start_time_quality=TimeQuality.EXACT,
        end_time_quality=TimeQuality.EXACT,
        odometer_start=body.odometer_start,
        odometer_end=body.odometer_end,
        odometer_source="user" if has_odo else "none",
        distance_km=round(body.distance_km, 3),
        distance_quality=DistanceQuality.USER,
        start_address=body.start_address.strip(),
        end_address=body.end_address.strip(),
        position_source=PositionSource.USER,
        start_location_id=s_loc.id if s_loc else None,
        end_location_id=e_loc.id if e_loc else None,
        fuel_quality=ValueQuality.UNKNOWN,
        energy_quality=ValueQuality.UNKNOWN,
        is_complete=has_odo,
        quality_flags=["user_entered"] + ([] if has_odo else ["missing_odometer"]),
        updated_by=principal.actor.label,
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    decided = body.category != Category.UNDECIDED
    purpose = body.purpose.strip() if body.purpose and body.purpose.strip() else None
    trip = seg.create_logical_for_raw(
        db, raw,
        category=body.category,
        category_source=DecisionSource.USER if decided else DecisionSource.SYSTEM,
        purpose=purpose,
        purpose_source=DecisionSource.USER if purpose else None,
        purpose_confirmed=bool(purpose),
        review_status=ReviewStatus.APPROVED if decided else ReviewStatus.NEEDS_REVIEW,
        actor=principal.actor.label,
    )
    audit.record(db, principal.actor, "trip.manual_create", "logical_trip", trip.id, after=body.model_dump(mode="json"))
    pipeline.after_trips_changed(
        db, user_id=principal.user.id, vehicle=vehicle, actor=principal.actor, notify=False, origin="manual", use_llm=False
    )
    db.commit()
    active = db.execute(
        select(LogicalTrip.id).join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
        .where(TripSegment.raw_trip_id == raw.id, LogicalTrip.state == LogicalState.ACTIVE)
    ).scalars().first()
    return trip_out(db, get_owned_trip(db, principal, active or trip.id))


@router.delete("/trips/{trip_id}", status_code=204, summary="Ta bort en manuellt inlagd resa")
def delete_manual_trip(trip_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    from app.db import utcnow

    trip = get_owned_trip(db, principal, trip_id)
    raws = seg.ordered_raws(trip)
    if trip.state != LogicalState.ACTIVE or len(raws) != 1 or raws[0].source != TripSource.MANUAL:
        raise bad_request(
            "Endast manuellt inlagda, ej sammanslagna resor kan tas bort. Importerade resor tas bort genom att ångra importen.",
            "not_deletable",
        )
    raws[0].voided_at = utcnow()
    raws[0].void_reason = "Borttagen av användaren"
    trip.state = LogicalState.VOIDED
    audit.record(db, principal.actor, "trip.manual_delete", "logical_trip", trip.id)
    vehicle = get_vehicle(db, principal, trip.vehicle_id)
    pipeline.after_trips_changed(
        db, user_id=principal.user.id, vehicle=vehicle, actor=principal.actor, apply_merge_rules=False, notify=False,
        origin="manual", use_llm=False,
    )
    db.commit()
    return Response(status_code=204)


@router.get("/journeys", summary="Överordnade tjänsteresor (flera körningar, hotell, hemresa)")
def list_journeys(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    out = []
    for j in db.execute(
        select(BusinessJourney).where(BusinessJourney.vehicle_id == vehicle.id).order_by(BusinessJourney.started_at.desc())
    ).scalars():
        km, n = db.execute(
            select(func.coalesce(func.sum(LogicalTrip.distance_km), 0), func.count()).where(
                LogicalTrip.journey_id == j.id, LogicalTrip.state == LogicalState.ACTIVE
            )
        ).one()
        out.append({"id": str(j.id), "name": j.name, "purpose": j.purpose, "status": j.status, "started_at": j.started_at,
                    "ended_at": j.ended_at, "distance_km": round(float(km), 1), "trip_count": n})
    return out


class JourneyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=1000)
    confirm: bool = False


@router.patch("/journeys/{journey_id}", summary="Bekräfta eller döp om en tjänsteresa")
def update_journey(journey_id: uuid.UUID, body: JourneyUpdate, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    from app.services.trips import journeys

    j = db.get(BusinessJourney, journey_id)
    if j is None or j.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Tjänsteresan")
    if body.name:
        j.name = body.name
    if body.purpose is not None:
        j.purpose = body.purpose
    if body.confirm:
        journeys.confirm(db, j)
    db.commit()
    return {"id": str(j.id), "name": j.name, "purpose": j.purpose, "status": j.status}


@router.delete("/journeys/{journey_id}", status_code=204, summary="Lös upp en tjänsteresa (körningarna påverkas inte)")
def delete_journey(journey_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    from app.services.trips import journeys

    j = db.get(BusinessJourney, journey_id)
    if j is None or j.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Tjänsteresan")
    journeys.dissolve(db, j)
    db.commit()
    return Response(status_code=204)
