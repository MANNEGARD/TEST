"""Platser och AI-minne: kända adresser, typer, geofence och förslag."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, owned_vehicle_ids
from app.api.errors import bad_request, conflict, not_found
from app.api.serializers import location_out
from app.models import KnownLocation, LocationAlias, LocationPattern, RawTrip, Vehicle, WorkplacePeriod
from app.models.enums import LOCATION_TYPE_LABELS_SV, LocationType
from app.schemas.common import LocationOut
from app.services import audit
from app.services.learning import feedback
from app.services.places import registry as places
from app.services.places import suggest
from app.services.places.normalize import clean, parse_address, valid_coordinates

router = APIRouter(tags=["Locations"])

_TYPES = "|".join(t.value for t in LocationType)


class LocationIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = Field(default=None, pattern=f"^({_TYPES})$")
    address: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=200)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_m: int | None = Field(default=None, ge=30, le=5000)
    confirmed: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)
    clear_coordinates: bool = False


class MergeIn(BaseModel):
    into_id: uuid.UUID


class AliasIn(BaseModel):
    address: str = Field(min_length=1, max_length=300)


def _owned(db, principal, location_id: uuid.UUID) -> KnownLocation:
    loc = db.get(KnownLocation, location_id)
    if loc is None or loc.owner_user_id != principal.user.id or loc.merged_into_id is not None:
        raise not_found("Platsen")
    return loc


def _reevaluate(db, principal) -> None:
    for vid in owned_vehicle_ids(db, principal.user.id):
        feedback.after_decisions(db, user_id=principal.user.id, vehicle_id=vid, actor=principal.actor)


@router.get("/locations", response_model=list[LocationOut], summary="Kända platser med besöksstatistik")
def list_locations(
    principal: PrincipalDep,
    db: DbDep,
    q: str | None = Query(default=None, max_length=200),
    type: list[str] | None = Query(default=None),
    confirmed: bool | None = None,
    has_suggestion: bool | None = None,
    include_demo: bool = False,
    sort: str = Query(default="visits", pattern="^(visits|name|recent)$"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[LocationOut]:
    stmt = select(KnownLocation).where(
        KnownLocation.owner_user_id == principal.user.id, KnownLocation.merged_into_id.is_(None)
    )
    if not include_demo:
        stmt = stmt.where(KnownLocation.is_demo.is_(False))
    if type:
        stmt = stmt.where(KnownLocation.type.in_(type))
    if confirmed is not None:
        stmt = stmt.where(KnownLocation.confirmed.is_(confirmed))
    if has_suggestion is True:
        stmt = stmt.where(KnownLocation.suggested_type.is_not(None), KnownLocation.confirmed.is_(False))
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(func.lower(KnownLocation.name).like(like), func.lower(KnownLocation.address).like(like),
                func.lower(KnownLocation.company).like(like), func.lower(KnownLocation.city).like(like))
        )
    order = {"visits": KnownLocation.visit_count.desc(), "name": KnownLocation.name.asc(),
             "recent": KnownLocation.last_visited_at.desc().nulls_last()}[sort]
    rows = list(db.execute(stmt.order_by(order).limit(limit)).scalars())
    stats = suggest.location_stats(db, principal.user.id)
    return [location_out(loc, stats.get(loc.id)) for loc in rows]


@router.get("/locations/meta", summary="Platstyper och sammanfattning")
def meta(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    rows = db.execute(
        select(KnownLocation.type, KnownLocation.confirmed, func.count())
        .where(KnownLocation.owner_user_id == principal.user.id, KnownLocation.merged_into_id.is_(None),
               KnownLocation.is_demo.is_(False))
        .group_by(KnownLocation.type, KnownLocation.confirmed)
    ).all()
    by_type: dict[str, int] = {}
    unconfirmed = 0
    for t, conf, n in rows:
        by_type[t] = by_type.get(t, 0) + n
        if not conf:
            unconfirmed += n
    has_home = any(t == LocationType.HOME for t, conf, _ in rows if conf)
    return {"types": {k: v for k, v in LOCATION_TYPE_LABELS_SV.items()}, "by_type": by_type,
            "unconfirmed": unconfirmed, "has_confirmed_home": has_home}


@router.get("/locations/suggestions", summary="Förslag: platstyper att godkänna och platser att slå ihop")
def suggestions(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    suggest.refresh(db, principal.user.id)
    db.commit()
    typed = db.execute(
        select(KnownLocation).where(
            KnownLocation.owner_user_id == principal.user.id, KnownLocation.confirmed.is_(False),
            KnownLocation.suggested_type.is_not(None), KnownLocation.merged_into_id.is_(None),
            KnownLocation.is_demo.is_(False),
        ).order_by(KnownLocation.visit_count.desc())
    ).scalars()
    return {
        "types": [location_out(loc).model_dump(mode="json") for loc in typed],
        "merges": places.merge_suggestions(db, principal.user.id),
    }


@router.post("/locations", response_model=LocationOut, status_code=201, summary="Skapa plats")
def create_location(body: LocationIn, principal: WebPrincipalDep, db: DbDep) -> LocationOut:
    if not body.name:
        raise bad_request("Ange ett namn på platsen.")
    address = clean(body.address) or None
    if address and places.find_by_address(db, principal.user.id, address) is not None:
        raise conflict("Det finns redan en plats med den adressen.", "address_exists")
    if (body.latitude is None) != (body.longitude is None):
        raise bad_request("Ange både latitud och longitud.")
    loc = KnownLocation(
        owner_user_id=principal.user.id, name=body.name.strip(), type=body.type or LocationType.OTHER,
        address=address, city=parse_address(address).city if address else None, company=body.company,
        latitude=body.latitude, longitude=body.longitude, radius_m=body.radius_m or 150,
        coordinate_source="user" if body.latitude is not None else None, confirmed=True, created_from="user",
        notes=body.notes,
    )
    db.add(loc)
    db.flush()
    if address:
        places.add_alias(db, loc, address)
    audit.record(db, principal.actor, "location.create", "known_location", loc.id, after={"name": loc.name, "type": loc.type})
    db.commit()
    return location_out(loc)


@router.get("/locations/{location_id}", response_model=LocationOut, summary="En plats")
def get_location(location_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> LocationOut:
    loc = _owned(db, principal, location_id)
    return location_out(loc, suggest.location_stats(db, principal.user.id).get(loc.id))


@router.patch("/locations/{location_id}", response_model=LocationOut, summary="Ändra namn, typ, geofence m.m.")
def update_location(location_id: uuid.UUID, body: LocationIn, principal: WebPrincipalDep, db: DbDep) -> LocationOut:
    loc = _owned(db, principal, location_id)
    data = body.model_dump(exclude_unset=True)
    before = {"name": loc.name, "type": loc.type, "confirmed": loc.confirmed}
    if ("latitude" in data) != ("longitude" in data):
        raise bad_request("Ange både latitud och longitud.")
    if "latitude" in data and data["latitude"] is not None:
        if not valid_coordinates(data["latitude"], data["longitude"]):
            raise bad_request("Ogiltiga koordinater.")
        loc.latitude, loc.longitude, loc.coordinate_source = data["latitude"], data["longitude"], "user"
    if body.clear_coordinates:
        loc.latitude, loc.longitude, loc.coordinate_source = None, None, None
    for field in ("name", "type", "company", "radius_m", "notes", "confirmed"):
        if field in data and data[field] is not None:
            setattr(loc, field, data[field].strip() if isinstance(data[field], str) else data[field])
    if "company" in data and data["company"] in (None, ""):
        loc.company = None
    if "type" in data or "name" in data:
        loc.confirmed = True if "confirmed" not in data else loc.confirmed  # ett uttryckligt val bekräftar platsen
        loc.suggested_type, loc.suggested_name, loc.suggestion_reason = None, None, None
    if loc.type == LocationType.HOME and loc.confirmed:
        others = db.execute(
            select(KnownLocation).where(
                KnownLocation.owner_user_id == principal.user.id, KnownLocation.type == LocationType.HOME,
                KnownLocation.id != loc.id, KnownLocation.confirmed.is_(True), KnownLocation.is_demo.is_(loc.is_demo),
                KnownLocation.merged_into_id.is_(None),
            )
        ).scalars().all()
        if others and before["type"] != LocationType.HOME:
            raise conflict(
                f"{others[0].name!r} är redan markerad som hem. Ändra den först eller slå ihop platserna.", "home_exists"
            )
    audit.record(db, principal.actor, "location.update", "known_location", loc.id, before=before, after=data)
    db.flush()
    _reevaluate(db, principal)  # platstyper påverkar klassningen av oavgjorda resor
    db.commit()
    return location_out(loc, suggest.location_stats(db, principal.user.id).get(loc.id))


@router.post("/locations/{location_id}/accept-suggestion", response_model=LocationOut, summary="Godkänn föreslagen typ och namn")
def accept_suggestion(location_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> LocationOut:
    loc = _owned(db, principal, location_id)
    if not loc.suggested_type:
        raise bad_request("Platsen har inget förslag.")
    loc.type = loc.suggested_type
    if loc.suggested_name:
        if loc.type == LocationType.CUSTOMER:
            loc.company, loc.name = loc.suggested_name, loc.suggested_name
        else:
            loc.name = loc.suggested_name
    loc.confirmed = True
    loc.suggested_type, loc.suggested_name, loc.suggestion_reason = None, None, None
    audit.record(db, principal.actor, "location.accept_suggestion", "known_location", loc.id, after={"type": loc.type})
    db.flush()
    _reevaluate(db, principal)
    db.commit()
    return location_out(loc)


@router.post("/locations/{location_id}/reject-suggestion", response_model=LocationOut, summary="Avvisa förslaget")
def reject_suggestion(location_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> LocationOut:
    loc = _owned(db, principal, location_id)
    loc.suggested_type, loc.suggested_name, loc.suggestion_reason = None, None, None
    loc.confirmed = True  # användaren har tagit ställning: typen förblir som den är
    db.commit()
    return location_out(loc)


@router.post("/locations/{location_id}/merge", response_model=LocationOut, summary="Slå ihop med en annan plats")
def merge_location(location_id: uuid.UUID, body: MergeIn, principal: WebPrincipalDep, db: DbDep) -> LocationOut:
    other = _owned(db, principal, location_id)
    keep = _owned(db, principal, body.into_id)
    if keep.id == other.id:
        raise bad_request("En plats kan inte slås ihop med sig själv.")
    places.merge_locations(db, keep, other)
    for wp in db.execute(select(WorkplacePeriod).where(WorkplacePeriod.location_id == other.id)).scalars():
        wp.location_id = keep.id
    audit.record(db, principal.actor, "location.merge", "known_location", keep.id, after={"merged": str(other.id)})
    from app.services.learning import patterns

    patterns.rebuild(db, principal.user.id)
    _reevaluate(db, principal)
    db.commit()
    return location_out(keep, suggest.location_stats(db, principal.user.id).get(keep.id))


@router.post("/locations/{location_id}/aliases", response_model=LocationOut, summary="Lägg till en adress som alias")
def add_alias(location_id: uuid.UUID, body: AliasIn, principal: WebPrincipalDep, db: DbDep) -> LocationOut:
    loc = _owned(db, principal, location_id)
    existing = places.find_by_address(db, principal.user.id, body.address)
    if existing is not None and existing.id != loc.id:
        raise conflict("Adressen tillhör redan en annan plats. Slå ihop platserna i stället.", "address_exists")
    places.add_alias(db, loc, body.address)
    db.commit()
    return location_out(loc)


@router.delete("/locations/{location_id}", status_code=204, summary="Ta bort en plats som inte används")
def delete_location(location_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    loc = _owned(db, principal, location_id)
    used = db.execute(
        select(RawTrip.id).where(or_(RawTrip.start_location_id == loc.id, RawTrip.end_location_id == loc.id)).limit(1)
    ).first()
    if used:
        raise conflict("Platsen används av resor och kan inte tas bort. Slå ihop den med en annan plats i stället.", "in_use")
    for pat in db.execute(
        select(LocationPattern).where(
            (LocationPattern.destination_location_id == loc.id) | (LocationPattern.origin_location_id == loc.id)
        )
    ).scalars():
        db.delete(pat)
    db.delete(loc)
    db.commit()
    return Response(status_code=204)


__all__ = ["router", "LocationAlias", "Vehicle"]
