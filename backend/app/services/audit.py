"""Spårbarhet: alla viktiga ändringar loggas som AuditEvent."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


@dataclass(frozen=True)
class Actor:
    type: str  # user | system | ai | ha | import
    id: str | None = None
    user_id: uuid.UUID | None = None
    ip: str | None = None

    @property
    def label(self) -> str:
        return f"{self.type}:{self.id}" if self.id else self.type


SYSTEM = Actor("system", "system")


def user_actor(user_id: uuid.UUID, ip: str | None = None) -> Actor:
    return Actor("user", str(user_id), user_id, ip)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def record(
    db: Session,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    user_id: uuid.UUID | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        actor_type=actor.type,
        actor_id=actor.id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=_jsonable(before) if before is not None else None,
        after=_jsonable(after) if after is not None else None,
        ip=actor.ip,
        user_id=user_id or actor.user_id,
    )
    db.add(ev)
    return ev
