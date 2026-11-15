"""Home Assistant: maskin-API för custom-integrationen samt konfiguration i webben.

* ``/ha/*`` anropas av integrationen ``smgd_drivelog`` med Bearer-token (scope "ha").
  All trafik initieras från Home Assistant – ingen publik HA-webhook behövs.
* ``/integrations/home-assistant/*`` används av webbappen (session + CSRF).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import __version__
from app.api.deps import DbDep, Principal, WebPrincipalDep, require_scope
from app.api.errors import ApiError, bad_request, conflict
from app.db import utcnow
from app.models import LogicalTrip, NotificationRequest
from app.models.enums import IntegrationProvider, IntegrationStatus, LogicalState, TripSource
from app.services import audit, integrations
from app.services import settings as settings_svc
from app.services.ha import connection, demo, messages, notifier, policy, responses, summaries
from app.services.ha.client import HAError, validate_base_url, validate_notify_service
from app.services.ha.connection import CHANNEL_DEMO, CHANNEL_PULL, CHANNEL_PUSH

router = APIRouter()

HaPrincipal = Annotated[Principal, Depends(require_scope("ha"))]
_SERVICE_RE = re.compile(r"^mobile_app_[a-z0-9_]{1,200}$")
POLL_INTERVAL_SECONDS = 30
DISCONNECTED_KEY = "disconnected"  # satt av "Koppla bort" tills konfigurationen sparas igen


def _origin(url: str | None) -> str | None:
    """Schema, värd och port för en adress (det som avgör vart token skickas)."""
    if not url:
        return None
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}"


# ----------------------------------------------------------------------------- scheman


class CallbackIn(BaseModel):
    public_id: str | None = Field(default=None, max_length=24)
    action: str = Field(min_length=1, max_length=80, description="Åtgärdsnyckel eller helt åtgärds-id (DRIVELOG_…)")
    reply_text: str | None = Field(default=None, max_length=2000)
    event_id: str = Field(min_length=8, max_length=100, description="Unikt id per knapptryckning (idempotensnyckel)")


class CallbackOut(BaseModel):
    outcome: str
    message: str
    public_id: str | None = None
    kind: str | None = None
    status: str | None = None
    follow_up: str | None = None
    notify_user: bool = False


class OutboxItem(BaseModel):
    id: str
    type: str  # notify | clear
    service: str | None
    data: dict[str, Any]


class OutboxOut(BaseModel):
    delivery_mode: str
    items: list[OutboxItem]
    server_time: datetime


class AckFailure(BaseModel):
    id: str = Field(max_length=24)
    error: str = Field(default="", max_length=300)


class AckIn(BaseModel):
    delivered: list[str] = Field(default_factory=list, max_length=100)
    failed: list[AckFailure] = Field(default_factory=list, max_length=100)


class HeartbeatIn(BaseModel):
    ha_version: str | None = Field(default=None, max_length=40)
    integration_version: str | None = Field(default=None, max_length=40)
    notify_services: list[str] = Field(default_factory=list, max_length=100)
    notify_service: str | None = Field(default=None, max_length=220)
    queue_length: int | None = Field(default=None, ge=0, le=100000)


class HeartbeatOut(BaseModel):
    delivery_mode: str
    notify_service: str | None
    poll_interval: int
    server_version: str
    status: str


class ConfigIn(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=1000, description="Long-lived access token (lagras krypterat)")
    clear_token: bool = False
    notify_service: str | None = Field(default=None, max_length=220)
    delivery_mode: str | None = Field(default=None, pattern="^(push|pull)$")


# ----------------------------------------------------------------------------- maskin-API


@router.post("/ha/callback", response_model=CallbackOut, tags=["Home Assistant callbacks"],
             summary="Svar på en notis (knapptryckning eller textsvar) från Home Assistant")
def ha_callback(body: CallbackIn, principal: HaPrincipal, db: DbDep) -> dict[str, Any]:
    result = responses.handle(
        db,
        user_id=principal.user.id,
        public_id=body.public_id or "",
        action=body.action,
        reply_text=body.reply_text,
        idempotency_key=body.event_id,
        source="ha",
    )
    db.commit()
    return result


@router.get("/ha/outbox", response_model=OutboxOut, tags=["Home Assistant callbacks"],
            summary="Notiser som integrationen ska leverera (pull-läge)")
def ha_outbox(principal: HaPrincipal, db: DbDep) -> OutboxOut:
    items = notifier.outbox(db, principal.user.id)
    state = connection.load(db, principal.user.id)
    db.commit()
    return OutboxOut(delivery_mode=state.mode, items=[OutboxItem(**i) for i in items], server_time=utcnow())


@router.post("/ha/outbox/ack", tags=["Home Assistant callbacks"], summary="Kvittera levererade notiser (pull-läge)")
def ha_outbox_ack(body: AckIn, principal: HaPrincipal, db: DbDep) -> dict[str, int]:
    result = notifier.outbox_ack(
        db,
        principal.user.id,
        delivered=body.delivered,
        failed=[f.model_dump() for f in body.failed],
    )
    db.commit()
    return result


@router.post("/ha/heartbeat", response_model=HeartbeatOut, tags=["Home Assistant callbacks"],
             summary="Livstecken från integrationen med HA-version och tillgängliga notify-tjänster")
def ha_heartbeat(body: HeartbeatIn, principal: HaPrincipal, db: DbDep) -> HeartbeatOut:
    now = utcnow()
    conn = connection.get_or_create(db, principal.user.id)
    disconnected = bool((conn.config or {}).get(DISCONNECTED_KEY))
    services = sorted({s[len("notify.") :] if s.startswith("notify.") else s for s in body.notify_services})
    services = [s for s in services if _SERVICE_RE.match(s)][:50]
    integrations.update_config(
        conn,
        {
            "heartbeat": {
                "received_at": now.isoformat(),
                "ha_version": body.ha_version,
                "integration_version": body.integration_version,
                "notify_services": services,
                "queue_length": body.queue_length,
            }
        },
    )
    cfg = conn.config or {}
    # Integrationens eget val av enhet används om webben inte har valt någon.
    if not disconnected and not cfg.get("notify_service") and body.notify_service:
        try:
            integrations.update_config(conn, {"notify_service": validate_notify_service(body.notify_service)})
        except ValueError:
            pass
    state = connection.load(db, principal.user.id)
    # Har användaren kopplat bort Home Assistant i webben får ett livstecken inte återansluta;
    # det sker först när konfigurationen sparas i webben igen.
    if not disconnected:
        if conn.status == IntegrationStatus.NOT_CONFIGURED:
            conn.status = IntegrationStatus.CONFIGURED
        if state.mode == "pull" and conn.status in (IntegrationStatus.CONFIGURED, IntegrationStatus.ERROR):
            # Verifierad kontakt: Home Assistant nådde oss med en giltig token.
            conn.status = IntegrationStatus.CONNECTED
        if state.mode == "pull" and conn.status == IntegrationStatus.CONNECTED:
            integrations.mark_success(conn, now)
    db.commit()
    return HeartbeatOut(
        delivery_mode=state.mode,
        notify_service=(conn.config or {}).get("notify_service"),
        poll_interval=POLL_INTERVAL_SECONDS,
        server_version=__version__,
        status=conn.status,
    )


@router.get("/ha/summary", tags=["Home Assistant callbacks"], summary="Nyckeltal till Home Assistant-sensorer")
def ha_summary(principal: HaPrincipal, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    now = utcnow()
    tz = settings_svc.user_timezone(db, user_id)
    month = summaries.period_summary(db, user_id, policy.local_month_start(now, tz), now)
    vehicle_ids = summaries.real_vehicle_ids(db, user_id)
    last_trip: dict[str, Any] | None = None
    if vehicle_ids:
        trip = db.execute(
            select(LogicalTrip)
            .where(
                LogicalTrip.vehicle_id.in_(vehicle_ids),
                LogicalTrip.state == LogicalState.ACTIVE,
                LogicalTrip.source != TripSource.GAP_FILL,
                LogicalTrip.sort_time.is_not(None),
            )
            .order_by(LogicalTrip.sort_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        if trip is not None:
            origin, dest = messages.trip_labels(db, trip)
            last_trip = {
                "ended_at": trip.ended_at,
                "distance_km": round(trip.distance_km, 1),
                "category": trip.category,
                "review_status": trip.review_status,
                "route": f"{origin} → {dest}",  # platsetiketter, aldrig gatuadresser
            }
    volvo = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    pending = db.scalar(
        select(func.count())
        .select_from(NotificationRequest)
        .where(
            NotificationRequest.user_id == user_id,
            NotificationRequest.status.in_(notifier.OPEN_STATUSES),
            NotificationRequest.kind.in_(notifier.QUESTION_KINDS),
        )
    )
    return {
        "review_count": month["open_review_total"],
        "pending_questions": pending or 0,
        "work_km_month": month["work_km"],
        "private_km_month": month["private_km"],
        "undecided_km_month": month["undecided_km"],
        "trips_month": month["trips"],
        "open_gaps": month["open_gaps"],
        "last_trip": last_trip,
        "last_sync_at": volvo.last_success_at if volvo is not None else None,
        "vehicle_sync_status": volvo.status if volvo is not None else IntegrationStatus.NOT_CONFIGURED,
        "generated_at": now,
    }


# ----------------------------------------------------------------------------- webbkonfiguration

_WEB_TAG = "Integration health"


@router.get("/integrations/home-assistant", tags=[_WEB_TAG], summary="Status för Home Assistant-integrationen")
def ha_status(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    status = connection.public_status(db, principal.user.id)
    status["open_questions"] = (
        db.scalar(
            select(func.count())
            .select_from(NotificationRequest)
            .where(
                NotificationRequest.user_id == principal.user.id,
                NotificationRequest.status.in_(notifier.OPEN_STATUSES),
            )
        )
        or 0
    )
    return status


@router.put("/integrations/home-assistant", tags=[_WEB_TAG], summary="Spara anslutningsuppgifter för Home Assistant")
def ha_configure(body: ConfigIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    conn = connection.get_or_create(db, user_id)
    if conn.status == IntegrationStatus.DEMO:
        raise conflict("Stäng av demoläget innan du ansluter en riktig Home Assistant.", "demo_active")
    changes: dict[str, Any] = {DISCONNECTED_KEY: None}  # att spara är en uttrycklig återanslutning
    reconnecting = bool((conn.config or {}).get(DISCONNECTED_KEY))
    connection_changed = reconnecting
    old_url = (conn.config or {}).get("base_url")
    url_moved = False
    try:
        if body.base_url is not None:
            new_url = validate_base_url(body.base_url)
            connection_changed |= new_url != old_url
            url_moved = bool(old_url) and _origin(new_url) != _origin(old_url)
            changes["base_url"] = new_url
        if body.notify_service is not None:
            changes["notify_service"] = validate_notify_service(body.notify_service) if body.notify_service else None
    except ValueError as exc:
        raise bad_request(str(exc), "invalid_ha_config") from exc
    if body.token is not None and body.token.strip():
        integrations.set_secrets(conn, {"token": body.token.strip()})
        connection_changed = True
    elif body.clear_token or (url_moved and integrations.secrets(conn).get("token")):
        # Den sparade token skickas aldrig till en ny adress: byts värd, port eller schema
        # utan att en ny token anges rensas den och måste anges på nytt.
        integrations.set_secrets(conn, {"token": None})
        connection_changed = True
    integrations.update_config(conn, changes)
    if body.delivery_mode is not None:
        current = settings_svc.get_section(db, user_id, "notifications")["delivery_mode"]
        if current != body.delivery_mode:
            settings_svc.update_section(db, user_id, "notifications", {"delivery_mode": body.delivery_mode})
            connection_changed = True
    if conn.status == IntegrationStatus.NOT_CONFIGURED or connection_changed:
        # Sparade uppgifter är inte detsamma som en verifierad anslutning. I push-läget krävs
        # adress och token; i pull-läget blir anslutningen klar först vid integrationens
        # livstecken.
        state = connection.load(db, user_id)
        if state.mode == "push":
            ready = bool(state.base_url and state.token)
            conn.status = IntegrationStatus.CONFIGURED if ready else IntegrationStatus.NOT_CONFIGURED
        else:
            conn.status = IntegrationStatus.CONNECTED if state.heartbeat_fresh() else IntegrationStatus.NOT_CONFIGURED
        conn.last_error = None
    after: dict[str, Any] = {
        "fields": sorted(k for k, v in body.model_dump().items() if v not in (None, False) and k != "token"),
        "token_changed": bool(body.token),
    }
    if url_moved:
        after.update({"base_url_from": _origin(old_url), "base_url_to": _origin(changes["base_url"]),
                      "token_cleared": not bool(body.token and body.token.strip())})
    audit.record(db, principal.actor, "integration.ha_configured", "integration_connection", conn.id, after=after)
    db.commit()
    return connection.public_status(db, user_id)


@router.post("/integrations/home-assistant/test", tags=[_WEB_TAG], summary="Kontrollera anslutningen till Home Assistant")
def ha_test(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    state = connection.load(db, user_id)
    conn = state.conn
    disconnected = conn is not None and bool((conn.config or {}).get(DISCONNECTED_KEY))
    if conn is None or disconnected or (state.status == IntegrationStatus.NOT_CONFIGURED and state.mode != "pull"):
        raise conflict("Home Assistant är inte konfigurerat ännu.", "not_configured")
    if state.is_demo:
        return {"ok": False, "status": state.status, "message": "Demoläge – ingen riktig anslutning testas."}
    now = utcnow()
    if state.mode == "pull":
        if state.heartbeat_fresh(now):
            conn.status = IntegrationStatus.CONNECTED
            integrations.mark_success(conn, now)
            db.commit()
            return {
                "ok": True, "status": conn.status, "devices": connection.devices_from_heartbeat(state),
                "ha_version": state.heartbeat.get("ha_version"),
                "message": "Home Assistant-integrationen har kontakt med DriveLog.",
            }
        if conn.status == IntegrationStatus.CONNECTED:
            conn.status = IntegrationStatus.ERROR
        integrations.mark_error(conn, "Inget livstecken från Home Assistant-integrationen.")
        db.commit()
        return {
            "ok": False, "status": conn.status, "devices": [],
            "message": "Inget livstecken från Home Assistant-integrationen de senaste 10 minuterna. "
                       "Kontrollera att integrationen SMGD DriveLog är installerad och har rätt adress och token.",
        }
    if not (state.base_url and state.token):
        raise bad_request("Ange adress och åtkomsttoken för Home Assistant först.", "missing_credentials")
    try:
        with state.client() as client:
            info = client.check()
            devices = client.notify_services()
    except HAError as exc:
        integrations.mark_error(conn, exc.message, needs_reauth=exc.kind == "auth")
        if exc.kind != "auth":
            conn.status = IntegrationStatus.ERROR
        db.commit()
        return {"ok": False, "status": conn.status, "devices": [], "message": exc.message}
    conn.capabilities = info
    conn.status = IntegrationStatus.CONNECTED
    integrations.mark_success(conn, now)
    db.commit()
    return {
        "ok": True, "status": conn.status, "devices": devices, "ha_version": info.get("version"),
        "location_name": info.get("location_name"), "message": "Anslutningen till Home Assistant fungerar.",
    }


@router.get("/integrations/home-assistant/devices", tags=[_WEB_TAG], summary="Mobila enheter som kan ta emot notiser")
def ha_devices(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    state = connection.load(db, principal.user.id)
    if state.is_demo:
        return {"source": "demo", "devices": ["mobile_app_demotelefon"], "selected": state.notify_service}
    if state.mode == "pull" or not (state.base_url and state.token):
        return {"source": "heartbeat", "devices": connection.devices_from_heartbeat(state), "selected": state.notify_service}
    try:
        with state.client() as client:
            devices = client.notify_services()
    except HAError as exc:
        raise ApiError(502, exc.message, "ha_unavailable") from exc
    return {"source": "home_assistant", "devices": devices, "selected": state.notify_service}


@router.post("/integrations/home-assistant/test-notification", tags=[_WEB_TAG],
             summary="Skicka en testnotis med svarsknapp")
def ha_test_notification(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    state = connection.load(db, user_id)
    channel = state.channel
    if channel is None:
        raise conflict(
            "Anslut Home Assistant och välj en mobil enhet innan du skickar en testnotis.", "no_channel"
        )
    now = utcnow()
    req = notifier.create_test(db, user_id, now)
    delivered = notifier.deliver_now(db, req, now)
    if state.conn is not None:
        integrations.update_config(state.conn, {"last_test_sent_at": now.isoformat(), "last_test_confirmed_at": None})
    db.commit()
    if channel == CHANNEL_PUSH:
        message = (
            "Testnotisen skickades. Tryck på Fungerar i notisen för att bekräfta att svaren når fram."
            if delivered else f"Testnotisen kunde inte levereras: {req.last_error or 'okänt fel'}"
        )
    elif channel == CHANNEL_PULL:
        message = "Testnotisen hämtas av Home Assistant-integrationen inom en minut."
    else:
        message = "Demoläge: testnotisen visas på den simulerade telefonen."
    return {
        "public_id": req.public_id, "channel": channel, "delivered": bool(delivered and channel != CHANNEL_PULL),
        "queued": channel == CHANNEL_PULL, "demo": channel == CHANNEL_DEMO, "message": message,
    }


@router.post("/integrations/home-assistant/disconnect", tags=[_WEB_TAG], summary="Koppla bort Home Assistant")
def ha_disconnect(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    conn = integrations.get(db, user_id, IntegrationProvider.HOME_ASSISTANT)
    if conn is not None:
        notifier.cancel_all_open(db, user_id)
        integrations.disconnect(db, conn)
        integrations.update_config(conn, {DISCONNECTED_KEY: True})
        audit.record(db, principal.actor, "integration.ha_disconnected", "integration_connection", conn.id)
    db.commit()
    return connection.public_status(db, user_id)


@router.post("/integrations/home-assistant/demo/enable", tags=[_WEB_TAG], summary="Slå på demoläge (simulerad telefon)")
def ha_demo_enable(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    demo.enable(db, principal.user.id)
    audit.record(db, principal.actor, "integration.ha_demo_enabled", "integration_connection", None)
    db.commit()
    return connection.public_status(db, principal.user.id)


@router.post("/integrations/home-assistant/demo/disable", tags=[_WEB_TAG], summary="Stäng av demoläge")
def ha_demo_disable(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    demo.disable(db, principal.user.id)
    audit.record(db, principal.actor, "integration.ha_demo_disabled", "integration_connection", None)
    db.commit()
    return connection.public_status(db, principal.user.id)
