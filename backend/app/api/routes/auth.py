"""Autentisering: första-gången-uppsättning, inloggning, sessioner och API-tokens."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AnyPrincipalDep, DbDep, WebPrincipalDep, client_ip
from app.api.errors import ApiError, bad_request, not_found
from app.config import get_config
from app.db import utcnow
from app.models import ApiToken, User
from app.security import passwords, ratelimit, sessions
from app.services import audit, vehicles
from app.services.export import links

router = APIRouter(prefix="/auth", tags=["Authentication"])

ALLOWED_TOKEN_SCOPES = {"ha"}

_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """Hash för okända konton, beräknad en gång så att båda vägarna kostar en verifiering."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = passwords.hash_password("dummy-password-for-timing")
    return _DUMMY_HASH


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1000)


class SetupRequest(Credentials):
    display_name: str = Field(default="", max_length=200)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


class SetupStatus(BaseModel):
    needs_setup: bool


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=1000)
    new_password: str = Field(max_length=1000)


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["ha"])


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreated(TokenOut):
    token: str  # visas endast en gång


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin)


def _set_cookie(response: Response, token: str) -> None:
    cfg = get_config()
    response.set_cookie(
        cfg.cookie_name,
        token,
        max_age=cfg.session_ttl_hours * 3600,
        httponly=True,
        secure=cfg.use_secure_cookies,
        samesite="lax",
        path="/",
    )


def _validate_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise bad_request("Ange en giltig e-postadress.", "invalid_email")
    return email


@router.get("/setup-status", response_model=SetupStatus, summary="Behöver första kontot skapas?")
def setup_status(db: DbDep) -> SetupStatus:
    count = db.scalar(select(func.count()).select_from(User)) or 0
    return SetupStatus(needs_setup=count == 0)


@router.post("/setup", response_model=SessionOut, summary="Skapa första administratörskontot")
def setup(body: SetupRequest, request: Request, response: Response, db: DbDep) -> SessionOut:
    if not get_config().web_setup:
        raise ApiError(
            403,
            "Första kontot skapas inte här i den här installationen. Fyll i administratörens e-post och "
            "lösenord i inställningarna (Home Assistant: appens Konfiguration) och starta om.",
            "setup_disabled",
        )
    ip = client_ip(request)
    allowed, wait = ratelimit.hit(f"setup:{ip}", 10, 600)
    if not allowed:
        raise ApiError(429, f"För många försök. Vänta {wait} sekunder.", "rate_limited")
    if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
        raise ApiError(409, "Systemet är redan uppsatt. Logga in i stället.", "already_setup")
    email = _validate_email(body.email)
    problem = passwords.validate_password_strength(body.password)
    if problem:
        raise bad_request(problem, "weak_password")
    user = User(
        email=email,
        display_name=body.display_name.strip() or email.split("@")[0],
        password_hash=passwords.hash_password(body.password),
        is_admin=True,
    )
    db.add(user)
    db.flush()
    vehicles.default_vehicle(db, user.id)
    vehicles.default_driver(db, user.id, user.display_name)
    token, sess = sessions.create_session(db, user, user_agent=request.headers.get("user-agent", ""), ip=ip)
    audit.record(db, audit.user_actor(user.id, ip), "auth.setup", "user", user.id)
    db.commit()
    _set_cookie(response, token)
    return SessionOut(user=_user_out(user), csrf_token=sess.csrf_token)


@router.post("/login", response_model=SessionOut, summary="Logga in")
def login(body: Credentials, request: Request, response: Response, db: DbDep) -> SessionOut:
    cfg = get_config()
    ip = client_ip(request)
    email = body.email.strip().lower()
    limit, window = cfg.login_rate_limit_attempts, cfg.login_rate_limit_window_seconds
    ip_key, user_key = f"login:ip:{ip}", f"login:user:{email}"
    # Avvisade (429) försök registreras inte och förlänger alltså inte spärren.
    for key in (ip_key, user_key):
        allowed, wait = ratelimit.check(key, limit, window)
        if not allowed:
            raise ApiError(429, f"För många inloggningsförsök. Vänta {wait} sekunder.", "rate_limited")
    ratelimit.record(ip_key, window)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # Verifiera alltid exakt en hash så att svarstiden inte avslöjar om kontot finns.
    stored = user.password_hash if user else _dummy_hash()
    ok = passwords.verify_password(stored, body.password)
    if user is None or not ok or not user.is_active:
        # Kontospärren räknar bara misslyckade försök.
        ratelimit.record(user_key, window)
        audit.record(db, audit.Actor("user", None, None, ip), "auth.login_failed", "user", None, after={"email": email})
        db.commit()
        raise ApiError(401, "Fel e-postadress eller lösenord.", "invalid_credentials")
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(body.password)
    token, sess = sessions.create_session(db, user, user_agent=request.headers.get("user-agent", ""), ip=ip)
    audit.record(db, audit.user_actor(user.id, ip), "auth.login", "user", user.id)
    db.commit()
    ratelimit.reset(f"login:user:{email}")
    _set_cookie(response, token)
    return SessionOut(user=_user_out(user), csrf_token=sess.csrf_token)


@router.post("/logout", status_code=204, summary="Logga ut")
def logout(response: Response, principal: WebPrincipalDep, db: DbDep) -> Response:
    if principal.session is not None:
        sessions.destroy_session(db, principal.session)
    db.commit()
    response.delete_cookie(get_config().cookie_name, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=SessionOut, summary="Aktuell användare och CSRF-token")
def me(principal: WebPrincipalDep) -> SessionOut:
    assert principal.session is not None
    return SessionOut(user=_user_out(principal.user), csrf_token=principal.session.csrf_token)


@router.post("/change-password", status_code=204, summary="Byt lösenord")
def change_password(body: PasswordChange, principal: WebPrincipalDep, db: DbDep) -> Response:
    user = principal.user
    if not passwords.verify_password(user.password_hash, body.current_password):
        raise bad_request("Nuvarande lösenord stämmer inte.", "invalid_credentials")
    problem = passwords.validate_password_strength(body.new_password)
    if problem:
        raise bad_request(problem, "weak_password")
    user.password_hash = passwords.hash_password(body.new_password)
    # Övriga sessioner loggas ut när lösenordet byts.
    sessions.destroy_all_for_user(db, user.id, except_id=principal.session.id if principal.session else None)
    # Nedladdningslänkar till underlaget slutar gälla (signaturen bygger på link_epoch).
    links.revoke_all(db, user)
    audit.record(db, principal.actor, "auth.password_changed", "user", user.id)
    db.commit()
    return Response(status_code=204)


@router.get("/tokens", response_model=list[TokenOut], summary="Lista API-tokens")
def list_tokens(principal: WebPrincipalDep, db: DbDep) -> list[ApiToken]:
    return list(
        db.execute(select(ApiToken).where(ApiToken.user_id == principal.user.id).order_by(ApiToken.created_at.desc())).scalars()
    )


@router.post("/tokens", response_model=TokenCreated, status_code=201, summary="Skapa API-token (t.ex. för Home Assistant)")
def create_token(body: TokenCreate, principal: WebPrincipalDep, db: DbDep) -> TokenCreated:
    scopes = sorted(set(body.scopes))
    if not scopes or any(s not in ALLOWED_TOKEN_SCOPES for s in scopes):
        raise bad_request("Ogiltig behörighet för token.", "invalid_scope")
    secret, row = sessions.create_api_token(db, principal.user, body.name.strip(), scopes)
    audit.record(db, principal.actor, "auth.token_created", "api_token", row.id, after={"name": row.name, "scopes": scopes})
    db.commit()
    return TokenCreated(
        id=row.id, name=row.name, token_prefix=row.token_prefix, scopes=row.scopes, created_at=row.created_at,
        last_used_at=row.last_used_at, revoked_at=row.revoked_at, token=secret,
    )


@router.delete("/tokens/{token_id}", status_code=204, summary="Återkalla API-token (en återkallad token tas bort)")
def revoke_token(token_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    row = db.get(ApiToken, token_id)
    if row is None or row.user_id != principal.user.id:
        raise not_found("Token")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(db, principal.actor, "auth.token_revoked", "api_token", row.id)
    else:
        # Andra anropet rensar bort den redan återkallade raden ur listan.
        audit.record(db, principal.actor, "auth.token_deleted", "api_token", row.id, before={"name": row.name})
        db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.get("/whoami", summary="Verifiera en API-token (används av Home Assistant-integrationen)")
def whoami(principal: AnyPrincipalDep) -> dict[str, object]:
    return {
        "user": principal.user.display_name or principal.user.email,
        "auth": "token" if principal.api_token else "session",
        "scopes": (principal.api_token.scopes if principal.api_token else ["*"]),
    }
