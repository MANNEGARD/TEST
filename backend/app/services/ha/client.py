"""Klient mot Home Assistants officiella REST-API (push-läget).

Autentisering sker med en long-lived access token. Token loggas aldrig och lämnas
aldrig ut via API:t.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_SERVICE_RE = re.compile(r"^mobile_app_[a-z0-9_]{1,200}$")


class HAError(Exception):
    """Fel mot Home Assistant. ``kind``: unreachable | auth | http | invalid."""

    def __init__(self, message: str, kind: str = "http", status: int | None = None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.kind == "unreachable" or (self.status is not None and self.status >= 500)


def validate_base_url(url: str) -> str:
    """Normaliserar och kontrollerar adressen till Home Assistant."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Ange adressen till Home Assistant.")
    if len(raw) > 300:
        raise ValueError("Adressen är för lång.")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Adressen måste börja med http:// eller https://.")
    if not parts.hostname:
        raise ValueError("Adressen saknar värdnamn.")
    if parts.username or parts.password:
        raise ValueError("Adressen får inte innehålla användarnamn eller lösenord.")
    if parts.query or parts.fragment:
        raise ValueError("Adressen får inte innehålla frågesträng eller fragment.")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError("Ogiltig port i adressen.") from exc
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def validate_notify_service(name: str) -> str:
    """``notify.mobile_app_simons_iphone`` → ``mobile_app_simons_iphone``."""
    value = (name or "").strip().lower()
    if value.startswith("notify."):
        value = value[len("notify.") :]
    if not _SERVICE_RE.match(value):
        raise ValueError("Välj en notify-tjänst av typen notify.mobile_app_<enhet>.")
    return value


class HAClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self.base_url = validate_base_url(base_url)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            transport=transport,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HAClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, json: Any | None = None) -> Any:
        try:
            resp = self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise HAError("Home Assistant kunde inte nås.", "unreachable") from exc
        if resp.status_code in (401, 403):
            raise HAError("Home Assistant avvisade åtkomsttoken.", "auth", resp.status_code)
        if resp.status_code == 404:
            raise HAError("Tjänsten eller adressen finns inte i Home Assistant.", "invalid", 404)
        if resp.status_code >= 400:
            raise HAError(f"Home Assistant svarade med fel {resp.status_code}.", "http", resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise HAError("Oväntat svar – adressen verkar inte gå till Home Assistant.", "invalid") from exc

    def check(self) -> dict[str, Any]:
        """Verifierar adress och token. Returnerar version och installationens namn."""
        status = self._request("GET", "/api/")
        if not isinstance(status, dict) or "message" not in status:
            raise HAError("Oväntat svar – adressen verkar inte gå till Home Assistant.", "invalid")
        config = self._request("GET", "/api/config")
        if not isinstance(config, dict):
            raise HAError("Oväntat svar från Home Assistant.", "invalid")
        return {"version": config.get("version"), "location_name": config.get("location_name")}

    def notify_services(self) -> list[str]:
        """Tillgängliga ``notify.mobile_app_*``-tjänster (en per telefon/surfplatta)."""
        services = self._request("GET", "/api/services")
        out: list[str] = []
        if isinstance(services, list):
            for domain in services:
                if isinstance(domain, dict) and domain.get("domain") == "notify":
                    for name in (domain.get("services") or {}):
                        if isinstance(name, str) and _SERVICE_RE.match(name):
                            out.append(name)
        return sorted(out)

    def notify(self, service: str, data: dict[str, Any]) -> None:
        self._request("POST", f"/api/services/notify/{validate_notify_service(service)}", json=data)
