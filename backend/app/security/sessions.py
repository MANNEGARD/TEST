"""Serverlagrade sessioner. Kakan innehåller en slumpad token; databasen lagrar bara dess hash."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import ApiToken, AuthSession, User
from app.security import crypto

API_TOKEN_PREFIX = "dl_"  # noqa: S105 - prefix, ingen hemlighet


def create_session(db: Session, user: User, *, user_agent: str = "", ip: str = "") -> tuple[str, AuthSession]:
    cfg = get_config()
    token = crypto.random_token(32)
    now = utcnow()
    sess = AuthSession(
        user_id=user.id,
        token_hash=crypto.sha256_hex(token),
        csrf_token=crypto.random_token(24),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=cfg.session_ttl_hours),
        user_agent=user_agent[:400],
        ip=ip[:64],
    )
    db.add(sess)
    user.last_login_at = now
    db.flush()
    return token, sess


def resolve_session(db: Session, token: str | None) -> AuthSession | None:
    if not token:
        return None
    sess = db.execute(select(AuthSession).where(AuthSession.token_hash == crypto.sha256_hex(token))).scalar_one_or_none()
    if sess is None:
        return None
    now = utcnow()
    if sess.expires_at <= now:
        db.delete(sess)
        db.flush()
        return None
    if (now - sess.last_seen_at) > timedelta(minutes=5):
        sess.last_seen_at = now
    return sess


def destroy_session(db: Session, sess: AuthSession) -> None:
    db.delete(sess)
    db.flush()


def destroy_all_for_user(db: Session, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None) -> None:
    q = delete(AuthSession).where(AuthSession.user_id == user_id)
    if except_id is not None:
        q = q.where(AuthSession.id != except_id)
    db.execute(q)


def purge_expired(db: Session) -> int:
    result = db.execute(delete(AuthSession).where(AuthSession.expires_at <= utcnow()))
    return result.rowcount or 0


def create_api_token(db: Session, user: User, name: str, scopes: list[str]) -> tuple[str, ApiToken]:
    """Skapar en maskintoken. Klartexten visas endast en gång."""
    secret = API_TOKEN_PREFIX + crypto.random_token(32)
    row = ApiToken(
        user_id=user.id,
        name=name[:120],
        token_prefix=secret[:10],
        token_hash=crypto.sha256_hex(secret),
        scopes=scopes,
    )
    db.add(row)
    db.flush()
    return secret, row


def resolve_api_token(db: Session, secret: str | None) -> ApiToken | None:
    if not secret or not secret.startswith(API_TOKEN_PREFIX):
        return None
    row = db.execute(select(ApiToken).where(ApiToken.token_hash == crypto.sha256_hex(secret))).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    now = utcnow()
    if row.last_used_at is None or (now - row.last_used_at) > timedelta(minutes=5):
        row.last_used_at = now
    return row
