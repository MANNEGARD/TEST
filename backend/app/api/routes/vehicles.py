"""Fordon, förare, telemetri och mätarställningar."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, get_vehicle
from app.api.errors import bad_request, not_found
from app.api.serializers import vehicle_out
from app.db import utcnow
from app.models import Driver, Vehicle, VehicleObservation, VehicleOdometerSnapshot
from app.schemas.common import ORM, VehicleOut
from app.services import audit
from app.services import settings as settings_svc

router = APIRouter(tags=["Vehicles"])


class VehicleIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=120)
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    fuel_type: str | None = Field(default=None, max_length=40)
    registration_number: str | None = Field(default=None, max_length=20)
    is_default: bool | None = None
    is_active: bool | None = None


class DriverOut(ORM):
    id: uuid.UUID
    name: str
    is_default: bool


class DriverIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    is_default: bool = False


class SnapshotOut(ORM):
    id: uuid.UUID
    at: datetime
    odometer_km: float
    source: str
    note: str


class SnapshotIn(BaseModel):
    at: datetime
    odometer_km: float = Field(ge=0, le=5_000_000)
    kind: str = Field(default="manual", pattern="^(manual|year_start|year_end)$")
    note: str = Field(default="", max_length=500)


class ObservationOut(ORM):
    id: uuid.UUID
    source: str
    fetched_at: datetime
    last_seen_at: datetime
    odometer_km: float | None
    odometer_reported_at: datetime | None
    latitude: float | None
    longitude: float | None
    position_reported_at: datetime | None
    fuel_liters: float | None
    fuel_reported_at: datetime | None
    battery_percent: float | None
    battery_reported_at: datetime | None
    electric_range_km: float | None
    fuel_range_km: float | None
    engine_running: bool | None
    avg_fuel_consumption: float | None
    avg_energy_consumption: float | None
    errors: dict[str, Any]


@router.get("/vehicles", response_model=list[VehicleOut], summary="Lista fordon")
def list_vehicles(principal: PrincipalDep, db: DbDep) -> list[VehicleOut]:
    get_vehicle(db, principal, None)  # säkerställer att ett standardfordon finns
    db.commit()
    rows = db.execute(
        select(Vehicle).where(Vehicle.owner_user_id == principal.user.id).order_by(Vehicle.is_demo, Vehicle.created_at)
    ).scalars()
    return [vehicle_out(v) for v in rows]


@router.post("/vehicles", response_model=VehicleOut, status_code=201, summary="Lägg till fordon")
def create_vehicle(body: VehicleIn, principal: WebPrincipalDep, db: DbDep) -> VehicleOut:
    if not body.name:
        raise bad_request("Ange ett namn på fordonet.")
    v = Vehicle(owner_user_id=principal.user.id, name=body.name)
    for field in ("make", "model", "model_year", "fuel_type", "registration_number"):
        value = getattr(body, field)
        if value is not None:
            setattr(v, field, value)
    db.add(v)
    db.flush()
    audit.record(db, principal.actor, "vehicle.create", "vehicle", v.id, after={"name": v.name})
    db.commit()
    return vehicle_out(v)


@router.patch("/vehicles/{vehicle_id}", response_model=VehicleOut, summary="Ändra fordonsuppgifter")
def update_vehicle(vehicle_id: uuid.UUID, body: VehicleIn, principal: WebPrincipalDep, db: DbDep) -> VehicleOut:
    v = get_vehicle(db, principal, vehicle_id)
    before = {"name": v.name, "registration_number": v.registration_number}
    data = body.model_dump(exclude_unset=True)
    if data.get("is_default"):
        if v.is_demo:
            raise bad_request("Demobilen kan inte vara standardfordon.")
        for other in db.execute(select(Vehicle).where(Vehicle.owner_user_id == principal.user.id)).scalars():
            other.is_default = False
    for field, value in data.items():
        if field == "registration_number" and value is not None:
            value = value.strip().upper() or None
        setattr(v, field, value)
    audit.record(db, principal.actor, "vehicle.update", "vehicle", v.id, before=before, after=data)
    db.commit()
    return vehicle_out(v)


@router.get("/vehicles/{vehicle_id}/telemetry", summary="Senaste fordonsdata med både hämtnings- och fordonstid",
            tags=["Vehicle telemetry"])
def telemetry(vehicle_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    v = get_vehicle(db, principal, vehicle_id)
    obs = db.execute(
        select(VehicleObservation).where(VehicleObservation.vehicle_id == v.id)
        .order_by(VehicleObservation.fetched_at.desc()).limit(1)
    ).scalar_one_or_none()
    snapshot = db.execute(
        select(VehicleOdometerSnapshot).where(VehicleOdometerSnapshot.vehicle_id == v.id)
        .order_by(VehicleOdometerSnapshot.at.desc()).limit(1)
    ).scalar_one_or_none()
    stale_after = settings_svc.get_section(db, principal.user.id, "volvo")["stale_after_minutes"]
    stale = None
    if obs is not None:
        newest = max(
            (t for t in (obs.odometer_reported_at, obs.position_reported_at, obs.fuel_reported_at) if t is not None),
            default=None,
        )
        age = (utcnow() - (newest or obs.fetched_at)).total_seconds() / 60
        stale = age > stale_after
    return {
        "vehicle_id": str(v.id),
        "observation": ObservationOut.model_validate(obs).model_dump(mode="json") if obs else None,
        "latest_odometer_snapshot": SnapshotOut.model_validate(snapshot).model_dump(mode="json") if snapshot else None,
        "is_stale": stale,
        "stale_after_minutes": stale_after,
        "capabilities": v.capabilities or {},
    }


@router.get("/vehicles/{vehicle_id}/observations", response_model=list[ObservationOut],
            summary="Rådataobservationer", tags=["Vehicle telemetry"])
def observations(
    vehicle_id: uuid.UUID, principal: PrincipalDep, db: DbDep,
    start: datetime | None = Query(default=None, alias="from"), end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[VehicleObservation]:
    v = get_vehicle(db, principal, vehicle_id)
    q = select(VehicleObservation).where(VehicleObservation.vehicle_id == v.id)
    if start is not None:
        q = q.where(VehicleObservation.last_seen_at >= start)
    if end is not None:
        q = q.where(VehicleObservation.fetched_at <= end)
    return list(db.execute(q.order_by(VehicleObservation.fetched_at.desc()).limit(limit)).scalars())


@router.get("/vehicles/{vehicle_id}/odometer", response_model=list[SnapshotOut], summary="Kända mätarställningar",
            tags=["Vehicle telemetry"])
def odometer_snapshots(vehicle_id: uuid.UUID, principal: PrincipalDep, db: DbDep,
                       limit: int = Query(default=100, ge=1, le=1000)) -> list[VehicleOdometerSnapshot]:
    v = get_vehicle(db, principal, vehicle_id)
    return list(
        db.execute(
            select(VehicleOdometerSnapshot).where(VehicleOdometerSnapshot.vehicle_id == v.id)
            .order_by(VehicleOdometerSnapshot.at.desc()).limit(limit)
        ).scalars()
    )


@router.post("/vehicles/{vehicle_id}/odometer", response_model=SnapshotOut, status_code=201,
             summary="Registrera mätarställning manuellt (t.ex. vid årsskifte)", tags=["Vehicle telemetry"])
def add_odometer_snapshot(vehicle_id: uuid.UUID, body: SnapshotIn, principal: WebPrincipalDep, db: DbDep) -> VehicleOdometerSnapshot:
    v = get_vehicle(db, principal, vehicle_id)
    if body.at.tzinfo is None:
        raise bad_request("Tidpunkten måste ange tidszon.")
    existing = db.execute(
        select(VehicleOdometerSnapshot).where(
            VehicleOdometerSnapshot.vehicle_id == v.id, VehicleOdometerSnapshot.at == body.at,
            VehicleOdometerSnapshot.source == body.kind,
        )
    ).scalar_one_or_none()
    snap = existing or VehicleOdometerSnapshot(vehicle_id=v.id, at=body.at, source=body.kind, odometer_km=body.odometer_km)
    snap.odometer_km, snap.note, snap.created_by = body.odometer_km, body.note, principal.actor.label
    db.add(snap)
    audit.record(db, principal.actor, "odometer.manual", "vehicle", v.id, after=body.model_dump(mode="json"))
    db.commit()
    return snap


@router.delete("/vehicles/{vehicle_id}/odometer/{snapshot_id}", status_code=204, summary="Ta bort manuell mätarställning",
               tags=["Vehicle telemetry"])
def delete_odometer_snapshot(vehicle_id: uuid.UUID, snapshot_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    v = get_vehicle(db, principal, vehicle_id)
    snap = db.get(VehicleOdometerSnapshot, snapshot_id)
    if snap is None or snap.vehicle_id != v.id:
        raise not_found("Mätarställningen")
    if snap.source == "api":
        raise bad_request("Mätarställningar från fordonet kan inte tas bort.")
    db.delete(snap)
    db.commit()
    return Response(status_code=204)


@router.get("/drivers", response_model=list[DriverOut], summary="Lista förare")
def list_drivers(principal: PrincipalDep, db: DbDep) -> list[Driver]:
    return list(db.execute(select(Driver).where(Driver.owner_user_id == principal.user.id).order_by(Driver.created_at)).scalars())


@router.post("/drivers", response_model=DriverOut, status_code=201, summary="Lägg till förare")
def create_driver(body: DriverIn, principal: WebPrincipalDep, db: DbDep) -> Driver:
    if body.is_default:
        for d in db.execute(select(Driver).where(Driver.owner_user_id == principal.user.id)).scalars():
            d.is_default = False
    driver = Driver(owner_user_id=principal.user.id, name=body.name.strip(), is_default=body.is_default)
    db.add(driver)
    db.commit()
    return driver


@router.patch("/drivers/{driver_id}", response_model=DriverOut, summary="Ändra förare")
def update_driver(driver_id: uuid.UUID, body: DriverIn, principal: WebPrincipalDep, db: DbDep) -> Driver:
    driver = db.get(Driver, driver_id)
    if driver is None or driver.owner_user_id != principal.user.id:
        raise not_found("Föraren")
    if body.is_default:
        for d in db.execute(select(Driver).where(Driver.owner_user_id == principal.user.id)).scalars():
            d.is_default = False
    driver.name, driver.is_default = body.name.strip(), body.is_default
    db.commit()
    return driver
