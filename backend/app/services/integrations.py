"""Gemensam hantering av integrationsanslutningar och deras krypterade hemligheter."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import IntegrationConnection, IntegrationSyncRun
from app.models.enums import IntegrationStatus
from app.security import crypto


def get(db: Session, user_id: uuid.UUID, provider: str) -> IntegrationConnection | None:
    return db.execute(
        select(IntegrationConnection).where(
            IntegrationConnection.user_id == user_id, IntegrationConnection.provider == provider
        )
    ).scalar_one_or_none()


def get_or_create(db: Session, user_id: uuid.UUID, provider: str) -> IntegrationConnection:
    conn = get(db, user_id, provider)
    if conn is None:
        conn = IntegrationConnection(user_id=user_id, provider=provider, status=IntegrationStatus.NOT_CONFIGURED)
        db.add(conn)
        db.flush()
    return conn


def secrets(conn: IntegrationConnection | None) -> dict[str, Any]:
    if conn is None or not conn.secrets_encrypted:
        return {}
    try:
        return crypto.decrypt_json(conn.secrets_encrypted)
    except crypto.SecretDecryptionError:
        return {}


def set_secrets(conn: IntegrationConnection, values: dict[str, Any], *, merge: bool = True) -> None:
    current = secrets(conn) if merge else {}
    for k, v in values.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    conn.secrets_encrypted = crypto.encrypt_json(current) if current else None


def update_config(conn: IntegrationConnection, values: dict[str, Any]) -> None:
    cfg = dict(conn.config or {})
    for k, v in values.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    conn.config = cfg


def mark_success(conn: IntegrationConnection, when: datetime | None = None) -> None:
    now = when or utcnow()
    conn.last_sync_at = now
    conn.last_success_at = now
    conn.last_error = None
    conn.consecutive_failures = 0
    conn.backoff_until = None
    if conn.status in (IntegrationStatus.ERROR, IntegrationStatus.CONFIGURED):
        conn.status = IntegrationStatus.CONNECTED


def mark_error(conn: IntegrationConnection, message: str, *, needs_reauth: bool = False) -> None:
    now = utcnow()
    conn.last_sync_at = now
    conn.last_error = message[:1000]
    conn.last_error_at = now
    conn.consecutive_failures = (conn.consecutive_failures or 0) + 1
    if needs_reauth:
        conn.status = IntegrationStatus.NEEDS_REAUTH
    elif conn.status == IntegrationStatus.CONNECTED and conn.consecutive_failures >= 3:
        conn.status = IntegrationStatus.ERROR


def disconnect(db: Session, conn: IntegrationConnection, *, keep_config: bool = False) -> None:
    """Kopplar bort integrationen och raderar alla lagrade hemligheter."""
    conn.secrets_encrypted = None
    conn.status = IntegrationStatus.NOT_CONFIGURED
    conn.scopes = []
    conn.capabilities = {}
    conn.token_expires_at = None
    conn.last_error = None
    conn.backoff_until = None
    conn.consecutive_failures = 0
    if not keep_config:
        conn.config = {}
    db.flush()


def start_run(db: Session, conn: IntegrationConnection, kind: str) -> IntegrationSyncRun:
    run = IntegrationSyncRun(connection_id=conn.id, kind=kind)
    db.add(run)
    db.flush()
    return run


def finish_run(
    run: IntegrationSyncRun,
    status: str,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    http_status: int | None = None,
) -> None:
    run.finished_at = utcnow()
    run.status = status
    run.stats = stats or {}
    run.error = error[:1000] if error else None
    run.http_status = http_status


def public_view(conn: IntegrationConnection | None, provider: str) -> dict[str, Any]:
    """Säker vy för API/UI – aldrig hemligheter, bara om de finns."""
    if conn is None:
        return {
            "provider": provider,
            "status": IntegrationStatus.NOT_CONFIGURED,
            "config": {},
            "has_secrets": {},
            "scopes": [],
            "capabilities": {},
            "last_sync_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_error_at": None,
            "token_expires_at": None,
        }
    sec = secrets(conn)
    return {
        "provider": provider,
        "status": conn.status,
        "config": conn.config or {},
        "has_secrets": {k: True for k in sec},
        "scopes": conn.scopes or [],
        "capabilities": conn.capabilities or {},
        "last_sync_at": conn.last_sync_at,
        "last_success_at": conn.last_success_at,
        "last_error": conn.last_error,
        "last_error_at": conn.last_error_at,
        "token_expires_at": conn.token_expires_at,
        "backoff_until": conn.backoff_until,
    }
