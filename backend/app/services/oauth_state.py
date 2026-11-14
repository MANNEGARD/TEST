"""Kortlivat OAuth-tillstånd (state + PKCE) som lagras på serversidan."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import OAuthState
from app.security import crypto

STATE_TTL_MINUTES = 10


class OAuthStateError(Exception):
    """Ogiltigt, förbrukat eller för gammalt state-värde."""


def pkce_pair() -> tuple[str, str]:
    """Returnerar (verifier, challenge) enligt RFC 7636, metod S256."""
    verifier = crypto.random_token(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def create(db: Session, *, user_id: uuid.UUID, provider: str, verifier: str | None) -> str:
    """Skapar ett engångs-state och returnerar klartexten (endast hash lagras)."""
    db.execute(delete(OAuthState).where(OAuthState.expires_at <= utcnow()))
    state = crypto.random_token(32)
    db.add(
        OAuthState(
            state_hash=crypto.sha256_hex(state),
            provider=provider,
            user_id=user_id,
            verifier_encrypted=crypto.encrypt_text(verifier) if verifier else None,
            expires_at=utcnow() + timedelta(minutes=STATE_TTL_MINUTES),
        )
    )
    db.flush()
    return state


def consume(db: Session, *, state: str | None, provider: str, user_id: uuid.UUID) -> str | None:
    """Verifierar och förbrukar state. Returnerar PKCE-verifieraren."""
    if not state:
        raise OAuthStateError("State saknas.")
    row = db.execute(select(OAuthState).where(OAuthState.state_hash == crypto.sha256_hex(state))).scalar_one_or_none()
    if row is None:
        raise OAuthStateError("Okänt eller redan använt state.")
    verifier = crypto.decrypt_text(row.verifier_encrypted) if row.verifier_encrypted else None
    valid = row.provider == provider and row.user_id == user_id and row.expires_at > utcnow()
    db.delete(row)  # engångsvärde – förbrukas även vid fel
    db.flush()
    if not valid:
        raise OAuthStateError("State är ogiltigt eller har gått ut.")
    return verifier
