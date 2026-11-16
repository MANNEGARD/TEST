"""Synkronisering av Outlook-kalendern via Microsoft Graph."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import CalendarEvent, IntegrationConnection
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import integrations, oauth_state
from app.services import settings as settings_svc
from app.services.calendar import graph
from app.services.calendar.ics import ICS_CALENDAR_ID

logger = logging.getLogger(__name__)

PROVIDER = IntegrationProvider.MICROSOFT
REFRESH_MARGIN = timedelta(minutes=5)
ONLINE_PROVIDERS = {"teamsforbusiness", "skypeforbusiness", "skypeforconsumer"}


class CalendarError(Exception):
    def __init__(self, message: str, code: str = "calendar_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def redirect_uri() -> str:
    cfg = get_config()
    return f"{cfg.base_url.rstrip('/')}{cfg.api_prefix}/integrations/microsoft/callback"


def save_credentials(db: Session, user_id: uuid.UUID, *, client_id: str, client_secret: str | None, tenant: str) -> IntegrationConnection:
    conn = integrations.get_or_create(db, user_id, PROVIDER)
    integrations.update_config(conn, {"client_id": client_id.strip(), "tenant": tenant.strip() or "common"})
    if client_secret:
        integrations.set_secrets(conn, {"client_secret": client_secret.strip()})
    if conn.status in (IntegrationStatus.NOT_CONFIGURED, IntegrationStatus.DEMO):
        conn.status = IntegrationStatus.CONFIGURED
    db.flush()
    return conn


def start_authorization(db: Session, user_id: uuid.UUID) -> str:
    conn = integrations.get(db, user_id, PROVIDER)
    cfg = (conn.config or {}) if conn else {}
    if conn is None or not cfg.get("client_id") or not integrations.secrets(conn).get("client_secret"):
        raise CalendarError("Ange först applikations-ID och klienthemlighet för Microsoft-appen.", "not_configured")
    verifier, challenge = oauth_state.pkce_pair()
    state = oauth_state.create(db, user_id=user_id, provider=PROVIDER, verifier=verifier)
    return graph.authorize_url(
        client_id=cfg["client_id"], tenant=cfg.get("tenant"), redirect_uri=redirect_uri(), state=state,
        code_challenge=challenge,
    )


def complete_authorization(
    db: Session, user_id: uuid.UUID, *, code: str, state: str, transport: httpx.BaseTransport | None = None
) -> IntegrationConnection:
    verifier = oauth_state.consume(db, state=state, provider=PROVIDER, user_id=user_id)
    conn = integrations.get(db, user_id, PROVIDER)
    if conn is None:
        raise CalendarError("Integrationen är inte konfigurerad.", "not_configured")
    cfg, sec = conn.config or {}, integrations.secrets(conn)
    tokens = graph.exchange_code(
        client_id=cfg["client_id"], client_secret=sec["client_secret"], tenant=cfg.get("tenant"), code=code,
        redirect_uri=redirect_uri(), code_verifier=verifier, transport=transport,
    )
    if not tokens.refresh_token:
        raise CalendarError("Microsoft lämnade ingen uppdateringstoken (offline_access saknas).", "no_refresh_token")
    _store_tokens(conn, tokens)
    conn.status = IntegrationStatus.CONNECTED
    conn.scopes = sorted(set(tokens.scope.split())) if tokens.scope else list(graph.SCOPES)
    conn.last_error = None
    conn.consecutive_failures = 0
    if tokens.account:
        integrations.update_config(conn, {"account": tokens.account})
    db.flush()
    return conn


def _store_tokens(conn: IntegrationConnection, tokens: graph.TokenSet) -> None:
    values: dict[str, Any] = {"access_token": tokens.access_token}
    if tokens.refresh_token:  # Microsoft roterar uppdateringstoken – spara alltid den nya
        values["refresh_token"] = tokens.refresh_token
    integrations.set_secrets(conn, values)
    conn.token_expires_at = tokens.expires_at


def access_token(db: Session, conn: IntegrationConnection, *, transport: httpx.BaseTransport | None = None) -> str:
    """Giltig åtkomsttoken; förnyas automatiskt strax innan den går ut."""
    sec = integrations.secrets(conn)
    if not sec.get("refresh_token"):
        integrations.mark_error(conn, "Uppdateringstoken saknas.", needs_reauth=True)
        raise CalendarError("Kalendern behöver anslutas på nytt.", "needs_reauth")
    if sec.get("access_token") and conn.token_expires_at and conn.token_expires_at - utcnow() > REFRESH_MARGIN:
        return sec["access_token"]
    cfg = conn.config or {}
    try:
        tokens = graph.refresh_tokens(
            client_id=cfg["client_id"], client_secret=sec["client_secret"], tenant=cfg.get("tenant"),
            refresh_token=sec["refresh_token"], transport=transport,
        )
    except graph.GraphAuthError as exc:
        integrations.mark_error(conn, str(exc), needs_reauth=True)
        db.flush()
        raise CalendarError("Kalendern behöver anslutas på nytt.", "needs_reauth") from exc
    _store_tokens(conn, tokens)
    db.flush()
    return tokens.access_token


def list_calendars(db: Session, user_id: uuid.UUID, *, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    conn = _connected(db, user_id)
    try:
        client = graph.GraphClient(access_token(db, conn, transport=transport), transport=transport)
        try:
            calendars = client.list_calendars()
        finally:
            client.close()
    except graph.GraphAuthError as exc:
        integrations.mark_error(conn, str(exc), needs_reauth=True)
        db.flush()
        raise CalendarError("Kalendern behöver anslutas på nytt.", "needs_reauth") from exc
    except graph.GraphThrottled as exc:
        raise CalendarError("Microsoft begränsar just nu antalet anrop. Försök igen om en stund.", "throttled") from exc
    except graph.GraphError as exc:
        integrations.mark_error(conn, str(exc))
        db.flush()
        raise CalendarError(str(exc), "graph_error") from exc
    selected = set(settings_svc.get_section(db, user_id, "calendar")["calendar_ids"])
    for c in calendars:
        c["selected"] = c["id"] in selected or (not selected and c["is_default"])
    return calendars


def _connected(db: Session, user_id: uuid.UUID) -> IntegrationConnection:
    conn = integrations.get(db, user_id, PROVIDER)
    if conn is None or conn.status not in (IntegrationStatus.CONNECTED, IntegrationStatus.ERROR):
        raise CalendarError("Outlook är inte anslutet.", "not_connected")
    return conn


def _event_values(raw: dict[str, Any], include_body: bool) -> dict[str, Any] | None:
    start, end = graph.parse_graph_datetime(raw.get("start")), graph.parse_graph_datetime(raw.get("end"))
    if start is None or end is None or not raw.get("id"):
        return None
    loc = raw.get("location") or {}
    address = loc.get("address") or {}
    coords = loc.get("coordinates") or {}
    street = " ".join(p for p in [address.get("street")] if p)
    addr_text = ", ".join(p for p in [street, address.get("postalCode"), address.get("city")] if p) or None
    provider = (raw.get("onlineMeetingProvider") or "").lower()
    lat, lon = coords.get("latitude"), coords.get("longitude")
    return {
        "external_id": str(raw["id"])[:400],
        "ical_uid": (raw.get("iCalUId") or None),
        "subject": (raw.get("subject") or "")[:500],
        "start_at": start,
        "end_at": max(end, start),
        "is_all_day": bool(raw.get("isAllDay")),
        "is_cancelled": bool(raw.get("isCancelled")),
        "show_as": raw.get("showAs"),
        "sensitivity": raw.get("sensitivity"),
        "is_online": bool(raw.get("isOnlineMeeting")) or provider in ONLINE_PROVIDERS,
        "online_provider": raw.get("onlineMeetingProvider"),
        "location_name": (loc.get("displayName") or None),
        "location_address": addr_text,
        "location_city": address.get("city") or None,
        "latitude": float(lat) if isinstance(lat, (int, float)) else None,
        "longitude": float(lon) if isinstance(lon, (int, float)) else None,
        "body_preview": (raw.get("bodyPreview") or None) if include_body else None,
        "series_master_id": raw.get("seriesMasterId"),
    }


def sync_user(
    db: Session, user_id: uuid.UUID, *, now: datetime | None = None, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Hämtar kalenderhändelser i det konfigurerade fönstret och uppdaterar lagrade poster."""
    now = now or utcnow()
    conn = _connected(db, user_id)
    cal = settings_svc.get_section(db, user_id, "calendar")
    if conn.backoff_until and conn.backoff_until > now:
        return {"status": "throttled", "retry_at": conn.backoff_until.isoformat()}
    run = integrations.start_run(db, conn, "calendar_sync")
    start = now - timedelta(days=int(cal["sync_days_back"]))
    end = now + timedelta(days=int(cal["sync_days_forward"]))
    include_body = bool(cal["store_body_preview"])
    stats = {"created": 0, "updated": 0, "removed": 0, "calendars": 0}
    try:
        client = graph.GraphClient(access_token(db, conn, transport=transport), transport=transport)
        try:
            calendar_ids = list(cal["calendar_ids"])
            if not calendar_ids:
                calendar_ids = [c["id"] for c in client.list_calendars() if c["is_default"]][:1]
            for cal_id in calendar_ids:
                raw_events = client.calendar_view(cal_id, start, end, include_body_preview=include_body)
                stats["calendars"] += 1
                _upsert_events(db, user_id, conn, cal_id, raw_events, start, end, include_body, stats)
            stats["removed"] += retire_unselected(db, user_id, calendar_ids)
        finally:
            client.close()
    except graph.GraphThrottled as exc:
        conn.backoff_until = now + timedelta(seconds=max(30.0, exc.retry_after))
        integrations.finish_run(run, "throttled", stats=stats, error=str(exc), http_status=429)
        db.flush()
        return {"status": "throttled", "retry_at": conn.backoff_until.isoformat()}
    except graph.GraphAuthError as exc:
        integrations.mark_error(conn, str(exc), needs_reauth=True)
        integrations.finish_run(run, "error", stats=stats, error=str(exc), http_status=exc.status)
        db.flush()
        raise CalendarError("Kalendern behöver anslutas på nytt.", "needs_reauth") from exc
    except CalendarError as exc:
        integrations.finish_run(run, "error", stats=stats, error=exc.message)
        db.flush()
        raise
    except graph.GraphError as exc:
        integrations.mark_error(conn, str(exc))
        integrations.finish_run(run, "error", stats=stats, error=str(exc), http_status=exc.status)
        db.flush()
        raise CalendarError(str(exc), "graph_error") from exc
    integrations.mark_success(conn, now)
    integrations.finish_run(run, "ok", stats=stats)
    db.flush()
    return {"status": "ok", **stats}


def _upsert_events(
    db: Session,
    user_id: uuid.UUID,
    conn: IntegrationConnection,
    calendar_id: str,
    raw_events: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    include_body: bool,
    stats: dict[str, int],
) -> None:
    existing = {
        e.external_id: e
        for e in db.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == user_id,
                CalendarEvent.calendar_id == calendar_id,
                CalendarEvent.is_demo.is_(False),
                CalendarEvent.end_at >= start,
                CalendarEvent.start_at <= end,
            )
        ).scalars()
    }
    seen: set[str] = set()
    now = utcnow()
    for raw in raw_events:
        values = _event_values(raw, include_body)
        if values is None:
            continue
        seen.add(values["external_id"])
        ev = existing.get(values["external_id"])
        if ev is None:
            ev = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.user_id == user_id,
                    CalendarEvent.calendar_id == calendar_id,
                    CalendarEvent.external_id == values["external_id"],
                )
            ).scalar_one_or_none()
        if ev is None:
            ev = CalendarEvent(user_id=user_id, connection_id=conn.id, calendar_id=calendar_id, **values)
            db.add(ev)
            stats["created"] += 1
        else:
            for k, v in values.items():
                setattr(ev, k, v)
            ev.removed_at = None
            stats["updated"] += 1
        ev.fetched_at = now
    for ext_id, ev in existing.items():
        if ext_id not in seen and ev.removed_at is None:
            ev.removed_at = now  # borttagen eller flyttad ur fönstret i Outlook
            stats["removed"] += 1
    db.flush()


def retire_unselected(db: Session, user_id: uuid.UUID, calendar_ids: list[str]) -> int:
    """Markerar lagrade händelser från kalendrar som inte längre är valda som borttagna,
    så att de inte påverkar matchning och klassning. Returnerar antal markerade."""
    if not calendar_ids:
        return 0
    now = utcnow()
    count = 0
    for ev in db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == user_id,
            CalendarEvent.is_demo.is_(False),
            CalendarEvent.removed_at.is_(None),
            CalendarEvent.calendar_id.not_in(calendar_ids),
            CalendarEvent.calendar_id != ICS_CALENDAR_ID,  # kalenderlänkens händelser är en egen källa
        )
    ).scalars():
        ev.removed_at = now
        count += 1
    db.flush()
    return count


def disconnect(db: Session, user_id: uuid.UUID, *, delete_events: bool = True, forget_app: bool = False) -> None:
    """Kopplar bort Outlook och raderar tokens (och som standard lagrade kalenderposter).

    Med ``forget_app`` raderas även Microsoft-appens uppgifter (klient-ID och klienthemlighet).
    """
    conn = integrations.get(db, user_id, PROVIDER)
    if conn is None:
        return
    if delete_events:
        for ev in db.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == user_id, CalendarEvent.is_demo.is_(False),
                CalendarEvent.calendar_id != ICS_CALENDAR_ID,  # kalenderlänken kopplas bort för sig
            )
        ).scalars():
            db.delete(ev)
    client_cfg = {k: v for k, v in (conn.config or {}).items() if k in ("client_id", "tenant")}
    secret = integrations.secrets(conn).get("client_secret")
    if forget_app:
        client_cfg, secret = {}, None
    integrations.disconnect(db, conn)
    conn.config = client_cfg
    if secret:  # appens klienthemlighet behålls så att man kan ansluta igen; tokens raderas
        integrations.set_secrets(conn, {"client_secret": secret}, merge=False)
        conn.status = IntegrationStatus.CONFIGURED
    db.flush()


ERROR_RETRY_INTERVAL = timedelta(hours=2)


def sync_all_due(db: Session, *, now: datetime | None = None, min_interval_minutes: int = 30) -> dict[str, int]:
    """Synkar alla anslutna kalendrar som inte synkats nyligen (anropas av bakgrundsjobb).

    Anslutningar med status ERROR (t.ex. efter tre nätverksfel i rad) försöks igen, men
    glesare, så att synken inte stannar tyst tills ägaren klickar "Synka nu" (fynd 21).
    NEEDS_REAUTH försöks inte: där behövs en ny inloggning. Efter en lyckad synk
    matchas resorna mot kalendern och klassas om, som efter "Synka nu" (fynd 20).
    """
    now = now or utcnow()
    done = failed = 0
    conns = db.execute(
        select(IntegrationConnection).where(
            IntegrationConnection.provider == PROVIDER,
            IntegrationConnection.status.in_((IntegrationStatus.CONNECTED, IntegrationStatus.ERROR)),
        )
    ).scalars().all()
    for conn in conns:
        interval = timedelta(minutes=min_interval_minutes)
        if conn.status == IntegrationStatus.ERROR:
            interval = max(interval, ERROR_RETRY_INTERVAL)
        if conn.last_sync_at and now - conn.last_sync_at < interval:
            continue
        user_id = conn.user_id
        try:
            sync_user(db, user_id, now=now)
            db.commit()  # händelserna och statusen sparas även om omklassningen skulle fallera
        except CalendarError as exc:
            logger.warning("Kalendersynk misslyckades: %s", exc.message)
            failed += 1
            db.commit()
            continue
        done += 1
        rematch_after_sync(db, user_id, now)
    return {"synced": done, "failed": failed}


def rematch_after_sync(db: Session, user_id: uuid.UUID, now: datetime) -> None:
    """Matchar ägarens riktiga fordons resor mot kalendern och klassar om efter en lyckad
    schemalagd synk (Graph eller kalenderlänk). Ett fel här rullas tillbaka och loggas; den
    redan sparade synken står kvar."""
    from app.models import Vehicle
    from app.services import audit
    from app.services.calendar import matching
    from app.services.learning import feedback

    try:
        for vehicle in db.execute(
            select(Vehicle).where(Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(False))
        ).scalars().all():
            matching.match_vehicle(db, user_id=user_id, vehicle=vehicle, since=now - timedelta(days=90))
            feedback.after_decisions(db, user_id=user_id, vehicle_id=vehicle.id, actor=audit.SYSTEM)
        db.commit()
    except Exception:
        logger.exception("Omklassning efter kalendersynk misslyckades")
        db.rollback()
