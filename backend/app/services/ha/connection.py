"""Home Assistant-anslutningen: lagrad konfiguration och aktuell leveranskanal."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import IntegrationConnection
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import integrations
from app.services import settings as settings_svc
from app.services.ha.client import HAClient

CHANNEL_DEMO = "demo"
CHANNEL_PUSH = "ha_push"
CHANNEL_PULL = "ha_pull"
CHANNEL_DIGEST = "digest"

HEARTBEAT_FRESH = timedelta(minutes=10)


@dataclass
class HAState:
    conn: IntegrationConnection | None
    mode: str  # push | pull
    status: str
    base_url: str | None
    token: str | None
    notify_service: str | None

    @property
    def is_demo(self) -> bool:
        return self.status == IntegrationStatus.DEMO

    @property
    def channel(self) -> str | None:
        """Kanalen som notiser levereras genom just nu, eller None om ingen finns."""
        if self.conn is None or self.status == IntegrationStatus.NOT_CONFIGURED:
            return None
        if self.is_demo:
            return CHANNEL_DEMO
        if self.mode == "pull":
            return CHANNEL_PULL
        if self.base_url and self.token and self.notify_service:
            return CHANNEL_PUSH
        return None

    def client(self) -> HAClient:
        assert self.base_url and self.token
        return HAClient(self.base_url, self.token)

    @property
    def heartbeat(self) -> dict[str, Any]:
        return dict((self.conn.config or {}).get("heartbeat") or {}) if self.conn else {}

    def heartbeat_at(self) -> datetime | None:
        raw = self.heartbeat.get("received_at")
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    def heartbeat_fresh(self, now: datetime | None = None) -> bool:
        at = self.heartbeat_at()
        return at is not None and ((now or utcnow()) - at) <= HEARTBEAT_FRESH


def load(db: Session, user_id: uuid.UUID) -> HAState:
    conn = integrations.get(db, user_id, IntegrationProvider.HOME_ASSISTANT)
    mode = settings_svc.get_section(db, user_id, "notifications")["delivery_mode"]
    if conn is None:
        return HAState(None, mode, IntegrationStatus.NOT_CONFIGURED, None, None, None)
    cfg = conn.config or {}
    secrets = integrations.secrets(conn)
    return HAState(
        conn=conn,
        mode=mode,
        status=conn.status,
        base_url=cfg.get("base_url"),
        token=secrets.get("token"),
        notify_service=cfg.get("notify_service"),
    )


def get_or_create(db: Session, user_id: uuid.UUID) -> IntegrationConnection:
    return integrations.get_or_create(db, user_id, IntegrationProvider.HOME_ASSISTANT)


def devices_from_heartbeat(state: HAState) -> list[str]:
    services = state.heartbeat.get("notify_services") or []
    return sorted(s for s in services if isinstance(s, str))


def public_status(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Statusvy för webben. Innehåller aldrig åtkomsttoken."""
    state = load(db, user_id)
    conn = state.conn
    cfg = (conn.config or {}) if conn else {}
    hb = state.heartbeat
    status = state.status
    last_error = conn.last_error if conn else None
    if state.mode == "pull" and status == IntegrationStatus.CONNECTED and not state.heartbeat_fresh():
        # Pull-läget bygger på att integrationen hör av sig – utan livstecken är det
        # inte längre en verifierad anslutning. Frågorna ligger kvar i utkorgen.
        status = IntegrationStatus.ERROR
        last_error = last_error or "Inget livstecken från Home Assistant-integrationen de senaste 10 minuterna."
    return {
        "status": status,
        "demo": state.is_demo,
        "delivery_mode": state.mode,
        "channel": state.channel,
        "base_url": state.base_url,
        "has_token": bool(state.token),
        "notify_service": state.notify_service,
        "ha_version": (conn.capabilities or {}).get("version") if conn else None,
        "location_name": (conn.capabilities or {}).get("location_name") if conn else None,
        "last_success_at": conn.last_success_at if conn else None,
        "last_error": last_error,
        "last_error_at": conn.last_error_at if conn else None,
        "heartbeat": {
            "received_at": hb.get("received_at"),
            "ha_version": hb.get("ha_version"),
            "integration_version": hb.get("integration_version"),
            "notify_services": devices_from_heartbeat(state),
            "queue_length": hb.get("queue_length"),
            "fresh": state.heartbeat_fresh(),
        },
        "last_test_sent_at": cfg.get("last_test_sent_at"),
        "last_test_confirmed_at": cfg.get("last_test_confirmed_at"),
    }
