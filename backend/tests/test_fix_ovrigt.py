"""Regressionstester för rättningar som inte hör till någon enskild modul."""

from __future__ import annotations

from starlette.requests import Request

from app.api.deps import client_ip


def _request(headers: dict[str, str], client: tuple[str, int] | None = ("192.0.2.10", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": client})


def test_client_ip_uses_the_value_added_by_the_proxy() -> None:
    # Klienten skickar själv ett påhittat värde; proxyn lägger till den verkliga adressen sist.
    request = _request({"X-Forwarded-For": "10.9.9.9, 203.0.113.7"})
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_single_value_and_fallback() -> None:
    assert client_ip(_request({"X-Forwarded-For": "203.0.113.7"})) == "203.0.113.7"
    assert client_ip(_request({})) == "192.0.2.10"
    assert client_ip(_request({}, client=None)) == ""
