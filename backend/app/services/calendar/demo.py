"""Demoläge för kalendern: påhittade bokningar som bara matchas mot demobilens resor."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CalendarEvent, CalendarMatch, KnownLocation
from app.models.enums import IntegrationProvider, IntegrationStatus, LocationType
from app.services import audit, integrations, vehicles
from app.services.calendar import matching
from app.services.trips import segments as seg

DEMO_CALENDAR_ID = "demo"
DEMO_SUBJECTS = ("Kundmöte (demo)", "Avstämning (demo)", "Verksamhetsuppföljning (demo)", "Personalmöte (demo)")


def enable(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Skapar demobokningar vid demobilens ankomster till kontor/kunder.

    En riktig anslutning skrivs aldrig över och visas aldrig som demo.
    """
    conn = integrations.get_or_create(db, user_id, IntegrationProvider.MICROSOFT)
    if conn.status in (IntegrationStatus.NOT_CONFIGURED, IntegrationStatus.CONFIGURED):
        conn.status = IntegrationStatus.DEMO
    vehicle = vehicles.demo_vehicle(db, user_id, create=True)
    assert vehicle is not None
    created = 0
    locs = {
        loc.id: loc
        for loc in db.execute(select(KnownLocation).where(KnownLocation.owner_user_id == user_id)).scalars()
    }
    for i, trip in enumerate(seg.active_trips(db, vehicle.id)):
        loc = locs.get(trip.end_location_id) if trip.end_location_id else None
        if trip.ended_at is None or loc is None or loc.type not in (LocationType.OFFICE, LocationType.CUSTOMER):
            continue
        ext = f"demo-{trip.id}"
        exists = db.execute(
            select(CalendarEvent.id).where(CalendarEvent.user_id == user_id, CalendarEvent.external_id == ext)
        ).first()
        if exists:
            continue
        start = (trip.ended_at + timedelta(minutes=20)).replace(second=0, microsecond=0)
        db.add(
            CalendarEvent(
                user_id=user_id, calendar_id=DEMO_CALENDAR_ID, external_id=ext,
                subject=DEMO_SUBJECTS[i % len(DEMO_SUBJECTS)], start_at=start, end_at=start + timedelta(hours=1),
                location_name=loc.name, location_city=loc.city, is_demo=True, show_as="busy", sensitivity="normal",
            )
        )
        created += 1
    db.flush()
    matched = matching.match_vehicle(db, user_id=user_id, vehicle=vehicle)
    from app.services.learning import feedback

    feedback.after_decisions(db, user_id=user_id, vehicle_id=vehicle.id, actor=audit.SYSTEM)
    return {"status": conn.status, "events_created": created, "trips_with_match": matched, "is_demo": True}


def disable(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    removed = 0
    for ev in db.execute(
        select(CalendarEvent).where(CalendarEvent.user_id == user_id, CalendarEvent.is_demo.is_(True))
    ).scalars():
        for m in db.execute(select(CalendarMatch).where(CalendarMatch.calendar_event_id == ev.id)).scalars():
            db.delete(m)
        db.delete(ev)
        removed += 1
    conn = integrations.get(db, user_id, IntegrationProvider.MICROSOFT)
    if conn is not None and conn.status == IntegrationStatus.DEMO:
        has_app = bool((conn.config or {}).get("client_id"))
        conn.status = IntegrationStatus.CONFIGURED if has_app else IntegrationStatus.NOT_CONFIGURED
    db.flush()
    return {"status": conn.status if conn else IntegrationStatus.NOT_CONFIGURED, "events_removed": removed}
