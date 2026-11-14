"""Microsoft Graph: OAuth 2.0 (authorization code + PKCE) och kalenderläsning.

Minsta nödvändiga behörighet: ``Calendars.Read`` (delegerad) + ``offline_access``.
Tenant ``common`` stöder både privata Microsoft-konton och arbets-/skolkonton.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ("offline_access", "Calendars.Read", "openid", "email")
EVENT_FIELDS = (
    "subject", "start", "end", "isAllDay", "isCancelled", "showAs", "sensitivity", "type", "seriesMasterId",
    "location", "locations", "isOnlineMeeting", "onlineMeetingProvider", "iCalUId",
)
PAGE_SIZE = 200
MAX_PAGES = 50
VALID_TENANTS = ("common", "organizations", "consumers")


class GraphError(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class GraphAuthError(GraphError):
    """Åtkomsten är ogiltig eller återkallad – användaren måste ansluta igen."""


class GraphThrottled(GraphError):
    def __init__(self, retry_after: float):
        super().__init__("Microsoft Graph begränsar anropstakten.", status=429)
        self.retry_after = retry_after


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scope: str
    account: str | None = None


def _tenant(tenant: str | None) -> str:
    t = (tenant or "common").strip()
    if t in VALID_TENANTS:
        return t
    # tenant-id (GUID) eller verifierad domän
    if all(c.isalnum() or c in "-." for c in t) and 3 <= len(t) <= 100:
        return t
    raise GraphError("Ogiltigt tenant-värde.")


def authorize_url(*, client_id: str, tenant: str | None, redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{AUTHORITY}/{_tenant(tenant)}/oauth2/v2.0/authorize?{urlencode(params)}"


def _account_from_id_token(id_token: str | None) -> str | None:
    """Läser kontonamnet ur id-token (endast för visning; token kom direkt från Microsoft över TLS)."""
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    return claims.get("preferred_username") or claims.get("email")


def _token_request(
    *, tenant: str | None, data: dict[str, str], transport: httpx.BaseTransport | None = None
) -> TokenSet:
    url = f"{AUTHORITY}/{_tenant(tenant)}/oauth2/v2.0/token"
    try:
        with httpx.Client(timeout=20.0, transport=transport) as client:
            resp = client.post(url, data=data)
    except httpx.HTTPError as exc:
        raise GraphError("Kunde inte nå Microsofts inloggningstjänst.") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise GraphError("Oväntat svar från Microsofts inloggningstjänst.", status=resp.status_code) from exc
    if resp.status_code >= 400:
        code = body.get("error", "")
        if code in ("invalid_grant", "interaction_required", "consent_required", "invalid_client", "unauthorized_client"):
            raise GraphAuthError(f"Microsoft avvisade inloggningen ({code}).", status=resp.status_code)
        raise GraphError(f"Fel från Microsofts inloggningstjänst ({code or resp.status_code}).", status=resp.status_code)
    if "access_token" not in body:
        raise GraphError("Svaret saknade åtkomsttoken.")
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 3600))),
        scope=body.get("scope", ""),
        account=_account_from_id_token(body.get("id_token")),
    )


def exchange_code(
    *, client_id: str, client_secret: str, tenant: str | None, code: str, redirect_uri: str, code_verifier: str | None,
    transport: httpx.BaseTransport | None = None,
) -> TokenSet:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    return _token_request(tenant=tenant, data=data, transport=transport)


def refresh_tokens(
    *, client_id: str, client_secret: str, tenant: str | None, refresh_token: str,
    transport: httpx.BaseTransport | None = None,
) -> TokenSet:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(SCOPES),
    }
    return _token_request(tenant=tenant, data=data, transport=transport)


class GraphClient:
    def __init__(self, access_token: str, *, transport: httpx.BaseTransport | None = None, max_retries: int = 2):
        self._client = httpx.Client(
            timeout=30.0,
            transport=transport,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Prefer": 'outlook.timezone="UTC"',
            },
        )
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(min(4.0, 0.5 * 2**attempt) + random.uniform(0, 0.3))
                    continue
                raise GraphError("Kunde inte nå Microsoft Graph.") from exc
            if resp.status_code == 401:
                raise GraphAuthError("Åtkomsten till Microsoft Graph är ogiltig eller har gått ut.", status=401)
            if resp.status_code == 403:
                raise GraphAuthError("Behörighet saknas för kalendern (Calendars.Read).", status=403)
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", "30"))
                except ValueError:
                    retry_after = 30.0
                if attempt < self._max_retries and retry_after <= 5:
                    attempt += 1
                    time.sleep(retry_after + random.uniform(0, 0.3))
                    continue
                raise GraphThrottled(retry_after)
            if resp.status_code >= 500 and attempt < self._max_retries:
                attempt += 1
                time.sleep(min(4.0, 0.5 * 2**attempt) + random.uniform(0, 0.3))
                continue
            if resp.status_code >= 400:
                raise GraphError(f"Fel från Microsoft Graph ({resp.status_code}).", status=resp.status_code)
            try:
                return resp.json()
            except ValueError as exc:
                raise GraphError("Microsoft Graph svarade inte med JSON.") from exc

    def list_calendars(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = f"{GRAPH_BASE}/me/calendars"
        params: dict[str, Any] | None = {"$select": "id,name,isDefaultCalendar,canEdit,owner", "$top": 100}
        pages = 0
        while url and pages < MAX_PAGES:
            body = self._get(url, params)
            for c in body.get("value", []):
                out.append(
                    {
                        "id": c.get("id"),
                        "name": c.get("name") or "Kalender",
                        "is_default": bool(c.get("isDefaultCalendar")),
                        "owner": ((c.get("owner") or {}).get("address")),
                    }
                )
            url, params = body.get("@odata.nextLink"), None  # nextLink innehåller redan alla parametrar
            pages += 1
        return out

    def calendar_view(
        self, calendar_id: str, start: datetime, end: datetime, *, include_body_preview: bool = False
    ) -> list[dict[str, Any]]:
        """Alla händelser (återkommande expanderade) i intervallet. Följer sidindelningen."""
        fields = list(EVENT_FIELDS) + (["bodyPreview"] if include_body_preview else [])
        params: dict[str, Any] | None = {
            "startDateTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": ",".join(fields),
            "$top": PAGE_SIZE,
        }
        url: str | None = f"{GRAPH_BASE}/me/calendars/{calendar_id}/calendarView"
        out: list[dict[str, Any]] = []
        pages = 0
        while url and pages < MAX_PAGES:
            body = self._get(url, params)
            out.extend(body.get("value", []))
            url, params = body.get("@odata.nextLink"), None
            pages += 1
        if url:
            logger.warning("Kalendervyn avbröts efter %s sidor.", MAX_PAGES)
        return out


def parse_graph_datetime(value: dict[str, Any] | None) -> datetime | None:
    """Graph anger {dateTime, timeZone}; vi begär UTC via Prefer-huvudet."""
    if not value or not value.get("dateTime"):
        return None
    raw = str(value["dateTime"])
    if "." in raw:
        head, frac = raw.split(".", 1)
        raw = f"{head}.{frac[:6]}"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        tz_name = (value.get("timeZone") or "UTC").upper()
        if tz_name not in ("UTC", "ETC/UTC", "GMT"):
            logger.warning("Oväntad tidszon från Graph: %s (tolkas som UTC)", value.get("timeZone"))
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
