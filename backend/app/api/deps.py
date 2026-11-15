"""Gemensamma FastAPI-beroenden: autentisering, CSRF och åtkomstkontroll."""

from __future__ import annotations

import functools
import ipaddress
import os
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, not_found
from app.config import get_config
from app.db import get_db
from app.models import ApiToken, AuthSession, LogicalTrip, User, Vehicle
from app.security import crypto, sessions
from app.services import audit
from app.services.audit import Actor
from app.services.trips import segments as seg

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

DbDep = Annotated[Session, Depends(get_db)]


@dataclass
class Principal:
    user: User
    session: AuthSession | None = None
    api_token: ApiToken | None = None
    ip: str | None = None

    @property
    def actor(self) -> Actor:
        if self.api_token is not None:
            return Actor("ha" if "ha" in (self.api_token.scopes or []) else "user", str(self.user.id), self.user.id, self.ip)
        return audit.user_actor(self.user.id, self.ip)

    def has_scope(self, scope: str) -> bool:
        if self.api_token is None:
            return True
        return scope in (self.api_token.scopes or []) or "*" in (self.api_token.scopes or [])


@functools.lru_cache(maxsize=8)
def _trusted_proxies(raw: str) -> tuple[bool, frozenset[str], tuple]:
    """Tolkar FORWARDED_ALLOW_IPS som uvicorn: kommaseparerade adresser/nät eller "*"."""
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if "*" in items:
        return True, frozenset(), ()
    literals: set[str] = set()
    networks: list = []
    for item in items:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            literals.add(item)  # t.ex. en unix-socket; jämförs som text
    return False, frozenset(literals), tuple(networks)


def _is_trusted_proxy(host: str) -> bool:
    always, literals, networks = _trusted_proxies(os.environ.get("FORWARDED_ALLOW_IPS", "*"))
    if always or host in literals:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def client_ip(request: Request) -> str:
    """Klientens adress.

    X-Forwarded-For används bara när den direkta avsändaren är en betrodd proxy enligt
    FORWARDED_ALLOW_IPS (samma variabel som uvicorn läser; osatt = "*", som i Compose-driften där
    bara proxyn når API:t). Då är det SISTA värdet det som den egna proxyn lade till; värden
    längre till vänster kan klienten själv ha skickat med.

    uvicorn --proxy-headers har redan ersatt request.client med klientens adress när avsändaren var
    betrodd. Den adressen är då (normalt) inte själv en betrodd proxy och används som den är. Har
    uvicorn inte skrivit om den (t.ex. i testerna) gäller samma regel för den riktiga avsändaren.
    Ett anrop direkt mot port 8000 från en obetrodd adress räknas alltså alltid på sin egen adress,
    hur X-Forwarded-For än ser ut.
    """
    peer = request.client.host if request.client else ""
    fwd = request.headers.get("x-forwarded-for")
    if fwd and _is_trusted_proxy(peer):
        last = fwd.split(",")[-1].strip()
        if last:
            return last[:64]
    return peer[:64]


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def get_principal(request: Request, db: DbDep) -> Principal:
    """Autentiserar via sessionskaka (webb) eller Bearer-token (maskin)."""
    cfg = get_config()
    ip = client_ip(request)
    bearer = _bearer(request)
    if bearer:
        token = sessions.resolve_api_token(db, bearer)
        if token is None:
            raise ApiError(401, "Ogiltig eller återkallad API-token.", "invalid_token")
        user = db.get(User, token.user_id)
        if user is None or not user.is_active:
            raise ApiError(401, "Kontot är inte aktivt.", "inactive_user")
        db.commit()
        return Principal(user=user, api_token=token, ip=ip)

    sess = sessions.resolve_session(db, request.cookies.get(cfg.cookie_name))
    if sess is None:
        raise ApiError(401, "Du är inte inloggad.", "not_authenticated")
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise ApiError(401, "Kontot är inte aktivt.", "inactive_user")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token") or ""
        if not sent or not crypto.constant_time_equals(sent, sess.csrf_token):
            raise ApiError(403, "CSRF-kontrollen misslyckades. Ladda om sidan och försök igen.", "csrf_failed")
    db.commit()
    return Principal(user=user, session=sess, ip=ip)


# Rått beroende som godtar både session och API-token. Används ENDAST av routes
# som uttryckligen tillåter maskintokens (require_scope och /auth/whoami).
AnyPrincipalDep = Annotated[Principal, Depends(get_principal)]


def get_session_principal(principal: AnyPrincipalDep) -> Principal:
    """Standardberoendet: API-tokens är spärrade om routen inte uttryckligen tillåter dem.

    En token (t.ex. Home Assistants med scope "ha") når bara routes som använder
    require_scope(...) eller AnyPrincipalDep – aldrig resten av körjournalen.
    """
    if principal.api_token is not None:
        raise ApiError(403, "API-token saknar behörighet för åtgärden.", "insufficient_scope")
    return principal


PrincipalDep = Annotated[Principal, Depends(get_session_principal)]


def get_web_principal(principal: AnyPrincipalDep) -> Principal:
    """Endast inloggade webbanvändare (inte maskintokens)."""
    if principal.api_token is not None:
        raise ApiError(403, "Den här åtgärden kräver inloggning i webbappen.", "session_required")
    return principal


WebPrincipalDep = Annotated[Principal, Depends(get_web_principal)]


def require_scope(scope: str):
    def _dep(principal: AnyPrincipalDep) -> Principal:
        if not principal.has_scope(scope):
            raise ApiError(403, "API-token saknar behörighet för åtgärden.", "insufficient_scope")
        return principal

    return _dep


def get_vehicle(db: Session, principal: Principal, vehicle_id: uuid.UUID | None) -> Vehicle:
    """Hämtar ett fordon som användaren äger; utan id används standardfordonet."""
    if vehicle_id is not None:
        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is None or vehicle.owner_user_id != principal.user.id:
            raise not_found("Fordonet")
        return vehicle
    from app.services import vehicles

    return vehicles.default_vehicle(db, principal.user.id)


def get_owned_trip(db: Session, principal: Principal, trip_id: uuid.UUID) -> LogicalTrip:
    trip = seg.load_trip(db, trip_id)
    if trip is None:
        raise not_found("Resan")
    owner = db.execute(select(Vehicle.owner_user_id).where(Vehicle.id == trip.vehicle_id)).scalar_one_or_none()
    if owner != principal.user.id:
        raise not_found("Resan")
    return trip


def owned_vehicle_ids(db: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    return [v for (v,) in db.execute(select(Vehicle.id).where(Vehicle.owner_user_id == user_id)).all()]
