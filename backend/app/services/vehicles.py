"""Fordon och förare."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Driver, Vehicle

DEMO_VEHICLE_NAME = "Demobil (påhittade data)"


def default_vehicle(db: Session, user_id: uuid.UUID, *, create: bool = True) -> Vehicle:
    """Användarens standardfordon (aldrig demobilen). Skapas vid behov."""
    rows = list(
        db.execute(
            select(Vehicle)
            .where(Vehicle.owner_user_id == user_id, Vehicle.is_active.is_(True), Vehicle.is_demo.is_(False))
            .order_by(Vehicle.is_default.desc(), Vehicle.created_at.asc())
        ).scalars()
    )
    if rows:
        return rows[0]
    if not create:
        raise LookupError("Inget fordon finns.")
    vehicle = Vehicle(owner_user_id=user_id, name="Min bil", is_default=True)
    db.add(vehicle)
    db.flush()
    return vehicle


def demo_vehicle(db: Session, user_id: uuid.UUID, *, create: bool = True) -> Vehicle | None:
    """Separat demofordon – demodata blandas aldrig med verkliga uppgifter."""
    vehicle = db.execute(
        select(Vehicle).where(Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(True)).limit(1)
    ).scalar_one_or_none()
    if vehicle is None and create:
        vehicle = Vehicle(
            owner_user_id=user_id,
            name=DEMO_VEHICLE_NAME,
            make="Volvo",
            model="V90 Recharge T8 (demo)",
            model_year=2021,
            fuel_type="PETROL/ELECTRIC",
            registration_number="DEMO123",
            is_demo=True,
        )
        db.add(vehicle)
        db.flush()
    return vehicle


def default_driver(db: Session, user_id: uuid.UUID, fallback_name: str = "") -> Driver:
    driver = db.execute(
        select(Driver).where(Driver.owner_user_id == user_id).order_by(Driver.is_default.desc(), Driver.created_at.asc())
    ).scalars().first()
    if driver is None:
        driver = Driver(owner_user_id=user_id, name=fallback_name or "Förare", is_default=True)
        db.add(driver)
        db.flush()
    return driver


def mask_vin(vin: str | None) -> str | None:
    if not vin:
        return None
    return vin[:3] + "•" * max(0, len(vin) - 7) + vin[-4:] if len(vin) > 7 else "•" * len(vin)
