"""OAuth 2.0 mot Volvo ID: authorization code + PKCE (S256) med klienthemlighet.

* ``state`` lagras endast som hash, PKCE-verifieraren krypterad; båda är kortlivade
  (10 minuter) och kan användas en enda gång.
* Refresh tokens roterar – den nya sparas och committas direkt så att den inte kan
  gå förlorad om en senare del av körningen misslyckas.
* ``invalid_grant`` (utgången/återkallad behörighet, max ca 3 månader) → status
  ``needs_reauth``; användaren måste ansluta igen.
* Testtoken-läge: en kortlivad access token från utvecklarportalen, utan förnyelse.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import IntegrationConnection, OAuthState
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.security import crypto
from app.services import integrations
from app.services.volvo import client as volvo_client
from app.services.volvo import constants as C
from app.services.volvo.client import VolvoAuthError, VolvoError

logger = logging.getLogger(__name__)

STATE_TTL = timedelta(minutes=10)
EXPIRY_SKEW = timedelta(seconds=60)
GRANT_MAX_LIFETIME = timedelta(days=90)
TOKEN_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def redirect_uri() -> str:
    cfg = get_config()
    return f"{cfg.base_url.rstrip('/')}{cfg.api_prefix}/integrations/volvo/callback"


def connection(db: Session, user_id: uuid.UUID, *, create: bool = True) -> IntegrationConnection | None:
    if create:
        return integrations.get_or_create(db, user_id, IntegrationProvider.VOLVO)
    return integrations.get(db, user_id, IntegrationProvider.VOLVO)


def auth_urls(conn: IntegrationConnection) -> dict[str, str]:
    server = (conn.config or {}).get("auth_server") or C.DEFAULT_AUTH_SERVER
    return C.AUTH_SERVERS.get(server, C.AUTH_SERVERS[C.DEFAULT_AUTH_SERVER])


def requested_scopes(conn: IntegrationConnection) -> list[str]:
    scopes = (conn.config or {}).get("requested_scopes")
    if isinstance(scopes, list) and scopes:
        return [str(s) for s in scopes]
    return list(C.DEFAULT_SCOPES)


def has_real_credentials(conn: IntegrationConnection | None) -> bool:
    """Finns riktiga anslutningsuppgifter eller tokens (till skillnad från demoläge)?"""
    if conn is None:
        return False
    sec = integrations.secrets(conn)
    return any(sec.get(k) for k in ("client_secret", "vcc_api_key", "access_token", "refresh_token"))


def save_credentials(
    db: Session,
    conn: IntegrationConnection,
    *,
    client_id: str,
    client_secret: str | None,
    vcc_api_key: str | None,
    auth_server: str | None = None,
    scopes: list[str] | None = None,
) -> IntegrationConnection:
    """Sparar utvecklarapplikationens uppgifter. Hemligheter krypteras; tomt värde = behåll."""
    client_id = client_id.strip()
    if not client_id:
        raise VolvoError("Client ID måste anges.", code="invalid_credentials")
    if auth_server is not None and auth_server not in C.AUTH_SERVERS:
        raise VolvoError("Okänd inloggningsserver.", code="invalid_credentials")
    updates: dict[str, Any] = {}
    if client_secret:
        updates["client_secret"] = client_secret.strip()
    if vcc_api_key:
        updates["vcc_api_key"] = vcc_api_key.strip()
    if updates:
        integrations.set_secrets(conn, updates)
    sec = integrations.secrets(conn)
    if not sec.get("client_secret") or not sec.get("vcc_api_key"):
        raise VolvoError("Client secret och VCC API-nyckel måste anges.", code="invalid_credentials")
    previous_client = (conn.config or {}).get("client_id")
    cfg_updates: dict[str, Any] = {"client_id": client_id, "mode": C.MODE_OAUTH}
    if auth_server is not None:
        cfg_updates["auth_server"] = auth_server
    if scopes is not None:
        cfg_updates["requested_scopes"] = [s for s in dict.fromkeys(scopes) if s] or None
    integrations.update_config(conn, cfg_updates)
    if previous_client and previous_client != client_id:
        # Ny klient → gamla tokens gäller inte längre.
        integrations.set_secrets(conn, {"access_token": None, "refresh_token": None})
        conn.token_expires_at = None
    has_tokens = bool(integrations.secrets(conn).get("refresh_token"))
    if conn.status in (IntegrationStatus.NOT_CONFIGURED, IntegrationStatus.DEMO) or not has_tokens:
        conn.status = IntegrationStatus.CONFIGURED  # uppgifter sparade, ingen verifierad anslutning än
    conn.last_error = None
    db.flush()
    return conn


# ------------------------------------------------------------------ PKCE och state


def _pkce_pair() -> tuple[str, str]:
    verifier = crypto.random_token(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_authorization(db: Session, conn: IntegrationConnection, user_id: uuid.UUID) -> str:
    """Skapar state + PKCE och returnerar adressen användaren ska skickas till."""
    client_id = (conn.config or {}).get("client_id")
    sec = integrations.secrets(conn)
    if not client_id or not sec.get("client_secret") or not sec.get("vcc_api_key"):
        raise VolvoError("Spara Client ID, Client secret och VCC API-nyckel först.", code="not_configured")
    purge_expired_states(db)
    state = crypto.random_token(32)
    verifier, challenge = _pkce_pair()
    now = utcnow()
    db.add(
        OAuthState(
            state_hash=crypto.sha256_hex(state),
            provider=IntegrationProvider.VOLVO,
            user_id=user_id,
            verifier_encrypted=crypto.encrypt_text(verifier),
            created_at=now,
            expires_at=now + STATE_TTL,
        )
    )
    db.flush()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "scope": " ".join(requested_scopes(conn)),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{auth_urls(conn)['authorize']}?{urlencode(params)}"


def purge_expired_states(db: Session) -> None:
    db.execute(delete(OAuthState).where(OAuthState.expires_at <= utcnow()))


def _consume_state(db: Session, user_id: uuid.UUID, state: str | None) -> str:
    """Validerar och förbrukar state. Returnerar PKCE-verifieraren."""
    if not state:
        raise VolvoError("Svaret från Volvo saknar state.", code="invalid_state")
    row = db.execute(
        select(OAuthState).where(
            OAuthState.state_hash == crypto.sha256_hex(state), OAuthState.provider == IntegrationProvider.VOLVO
        )
    ).scalar_one_or_none()
    if row is None:
        raise VolvoError("Inloggningen kunde inte verifieras (okänt eller redan använt state).", code="invalid_state")
    expired = row.expires_at <= utcnow()
    wrong_user = row.user_id != user_id
    verifier_enc = row.verifier_encrypted
    db.delete(row)  # engångs – även vid fel
    db.flush()
    if wrong_user:
        raise VolvoError("Inloggningen påbörjades av ett annat konto.", code="invalid_state")
    if expired or not verifier_enc:
        raise VolvoError("Inloggningen tog för lång tid. Försök igen.", code="state_expired")
    return crypto.decrypt_text(verifier_enc)


def discard_state(db: Session, user_id: uuid.UUID, state: str | None) -> None:
    """Förbrukar state utan tokenutbyte (t.ex. när användaren avbröt hos Volvo)."""
    try:
        _consume_state(db, user_id, state)
    except VolvoError:
        pass


# ------------------------------------------------------------------ token-endpoint


def _token_request(
    conn: IntegrationConnection, data: dict[str, str], *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """POST mot token-endpointen med HTTP Basic (client_id:client_secret)."""
    client_id = (conn.config or {}).get("client_id") or ""
    client_secret = integrations.secrets(conn).get("client_secret") or ""
    if not client_id or not client_secret:
        raise VolvoError("Client ID och Client secret saknas.", code="not_configured")
    transport = transport if transport is not None else volvo_client.HTTP_TRANSPORT
    try:
        with httpx.Client(timeout=TOKEN_TIMEOUT, transport=transport) as http:
            resp = http.post(
                auth_urls(conn)["token"],
                data=data,
                auth=(client_id, client_secret),
                headers={"accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise VolvoError("Kunde inte nå Volvos inloggningsserver.", code="network") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
        return body
    error = body.get("error") if isinstance(body, dict) else None
    if error == "invalid_grant":
        raise VolvoAuthError("Volvo-behörigheten har gått ut eller återkallats. Anslut igen.", http_status=resp.status_code)
    if error in ("invalid_client", "unauthorized_client") or resp.status_code == 401:
        raise VolvoError(
            "Volvo avvisade klientuppgifterna. Kontrollera Client ID och Client secret.",
            code="invalid_client",
            http_status=resp.status_code,
        )
    if resp.status_code == 429:
        raise volvo_client.VolvoThrottled()
    raise VolvoError(
        f"Volvos inloggningsserver svarade med fel ({error or resp.status_code}).",
        code="token_error",
        http_status=resp.status_code,
    )


def _store_tokens(db: Session, conn: IntegrationConnection, body: dict[str, Any], now: datetime) -> str:
    access = str(body["access_token"])
    updates: dict[str, Any] = {"access_token": access}
    if body.get("refresh_token"):
        updates["refresh_token"] = str(body["refresh_token"])  # rotation: gamla är nu ogiltig
    integrations.set_secrets(conn, updates)
    try:
        expires_in = int(body.get("expires_in") or 300)
    except (TypeError, ValueError):
        expires_in = 300
    conn.token_expires_at = now + timedelta(seconds=max(30, expires_in))
    scope = body.get("scope")
    if isinstance(scope, str) and scope.strip():
        conn.scopes = scope.split()
    db.flush()
    db.commit()  # den roterade refresh token får aldrig gå förlorad
    return access


def complete_authorization(
    db: Session,
    user_id: uuid.UUID,
    *,
    state: str | None,
    code: str | None,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> IntegrationConnection:
    """Byter auktoriseringskoden mot tokens och markerar anslutningen som ansluten."""
    now = now or utcnow()
    try:
        verifier = _consume_state(db, user_id, state)
    finally:
        db.commit()  # state är förbrukat oavsett hur det går
    if not code:
        raise VolvoError("Svaret från Volvo saknar auktoriseringskod.", code="missing_code")
    conn = connection(db, user_id)
    assert conn is not None
    try:
        body = _token_request(
            conn,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
                "code_verifier": verifier,
            },
            transport=transport,
        )
    except VolvoError as exc:
        integrations.mark_error(conn, exc.message)
        db.commit()
        raise
    if not conn.scopes:
        conn.scopes = requested_scopes(conn)
    integrations.update_config(
        conn, {"mode": C.MODE_OAUTH, "grant_started_at": now.isoformat(), "connected_at": now.isoformat()}
    )
    conn.status = IntegrationStatus.CONNECTED
    conn.last_error = None
    conn.consecutive_failures = 0
    conn.backoff_until = None
    _store_tokens(db, conn, body, now)
    return conn


def refresh_access_token(
    db: Session,
    conn: IntegrationConnection,
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
    failed_token: str | None = None,
) -> str:
    """Förnyar access token. Vid ``invalid_grant`` krävs ny inloggning.

    ``failed_token`` anges efter ett 401-svar: då förnyas alltid, såvida inte en annan
    arbetare redan hunnit byta till en annan token.
    """
    now = now or utcnow()
    # Lås raden så att två arbetare inte förbrukar samma roterande refresh token.
    db.flush()
    db.execute(select(IntegrationConnection.id).where(IntegrationConnection.id == conn.id).with_for_update())
    db.refresh(conn)
    sec = integrations.secrets(conn)
    if (conn.config or {}).get("mode") == C.MODE_TEST_TOKEN:
        _require_reauth(db, conn, "Testtoken har gått ut. Skapa en ny i Volvos utvecklarportal eller anslut med Volvo ID.")
    current = sec.get("access_token")
    still_valid = conn.token_expires_at is not None and conn.token_expires_at - EXPIRY_SKEW > now
    if current and still_valid and current != failed_token:
        return str(current)  # en annan arbetare hann förnya
    refresh_token = sec.get("refresh_token")
    if not refresh_token:
        _require_reauth(db, conn, "Ingen giltig Volvo-behörighet finns. Anslut igen.")
    try:
        body = _token_request(
            conn, {"grant_type": "refresh_token", "refresh_token": str(refresh_token)}, transport=transport
        )
    except VolvoAuthError as exc:
        _require_reauth(db, conn, exc.message)
    return _store_tokens(db, conn, body, now)


def _require_reauth(db: Session, conn: IntegrationConnection, message: str) -> None:
    integrations.set_secrets(conn, {"access_token": None, "refresh_token": None})
    conn.token_expires_at = None
    integrations.mark_error(conn, message, needs_reauth=True)
    db.flush()
    db.commit()
    raise VolvoAuthError(message)


def get_access_token(
    db: Session, conn: IntegrationConnection, *, now: datetime | None = None, transport: httpx.BaseTransport | None = None
) -> str:
    """Giltig access token; förnyas automatiskt strax innan den går ut."""
    now = now or utcnow()
    sec = integrations.secrets(conn)
    access = sec.get("access_token")
    if (conn.config or {}).get("mode") == C.MODE_TEST_TOKEN:
        if not access:
            raise VolvoAuthError("Ingen testtoken är sparad.")
        return str(access)
    if access and conn.token_expires_at and conn.token_expires_at - EXPIRY_SKEW > now:
        return str(access)
    return refresh_access_token(db, conn, now=now, transport=transport)


def set_test_token(db: Session, conn: IntegrationConnection, *, access_token: str, vcc_api_key: str | None) -> None:
    """Testtoken-läge: kortlivad token från utvecklarportalen, ingen automatisk förnyelse."""
    access_token = access_token.strip()
    if len(access_token) < 20:
        raise VolvoError("Testtoken ser inte giltig ut.", code="invalid_credentials")
    updates: dict[str, Any] = {"access_token": access_token, "refresh_token": None}
    if vcc_api_key:
        updates["vcc_api_key"] = vcc_api_key.strip()
    integrations.set_secrets(conn, updates)
    if not integrations.secrets(conn).get("vcc_api_key"):
        raise VolvoError("VCC API-nyckel måste anges.", code="invalid_credentials")
    integrations.update_config(conn, {"mode": C.MODE_TEST_TOKEN, "test_token_set_at": utcnow().isoformat()})
    conn.token_expires_at = None
    conn.scopes = []
    conn.status = IntegrationStatus.CONFIGURED  # blir CONNECTED först när ett riktigt anrop lyckats
    conn.last_error = None
    conn.backoff_until = None
    db.flush()


def reauth_due_at(conn: IntegrationConnection) -> datetime | None:
    """Senaste tidpunkt då användaren måste logga in igen (max 3 månader per behörighet)."""
    started = (conn.config or {}).get("grant_started_at")
    if not started or (conn.config or {}).get("mode") != C.MODE_OAUTH:
        return None
    try:
        return datetime.fromisoformat(started) + GRANT_MAX_LIFETIME
    except ValueError:
        return None


def is_test_token_mode(conn: IntegrationConnection | None) -> bool:
    return conn is not None and (conn.config or {}).get("mode") == C.MODE_TEST_TOKEN


def disconnect(db: Session, conn: IntegrationConnection) -> None:
    """Kopplar bort Volvo-kontot: alla hemligheter och tokens raderas.

    Fordonen i DriveLog och redan insamlade observationer/resor finns kvar, men
    kopplingen till Volvo-kontot tas bort. Ett aktiverat demoläge påverkas inte.
    """
    from app.models import Vehicle

    demo_cfg = (conn.config or {}).get("demo")
    integrations.disconnect(db, conn)
    for vehicle in db.execute(
        select(Vehicle).where(Vehicle.volvo_connection_id == conn.id, Vehicle.is_demo.is_(False))
    ).scalars():
        vehicle.volvo_connection_id = None
    db.execute(delete(OAuthState).where(OAuthState.user_id == conn.user_id, OAuthState.provider == IntegrationProvider.VOLVO))
    conn.last_sync_at = conn.last_success_at = None
    if demo_cfg:
        integrations.update_config(conn, {"demo": demo_cfg})
        if demo_cfg.get("enabled"):
            conn.status = IntegrationStatus.DEMO
    db.flush()
