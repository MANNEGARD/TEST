"""Radering av historik (sekretess). Raderingen är permanent och loggas."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    BusinessJourney,
    CalendarEvent,
    CalendarMatch,
    ChargingRecord,
    DataGap,
    ExportJob,
    FuelRecord,
    ImportBatch,
    ImportedFile,
    LearningFeedback,
    LocationPattern,
    LogicalTrip,
    MergeDecision,
    NotificationRequest,
    RawTrip,
    ReviewItem,
    TripMerge,
    Vehicle,
    VehicleObservation,
    VehicleOdometerSnapshot,
)
from app.services import audit
from app.services.audit import Actor

SCOPES = ("observations", "calendar", "exports", "trips", "notifications")


def delete_history(db: Session, *, user_id: uuid.UUID, scope: str, vehicle: Vehicle | None, actor: Actor) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError("Okänt omfång.")
    removed: dict[str, int] = {}
    if scope == "observations":
        assert vehicle is not None
        removed["observations"] = db.execute(
            delete(VehicleObservation).where(VehicleObservation.vehicle_id == vehicle.id)
        ).rowcount or 0
    elif scope == "calendar":
        ids = [i for (i,) in db.execute(select(CalendarEvent.id).where(CalendarEvent.user_id == user_id)).all()]
        if ids:
            db.execute(delete(CalendarMatch).where(CalendarMatch.calendar_event_id.in_(ids)))
        removed["calendar_events"] = db.execute(delete(CalendarEvent).where(CalendarEvent.user_id == user_id)).rowcount or 0
    elif scope == "exports":
        n = 0
        for job in db.execute(select(ExportJob).where(ExportJob.user_id == user_id)).scalars():
            if job.file_path:
                try:
                    Path(job.file_path).unlink(missing_ok=True)
                except OSError:
                    pass
            db.delete(job)
            n += 1
        removed["exports"] = n
    elif scope == "notifications":
        n = 0
        for req in db.execute(select(NotificationRequest).where(NotificationRequest.user_id == user_id)).scalars():
            db.delete(req)
            n += 1
        removed["notifications"] = n
    elif scope == "trips":
        assert vehicle is not None
        vid = vehicle.id
        trip_ids = [i for (i,) in db.execute(select(LogicalTrip.id).where(LogicalTrip.vehicle_id == vid)).all()]
        if trip_ids:
            db.execute(delete(LearningFeedback).where(LearningFeedback.logical_trip_id.in_(trip_ids)))
            db.execute(delete(NotificationRequest).where(NotificationRequest.logical_trip_id.in_(trip_ids)))
        db.execute(delete(ReviewItem).where(ReviewItem.vehicle_id == vid))
        db.execute(delete(DataGap).where(DataGap.vehicle_id == vid))
        db.execute(delete(TripMerge).where(TripMerge.vehicle_id == vid))
        db.execute(delete(MergeDecision).where(MergeDecision.vehicle_id == vid))
        # ORM-radering så att segment, klassningar m.m. följer med via kaskad.
        for trip in db.execute(select(LogicalTrip).where(LogicalTrip.vehicle_id == vid)).scalars():
            trip.merged_into_id = None
        db.flush()
        n_trips = 0
        for trip in db.execute(select(LogicalTrip).where(LogicalTrip.vehicle_id == vid)).scalars():
            db.delete(trip)
            n_trips += 1
        db.flush()
        removed["trips"] = n_trips
        removed["raw_trips"] = db.execute(delete(RawTrip).where(RawTrip.vehicle_id == vid)).rowcount or 0
        db.execute(delete(BusinessJourney).where(BusinessJourney.vehicle_id == vid))
        db.execute(delete(FuelRecord).where(FuelRecord.vehicle_id == vid))
        db.execute(delete(ChargingRecord).where(ChargingRecord.vehicle_id == vid))
        db.execute(delete(VehicleOdometerSnapshot).where(VehicleOdometerSnapshot.vehicle_id == vid))
        files = 0
        for batch in db.execute(select(ImportBatch).where(ImportBatch.vehicle_id == vid)).scalars():
            for f in db.execute(select(ImportedFile).where(ImportedFile.batch_id == batch.id)).scalars():
                if f.stored_path:
                    other = db.execute(
                        select(ImportedFile.id).where(ImportedFile.stored_path == f.stored_path, ImportedFile.batch_id != batch.id).limit(1)
                    ).first()
                    if not other:
                        try:
                            Path(f.stored_path).unlink(missing_ok=True)
                            files += 1
                        except OSError:
                            pass
            db.delete(batch)
        removed["import_files"] = files
        db.execute(delete(LocationPattern).where(LocationPattern.owner_user_id == user_id, LocationPattern.is_manual_rule.is_(False)))
    audit.record(db, actor, "privacy.delete_history", "user", user_id,
                 after={"scope": scope, "vehicle_id": str(vehicle.id) if vehicle else None, "removed": removed})
    db.flush()
    return removed
