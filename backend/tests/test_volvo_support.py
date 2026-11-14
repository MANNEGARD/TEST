"""Gemensamma hjälpmedel för Volvo-testerna (innehåller inga egna tester)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

import app.models as M
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import integrations
from app.services.volvo import collector
from app.services.volvo import constants as C
from app.services.volvo.client import ApiResult, QuotaTracker

VIN = "YV1PZA3TEST000042"
T0 = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # en måndag

# Påhittade koordinater (ca 10 km mellan A och B).
A = (59.000, 15.000)
B = (59.090, 15.000)
C_ = (59.090, 15.180)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db, user, vehicle, *, status: str = IntegrationStatus.CONNECTED, tokens: bool = True) -> M.IntegrationConnection:
    """Skapar en ansluten Volvo-integration och kopplar fordonet."""
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.VOLVO)
    integrations.update_config(conn, {"client_id": "vid-dc-test-client", "mode": C.MODE_OAUTH})
    secrets: dict[str, Any] = {"client_secret": "klienthemlighet-xyz", "vcc_api_key": "vcc-nyckel-abc"}
    if tokens:
        secrets.update({"access_token": "AT-gammal-0000000000", "refresh_token": "RT-gammal-0000000000"})
        conn.token_expires_at = datetime.now(UTC) + timedelta(hours=1)
    integrations.set_secrets(conn, secrets)
    conn.status = status
    vehicle.vin = VIN
    vehicle.volvo_connection_id = conn.id
    db.flush()
    return conn


@dataclass
class FakeCar:
    """Simulerad bil som svarar med Volvo-formade JSON-svar (för detekteringstester)."""

    odometer: float = 1000
    odo_ts: datetime = T0 - timedelta(hours=1)
    lat: float = A[0]
    lon: float = A[1]
    pos_ts: datetime = T0 - timedelta(hours=1)
    engine: str = "STOPPED"
    engine_ts: datetime = T0 - timedelta(hours=1)
    fuel: float = 40.0
    numbers_as_strings: bool = False
    unsupported: set[str] = field(default_factory=lambda: {"diagnostics"})
    calls: list[str] = field(default_factory=list)
    is_demo: bool = False

    def __post_init__(self) -> None:
        self.quota = QuotaTracker()

    def _v(self, value: Any) -> Any:
        return str(value) if self.numbers_as_strings else value

    def fetch(self, endpoint_name: str, vin: str) -> ApiResult:
        self.calls.append(endpoint_name)
        if endpoint_name in self.unsupported:
            return ApiResult("unsupported", 404, message="Not found")
        if endpoint_name == "odometer":
            data: Any = {"data": {"odometer": {"value": self._v(int(self.odometer)), "unit": "km", "timestamp": iso(self.odo_ts)}}}
        elif endpoint_name == "location":
            data = {
                "status": 200,
                "data": {
                    "type": "Feature",
                    "properties": {"heading": "90", "timestamp": iso(self.pos_ts)},
                    "geometry": {"type": "Point", "coordinates": [self.lon, self.lat, 0]},
                },
            }
        elif endpoint_name == "fuel":
            data = {"data": {"fuelAmount": {"value": self._v(self.fuel), "unit": "l", "timestamp": iso(self.odo_ts)}}}
        elif endpoint_name == "engine_status":
            data = {"data": {"engineStatus": {"value": self.engine, "timestamp": iso(self.engine_ts)}}}
        elif endpoint_name == "statistics":
            data = {"data": {"averageFuelConsumption": {"value": self._v(5.1), "unit": "l/100km", "timestamp": iso(self.odo_ts)}}}
        elif endpoint_name == "energy_state":
            data = {"batteryChargeLevel": {"status": "ERROR", "code": "NOT_SUPPORTED"}}
        else:
            data = {"data": {}}
        return ApiResult("ok", 200, data=data)

    def list_vehicles(self) -> list[str]:
        return [VIN]

    def vehicle_details(self, vin: str) -> ApiResult:
        return ApiResult("ok", 200, data={"data": {"vin": VIN, "modelYear": 2021, "descriptions": {"model": "V90"}}})

    def close(self) -> None:
        return None

    # -- rörelser
    def drive_to(self, pos: tuple[float, float], km: float, at: datetime) -> None:
        """Ankomst: mätarställning och position uppdateras samtidigt (position först vid resans slut)."""
        self.odometer += km
        self.odo_ts = at
        self.lat, self.lon = pos
        self.pos_ts = at
        self.engine, self.engine_ts = "STOPPED", at

    def moving(self, km: float, at: datetime) -> None:
        """Under färd: mätarställningen ökar, positionen uppdateras inte."""
        self.odometer += km
        self.odo_ts = at
        self.engine, self.engine_ts = "RUNNING", at


def poll(db, vehicle, car: FakeCar, at: datetime, **kw: Any) -> dict[str, Any]:
    return collector.poll_vehicle(db, vehicle, now=at, client=car, **kw)


def poll_every(db, vehicle, car: FakeCar, start: datetime, end: datetime, minutes: int = 5) -> list[dict[str, Any]]:
    out = []
    t = start
    while t <= end:
        out.append(poll(db, vehicle, car, t))
        t += timedelta(minutes=minutes)
    return out


# ----------------------------------------------------------------------------- HTTP-mock


def api_body(endpoint: str, *, odometer: Any = 1234, ts: str = "2026-10-05T06:30:00Z") -> dict[str, Any]:
    if endpoint == "odometer":
        return {"data": {"odometer": {"value": odometer, "unit": "km", "timestamp": ts}}}
    if endpoint == "location":
        return {
            "status": 200,
            "operationId": "op",
            "data": {
                "type": "Feature",
                "properties": {"heading": "129", "timestamp": ts},
                "geometry": {"type": "Point", "coordinates": [A[1], A[0], 12.0]},
            },
        }
    if endpoint == "fuel":
        return {"data": {"fuelAmount": {"value": "41.5", "unit": "l", "timestamp": ts},
                         "batteryChargeLevel": {"value": 80, "unit": "%", "timestamp": ts}}}
    if endpoint == "engine-status":
        return {"data": {"engineStatus": {"value": "STOPPED", "timestamp": ts}}}
    if endpoint == "statistics":
        return {"data": {"averageFuelConsumption": {"value": "5.4", "unit": "l/100km", "timestamp": ts}}}
    if endpoint == "state":
        return {"batteryChargeLevel": {"status": "OK", "value": 81, "unit": "percentage", "updatedAt": ts},
                "chargingPower": {"status": "ERROR", "code": "NOT_SUPPORTED"}}
    return {"data": {}}


def endpoint_of(request: httpx.Request) -> str:
    return request.url.path.rstrip("/").rsplit("/", 1)[-1]


class Recorder:
    """MockTransport-hanterare som loggar anrop och svarar enligt en tabell."""

    def __init__(self, overrides: dict[str, Any] | None = None, token_body: dict[str, Any] | None = None):
        self.calls: list[httpx.Request] = []
        self.overrides = overrides or {}
        self.token_body = token_body or {"access_token": "AT-ny-111111111111", "refresh_token": "RT-ny-22222222222",
                                         "expires_in": 3599}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host in ("auth.volvocars.com", "volvoid.eu.volvocars.com"):
            body = self.token_body
            if callable(body):
                return body(request)
            return httpx.Response(200, json=body)
        ep = endpoint_of(request)
        if request.url.path.endswith("/vehicles"):
            ep = "vehicles"
        elif request.url.path.endswith(VIN):
            ep = "details"
        override = self.overrides.get(ep)
        if callable(override):
            return override(request)
        if isinstance(override, httpx.Response):
            return override
        if ep == "vehicles":
            return httpx.Response(200, json={"data": [{"vin": VIN}]})
        if ep == "details":
            return httpx.Response(200, json={"data": {"vin": VIN, "modelYear": 2021, "fuelType": "PETROL/ELECTRIC",
                                                       "descriptions": {"model": "V90 Recharge"},
                                                       "specifications": {"licensePlate": "ABC123"}}})
        return httpx.Response(200, json=api_body(ep), headers={"vcc-api-operationId": "op-1"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def api_calls(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.host == "api.volvocars.com"]

    def token_calls(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.host != "api.volvocars.com"]
