"""Tankningar, laddningar och sammanställning av bensin och el."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.deps import DbDep, Principal, PrincipalDep, get_vehicle, owned_vehicle_ids
from app.api.errors import bad_request, not_found
from app.api.routes.dashboard import PERIOD_PATTERN, period_scope
from app.models import ChargingRecord, FuelRecord
from app.models.enums import ValueQuality
from app.schemas.common import ORM
from app.services import audit
from app.services.stats import energy as energy_stats

router = APIRouter(prefix="/energy", tags=["Energy"])

QUALITY_PATTERN = "^(verified|estimated)$"


def _aware(value: datetime | None, field: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field} måste anges med tidszon (t.ex. 2026-09-14T08:00:00+02:00).")
    return value.astimezone(UTC) if value is not None else None


# ----------------------------------------------------------------------------- scheman


class FuelOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    at: datetime
    liters: float | None
    liters_quality: str
    total_cost: float | None
    price_per_liter: float | None
    odometer_km: float | None
    station: str | None
    full_tank: bool
    source: str
    note: str | None
    created_at: datetime


class FuelIn(BaseModel):
    vehicle_id: uuid.UUID | None = None
    at: datetime
    liters: float = Field(gt=0, le=1000)
    liters_quality: str = Field(default=ValueQuality.VERIFIED, pattern=QUALITY_PATTERN)
    total_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    price_per_liter: float | None = Field(default=None, ge=0, le=1000)
    odometer_km: float | None = Field(default=None, ge=0, le=5_000_000)
    station: str | None = Field(default=None, max_length=200)
    full_tank: bool = False
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "Tidpunkten")  # type: ignore[return-value]


class FuelPatch(BaseModel):
    at: datetime | None = None
    liters: float | None = Field(default=None, gt=0, le=1000)
    liters_quality: str | None = Field(default=None, pattern=QUALITY_PATTERN)
    total_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    price_per_liter: float | None = Field(default=None, ge=0, le=1000)
    odometer_km: float | None = Field(default=None, ge=0, le=5_000_000)
    station: str | None = Field(default=None, max_length=200)
    full_tank: bool | None = None
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("at")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        return _aware(v, "Tidpunkten")


class ChargingOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    kwh: float | None
    kwh_quality: str
    total_cost: float | None
    location: str | None
    source: str
    note: str | None
    created_at: datetime


class ChargingIn(BaseModel):
    vehicle_id: uuid.UUID | None = None
    started_at: datetime
    ended_at: datetime | None = None
    kwh: float = Field(gt=0, le=1000)
    kwh_quality: str = Field(default=ValueQuality.VERIFIED, pattern=QUALITY_PATTERN)
    total_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    location: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("started_at")
    @classmethod
    def _tz_start(cls, v: datetime) -> datetime:
        return _aware(v, "Starttiden")  # type: ignore[return-value]

    @field_validator("ended_at")
    @classmethod
    def _tz_end(cls, v: datetime | None) -> datetime | None:
        return _aware(v, "Sluttiden")

    @model_validator(mode="after")
    def _order(self) -> ChargingIn:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("Sluttiden får inte ligga före starttiden.")
        return self


class ChargingPatch(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None
    kwh: float | None = Field(default=None, gt=0, le=1000)
    kwh_quality: str | None = Field(default=None, pattern=QUALITY_PATTERN)
    total_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    location: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("started_at")
    @classmethod
    def _tz_start(cls, v: datetime | None) -> datetime | None:
        return _aware(v, "Starttiden")

    @field_validator("ended_at")
    @classmethod
    def _tz_end(cls, v: datetime | None) -> datetime | None:
        return _aware(v, "Sluttiden")


# ----------------------------------------------------------------------------- hjälpare


def _owned_fuel(db, principal: Principal, record_id: uuid.UUID) -> FuelRecord:
    rec = db.get(FuelRecord, record_id)
    if rec is None or rec.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Tankningen")
    return rec


def _owned_charging(db, principal: Principal, record_id: uuid.UUID) -> ChargingRecord:
    rec = db.get(ChargingRecord, record_id)
    if rec is None or rec.vehicle_id not in owned_vehicle_ids(db, principal.user.id):
        raise not_found("Laddningen")
    return rec


def _snapshot(rec: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: getattr(rec, f) for f in fields}


_FUEL_FIELDS = ("at", "liters", "liters_quality", "total_cost", "price_per_liter", "odometer_km", "station",
                "full_tank", "note", "source")
_CHARGE_FIELDS = ("started_at", "ended_at", "kwh", "kwh_quality", "total_cost", "location", "note", "source")


# ----------------------------------------------------------------------------- sammanställning


@router.get("/summary", summary="Bensin och el för perioden (verifierat, uppskattat, okänt)")
def summary(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    period: str = Query(default="month", pattern=PERIOD_PATTERN),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    year: int | None = Query(default=None, ge=1990, le=2200),
) -> dict[str, Any]:
    scope = period_scope(db, principal, vehicle_id=vehicle_id, period=period, date_from=date_from, date_to=date_to,
                         year=year)
    out = energy_stats.summary(db, vehicle=scope.vehicle, start=scope.period.start, end=scope.period.end, tz=scope.tz)
    out["period"] = {**scope.period.as_dict(), "timezone": str(scope.tz)}
    db.commit()
    return out


@router.post("/detect-refuels", summary="Föreslå tankningar ur fordonets tanknivå")
def detect_refuels(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    created = energy_stats.detect_refuels(db, vehicle)
    if created:
        audit.record(db, principal.actor, "energy.detect_refuels", "vehicle", vehicle.id, after={"created": len(created)})
    db.commit()
    return {"created": len(created), "items": [FuelOut.model_validate(r).model_dump(mode="json") for r in created]}


# ----------------------------------------------------------------------------- tankningar


@router.get("/fuel", response_model=list[FuelOut], summary="Tankningar")
def list_fuel(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> list[FuelRecord]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    rows = energy_stats.fuel_records(db, vehicle.id, start, end)
    db.commit()
    return rows


@router.post("/fuel", response_model=FuelOut, status_code=201, summary="Registrera tankning")
def create_fuel(body: FuelIn, principal: PrincipalDep, db: DbDep) -> FuelRecord:
    vehicle = get_vehicle(db, principal, body.vehicle_id)
    data = body.model_dump(exclude={"vehicle_id"})
    if data["price_per_liter"] is None and data["total_cost"] is not None:
        data["price_per_liter"] = round(data["total_cost"] / data["liters"], 2)
    rec = FuelRecord(vehicle_id=vehicle.id, source="manual", **data)
    db.add(rec)
    db.flush()
    audit.record(db, principal.actor, "energy.fuel_create", "fuel_record", rec.id, after=_snapshot(rec, _FUEL_FIELDS))
    db.commit()
    return rec


@router.get("/fuel/{record_id}", response_model=FuelOut, summary="En tankning")
def get_fuel(record_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> FuelRecord:
    return _owned_fuel(db, principal, record_id)


@router.patch("/fuel/{record_id}", response_model=FuelOut, summary="Ändra tankning")
def update_fuel(record_id: uuid.UUID, body: FuelPatch, principal: PrincipalDep, db: DbDep) -> FuelRecord:
    rec = _owned_fuel(db, principal, record_id)
    changes = body.model_dump(exclude_unset=True)
    for key in ("at", "liters", "liters_quality", "full_tank"):
        if key in changes and changes[key] is None:
            raise bad_request("Fältet får inte tömmas.", "invalid_value", {"field": key})
    before = _snapshot(rec, _FUEL_FIELDS)
    for key, value in changes.items():
        setattr(rec, key, value)
    if rec.source == "detected" and ("liters" in changes or "at" in changes):
        # Användaren har bekräftat/korrigerat förslaget – posten är nu manuell.
        rec.source = "manual"
    db.flush()
    audit.record(db, principal.actor, "energy.fuel_update", "fuel_record", rec.id, before=before,
                 after=_snapshot(rec, _FUEL_FIELDS))
    db.commit()
    return rec


@router.delete("/fuel/{record_id}", status_code=204, summary="Ta bort tankning")
def delete_fuel(record_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> Response:
    rec = _owned_fuel(db, principal, record_id)
    audit.record(db, principal.actor, "energy.fuel_delete", "fuel_record", rec.id, before=_snapshot(rec, _FUEL_FIELDS))
    db.delete(rec)
    db.commit()
    return Response(status_code=204)


# ----------------------------------------------------------------------------- laddningar


@router.get("/charging", response_model=list[ChargingOut], summary="Laddningar")
def list_charging(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> list[ChargingRecord]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    rows = energy_stats.charging_records(db, vehicle.id, start, end)
    db.commit()
    return rows


@router.post("/charging", response_model=ChargingOut, status_code=201, summary="Registrera laddning")
def create_charging(body: ChargingIn, principal: PrincipalDep, db: DbDep) -> ChargingRecord:
    vehicle = get_vehicle(db, principal, body.vehicle_id)
    rec = ChargingRecord(vehicle_id=vehicle.id, source="manual", **body.model_dump(exclude={"vehicle_id"}))
    db.add(rec)
    db.flush()
    audit.record(db, principal.actor, "energy.charging_create", "charging_record", rec.id,
                 after=_snapshot(rec, _CHARGE_FIELDS))
    db.commit()
    return rec


@router.get("/charging/{record_id}", response_model=ChargingOut, summary="En laddning")
def get_charging(record_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> ChargingRecord:
    return _owned_charging(db, principal, record_id)


@router.patch("/charging/{record_id}", response_model=ChargingOut, summary="Ändra laddning")
def update_charging(record_id: uuid.UUID, body: ChargingPatch, principal: PrincipalDep, db: DbDep) -> ChargingRecord:
    rec = _owned_charging(db, principal, record_id)
    changes = body.model_dump(exclude_unset=True)
    for key in ("started_at", "kwh", "kwh_quality"):
        if key in changes and changes[key] is None:
            raise bad_request("Fältet får inte tömmas.", "invalid_value", {"field": key})
    started = changes.get("started_at", rec.started_at)
    ended = changes.get("ended_at", rec.ended_at)
    if ended is not None and started is not None and ended < started:
        raise bad_request("Sluttiden får inte ligga före starttiden.", "invalid_value")
    before = _snapshot(rec, _CHARGE_FIELDS)
    for key, value in changes.items():
        setattr(rec, key, value)
    db.flush()
    audit.record(db, principal.actor, "energy.charging_update", "charging_record", rec.id, before=before,
                 after=_snapshot(rec, _CHARGE_FIELDS))
    db.commit()
    return rec


@router.delete("/charging/{record_id}", status_code=204, summary="Ta bort laddning")
def delete_charging(record_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> Response:
    rec = _owned_charging(db, principal, record_id)
    audit.record(db, principal.actor, "energy.charging_delete", "charging_record", rec.id,
                 before=_snapshot(rec, _CHARGE_FIELDS))
    db.delete(rec)
    db.commit()
    return Response(status_code=204)
