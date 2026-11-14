"""Microsoft Outlook (Graph): anslutning, kalenderval, synk och matchningar."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, get_owned_trip, get_principal, owned_vehicle_ids
from app.api.errors import ApiError, bad_request, not_found
from app.config import get_config
from app.db import utcnow
from app.models import CalendarEvent, CalendarMatch, Vehicle
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import audit, integrations, oauth_state
from app.services import settings as settings_svc
from app.services.calendar import graph, matching, sync
from app.services.learning import feedback

router = APIRouter(tags=["Calendar matching"])
logger = logging.getLogger(__name__)
PROVIDER = IntegrationProvider.MICROSOFT


class Credentials(BaseModel):
    client_id: str = Field(min_length=8, max_length=100)
    client_secret: str | None = Field(default=None, max_length=500)
    tenant: str = Field(default="common", max_length=100)


class CalendarSelection(BaseModel):
    calendar_ids: list[str] = Field(max_length=50)


class MatchChoice(BaseModel):
    selected: bool


def _calendar_error(exc: sync.CalendarError) -> ApiError:
    status = {"not_connected": 409, "not_configured": 409, "needs_reauth": 409}.get(exc.code, 502)
    return ApiError(status, exc.message, exc.code)


def _rematch(db, principal, since_days: int = 120) -> int:
    total = 0
    for vid in owned_vehicle_ids(db, principal.user.id):
        vehicle = db.get(Vehicle, vid)
        if vehicle is None:
            continue
        total += matching.match_vehicle(
            db, user_id=principal.user.id, vehicle=vehicle, since=utcnow() - timedelta(days=since_days)
        )
        feedback.after_decisions(db, user_id=principal.user.id, vehicle_id=vid, actor=audit.SYSTEM)
    return total


@router.get("/integrations/microsoft/status", summary="Status för Outlook-anslutningen", tags=["Integration health"])
def status(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    conn = integrations.get(db, principal.user.id, PROVIDER)
    view = integrations.public_view(conn, PROVIDER)
    cal = settings_svc.get_section(db, principal.user.id, "calendar")
    events = db.execute(
        select(CalendarEvent.id).where(CalendarEvent.user_id == principal.user.id, CalendarEvent.is_demo.is_(False),
                                       CalendarEvent.removed_at.is_(None))
    ).all()
    view.update(
        {
            "redirect_uri": sync.redirect_uri(),
            "required_permissions": ["Calendars.Read", "offline_access"],
            "selected_calendar_ids": cal["calendar_ids"],
            "stored_events": len(events),
            "stores_body_preview": cal["store_body_preview"],
        }
    )
    return view


@router.put("/integrations/microsoft/credentials", summary="Spara Microsoft-appens uppgifter", tags=["Integration health"])
def save_credentials(body: Credentials, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    conn = integrations.get(db, principal.user.id, PROVIDER)
    if not body.client_secret and not integrations.secrets(conn).get("client_secret"):
        raise bad_request("Ange klienthemligheten för Microsoft-appen.")
    try:
        graph._tenant(body.tenant)
    except graph.GraphError as exc:
        raise bad_request(str(exc)) from exc
    conn = sync.save_credentials(
        db, principal.user.id, client_id=body.client_id, client_secret=body.client_secret, tenant=body.tenant
    )
    audit.record(db, principal.actor, "integration.credentials", "integration", conn.id, after={"provider": PROVIDER})
    db.commit()
    return integrations.public_view(conn, PROVIDER)


@router.post("/integrations/microsoft/connect", summary="Starta OAuth-inloggning mot Microsoft", tags=["Integration health"])
def connect(principal: WebPrincipalDep, db: DbDep) -> dict[str, str]:
    try:
        url = sync.start_authorization(db, principal.user.id)
    except sync.CalendarError as exc:
        raise _calendar_error(exc) from exc
    db.commit()
    return {"authorize_url": url}


@router.get("/integrations/microsoft/callback", include_in_schema=False)
def callback(
    request: Request, db: DbDep, code: str | None = None, state: str | None = None,
    error: str | None = None, error_description: str | None = None,
) -> RedirectResponse:
    """OAuth-redirect från Microsoft (webbläsar-GET med sessionskaka).

    Utan giltig session görs ingenting – webbläsaren skickas tillbaka med ett fel.
    """
    base = get_config().base_url.rstrip("/")
    try:
        principal = get_principal(request, db)
    except ApiError:
        return RedirectResponse(f"{base}/integrations?microsoft=error&reason=not_logged_in", status_code=302)
    if principal.api_token is not None:
        return RedirectResponse(f"{base}/integrations?microsoft=error&reason=session_required", status_code=302)
    if error or not code:
        logger.info("Microsoft-inloggningen avbröts: %s", error)
        return RedirectResponse(f"{base}/integrations?microsoft=error&reason={(error or 'missing_code')[:40]}", status_code=302)
    try:
        conn = sync.complete_authorization(db, principal.user.id, code=code, state=state or "")
        audit.record(db, principal.actor, "integration.connected", "integration", conn.id, after={"provider": PROVIDER})
        db.commit()
    except oauth_state.OAuthStateError:
        db.commit()
        return RedirectResponse(f"{base}/integrations?microsoft=error&reason=state", status_code=302)
    except (sync.CalendarError, graph.GraphError) as exc:
        db.rollback()
        logger.warning("Microsoft-anslutningen misslyckades: %s", exc)
        return RedirectResponse(f"{base}/integrations?microsoft=error&reason=token", status_code=302)
    return RedirectResponse(f"{base}/integrations?microsoft=connected", status_code=302)


@router.get("/integrations/microsoft/calendars", summary="Tillgängliga kalendrar", tags=["Integration health"])
def calendars(principal: WebPrincipalDep, db: DbDep) -> list[dict[str, Any]]:
    try:
        out = sync.list_calendars(db, principal.user.id)
    except sync.CalendarError as exc:
        db.commit()
        raise _calendar_error(exc) from exc
    db.commit()
    return out


@router.put("/integrations/microsoft/calendars", summary="Välj vilka kalendrar som ska användas", tags=["Integration health"])
def select_calendars(body: CalendarSelection, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    settings_svc.update_section(db, principal.user.id, "calendar", {"calendar_ids": body.calendar_ids})
    db.commit()
    return {"calendar_ids": body.calendar_ids}


@router.post("/integrations/microsoft/sync", summary="Synka kalendern nu och matcha mot resor", tags=["Integration health"])
def sync_now(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    try:
        result = sync.sync_user(db, principal.user.id)
    except sync.CalendarError as exc:
        db.commit()
        raise _calendar_error(exc) from exc
    result["trips_with_match"] = _rematch(db, principal)
    db.commit()
    return result


@router.post("/integrations/microsoft/disconnect", summary="Koppla bort Outlook och radera tokens och kalenderposter",
             tags=["Integration health"])
def disconnect(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    sync.disconnect(db, principal.user.id, delete_events=True)
    _rematch(db, principal, since_days=3650)
    audit.record(db, principal.actor, "integration.disconnected", "integration", None, after={"provider": PROVIDER})
    db.commit()
    return integrations.public_view(integrations.get(db, principal.user.id, PROVIDER), PROVIDER)


@router.post("/integrations/microsoft/demo/enable", summary="Demoläge: påhittade kalenderposter för demobilen",
             tags=["Integration health"])
def demo_enable(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    from app.services.calendar import demo

    result = demo.enable(db, principal.user.id)
    db.commit()
    return result


@router.post("/integrations/microsoft/demo/disable", summary="Stäng av kalenderns demoläge", tags=["Integration health"])
def demo_disable(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    from app.services.calendar import demo

    result = demo.disable(db, principal.user.id)
    db.commit()
    return result


@router.get("/calendar/events", summary="Lagrade kalenderhändelser i ett intervall")
def events(
    principal: PrincipalDep, db: DbDep,
    start: datetime | None = Query(default=None, alias="from"), end: datetime | None = Query(default=None, alias="to"),
    demo: bool = False, limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    start = start or utcnow() - timedelta(days=14)
    end = end or utcnow() + timedelta(days=14)
    rows = db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == principal.user.id, CalendarEvent.is_demo.is_(demo),
            CalendarEvent.removed_at.is_(None), CalendarEvent.end_at >= start, CalendarEvent.start_at <= end,
        ).order_by(CalendarEvent.start_at).limit(limit)
    ).scalars()
    return [
        {"id": str(e.id), "subject": e.subject, "start_at": e.start_at, "end_at": e.end_at, "is_all_day": e.is_all_day,
         "is_online": e.is_online, "location": e.location_name, "city": e.location_city, "show_as": e.show_as,
         "is_private": (e.sensitivity or "").lower() in ("private", "personal"), "is_demo": e.is_demo}
        for e in rows
    ]


@router.post("/trips/{trip_id}/calendar-matches/refresh", summary="Räkna om kalenderträffar för en resa")
def refresh_matches(trip_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    trip = get_owned_trip(db, principal, trip_id)
    vehicle = db.get(Vehicle, trip.vehicle_id)
    rows = matching.match_trip(db, trip, user_id=principal.user.id, is_demo=bool(vehicle and vehicle.is_demo))
    feedback.after_decisions(db, user_id=principal.user.id, vehicle_id=trip.vehicle_id, actor=audit.SYSTEM)
    db.commit()
    return {"matches": len(rows), "selected": sum(1 for m in rows if m.is_selected)}


@router.post("/trips/{trip_id}/calendar-matches/{match_id}", summary="Välj eller avvisa en kalenderträff")
def choose_match(trip_id: uuid.UUID, match_id: uuid.UUID, body: MatchChoice, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    trip = get_owned_trip(db, principal, trip_id)
    m = db.get(CalendarMatch, match_id)
    if m is None or m.logical_trip_id != trip.id:
        raise not_found("Kalenderträffen")
    matching.set_user_choice(db, trip, m, selected=body.selected)
    audit.record(db, principal.actor, "calendar.match_choice", "logical_trip", trip.id,
                 after={"event_id": str(m.calendar_event_id), "selected": body.selected})
    feedback.after_decisions(db, user_id=principal.user.id, vehicle_id=trip.vehicle_id, actor=audit.SYSTEM)
    db.commit()
    return {"id": str(m.id), "is_selected": m.is_selected, "decided_by": m.decided_by}


__all__ = ["router", "IntegrationStatus"]
