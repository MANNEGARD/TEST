"""Gemensamma hjälpare för Home Assistant-testerna (inga tester i den här filen)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import app.models as M
from app.models.enums import IntegrationStatus, LocationType, ReviewKind
from app.services import integrations
from app.services import settings as settings_svc
from app.services.ha import client as ha_client
from app.services.ha import connection, notifier

# Tisdag 15 september 2026 kl. 12:00 svensk tid – utanför tysta timmar.
NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
HA_URL = "http://ha.local:8123"
HA_TOKEN = "ha-long-lived-hemlig-token-0123456789"
SERVICE = "mobile_app_testtelefon"
HOME = "Hemvägen 1, 123 45 Hemby"
OREBRO = "Drottninggatan 12, 702 10 Örebro"
JONKOPING = "Östra Storgatan 5, 553 21 Jönköping"


class FakeHA:
    """Simulerad Home Assistant bakom httpx.MockTransport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.offline = False
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("nere", request=request)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == f"Bearer {HA_TOKEN}"
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "fel"})
        path = request.url.path
        if path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if path == "/api/config":
            return httpx.Response(200, json={"version": "2026.11.1", "location_name": "Hemma"})
        if path == "/api/services":
            return httpx.Response(
                200,
                json=[
                    {"domain": "light", "services": {"turn_on": {}}},
                    {"domain": "notify", "services": {SERVICE: {}, "persistent_notification": {}, "mobile_app_ipad": {}}},
                ],
            )
        if path.startswith("/api/services/notify/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    @property
    def notifications(self) -> list[dict[str, Any]]:
        return [b for (m, p, b) in self.calls if m == "POST" and p == f"/api/services/notify/{SERVICE}"]

    @property
    def sent(self) -> list[dict[str, Any]]:
        return [n for n in self.notifications if n.get("message") != "clear_notification"]

    @property
    def clears(self) -> list[dict[str, Any]]:
        return [n for n in self.notifications if n.get("message") == "clear_notification"]


@pytest.fixture()
def fake_ha():
    ha = FakeHA()
    ha_client.use_transport(httpx.MockTransport(ha.handler))
    yield ha
    ha_client.use_transport(None)


def setup_push(db, user, *, status: str = IntegrationStatus.CONNECTED, **notif: Any) -> M.IntegrationConnection:
    conn = connection.get_or_create(db, user.id)
    integrations.update_config(conn, {"base_url": HA_URL, "notify_service": SERVICE})
    integrations.set_secrets(conn, {"token": HA_TOKEN})
    conn.status = status
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "push", **notif})
    db.flush()
    return conn


def setup_pull(db, user, **notif: Any) -> M.IntegrationConnection:
    conn = connection.get_or_create(db, user.id)
    integrations.update_config(conn, {"notify_service": SERVICE})
    conn.status = IntegrationStatus.CONNECTED
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull", **notif})
    db.flush()
    return conn


def places(trips) -> dict[str, M.KnownLocation]:
    return {"home": trips.location(HOME, name="Hemmet", type=LocationType.HOME)}


def recent_trip(trips, start: str = HOME, end: str = OREBRO, *, km: float = 42.0, hours_ago: float = 1.0, **kw: Any):
    """En resa som slutade ``hours_ago`` timmar före NOW."""
    minutes = kw.pop("minutes", 40)
    start_at = NOW - timedelta(hours=hours_ago) - timedelta(minutes=minutes)
    return trips.trip(start, end, km=km, minutes=minutes, start_at=start_at, **kw)


def enqueue_classify(db, user, vehicle, trip, now: datetime = NOW) -> list[M.NotificationRequest]:
    return notifier.enqueue_for_changes(
        db, user_id=user.id, vehicle=vehicle, merge_items=[], trip_items=[(trip, [ReviewKind.CATEGORY])], now=now
    )


def deliver(db, now: datetime) -> dict[str, int]:
    return notifier.deliver_due(db, now=now)


def after_batch(minutes: float = 11) -> datetime:
    return NOW + timedelta(minutes=minutes)
