"""Insamling av fordonsdata från Volvos API.

Varje körning loggas som ``IntegrationSyncRun``. Svaren blir en
``VehicleObservation`` med både hämtningstid (``fetched_at``) och bilens egna
tidsstämplar per datapunkt. Identiskt innehåll ger ingen ny rad – i stället
uppdateras ``last_seen_at``/``seen_count`` på den senaste observationen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    IntegrationConnection,
    Vehicle,
    VehicleObservation,
    VehicleOdometerSnapshot,
)
from app.models.enums import IntegrationProvider, IntegrationStatus, ReviewKind
from app.services import integrations
from app.services import settings as settings_svc
from app.services.trips import review
from app.services.volvo import client as vc
from app.services.volvo import constants as C
from app.services.volvo import oauth
from app.services.volvo.client import ApiResult, QuotaTracker, VolvoAuthError, VolvoClient, VolvoError

logger = logging.getLogger(__name__)

_HASH_FIELDS = (
    "odometer_km", "odometer_reported_at", "latitude", "longitude", "heading", "position_reported_at",
    "fuel_liters", "fuel_reported_at", "battery_percent", "battery_reported_at", "electric_range_km",
    "fuel_range_km", "charging_status", "engine_running", "engine_reported_at", "avg_fuel_consumption",
    "avg_energy_consumption", "avg_speed", "trip_meter_auto_km", "trip_meter_manual_km",
    "avg_speed_auto", "statistics_reported_at", "avg_fuel_consumption_auto", "avg_energy_consumption_auto",
)
# "Bilen används" (command-accessibility, 1.5.0). Status och orsak ingår i content_hash, så att
# en ändrad status ger en ny avläsningsrad med egen hämtningstid. Bilens tidsstämpel ingår
# inte: den kan tänkas följa varje anrop och skulle då ge en ny rad vid varje hämtning.
# Fälten tas bara med i hashen när en status har lästs, så att en bil utan stöd för
# ändpunkten får samma hash som före 1.5.0.
_ACCESS_HASH_FIELDS = ("accessibility_status", "accessibility_reason")
_ACCESS_FIELDS = (*_ACCESS_HASH_FIELDS, "accessibility_reported_at", "car_in_use")
_REPORTED_FIELDS = (
    "odometer_reported_at", "position_reported_at", "fuel_reported_at", "battery_reported_at", "engine_reported_at",
)


class VehicleApi(Protocol):
    """Gemensamt gränssnitt för den riktiga klienten och demosimulatorn."""

    is_demo: bool
    quota: QuotaTracker

    def fetch(self, endpoint_name: str, vin: str) -> ApiResult: ...
    def list_vehicles(self) -> list[str]: ...
    def vehicle_details(self, vin: str) -> ApiResult: ...
    def close(self) -> None: ...


# ----------------------------------------------------------------------- klient


def build_client(
    db: Session,
    conn: IntegrationConnection,
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> VolvoClient:
    """Riktig API-klient för anslutningen (förnyar token vid behov)."""
    now = now or utcnow()
    sec = integrations.secrets(conn)
    api_key = sec.get("vcc_api_key")
    if not api_key:
        raise VolvoError("VCC API-nyckel saknas.", code="not_configured")
    token = oauth.get_access_token(db, conn, now=now, transport=transport)
    quota = QuotaTracker.from_dict((conn.capabilities or {}).get("quota"))

    def _refresh(failed_token: str) -> str | None:
        return oauth.refresh_access_token(db, conn, now=utcnow(), transport=transport, failed_token=failed_token)

    return VolvoClient(
        access_token=token,
        api_key=str(api_key),
        on_unauthorized=_refresh,
        quota=quota,
        limiter=vc.limiter_for(str(conn.id)),
        transport=transport,
    )


def client_for(
    db: Session,
    conn: IntegrationConnection,
    vehicle: Vehicle | None,
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> VehicleApi:
    if vehicle is not None and vehicle.is_demo:
        from app.services.volvo import demo

        return demo.DemoClient(conn)
    return build_client(db, conn, now=now, transport=transport)


def in_backoff(conn: IntegrationConnection, now: datetime) -> bool:
    return conn.backoff_until is not None and conn.backoff_until > now


def _save_quota(conn: IntegrationConnection, api: VehicleApi) -> None:
    if api.is_demo:
        return  # demosimulatorn förbrukar ingen riktig kvot
    caps = dict(conn.capabilities or {})
    caps["quota"] = api.quota.to_dict()
    conn.capabilities = caps


def _mark_success(conn: IntegrationConnection, vehicle: Vehicle | None, now: datetime) -> None:
    """Demodata får aldrig få en riktig anslutning att se ansluten ut."""
    if vehicle is not None and vehicle.is_demo:
        if conn.status == IntegrationStatus.DEMO:
            conn.last_sync_at = conn.last_success_at = now
        return
    integrations.mark_success(conn, now)


def _mark_error(conn: IntegrationConnection, vehicle: Vehicle | None, message: str, *, needs_reauth: bool = False) -> None:
    if vehicle is not None and vehicle.is_demo:
        return
    integrations.mark_error(conn, message, needs_reauth=needs_reauth)


def _apply_throttle(conn: IntegrationConnection, now: datetime, retry_after: float | None) -> None:
    conn.consecutive_failures = (conn.consecutive_failures or 0) + 1
    delay = vc.backoff_seconds(conn.consecutive_failures - 1, retry_after=retry_after)
    conn.backoff_until = now + timedelta(seconds=delay)
    conn.last_error = "Volvos anropsgräns nåddes; insamlingen pausas tillfälligt."
    conn.last_error_at = now
    conn.last_sync_at = now


# ----------------------------------------------------------------------- tolkning


def parse_results(results: dict[str, ApiResult]) -> dict[str, Any]:
    """Översätter API-svaren till observationsfält. Saknade fält förblir None."""
    f: dict[str, Any] = {name: None for name in (*_HASH_FIELDS, *_ACCESS_FIELDS)}

    r = results.get("odometer")
    if r is not None and r.ok:
        value, unit, ts = vc.cv_field(r.data, "odometer")
        f["odometer_km"], f["odometer_reported_at"] = vc.to_km(value, unit), ts

    r = results.get("location")
    if r is not None and r.ok:
        data = r.data.get("data") if isinstance(r.data, dict) else None
        if isinstance(data, dict):
            coords = (data.get("geometry") or {}).get("coordinates") or []
            props = data.get("properties") or {}
            if len(coords) >= 2:
                lon, lat = vc.parse_number(coords[0]), vc.parse_number(coords[1])  # GeoJSON: [lon, lat, höjd]
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    f["latitude"], f["longitude"] = lat, lon
                    f["heading"] = vc.parse_number(props.get("heading"))
                    f["position_reported_at"] = vc.parse_timestamp(props.get("timestamp"))

    r = results.get("fuel")
    if r is not None and r.ok:
        value, unit, ts = vc.cv_field(r.data, "fuelAmount")
        liters = vc.parse_number(value)
        if liters is not None and (unit or "l").lower() in ("l", "liter", "liters", "litre", "litres"):
            f["fuel_liters"], f["fuel_reported_at"] = liters, ts
        value, _unit, ts = vc.cv_field(r.data, "batteryChargeLevel")
        if vc.parse_number(value) is not None:
            f["battery_percent"], f["battery_reported_at"] = vc.parse_number(value), ts

    r = results.get("statistics")
    if r is not None and r.ok:
        f["avg_fuel_consumption"] = vc.parse_number(vc.cv_field(r.data, "averageFuelConsumption")[0])
        f["avg_energy_consumption"] = vc.parse_number(vc.cv_field(r.data, "averageEnergyConsumption")[0])
        f["avg_speed"] = vc.parse_number(vc.cv_field(r.data, "averageSpeed")[0])
        value, unit, trip_ts = vc.cv_field(r.data, "tripMeterAutomatic")
        f["trip_meter_auto_km"] = vc.to_km(value, unit)
        # Snittfarten sedan trippmätaren nollställdes, och när bilen rapporterade
        # statistiken (för starttiden, byggkontraktet D.1).
        value, unit, speed_ts = vc.cv_field(r.data, "averageSpeedAutomatic")
        f["avg_speed_auto"] = _to_kmh(value, unit)
        # Snittförbrukningen för samma period som tripMeterAutomatic (trippmätarmetoden).
        # averageFuelConsumption hör till den manuella trippmätaren och blandas inte in.
        f["avg_fuel_consumption_auto"] = vc.parse_number(vc.cv_field(r.data, "averageFuelConsumptionAutomatic")[0])
        f["avg_energy_consumption_auto"] = vc.parse_number(
            vc.cv_field(r.data, "averageEnergyConsumptionAutomatic")[0]
        )
        f["statistics_reported_at"] = trip_ts or speed_ts
        value, unit, _ = vc.cv_field(r.data, "tripMeterManual")
        f["trip_meter_manual_km"] = vc.to_km(value, unit)
        value, unit, _ = vc.cv_field(r.data, "distanceToEmptyTank")
        f["fuel_range_km"] = vc.to_km(value, unit)
        value, unit, _ = vc.cv_field(r.data, "distanceToEmptyBattery")
        f["electric_range_km"] = vc.to_km(value, unit)

    r = results.get("engine_status")
    if r is not None and r.ok:
        value, _unit, ts = vc.cv_field(r.data, "engineStatus")
        if isinstance(value, str):
            f["engine_running"] = value.upper() == "RUNNING"
            f["engine_reported_at"] = ts

    r = results.get("energy_state")
    if r is not None and r.ok:
        value, _unit, ts = vc.energy_field(r.data, "batteryChargeLevel")
        level = vc.parse_number(value)
        # Energy-API:ets värde används om det är nyare än Connected Vehicle-värdet.
        if level is not None and (f["battery_reported_at"] is None or (ts and ts > f["battery_reported_at"])):
            f["battery_percent"], f["battery_reported_at"] = level, ts
        value, unit, _ = vc.energy_field(r.data, "electricRange")
        rng = vc.to_km(value, unit)
        if rng is not None:
            f["electric_range_km"] = rng
        value, _unit, _ = vc.energy_field(r.data, "chargingStatus")
        if isinstance(value, str):
            f["charging_status"] = value[:60]

    r = results.get(C.ACCESSIBILITY_ENDPOINT)
    if r is not None and r.ok:
        status, reason, ts = vc.accessibility_field(r.data)
        if status is not None:
            f["accessibility_status"], f["accessibility_reason"] = status, reason
            f["accessibility_reported_at"] = ts
            f["car_in_use"] = vc.is_car_in_use(status, reason)
    return f


def _to_kmh(value: Any, unit: str | None) -> float | None:
    num = vc.parse_number(value)
    if num is None:
        return None
    u = (unit or "km/h").lower().replace(" ", "")
    if u in ("mph", "mi/h", "miles/h"):
        return round(num * C.MILES_TO_KM, 1)
    if u in ("km/h", "kmh", "kph", "km/hr"):
        return num
    return None  # okänd enhet: hellre inget värde än ett fel


def content_hash(fields: dict[str, Any]) -> str:
    basis = {
        k: (fields.get(k).isoformat() if isinstance(fields.get(k), datetime) else fields.get(k)) for k in _HASH_FIELDS
    }
    if fields.get("accessibility_status") is not None:
        basis.update({k: fields.get(k) for k in _ACCESS_HASH_FIELDS})
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def latest_observation(db: Session, vehicle_id: uuid.UUID) -> VehicleObservation | None:
    return db.execute(
        select(VehicleObservation)
        .where(VehicleObservation.vehicle_id == vehicle_id)
        .order_by(VehicleObservation.fetched_at.desc(), VehicleObservation.last_seen_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def store_observation(
    db: Session,
    vehicle: Vehicle,
    results: dict[str, ApiResult],
    *,
    fetched_at: datetime,
    sync_run_id: uuid.UUID | None = None,
    source: str = "volvo_api",
) -> tuple[VehicleObservation | None, bool]:
    """Sparar en observation. Returnerar (observation, skapades_ny_rad)."""
    if not any(r.ok for n, r in results.items() if n != C.ACCESSIBILITY_ENDPOINT):
        # Inget observerades (ett svar bara om tillgängligheten räcker inte heller) – hitta
        # inte på en tom datapunkt.
        return None, False
    fields = parse_results(results)
    digest = content_hash(fields)
    last = latest_observation(db, vehicle.id)
    if last is not None and last.content_hash == digest:
        last.last_seen_at = max(last.last_seen_at, fetched_at)
        last.seen_count = (last.seen_count or 1) + 1
        db.flush()
        return last, False

    errors = {
        name: {"status": r.status, "http_status": r.http_status, "message": r.message}
        for name, r in results.items()
        if not r.ok
    }
    obs = VehicleObservation(
        vehicle_id=vehicle.id,
        source=source,
        sync_run_id=sync_run_id,
        fetched_at=fetched_at,
        last_seen_at=fetched_at,
        seen_count=1,
        content_hash=digest,
        raw={name: vc.strip_identifiers(r.data) for name, r in results.items() if r.ok},
        errors=errors,
        **fields,
    )
    db.add(obs)
    db.flush()

    odo = fields.get("odometer_km")
    if odo is not None and (last is None or last.odometer_km is None or abs(odo - last.odometer_km) >= 0.5):
        at = fields.get("odometer_reported_at") or fetched_at
        exists = db.execute(
            select(VehicleOdometerSnapshot.id).where(
                VehicleOdometerSnapshot.vehicle_id == vehicle.id,
                VehicleOdometerSnapshot.at == at,
                VehicleOdometerSnapshot.source == "api",
            )
        ).first()
        if exists is None:
            db.add(
                VehicleOdometerSnapshot(
                    vehicle_id=vehicle.id, at=at, odometer_km=odo, source="api",
                    note="Rapporterad av fordonet" if source != "demo" else "Demodata",
                )
            )
            db.flush()
    return obs, True


# ----------------------------------------------------------------------- kapabiliteter


def _cap_state(result: ApiResult) -> str | None:
    if result.ok:
        return C.CAP_SUPPORTED
    if result.status == "unsupported":
        return C.CAP_UNSUPPORTED
    if result.status == "no_data":
        return C.CAP_NO_DATA
    return None  # tillfälligt fel säger inget om stödet


def update_capabilities(vehicle: Vehicle, results: dict[str, ApiResult], now: datetime) -> None:
    caps = dict(vehicle.capabilities or {})
    endpoints = dict(caps.get("endpoints") or {})
    for name, result in results.items():
        ep = C.ENDPOINTS.get(name)
        if ep is None:
            continue
        entry = dict(endpoints.get(name) or {"state": C.CAP_UNKNOWN})
        entry["label"] = ep.label
        entry["api"] = ep.api
        state = _cap_state(result)
        if state == C.CAP_UNSUPPORTED and entry.get("state") == C.CAP_SUPPORTED:
            # En endpoint som fungerat nedgraderas inte på ett enstaka 403/404 (då hämtas den
            # inte på ett dygn och körda km kan tappas) – först efter flera svar i rad.
            streak = int(entry.get("unsupported_streak") or 0) + 1
            entry["unsupported_streak"] = streak
            if streak < C.UNSUPPORTED_CONFIRMATIONS:
                state = None
        elif state is not None:
            entry.pop("unsupported_streak", None)
        if state is not None:
            entry.update({"state": state, "http_status": result.http_status, "checked_at": now.isoformat()})
            entry["message"] = None if result.ok else result.message
        else:
            entry["last_error"] = {"at": now.isoformat(), "status": result.status, "message": result.message}
        endpoints[name] = entry
    caps["endpoints"] = endpoints
    energy = results.get("energy_capabilities")
    if energy is not None and energy.ok and isinstance(energy.data, dict):
        caps["energy"] = {
            key: bool(val.get("isSupported")) for key, val in energy.data.items() if isinstance(val, dict) and "isSupported" in val
        }
    vehicle.capabilities = caps


def _should_poll(vehicle: Vehicle, name: str, now: datetime) -> bool:
    entry = ((vehicle.capabilities or {}).get("endpoints") or {}).get(name) or {}
    if entry.get("state") != C.CAP_UNSUPPORTED:
        return True
    checked = vc.parse_timestamp(entry.get("checked_at"))
    return checked is None or (now - checked) >= timedelta(hours=C.REPROBE_UNSUPPORTED_HOURS)


def probe_capabilities(
    db: Session,
    conn: IntegrationConnection,
    vehicle: Vehicle,
    *,
    client: VehicleApi | None = None,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Provar varje läs-endpoint en gång för just den här bilen och sparar utfallet."""
    now = now or utcnow()
    if not vehicle.vin:
        raise VolvoError("Inget fordon är valt.", code="no_vehicle")
    if client is None and not vehicle.is_demo and in_backoff(conn, now):
        raise vc.VolvoThrottled()
    own = client is None
    api = client or client_for(db, conn, vehicle, now=now, transport=transport)
    results: dict[str, ApiResult] = {}
    try:
        for name in C.ENDPOINTS:
            result = api.fetch(name, vehicle.vin)
            results[name] = result
            if result.status == "throttled":
                if not api.is_demo:
                    _apply_throttle(conn, now, result.retry_after)
                break
    finally:
        _save_quota(conn, api)
        if own:
            api.close()
    update_capabilities(vehicle, results, now)
    caps = dict(vehicle.capabilities or {})
    caps["probed_at"] = now.isoformat()
    vehicle.capabilities = caps
    if any(r.ok for r in results.values()):
        _mark_success(conn, vehicle, now)
    db.flush()
    return caps


# ----------------------------------------------------------------------- fordonsval


def vehicle_ref(vin: str) -> str:
    return hashlib.sha256(vin.encode()).hexdigest()[:12]


def discover_vehicles(
    db: Session,
    conn: IntegrationConnection,
    *,
    client: VehicleApi | None = None,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Listar bilarna på Volvo-kontot. VIN lagras krypterat; UI får maskerade värden."""
    now = now or utcnow()
    if client is None and in_backoff(conn, now):
        raise vc.VolvoThrottled()
    own = client is None
    api = client or build_client(db, conn, now=now, transport=transport)
    found: list[dict[str, Any]] = []
    try:
        for vin in api.list_vehicles():
            details = api.vehicle_details(vin)
            info: dict[str, Any] = {"vin": vin}
            if details.ok and isinstance(details.data, dict):
                d = details.data.get("data") or {}
                desc = d.get("descriptions") or {}
                spec = d.get("specifications") or {}
                info.update(
                    {
                        "model": desc.get("model"),
                        "model_year": d.get("modelYear"),
                        "fuel_type": d.get("fuelType"),
                        "gearbox": d.get("gearbox"),
                        "external_colour": d.get("externalColour"),
                        "battery_capacity_kwh": d.get("batteryCapacityKWH"),
                        "license_plate": spec.get("licensePlate"),
                    }
                )
            found.append(info)
    finally:
        _save_quota(conn, api)
        if own:
            api.close()
    integrations.set_secrets(conn, {"vehicles": found})
    integrations.mark_success(conn, now)
    integrations.update_config(conn, {"vehicles": [public_vehicle(v) for v in found]})
    db.flush()
    return found


def public_vehicle(info: dict[str, Any]) -> dict[str, Any]:
    vin = str(info.get("vin") or "")
    return {
        "ref": vehicle_ref(vin),
        "vin_masked": (vin[:3] + "•" * max(0, len(vin) - 7) + vin[-4:]) if len(vin) > 7 else "•" * len(vin),
        "model": info.get("model"),
        "model_year": info.get("model_year"),
        "fuel_type": info.get("fuel_type"),
        "license_plate": info.get("license_plate"),
    }


def select_vehicle(db: Session, conn: IntegrationConnection, target: Vehicle, ref: str) -> Vehicle:
    """Kopplar en bil på Volvo-kontot till ett fordon i DriveLog."""
    if target.is_demo:
        raise VolvoError("Demobilen kan inte kopplas till ett riktigt Volvo-konto.", code="demo_vehicle")
    known = integrations.secrets(conn).get("vehicles") or []
    info = next((v for v in known if vehicle_ref(str(v.get("vin") or "")) == ref), None)
    if info is None:
        raise VolvoError("Bilen finns inte på Volvo-kontot. Hämta fordonslistan igen.", code="unknown_vehicle")
    target.vin = str(info["vin"])
    target.make = target.make or "Volvo"
    if info.get("model"):
        target.model = str(info["model"])[:120]
    try:
        target.model_year = int(info["model_year"]) if info.get("model_year") else target.model_year
    except (TypeError, ValueError):
        pass
    if info.get("fuel_type"):
        target.fuel_type = str(info["fuel_type"])[:40]
    # Registreringsnumret från API:t används bara om användaren inte själv har angett ett.
    if info.get("license_plate") and not target.registration_number:
        target.registration_number = str(info["license_plate"])[:20]
    target.volvo_connection_id = conn.id
    caps = dict(target.capabilities or {})
    caps["details"] = {k: info.get(k) for k in ("model", "model_year", "fuel_type", "gearbox", "battery_capacity_kwh")}
    target.capabilities = caps
    integrations.update_config(conn, {"vehicle_id": str(target.id), "vehicle_ref": ref})
    db.flush()
    return target


# ----------------------------------------------------------------------- färskhet


def check_freshness(
    db: Session, conn: IntegrationConnection, vehicle: Vehicle, obs: VehicleObservation | None, settings: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Upptäcker föråldrade uppgifter: bilen har inte rapporterat något nytt på länge."""
    stale_after = timedelta(minutes=float(settings.get("stale_after_minutes", 1440)))
    newest = None
    if obs is not None:
        stamps = [getattr(obs, f) for f in _REPORTED_FIELDS if getattr(obs, f) is not None]
        newest = max(stamps) if stamps else None
    stale = newest is not None and (now - newest) > stale_after
    info = {
        "stale": bool(stale),
        "newest_reported_at": newest.isoformat() if newest else None,
        "checked_at": now.isoformat(),
        "stale_after_minutes": stale_after.total_seconds() / 60,
    }
    caps = dict(vehicle.capabilities or {})
    previous = caps.get("data_freshness") or {}
    caps["data_freshness"] = info
    vehicle.capabilities = caps
    if stale and newest is not None:
        key = f"stale:{vehicle.id}:{newest.strftime('%Y%m%d%H')}"
        review.upsert(
            db,
            vehicle_id=vehicle.id,
            kind=ReviewKind.ANOMALY,
            dedupe_key=key,
            title="Fordonet har inte rapporterat nya uppgifter",
            detail=(
                "Senaste uppgiften från bilen är äldre än den inställda gränsen. Bilen kan stå stilla, "
                "sakna uppkoppling eller vara i viloläge. Resor under perioden kan saknas."
            ),
            data={"type": "stale_data", "newest_reported_at": newest.isoformat()},
        )
    elif previous.get("stale") and previous.get("newest_reported_at"):
        prev = vc.parse_timestamp(previous["newest_reported_at"])
        if prev is not None:
            review.resolve_by_key(
                db, vehicle.id, f"stale:{vehicle.id}:{prev.strftime('%Y%m%d%H')}", by="system",
                resolution={"auto": True, "reason": "Nya uppgifter har rapporterats"},
            )
    return info


# ----------------------------------------------------------------------- insamling


def poll_vehicle(
    db: Session,
    vehicle: Vehicle,
    *,
    now: datetime | None = None,
    client: VehicleApi | None = None,
    detect: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Hämtar aktuella fordonsuppgifter, sparar en observation och kör resedetekteringen.

    Under en aktiv backoff (efter HTTP 429) görs inga anrop alls.
    """
    now = now or utcnow()
    user_id = vehicle.owner_user_id
    conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    if conn is None:
        raise VolvoError("Volvo-integrationen är inte konfigurerad.", code="not_configured")
    if not vehicle.vin:
        raise VolvoError("Inget fordon är valt.", code="no_vehicle")
    if not vehicle.is_demo and client is None and in_backoff(conn, now):
        return {
            "status": "throttled",
            "message": "Insamlingen är pausad efter Volvos anropsgräns; inga anrop görs förrän pausen är över.",
            "backoff_until": conn.backoff_until.isoformat() if conn.backoff_until else None,
            "endpoints": {},
            "observation": "none",
            "new_trips": 0,
            "new_trip_ids": [],
        }
    settings = settings_svc.get_section(db, user_id, "volvo")
    # Raden under "Senaste fel" före hämtningen: mark_success tömmer den, men en rad om en
    # misslyckad bearbetning ska stå kvar tills bearbetningen faktiskt har lyckats (_process).
    previous_error = (conn.last_error, conn.last_error_at)
    run = integrations.start_run(db, conn, "demo_poll" if vehicle.is_demo else "poll")
    stats: dict[str, Any] = {"endpoints": {}, "observation": "none", "new_trips": 0}
    results: dict[str, ApiResult] = {}
    own = client is None
    api: VehicleApi | None = client
    try:
        if api is None:
            api = client_for(db, conn, vehicle, now=now, transport=transport)
        throttled: ApiResult | None = None
        for name in C.POLL_ENDPOINTS:
            if not _should_poll(vehicle, name, now):
                stats["endpoints"][name] = "skipped_unsupported"
                continue
            result = api.fetch(name, vehicle.vin)
            results[name] = result
            stats["endpoints"][name] = result.status
            if result.status == "throttled":
                throttled = result
                break
    except VolvoAuthError as exc:
        if api is not None:
            _save_quota(conn, api)
            if own:
                api.close()
        if conn.status != IntegrationStatus.NEEDS_REAUTH:
            _mark_error(conn, vehicle, exc.message, needs_reauth=True)
        integrations.finish_run(run, "error", stats=stats, error=exc.message, http_status=exc.http_status)
        db.flush()
        return {"status": "needs_reauth", "message": exc.message, **stats}
    except VolvoError as exc:
        if api is not None:
            _save_quota(conn, api)
            if own:
                api.close()
        if isinstance(exc, vc.VolvoThrottled):
            _apply_throttle(conn, now, exc.retry_after)
            integrations.finish_run(run, "throttled", stats=stats, error=exc.message, http_status=429)
            db.flush()
            return {"status": "throttled", "message": exc.message, **stats}
        _mark_error(conn, vehicle, exc.message)
        integrations.finish_run(run, "error", stats=stats, error=exc.message, http_status=exc.http_status)
        db.flush()
        return {"status": "error", "message": exc.message, **stats}

    assert api is not None
    _save_quota(conn, api)
    if own:
        api.close()
    update_capabilities(vehicle, results, now)

    obs, created = store_observation(
        db, vehicle, results, fetched_at=now, sync_run_id=run.id, source="demo" if vehicle.is_demo else "volvo_api"
    )
    stats["observation"] = "new" if created else ("duplicate" if obs is not None else "none")
    # "Bilen används" är ett tillägg: ett fel i den ändpunkten (eller ett svar bara från den)
    # avgör aldrig om hämtningen lyckades.
    core = {n: r for n, r in results.items() if n != C.ACCESSIBILITY_ENDPOINT}
    ok_count = sum(1 for r in core.values() if r.ok)
    hard_errors = [n for n, r in core.items() if r.status == "error"]

    if throttled is not None:
        _apply_throttle(conn, now, throttled.retry_after)
        status = "throttled"
    elif ok_count == 0 and hard_errors:
        _mark_error(
            conn, vehicle, "Inga uppgifter kunde hämtas från Volvo: " + (results[hard_errors[0]].message or "okänt fel")
        )
        status = "error"
    else:
        if ok_count:
            _mark_success(conn, vehicle, now)
        elif not vehicle.is_demo:
            conn.last_sync_at = now
        status = "partial" if hard_errors else "ok"

    freshness = check_freshness(db, conn, vehicle, obs, settings, now) if obs is not None else None
    new_trips: list[Any] = []
    processing_error: dict[str, Any] | None = None
    if detect and obs is not None:
        new_trips, processing_error = _process(
            db, conn, vehicle, user_id, settings, now, origin=run.kind, previous_error=previous_error
        )
    stats["new_trips"] = len(new_trips)
    stats["calls"] = sum(r.attempts for r in results.values())
    if processing_error is not None:
        stats["processing_error"] = processing_error["step"]
    integrations.finish_run(
        run,
        status,
        stats=stats,
        error=None if status in ("ok", "partial") else conn.last_error,
        http_status=429 if status == "throttled" else None,
    )
    db.flush()
    out = {"status": status, "freshness": freshness, "new_trip_ids": [str(t.id) for t in new_trips], **stats}
    if processing_error is not None:
        out["processing_error"] = processing_error
    return out


def _process(
    db: Session,
    conn: IntegrationConnection,
    vehicle: Vehicle,
    user_id: uuid.UUID,
    settings: dict[str, Any],
    now: datetime,
    *,
    origin: str,
    previous_error: tuple[str | None, datetime | None] = (None, None),
) -> tuple[list[Any], dict[str, Any] | None]:
    """Resedetekteringen och efterbearbetningen efter en hämtning (1.4.2).

    Avläsningen är redan sparad i den yttre transaktionen. I det toleranta läget körs
    detekteringen i en egen sparpunkt: kraschar den rullas bara den tillbaka – avläsningarna
    förblir obearbetade och prövas igen vid nästa hämtning – och avläsningen, hämtningens
    utfall och kvoten sparas ändå. Delstegen efter att resan skapats har egna sparpunkter
    (``pipeline.after_trips_changed(isolate_steps=True)``). Misslyckade steg antecknas
    (``system.internal_errors``) och visas under "Senaste fel". I testmiljön (strängt läge)
    kastas undantagen vidare som förut.

    Raden under "Senaste fel" försvinner först när bearbetningen har lyckats: ett fel i
    detekteringen när detekteringen sedan körs utan fel, ett fel i ett delsteg när en
    efterbearbetning sedan körs klart utan fel. En hämtning utan något att bearbeta lämnar raden
    kvar (se ``_keep_unresolved_processing_line``).
    """
    from app.services import internal_errors
    from app.services.volvo import detection

    internal_errors.take_failures(db)  # inget gammalt från en tidigare hämtning i sessionen
    internal_errors.take_pipeline_done(db)
    created = internal_errors.run_step(
        db, "detection", detection.process_vehicle, db, vehicle, user_id=user_id, settings=settings, now=now,
        default=None,
    )
    failures = internal_errors.take_failures(db)
    pipeline_done = internal_errors.take_pipeline_done(db)
    if not failures:
        _keep_unresolved_processing_line(conn, vehicle, previous_error, pipeline_done=pipeline_done)
        return list(created or []), None
    # I egen sparpunkt: går felen inte att anteckna får det aldrig ta avläsningen och resan med sig.
    internal_errors.save_failures_safely(db, user_id, failures, origin=origin)
    message = internal_errors.processing_message(failures)
    steps = list(dict.fromkeys(f["step"] for f in failures))
    if not vehicle.is_demo:
        # Hämtningen lyckades (mark_success har tömt raden); bearbetningen gjorde det inte.
        # Raden försvinner av sig själv vid nästa hämtning där bearbetningen lyckas. Den sparas
        # också för sig, så att ett tillfälligt fel i en senare hämtning inte gör att den glöms.
        conn.last_error = message
        conn.last_error_at = now
        integrations.update_config(conn, {UNRESOLVED_PROCESSING: {"message": message, "at": now.isoformat()}})
    step = "detection" if "detection" in steps else steps[0]
    return list(created or []), {"step": step, "steps": steps, "message": message}


# Nyckel i conn.config: den olösta raden om en misslyckad bearbetning, {"message", "at"}.
UNRESOLVED_PROCESSING = "unresolved_processing"


def _keep_unresolved_processing_line(
    conn: IntegrationConnection,
    vehicle: Vehicle,
    previous_error: tuple[str | None, datetime | None],
    *,
    pipeline_done: bool,
) -> None:
    """Lägger tillbaka raden om en misslyckad bearbetning om inget har bearbetats om.

    Ett fel i detekteringen är löst när detekteringen har körts utan fel (avläsningarna har då
    prövats igen). Ett fel i ett delsteg är löst först när en efterbearbetning har körts klart
    utan fel – annars saknar resan t.ex. sin fråga eller sin klassning och ingenting görs om.

    Raden läses ur ``conn.config[UNRESOLVED_PROCESSING]``, inte bara ur raden strax före
    hämtningen: har ett tillfälligt hämtningsfel (5xx, 429) skrivit över den ska den ändå
    komma tillbaka vid nästa lyckade hämtning. ``previous_error`` används bara som reserv för
    en rad som skrevs innan nyckeln fanns."""
    from app.services import internal_errors

    if vehicle.is_demo:
        return  # demobilen rör inte raden
    saved = (conn.config or {}).get(UNRESOLVED_PROCESSING)
    if isinstance(saved, dict) and isinstance(saved.get("message"), str):
        message, at = saved["message"], _parse_time(saved.get("at"))
    else:
        message, at = previous_error
    if not message or not message.startswith(internal_errors.PROCESSING_PREFIX):
        return
    if message == internal_errors.DETECTION_MESSAGE or pipeline_done:
        if saved is not None:
            integrations.update_config(conn, {UNRESOLVED_PROCESSING: None})
        return
    if saved is None:  # en rad från före uppgraderingen: spara den på det nya sättet
        integrations.update_config(
            conn, {UNRESOLVED_PROCESSING: {"message": message, "at": at.isoformat() if at else None}}
        )
    if conn.last_error is not None:
        return  # ett nytt fel i den här hämtningen går före; raden kommer tillbaka sedan
    conn.last_error, conn.last_error_at = message, at


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _due(conn: IntegrationConnection, interval: float, now: datetime) -> bool:
    if conn.backoff_until is not None and conn.backoff_until > now:
        return False
    if conn.last_sync_at is None:
        return True
    return (now - conn.last_sync_at).total_seconds() >= interval - 5


def poll_due(
    db: Session, now: datetime | None = None, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Pollar alla anslutna fordon vars intervall har löpt ut. Anropas av bakgrundsjobbet.

    Committar efter varje fordon. Demofordon hanteras av ``demo.tick``.
    """
    now = now or utcnow()
    out: dict[str, Any] = {"polled": 0, "skipped": 0, "errors": 0, "new_trips": 0, "throttled": 0}
    conns = list(
        db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.provider == IntegrationProvider.VOLVO,
                IntegrationConnection.status.in_((IntegrationStatus.CONNECTED, IntegrationStatus.ERROR)),
            )
        ).scalars()
    )
    for conn in conns:
        settings = settings_svc.get_section(db, conn.user_id, "volvo")
        interval = max(
            float(settings.get("min_poll_interval_seconds", 120)), float(settings.get("poll_interval_seconds", 300))
        )
        if not _due(conn, interval, now):
            out["skipped"] += 1
            continue
        vehicles = list(
            db.execute(
                select(Vehicle).where(
                    Vehicle.volvo_connection_id == conn.id,
                    Vehicle.is_active.is_(True),
                    Vehicle.is_demo.is_(False),
                    Vehicle.vin.is_not(None),
                )
            ).scalars()
        )
        if not vehicles:
            out["skipped"] += 1
            continue
        for vehicle in vehicles:
            try:
                result = poll_vehicle(db, vehicle, now=now, transport=transport)
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.exception("Insamlingen misslyckades för fordon %s", vc.mask_vin(vehicle.vin))
                # Felet antecknas och visas under "Senaste fel" (1.4.2), i en egen kort transaktion.
                from app.services import internal_errors

                internal_errors.record_after_rollback(
                    db, conn.user_id, "volvo.poll", exc, origin="poll", connection_id=conn.id, now=now
                )
                out["errors"] += 1
                continue
            out["polled"] += 1
            out["new_trips"] += int(result.get("new_trips", 0))
            if result["status"] == "throttled":
                out["throttled"] += 1
                break
            if result["status"] in ("error", "needs_reauth"):
                out["errors"] += 1
    return out
