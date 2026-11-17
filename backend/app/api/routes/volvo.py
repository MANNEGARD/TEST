"""Volvo Cars-integrationen: anslutning (OAuth2 + PKCE eller testtoken), fordonsval,
insamling och demoläge.

Hemligheter (Client secret, VCC API-nyckel, tokens) sparas endast krypterat och
returneras aldrig – svaren visar bara *om* de finns.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, Principal, PrincipalDep, WebPrincipalDep, get_principal, get_vehicle
from app.api.errors import ApiError, bad_request, conflict
from app.config import get_config
from app.db import utcnow
from app.models import IntegrationConnection, IntegrationSyncRun, Vehicle, VehicleObservation
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import audit, integrations, internal_errors, vehicles
from app.services.volvo import collector, demo, oauth
from app.services.volvo import constants as C
from app.services.volvo.client import VolvoError, VolvoThrottled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/volvo", tags=["Volvo"])


# ----------------------------------------------------------------------- scheman


class CredentialsIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str | None = Field(default=None, max_length=500, description="Tomt = behåll sparat värde")
    vcc_api_key: str | None = Field(default=None, max_length=500, description="Tomt = behåll sparat värde")
    auth_server: str | None = Field(default=None, pattern="^(new|legacy)$")
    scopes: list[str] | None = Field(default=None, max_length=40)


class TestTokenIn(BaseModel):
    access_token: str = Field(min_length=20, max_length=8000)
    vcc_api_key: str | None = Field(default=None, max_length=500)


class SelectVehicleIn(BaseModel):
    ref: str = Field(min_length=6, max_length=64)
    vehicle_id: uuid.UUID | None = None


class SyncIn(BaseModel):
    vehicle_id: uuid.UUID | None = None


class SimulateIn(BaseModel):
    scenario: str | None = Field(default=None, max_length=40)


# ----------------------------------------------------------------------- hjälpare

_STATUS_FOR_CODE = {
    "not_configured": 400,
    "invalid_credentials": 400,
    "invalid_state": 400,
    "state_expired": 400,
    "missing_code": 400,
    "no_vehicle": 400,
    "unknown_vehicle": 404,
    "demo_vehicle": 400,
    "demo_disabled": 409,
    "unknown_scenario": 400,
    "needs_reauth": 409,
    "throttled": 429,
    "invalid_client": 400,
    "network": 502,
    "token_error": 502,
}


def _api_error(exc: VolvoError) -> ApiError:
    status = _STATUS_FOR_CODE.get(exc.code, 502 if exc.http_status else 400)
    return ApiError(status, exc.message, exc.code)


def _conn(db: DbDep, principal: Principal, *, create: bool = True) -> IntegrationConnection | None:
    return oauth.connection(db, principal.user.id, create=create)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _vehicle_summary(db: DbDep, v: Vehicle) -> dict[str, Any]:
    caps = v.capabilities or {}
    last_obs = collector.latest_observation(db, v.id)
    return {
        "id": str(v.id),
        "name": v.name,
        "is_demo": v.is_demo,
        "vin_masked": vehicles.mask_vin(v.vin),
        "model": v.model,
        "model_year": v.model_year,
        "fuel_type": v.fuel_type,
        "capabilities": caps.get("endpoints") or {},
        "energy_capabilities": caps.get("energy") or {},
        "probed_at": caps.get("probed_at"),
        "data_freshness": caps.get("data_freshness"),
        "last_observation_at": _iso(last_obs.last_seen_at) if last_obs else None,
    }


def _last_run(db: DbDep, conn: IntegrationConnection, *, demo_runs: bool) -> dict[str, Any] | None:
    q = select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id == conn.id)
    q = q.where(IntegrationSyncRun.kind.like("demo%") if demo_runs else IntegrationSyncRun.kind.not_like("demo%"))
    run = db.execute(q.order_by(IntegrationSyncRun.started_at.desc()).limit(1)).scalar_one_or_none()
    if run is None:
        return None
    return {
        "kind": run.kind,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "http_status": run.http_status,
        "error": run.error,
        "stats": run.stats or {},
    }


def _status(db: DbDep, principal: Principal) -> dict[str, Any]:
    conn = _conn(db, principal, create=False)
    view = integrations.public_view(conn, IntegrationProvider.VOLVO)
    cfg = dict(view.get("config") or {})
    demo_cfg = cfg.pop("demo", None) or {}
    caps = dict(view.get("capabilities") or {})
    quota = caps.pop("quota", None)
    view["config"] = cfg
    view["capabilities"] = caps

    linked: list[Vehicle] = []
    if conn is not None:
        linked = list(
            db.execute(
                select(Vehicle).where(Vehicle.volvo_connection_id == conn.id, Vehicle.is_demo.is_(False))
            ).scalars()
        )
    demo_vehicle = vehicles.demo_vehicle(db, principal.user.id, create=False) if demo.is_enabled(conn) else None
    real_connected = conn is not None and conn.status == IntegrationStatus.CONNECTED
    stale = [v.id for v in linked if ((v.capabilities or {}).get("data_freshness") or {}).get("stale")]
    view.update(
        {
            "is_connected": real_connected,
            "mode": cfg.get("mode") or (C.MODE_DEMO if demo_cfg.get("enabled") else None),
            "test_token_mode": oauth.is_test_token_mode(conn),
            "test_token_notice": (
                "Testtoken från utvecklarportalen: kortlivad och förnyas inte automatiskt. "
                "Anslut med Volvo ID för kontinuerlig insamling."
            )
            if oauth.is_test_token_mode(conn)
            else None,
            "redirect_uri": oauth.redirect_uri(),
            "auth_server": cfg.get("auth_server") or C.DEFAULT_AUTH_SERVER,
            "requested_scopes": oauth.requested_scopes(conn) if conn is not None else list(C.DEFAULT_SCOPES),
            "reauth_due_at": _iso(oauth.reauth_due_at(conn)) if conn is not None else None,
            "quota": quota
            or {"date": None, "calls": {}, "budget_per_api": C.DAILY_BUDGET_PER_API,
                "documented_limit_per_api": C.DOCUMENTED_DAILY_LIMIT},
            "rate_limit_per_minute": C.REQUESTS_PER_MINUTE,
            "vehicles": [_vehicle_summary(db, v) for v in linked],
            "stale": bool(stale),
            "stale_vehicle_ids": [str(i) for i in stale],
            "last_run": _last_run(db, conn, demo_runs=False) if conn is not None else None,
            "endpoints": [
                {"name": e.name, "label": e.label, "api": e.api, "scopes": list(e.scopes), "polled": e.polled}
                for e in C.ENDPOINTS.values()
            ],
            "demo": {
                "enabled": bool(demo_cfg.get("enabled")),
                "vehicle": _vehicle_summary(db, demo_vehicle) if demo_vehicle is not None else None,
                "last_run": _last_run(db, conn, demo_runs=True) if conn is not None and demo_cfg else None,
                "scenarios": [{"key": k, "description": d} for k, d in demo.SCENARIOS.items()],
            },
        }
    )
    return view


def _redirect(**params: str) -> RedirectResponse:
    base = get_config().base_url.rstrip("/")
    return RedirectResponse(f"{base}/integrations?{urlencode(params)}", status_code=302)


# ----------------------------------------------------------------------- status och uppgifter


@router.get("/status", summary="Volvo-integrationens status, kapabiliteter och kvot")
def status(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return _status(db, principal)


@router.put("/credentials", summary="Spara utvecklarapplikationens uppgifter (krypteras)")
def save_credentials(body: CredentialsIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    conn = _conn(db, principal)
    assert conn is not None
    try:
        oauth.save_credentials(
            db,
            conn,
            client_id=body.client_id,
            client_secret=body.client_secret,
            vcc_api_key=body.vcc_api_key,
            auth_server=body.auth_server,
            scopes=body.scopes,
        )
    except VolvoError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    audit.record(
        db, principal.actor, "volvo.credentials_saved", "integration", conn.id,
        after={"client_id": body.client_id, "auth_server": body.auth_server, "secret_changed": bool(body.client_secret),
               "api_key_changed": bool(body.vcc_api_key)},
    )
    db.commit()
    return _status(db, principal)


@router.post("/connect", summary="Starta inloggning med Volvo ID (OAuth2 + PKCE)")
def connect(principal: WebPrincipalDep, db: DbDep) -> dict[str, str]:
    conn = _conn(db, principal)
    assert conn is not None
    try:
        url = oauth.start_authorization(db, conn, principal.user.id)
    except VolvoError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    db.commit()
    return {"authorize_url": url}


@router.get("/callback", summary="Återkomst från Volvo ID (webbläsaren omdirigeras hit)", include_in_schema=True)
def callback(
    request: Request,
    db: DbDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    try:
        principal = get_principal(request, db)
    except ApiError:
        return _redirect(volvo="error", reason="not_logged_in")
    if principal.api_token is not None:
        return _redirect(volvo="error", reason="session_required")
    user_id = principal.user.id
    if error:
        # Användaren avbröt eller Volvo nekade. State förbrukas ändå.
        oauth.discard_state(db, user_id, state)
        db.commit()
        reason = error if error in ("access_denied", "invalid_scope", "server_error", "temporarily_unavailable") else "denied"
        return _redirect(volvo="error", reason=reason)
    try:
        conn = oauth.complete_authorization(db, user_id, state=state, code=code)
    except VolvoError as exc:
        db.rollback()
        return _redirect(volvo="error", reason=exc.code)
    except Exception:  # aldrig 500 i webbläsaren – detaljer loggas utan hemligheter
        db.rollback()
        logger.exception("Oväntat fel vid Volvo-återkomst")
        return _redirect(volvo="error", reason="internal_error")
    audit.record(db, principal.actor, "volvo.connected", "integration", conn.id, after={"mode": C.MODE_OAUTH})
    db.commit()
    try:
        collector.discover_vehicles(db, conn)
        db.commit()
    except VolvoError as exc:
        db.rollback()
        logger.info("Fordonslistan kunde inte hämtas direkt efter anslutning (%s)", exc.code)
    except Exception:
        db.rollback()
        logger.exception("Oväntat fel när fordonslistan hämtades")
    return _redirect(volvo="connected")


@router.post("/test-token", summary="Anslut med en kortlivad testtoken från utvecklarportalen")
def test_token(body: TestTokenIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    conn = _conn(db, principal)
    assert conn is not None
    try:
        oauth.set_test_token(db, conn, access_token=body.access_token, vcc_api_key=body.vcc_api_key)
    except VolvoError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    audit.record(db, principal.actor, "volvo.test_token_set", "integration", conn.id, after={"mode": C.MODE_TEST_TOKEN})
    db.commit()
    # Verifiera direkt genom att hämta fordonslistan; först då räknas den som ansluten.
    try:
        collector.discover_vehicles(db, conn)
    except VolvoError as exc:
        db.commit()  # needs_reauth/fel sparas
        raise _api_error(exc) from exc
    db.commit()
    return _status(db, principal)


# ----------------------------------------------------------------------- fordon och synk


@router.get("/vehicles", summary="Bilar på det anslutna Volvo-kontot (maskerade VIN)")
def list_account_vehicles(
    principal: PrincipalDep, db: DbDep, request: Request, refresh: bool = False
) -> dict[str, Any]:
    conn = _conn(db, principal, create=False)
    if conn is None:
        return {"vehicles": [], "selected_ref": None}
    # Uppdateringen anropar Volvo (räknas mot dagskvoten). En navigering från en annan webbplats
    # skickar med sessionskakan (SameSite=Lax) men får bara den sparade listan. Webbappens egna
    # anrop kommer från samma ursprung; saknas huvudet (äldre webbläsare, anrop från servern)
    # görs uppdateringen som förut.
    if refresh and (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        refresh = False
    if refresh:
        try:
            collector.discover_vehicles(db, conn)
        except VolvoError as exc:
            db.commit()
            raise _api_error(exc) from exc
        db.commit()
    cfg = conn.config or {}
    return {"vehicles": cfg.get("vehicles") or [], "selected_ref": cfg.get("vehicle_ref")}


@router.post("/select-vehicle", summary="Koppla en bil på Volvo-kontot till ett fordon i DriveLog")
def select_vehicle(body: SelectVehicleIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    conn = _conn(db, principal, create=False)
    if conn is None or not oauth.has_real_credentials(conn):
        raise bad_request("Anslut Volvo-kontot först.", "not_configured")
    target = get_vehicle(db, principal, body.vehicle_id)
    known = integrations.secrets(conn).get("vehicles") or []
    info = next((v for v in known if collector.vehicle_ref(str(v.get("vin") or "")) == body.ref), None)
    if info is not None:
        clash = db.execute(
            select(Vehicle.id).where(
                Vehicle.owner_user_id == principal.user.id, Vehicle.vin == str(info.get("vin")), Vehicle.id != target.id
            )
        ).first()
        if clash is not None:
            raise conflict("Bilen är redan kopplad till ett annat fordon i DriveLog.", "vehicle_already_linked")
    # Tidigare kopplade fordon släpps så att samma Volvo-konto bara pollar den valda bilen.
    for other in db.execute(
        select(Vehicle).where(Vehicle.volvo_connection_id == conn.id, Vehicle.is_demo.is_(False), Vehicle.id != target.id)
    ).scalars():
        other.volvo_connection_id = None
    try:
        collector.select_vehicle(db, conn, target, body.ref)
    except VolvoError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    audit.record(db, principal.actor, "volvo.vehicle_selected", "vehicle", target.id, after={"ref": body.ref})
    db.commit()
    probe_error: str | None = None
    try:
        collector.probe_capabilities(db, conn, target)
        db.commit()
    except VolvoError as exc:
        db.commit()
        probe_error = exc.message
    out = _status(db, principal)
    out["probe_error"] = probe_error
    return out


@router.post("/sync", summary="Hämta fordonsdata nu")
def sync_now(principal: WebPrincipalDep, db: DbDep, body: SyncIn | None = None) -> dict[str, Any]:
    conn = _conn(db, principal, create=False)
    if conn is None:
        raise bad_request("Volvo-integrationen är inte konfigurerad.", "not_configured")
    if body is not None and body.vehicle_id is not None:
        vehicle = get_vehicle(db, principal, body.vehicle_id)
    else:
        vehicle = db.execute(
            select(Vehicle)
            .where(Vehicle.volvo_connection_id == conn.id, Vehicle.is_demo.is_(False), Vehicle.vin.is_not(None))
            .limit(1)
        ).scalar_one_or_none()
        if vehicle is None and demo.is_enabled(conn):
            vehicle = vehicles.demo_vehicle(db, principal.user.id, create=False)
    if vehicle is None or vehicle.volvo_connection_id != conn.id:
        raise bad_request("Inget fordon är kopplat till Volvo-kontot.", "no_vehicle")
    if not vehicle.is_demo and not oauth.has_real_credentials(conn):
        raise bad_request("Anslut Volvo-kontot först.", "not_configured")
    try:
        result = collector.poll_vehicle(db, vehicle)
    except VolvoError as exc:
        db.commit()
        raise _api_error(exc) from exc
    except Exception as exc:
        # Ett fel utanför de skyddade stegen: svaret blir fortfarande 500, men felet antecknas och
        # syns under "Senaste fel" och i felsökningsfilen (1.4.2).
        db.rollback()
        internal_errors.record_after_rollback(
            db, principal.user.id, "volvo.poll", exc, origin="sync", connection_id=conn.id
        )
        raise
    db.commit()
    result.pop("freshness", None)
    processing = result.get("processing_error")
    if processing:
        # Hämtningen lyckades men bearbetningen gjorde det inte (1.4.2). Svaret är detsamma som vid
        # en lyckad hämtning, men med status "error" och raden som meddelande – så som webben redan
        # visar ett misslyckat "Synka nu". Hämtningens eget utfall finns kvar i fetch_status.
        result["fetch_status"] = result.get("status")
        result["status"] = "error"
        result["message"] = processing["message"]
    obs = db.execute(
        select(VehicleObservation.last_seen_at)
        .where(VehicleObservation.vehicle_id == vehicle.id)
        .order_by(VehicleObservation.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {"vehicle_id": str(vehicle.id), "result": result, "last_observation_at": _iso(obs), "at": _iso(utcnow())}


@router.post("/disconnect", summary="Koppla bort Volvo-kontot (raderar alla hemligheter)")
def disconnect(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    conn = _conn(db, principal, create=False)
    if conn is not None:
        before = {"status": conn.status, "mode": (conn.config or {}).get("mode")}
        oauth.disconnect(db, conn)
        audit.record(db, principal.actor, "volvo.disconnected", "integration", conn.id, before=before)
        db.commit()
    return _status(db, principal)


# ----------------------------------------------------------------------- demoläge


@router.post("/demo/enable", summary="Aktivera demoläget (separat demobil med påhittade data)")
def demo_enable(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    try:
        demo.enable(db, principal.user.id)
    except VolvoError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    db.commit()
    return _status(db, principal)


@router.post("/demo/simulate", summary="Simulera en körning med demobilen")
def demo_simulate(principal: WebPrincipalDep, db: DbDep, body: SimulateIn | None = None) -> dict[str, Any]:
    try:
        result = demo.simulate_trip(db, principal.user.id, body.scenario if body else None)
    except VolvoThrottled as exc:  # pragma: no cover - demon har ingen kvot
        db.rollback()
        raise _api_error(exc) from exc
    except VolvoError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    db.commit()
    return result


@router.post("/demo/disable", summary="Stäng av demoläget och radera all demodata")
def demo_disable(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    removed = demo.disable(db, principal.user.id)
    db.commit()
    out = _status(db, principal)
    out["removed"] = removed
    return out


# ----------------------------------------------------------------------- felsökningsfil

# Felsökningsfilen ligger under /volvo (inte /integrations/volvo) enligt tilläggskontraktet 4.A.
diagnostics_router = APIRouter(prefix="/volvo", tags=["Volvo"])

_DIAG_OBS_FIELDS = (
    "fetched_at", "last_seen_at", "seen_count", "odometer_km", "odometer_reported_at", "fuel_liters",
    "fuel_reported_at", "battery_percent", "battery_reported_at", "electric_range_km", "fuel_range_km",
    "charging_status", "engine_running", "engine_reported_at", "avg_fuel_consumption", "avg_energy_consumption",
    "avg_speed", "avg_speed_auto", "trip_meter_auto_km", "trip_meter_manual_km", "statistics_reported_at",
    "avg_fuel_consumption_auto", "avg_energy_consumption_auto",
    # "Bilen används" (1.5.0): det bilen svarade, bilens tidsstämpel och det härledda värdet.
    "accessibility_status", "accessibility_reason", "accessibility_reported_at", "car_in_use",
)
_DIAG_CODE_FIELDS = frozenset({"accessibility_status", "accessibility_reason"})
_DIAG_TRIP_FIELDS = (
    "started_at", "ended_at", "start_time_quality", "end_time_quality", "distance_km", "odometer_start",
    "odometer_end", "fuel_liters", "fuel_quality", "energy_kwh", "energy_quality",
)
_SAFE_CODE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-.")


def _diag_value(value: Any) -> Any:
    """Tidpunkter som ISO-text och tal avrundade till högst tre decimaler."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, float):
        return round(value, 3)
    return value


def _safe_key(value: Any) -> str | None:
    """Bara korta koder (endpoint- och felnycklar), aldrig fri text."""
    text = str(value or "").strip().lower()
    return text if text and len(text) <= 60 and set(text) <= _SAFE_CODE else None


def _status_code(value: Any) -> str:
    """En statuskod från bilen som den kom (t.ex. CAR_IN_USE), eller en markering för annat."""
    text = str(value).strip()
    ok = text and len(text) <= 40 and all(ch.isascii() and (ch.isalnum() or ch in "_-.") for ch in text)
    return text if ok else "(annat värde)"


def _diag_vehicle(db: DbDep, principal: Principal, vehicle_id: uuid.UUID | None) -> Vehicle:
    if vehicle_id is not None:
        return get_vehicle(db, principal, vehicle_id)
    conn = _conn(db, principal, create=False)
    if conn is not None:
        linked = db.execute(
            select(Vehicle)
            .where(Vehicle.volvo_connection_id == conn.id, Vehicle.is_demo.is_(False))
            .order_by(Vehicle.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if linked is not None:
            return linked
        if demo.is_enabled(conn):
            demo_car = vehicles.demo_vehicle(db, principal.user.id, create=False)
            if demo_car is not None:
                return demo_car
    return vehicles.default_vehicle(db, principal.user.id)


def build_diagnostics(db: DbDep, vehicle: Vehicle, *, days: int, now: Any) -> dict[str, Any]:
    """Bilens avläsningar, hämtningarna och resorna som byggdes av dem – utan VIN,
    registreringsnummer, positioner, adresser, platsnamn, rådata, feltexter och nycklar.
    Databasens id ersätts av löpnummer (obs-1, trip-1). ``internal_errors`` är de senaste
    interna felen (services/internal_errors.py), med meddelandet tvättat."""
    from app import __version__
    from app.models import RawTrip
    from app.models.enums import TripSource
    from app.services.export import month_close
    from app.services.trips import start_time
    from app.services.volvo import consumption

    since = now - timedelta(days=days)
    observations = list(
        db.execute(
            select(VehicleObservation)
            .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.last_seen_at >= since)
            .order_by(VehicleObservation.fetched_at)
        ).scalars()
    )
    obs_ids: dict[str, str] = {}
    obs_out: list[dict[str, Any]] = []
    for n, obs in enumerate(observations, start=1):
        label = f"obs-{n}"
        obs_ids[str(obs.id)] = label
        row = {"id": label, **{f: _diag_value(getattr(obs, f)) for f in _DIAG_OBS_FIELDS}}
        for f in _DIAG_CODE_FIELDS:  # bilens statuskoder visas, fri text aldrig
            if row[f] is not None:
                row[f] = _status_code(row[f])
        row["errors_keys"] = sorted(k for k in (_safe_key(k) for k in (obs.errors or {})) if k)
        obs_out.append(row)

    runs: list[dict[str, Any]] = []
    if vehicle.volvo_connection_id is not None:
        kind = IntegrationSyncRun.kind.like("demo%") if vehicle.is_demo else IntegrationSyncRun.kind.not_like("demo%")
        for run in db.execute(
            select(IntegrationSyncRun)
            .where(IntegrationSyncRun.connection_id == vehicle.volvo_connection_id, IntegrationSyncRun.started_at >= since,
                   kind)
            .order_by(IntegrationSyncRun.started_at)
        ).scalars():
            stats = run.stats if isinstance(run.stats, dict) else {}
            runs.append({
                "started_at": _diag_value(run.started_at),
                "finished_at": _diag_value(run.finished_at),
                "status": _safe_key(run.status),
                "error_code": f"http_{run.http_status}" if run.http_status else None,
                "observation": _safe_key(stats.get("observation")),
                "new_trips": stats.get("new_trips") if isinstance(stats.get("new_trips"), int) else None,
            })

    trips_out: list[dict[str, Any]] = []
    raws = db.execute(
        select(RawTrip)
        .where(RawTrip.vehicle_id == vehicle.id, RawTrip.source.in_((TripSource.VOLVO_API, TripSource.DEMO)),
               RawTrip.ended_at >= since)
        .order_by(RawTrip.ended_at)
    ).scalars()
    raws = list(raws)
    # 1.5.0: varför starttiden, bensinen och elen är okända – korta koder, inga platser.
    locks = month_close.protection(db, vehicle.id)
    usage_reasons = consumption.unknown_reasons(db, vehicle, raws, locks, now=now)
    speeds = start_time._known_speeds(db, vehicle.id) if raws else []  # K4:s historik, en gång
    for n, raw in enumerate(raws, start=1):
        basis = raw.start_estimate_basis if isinstance(raw.start_estimate_basis, dict) else {}
        fuel_reason, energy_reason = usage_reasons.get(raw.id, (None, None))
        start_reasons = [k for k in (_safe_key(c) for c in start_time.unknown_start_reasons(db, raw, locks, speeds)) if k]
        trips_out.append({
            "id": f"trip-{n}",
            **{f: _diag_value(getattr(raw, f)) for f in _DIAG_TRIP_FIELDS},
            "voided": raw.voided_at is not None,
            "quality_flags": sorted(k for k in (_safe_key(f) for f in (raw.quality_flags or [])) if k),
            "observations": [obs_ids.get(i, "äldre") for i in (raw.observation_ids or [])],
            "created_at": _diag_value(raw.created_at),
            "protected_by_month": locks.in_file(raw),
            "start_method": _safe_key(basis.get("method")),
            "start_unknown_reason": start_reasons[-1] if start_reasons else None,
            "start_unknown_steps": start_reasons,
            "fuel_unknown_reason": _safe_key(fuel_reason),
            "energy_unknown_reason": _safe_key(energy_reason),
        })

    return {
        "generated_at": _diag_value(now),
        "app_version": __version__,
        "days": days,
        "vehicle": {"model": vehicle.model or "", "fuel_type": vehicle.fuel_type or "", "demo": bool(vehicle.is_demo)},
        "observations": obs_out,
        "sync_runs": runs,
        "trips": trips_out,
        # Månadsskiftet (1.5.0): månad, läge, om filen skapades i förväg och när – inga resor eller belopp.
        "months": month_close.month_overview(db, vehicle),
        # De senaste interna felen (1.4.2): steg, typ, kodställen och ett tvättat meddelande. Bilens
        # VIN och registreringsnummer tvättas bort en gång till här.
        "internal_errors": internal_errors.public_entries(
            db, vehicle.owner_user_id, scrub=(vehicle.vin, vehicle.registration_number)
        ),
    }


@diagnostics_router.get(
    "/diagnostics",
    summary="Felsökningsfil: bilens avläsningar, hämtningar och resor (utan VIN, regnr och positioner)",
)
def diagnostics(
    principal: WebPrincipalDep,
    db: DbDep,
    days: int = Query(14, ge=1, le=31),
    vehicle_id: uuid.UUID | None = None,
) -> Response:
    from app.services import settings as settings_svc

    vehicle = _diag_vehicle(db, principal, vehicle_id)
    now = utcnow()
    body = build_diagnostics(db, vehicle, days=days, now=now)
    local_day = now.astimezone(settings_svc.user_timezone(db, principal.user.id)).date().isoformat()
    filename = f"drivelog-felsokning-{local_day}.json"
    return Response(
        content=json.dumps(body, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
