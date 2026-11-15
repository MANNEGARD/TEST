"""Demonstrationsläge för Volvo-integrationen.

En simulerad bil ("Demobilen") matar påhittade API-svar genom exakt samma kedja som
en riktig bil: API-svar → observation → resedetektering → journalresa. Demodata
ligger på ett eget fordon (``is_demo``) med påhittade platser och blandas aldrig
med verkliga uppgifter. Demoläget skriver aldrig över riktiga anslutningsuppgifter
och en riktig anslutning visas aldrig som ansluten på grund av demodata.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

import app.models as M
from app.db import utcnow
from app.models.enums import IntegrationProvider, IntegrationStatus, LocationType
from app.services import audit, integrations, vehicles
from app.services.places import registry as places
from app.services.places.normalize import haversine_m
from app.services.volvo import collector, detection, oauth
from app.services.volvo.client import ApiResult, QuotaTracker, VolvoError

DEMO_VIN = "DEMO0000000000001"
POLL = timedelta(minutes=5)
SPEED_KMH = 70.0
ROAD_FACTOR = 1.25

# Påhittade platser (namn och adresser finns inte i verkligheten).
PLACES: dict[str, dict[str, Any]] = {
    "home": {"address": "Demovägen 1, Demostad", "name": "Hemmet (demo)", "type": LocationType.HOME,
             "lat": 59.0500, "lon": 14.9000},
    "office": {"address": "Exempelgatan 4, Provköping", "name": "Provköpingskontoret (demo)",
               "type": LocationType.OFFICE, "lat": 59.2700, "lon": 15.2100},
    "customer": {"address": "Testvägen 7, Provköping", "name": "Exempelkunden AB (demo)", "type": LocationType.CUSTOMER,
                 "lat": 59.3050, "lon": 15.2700, "company": "Exempelkunden AB"},
    "fuel": {"address": "Macken 2, Låtsasby", "name": "Bensinstation (demo)", "type": LocationType.FUEL_STATION,
             "lat": 59.1500, "lon": 15.0400},
    "hotel": {"address": "Hotellgränd 3, Fjärrstad", "name": "Hotell Exempel (demo)", "type": LocationType.HOTEL,
              "lat": 58.4100, "lon": 14.9000},
}

SCENARIOS: dict[str, str] = {
    "commute": "Resa mellan hemmet och kontoret, observerad under färd",
    "short_stop": "Resa via bensinstation med 8 minuters stopp (regel A – slås samman automatiskt)",
    "customer_visit": "Kundbesök med 35 minuters stopp (regel B – fråga om sammanslagning)",
    "silent": "Bilen rapporterar först efter ankomst (okänd starttid)",
    "round_trip": "Rundresa utan ny positionsrapport (flaggas som möjlig datalucka)",
}
_ROTATION = ("commute", "short_stop", "customer_visit", "commute", "silent", "round_trip")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _demo_cfg(conn: M.IntegrationConnection) -> dict[str, Any]:
    return dict((conn.config or {}).get("demo") or {})


def _save_demo_cfg(conn: M.IntegrationConnection, demo: dict[str, Any] | None) -> None:
    integrations.update_config(conn, {"demo": demo})


def is_enabled(conn: M.IntegrationConnection | None) -> bool:
    return conn is not None and bool(_demo_cfg(conn).get("enabled"))


class DemoClient:
    """Svarar med Volvo-formade JSON-svar utifrån den simulerade bilens läge."""

    is_demo = True

    def __init__(self, conn: M.IntegrationConnection):
        self._conn = conn
        self.quota = QuotaTracker()

    def _state(self) -> dict[str, Any]:
        state = _demo_cfg(self._conn).get("state")
        if not state:
            raise VolvoError("Demoläget är inte aktiverat.", code="demo_disabled")
        return state

    def close(self) -> None:
        return None

    def list_vehicles(self) -> list[str]:
        return [DEMO_VIN]

    def vehicle_details(self, vin: str) -> ApiResult:
        return ApiResult(
            "ok",
            200,
            data={
                "data": {
                    "vin": DEMO_VIN,
                    "modelYear": 2021,
                    "fuelType": "PETROL/ELECTRIC",
                    "descriptions": {"model": "V90 Recharge T8 (demo)"},
                    "specifications": {"licensePlate": "DEMO123"},
                }
            },
        )

    def fetch(self, endpoint_name: str, vin: str) -> ApiResult:
        s = self._state()
        if endpoint_name == "odometer":
            data: Any = {"data": {"odometer": {"value": s["odometer"], "unit": "km", "timestamp": s["odo_ts"]}}}
        elif endpoint_name == "location":
            data = {
                "status": 200,
                "operationId": "demo",
                "data": {
                    "type": "Feature",
                    "properties": {"heading": "90", "timestamp": s["pos_ts"]},
                    "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"], 0.0]},
                },
            }
        elif endpoint_name == "fuel":
            data = {
                "data": {
                    "fuelAmount": {"value": round(s["fuel"], 1), "unit": "l", "timestamp": s["odo_ts"]},
                    "batteryChargeLevel": {"value": round(s["battery"]), "unit": "%", "timestamp": s["odo_ts"]},
                }
            }
        elif endpoint_name == "statistics":
            ts = s["odo_ts"]
            data = {
                "data": {
                    "averageFuelConsumption": {"value": 5.4, "unit": "l/100km", "timestamp": ts},
                    "averageSpeed": {"value": 62, "unit": "km/h", "timestamp": ts},
                    "tripMeterAutomatic": {"value": s.get("trip_meter", 0.0), "unit": "km", "timestamp": ts},
                    "distanceToEmptyTank": {"value": round(s["fuel"] * 16), "unit": "km", "timestamp": ts},
                    "distanceToEmptyBattery": {"value": round(s["battery"] * 0.45), "unit": "km", "timestamp": ts},
                }
            }
        elif endpoint_name == "engine_status":
            data = {"data": {"engineStatus": {"value": s["engine"], "timestamp": s["engine_ts"]}}}
        elif endpoint_name == "energy_state":
            ts = s["odo_ts"]
            data = {
                "batteryChargeLevel": {"status": "OK", "value": round(s["battery"]), "unit": "percentage", "updatedAt": ts},
                "electricRange": {"status": "OK", "value": round(s["battery"] * 0.45), "unit": "km", "updatedAt": ts},
                "chargingStatus": {"status": "OK", "value": "IDLE", "updatedAt": ts},
                "chargingPower": {"status": "ERROR", "code": "NOT_SUPPORTED", "message": "Stöds inte av bilen"},
            }
        elif endpoint_name == "energy_capabilities":
            data = {
                "batteryChargeLevel": {"isSupported": True},
                "electricRange": {"isSupported": True},
                "chargingStatus": {"isSupported": True},
                "chargingPower": {"isSupported": False},
            }
        elif endpoint_name == "command_accessibility":
            data = {"data": {"availabilityStatus": {"value": "AVAILABLE", "timestamp": s["odo_ts"]}}}
        else:
            # Visar hur en endpoint som bilen saknar stöd för redovisas.
            return ApiResult("unsupported", 404, message="Stöds inte av demobilen")
        return ApiResult("ok", 200, data=data)


# ----------------------------------------------------------------------- uppsättning


def _ensure_places(db: Session, user_id: uuid.UUID) -> None:
    for spec in PLACES.values():
        loc = places.resolve_address(db, user_id, spec["address"], created_from="demo", is_demo=True)
        assert loc is not None
        if not loc.is_demo:
            continue  # adressen finns redan som riktig plats – rör den inte
        loc.name, loc.type, loc.confirmed = spec["name"], spec["type"], True
        loc.latitude, loc.longitude, loc.coordinate_source = spec["lat"], spec["lon"], "demo"
        loc.company = spec.get("company")
        loc.radius_m = 250
    db.flush()


def _mark_synced_now(conn: M.IntegrationConnection, now: datetime) -> None:
    """"Senast synkad" följer den verkliga klockan, inte simuleringens bakåtdaterade hämtningar."""
    if conn.status == IntegrationStatus.DEMO:
        conn.last_sync_at = conn.last_success_at = now


def enable(db: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> M.Vehicle:
    """Aktiverar demoläget: skapar demobilen, påhittade platser och en första observation."""
    now = now or utcnow()
    conn = oauth.connection(db, user_id)
    assert conn is not None
    vehicle = vehicles.demo_vehicle(db, user_id)
    assert vehicle is not None
    vehicle.vin = DEMO_VIN
    vehicle.volvo_connection_id = conn.id
    vehicle.is_active = True
    _ensure_places(db, user_id)
    demo = _demo_cfg(conn)
    if not demo.get("state"):
        start = now - timedelta(hours=10)
        home = PLACES["home"]
        demo = {
            "enabled": True,
            "vehicle_id": str(vehicle.id),
            "scenario_index": 0,
            "state": {
                "place": "home", "odometer": 52340, "odo_ts": _iso(start), "lat": home["lat"], "lon": home["lon"],
                "pos_ts": _iso(start), "fuel": 48.0, "battery": 86.0, "engine": "STOPPED", "engine_ts": _iso(start),
                "trip_meter": 0.0,
            },
        }
    demo["enabled"] = True
    demo["vehicle_id"] = str(vehicle.id)
    _save_demo_cfg(conn, demo)
    # Status "demo" visas bara när ingen riktig anslutning finns.
    if conn.status == IntegrationStatus.NOT_CONFIGURED and not oauth.has_real_credentials(conn):
        conn.status = IntegrationStatus.DEMO
    db.flush()
    client = DemoClient(conn)
    collector.probe_capabilities(db, conn, vehicle, client=client, now=now - timedelta(hours=9, minutes=55))
    if collector.latest_observation(db, vehicle.id) is None:
        collector.poll_vehicle(db, vehicle, now=now - timedelta(hours=9, minutes=50), client=client)
    _mark_synced_now(conn, now)
    audit.record(db, audit.user_actor(user_id), "volvo.demo_enabled", "vehicle", vehicle.id, user_id=user_id)
    db.flush()
    return vehicle


def _demo_vehicle(db: Session, user_id: uuid.UUID) -> M.Vehicle:
    vehicle = vehicles.demo_vehicle(db, user_id, create=False)
    conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    if vehicle is None or not is_enabled(conn):
        raise VolvoError("Demoläget är inte aktiverat.", code="demo_disabled")
    return vehicle


# ----------------------------------------------------------------------- simulering


def _road_km(a: dict[str, Any], b: dict[str, Any]) -> int:
    return max(1, round(haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) / 1000 * ROAD_FACTOR))


class _Sim:
    """Driver den simulerade bilen framåt i tiden och pollar som den riktiga insamlaren."""

    def __init__(self, db: Session, conn: M.IntegrationConnection, vehicle: M.Vehicle, start: datetime):
        self.db, self.conn, self.vehicle = db, conn, vehicle
        self.client = DemoClient(conn)
        self.t = start
        self.polls = 0
        self.new_trip_ids: list[str] = []

    @property
    def state(self) -> dict[str, Any]:
        return dict(_demo_cfg(self.conn)["state"])

    def _set(self, **changes: Any) -> None:
        demo = _demo_cfg(self.conn)
        state = dict(demo["state"])
        state.update(changes)
        demo["state"] = state
        _save_demo_cfg(self.conn, demo)

    def poll(self) -> None:
        result = collector.poll_vehicle(self.db, self.vehicle, now=self.t, client=self.client)
        self.polls += 1
        self.new_trip_ids += result.get("new_trip_ids", [])

    def wait(self, minutes: float) -> None:
        """Bilen står still; pollar var femte minut."""
        end = self.t + timedelta(minutes=minutes)
        while self.t + POLL <= end:
            self.t += POLL
            self.poll()
        self.t = end

    def drive(self, dest: str, *, km: int | None = None, observed: bool = True, report_position: bool = True) -> None:
        s = self.state
        origin = PLACES[s["place"]]
        target = PLACES[dest]
        km = km if km is not None else _road_km(origin, target)
        minutes = max(4.0, km / SPEED_KMH * 60)
        depart, arrive = self.t, self.t + timedelta(minutes=minutes)
        odo0, fuel0, batt0 = s["odometer"], s["fuel"], s["battery"]
        t = depart + POLL
        while t < arrive - timedelta(minutes=1):
            if observed:
                # Under färd uppdateras mätarställningen men inte positionen.
                frac = (t - depart) / (arrive - depart)
                done = int(km * frac)
                self._set(
                    odometer=odo0 + done, odo_ts=_iso(t - timedelta(seconds=30)), engine="RUNNING",
                    engine_ts=_iso(depart + timedelta(seconds=20)), fuel=fuel0 - done * 0.055,
                    battery=max(0.0, batt0 - done * 0.9),
                )
            # observed=False: bilen rapporterar inget under färd – hämtningen ser oförändrat läge.
            self.t = t
            self.poll()
            t += POLL
        changes: dict[str, Any] = {
            "odometer": odo0 + km, "odo_ts": _iso(arrive), "engine": "STOPPED", "engine_ts": _iso(arrive),
            "fuel": fuel0 - km * 0.055, "battery": max(0.0, batt0 - km * 0.9), "trip_meter": float(km),
        }
        if report_position:
            changes.update({"place": dest, "lat": target["lat"], "lon": target["lon"], "pos_ts": _iso(arrive)})
        self._set(**changes)
        self.t = arrive

    def settle(self, factor: float = 1) -> None:
        """Pollar tills resan bekräftats avslutad (längre än inställd stilleståndstid).

        ``factor`` > 1 när bara mätarställningen ändrats: detekteringen väntar då längre
        på en eventuellt eftersläpande positionsrapport.
        """
        from app.services import settings as settings_svc

        settle = float(settings_svc.get_section(self.db, self.vehicle.owner_user_id, "volvo")["settle_minutes"])
        self.wait(factor * settle + 2 * POLL.total_seconds() / 60)


def _duration_estimate(scenario: str) -> timedelta:
    return {
        "commute": timedelta(minutes=70),
        "short_stop": timedelta(minutes=85),
        "customer_visit": timedelta(minutes=130),
        "silent": timedelta(minutes=140),
        "round_trip": timedelta(minutes=90),
    }[scenario]


def _clear_demo_places_on_real_trips(db: Session, loc_ids: list[uuid.UUID]) -> None:
    """Tar bort demoplatsens adress från riktiga, rekonstruerade resor.

    Adressen kom då enbart från demoplatsen (en påhittad uppgift). Importerade resor
    behåller sin originaladress. Journalresorna räknas om.
    """
    from app.services.trips import segments as seg

    names = {
        loc.id: {v for v in (loc.address, loc.name) if v}
        for loc in db.execute(select(M.KnownLocation).where(M.KnownLocation.id.in_(loc_ids))).scalars()
    }
    raws = db.execute(
        select(M.RawTrip).where(
            M.RawTrip.import_batch_id.is_(None),
            M.RawTrip.start_location_id.in_(loc_ids) | M.RawTrip.end_location_id.in_(loc_ids),
        )
    ).scalars().all()
    touched: set[uuid.UUID] = set()
    for raw in raws:
        if raw.start_location_id in names and raw.start_address in names[raw.start_location_id]:
            raw.start_address = None
        if raw.end_location_id in names and raw.end_address in names[raw.end_location_id]:
            raw.end_address = None
        raw.start_location_id = None if raw.start_location_id in names else raw.start_location_id
        raw.end_location_id = None if raw.end_location_id in names else raw.end_location_id
        touched.add(raw.id)
    if not touched:
        return
    db.flush()
    trip_ids = db.execute(
        select(M.TripSegment.logical_trip_id).where(M.TripSegment.raw_trip_id.in_(touched)).distinct()
    ).scalars().all()
    for trip_id in trip_ids:
        trip = seg.load_trip(db, trip_id)
        if trip is not None:
            seg.refresh_logical(db, trip)
    db.flush()


def simulate_trip(db: Session, user_id: uuid.UUID, scenario: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Simulerar en körning med demobilen och kör den genom hela insamlingskedjan."""
    now = now or utcnow()
    vehicle = _demo_vehicle(db, user_id)
    conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    assert conn is not None
    demo = _demo_cfg(conn)
    if scenario is None:
        scenario = _ROTATION[int(demo.get("scenario_index", 0)) % len(_ROTATION)]
    if scenario not in SCENARIOS:
        raise VolvoError("Okänt demoscenario.", code="unknown_scenario")

    last = collector.latest_observation(db, vehicle.id)
    start = now - _duration_estimate(scenario)
    if last is not None:
        start = max(start, last.last_seen_at + timedelta(minutes=1))
    sim = _Sim(db, conn, vehicle, start)
    place = sim.state["place"]
    base = "office" if place == "home" else "home"
    sim.poll()  # bilen står still före avfärd

    if scenario == "commute":
        sim.drive(base)
    elif scenario == "short_stop":
        sim.drive("fuel")
        sim.wait(8)
        sim.drive(base if place != "fuel" else "office")
    elif scenario == "customer_visit":
        origin = place if place != "customer" else "office"
        sim.drive("customer")
        sim.wait(35)
        sim.drive(origin)
    elif scenario == "silent":
        sim.drive("hotel" if place != "hotel" else "home", observed=False)
    elif scenario == "round_trip":
        sim.drive(place, km=24, report_position=False)
    sim.settle(detection.LAG_SETTLE_FACTOR if scenario == "round_trip" else 1)

    _mark_synced_now(conn, now)
    demo = _demo_cfg(conn)
    demo["scenario_index"] = int(demo.get("scenario_index", 0)) + 1
    demo["last_simulated_at"] = now.isoformat()
    _save_demo_cfg(conn, demo)
    db.flush()
    return {
        "scenario": scenario,
        "description": SCENARIOS[scenario],
        "polls": sim.polls,
        "new_trip_ids": list(dict.fromkeys(sim.new_trip_ids)),
        "vehicle_id": str(vehicle.id),
        "ended_at": sim.t.isoformat(),
    }


def tick(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Bakgrundsjobb: håller demobilens synk levande (och kör ev. automatiska demoresor)."""
    now = now or utcnow()
    out = {"polled": 0, "simulated": 0}
    conns = db.execute(
        select(M.IntegrationConnection).where(M.IntegrationConnection.provider == IntegrationProvider.VOLVO)
    ).scalars()
    for conn in list(conns):
        demo = _demo_cfg(conn)
        if not demo.get("enabled"):
            continue
        vehicle = vehicles.demo_vehicle(db, conn.user_id, create=False)
        if vehicle is None:
            continue
        last = collector.latest_observation(db, vehicle.id)
        if demo.get("auto"):
            previous = demo.get("last_simulated_at")
            due = previous is None or (now - datetime.fromisoformat(previous)) >= timedelta(hours=4)
            if due and 6 <= now.hour <= 18:
                simulate_trip(db, conn.user_id, now=now)
                out["simulated"] += 1
                db.commit()
                continue
        if last is None or last.last_seen_at + POLL <= now:
            collector.poll_vehicle(db, vehicle, now=now, client=DemoClient(conn))
            out["polled"] += 1
        db.commit()
    return out


# ----------------------------------------------------------------------- borttagning


def disable(db: Session, user_id: uuid.UUID) -> dict[str, int]:
    """Stänger av demoläget och raderar all demodata (fordon, observationer, resor, platser)."""
    conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    vehicle = vehicles.demo_vehicle(db, user_id, create=False)
    removed = {"trips": 0, "observations": 0, "locations": 0}
    if vehicle is not None:
        vid = vehicle.id
        trip_ids = [t for (t,) in db.execute(select(M.LogicalTrip.id).where(M.LogicalTrip.vehicle_id == vid)).all()]
        removed["trips"] = len(trip_ids)
        if trip_ids:
            req_ids = [
                r for (r,) in db.execute(
                    select(M.NotificationRequest.id).where(M.NotificationRequest.logical_trip_id.in_(trip_ids))
                ).all()
            ]
            if req_ids:
                db.execute(delete(M.NotificationResponse).where(M.NotificationResponse.request_id.in_(req_ids)))
                db.execute(delete(M.NotificationRequest).where(M.NotificationRequest.id.in_(req_ids)))
            for model in (M.TripEvidence, M.TripClassification, M.TripPurpose, M.CalendarMatch, M.LearningFeedback, M.TripSegment):
                db.execute(delete(model).where(model.logical_trip_id.in_(trip_ids)))
        veh_reqs = [
            r for (r,) in db.execute(select(M.NotificationRequest.id).where(M.NotificationRequest.vehicle_id == vid)).all()
        ]
        if veh_reqs:
            db.execute(delete(M.NotificationResponse).where(M.NotificationResponse.request_id.in_(veh_reqs)))
            db.execute(delete(M.NotificationRequest).where(M.NotificationRequest.id.in_(veh_reqs)))
        db.execute(delete(M.ReviewItem).where(M.ReviewItem.vehicle_id == vid))
        db.execute(update(M.LogicalTrip).where(M.LogicalTrip.vehicle_id == vid).values(merged_into_id=None, journey_id=None))
        db.execute(delete(M.LogicalTrip).where(M.LogicalTrip.vehicle_id == vid))
        db.execute(delete(M.RawTrip).where(M.RawTrip.vehicle_id == vid))
        removed["observations"] = (
            db.execute(delete(M.VehicleObservation).where(M.VehicleObservation.vehicle_id == vid)).rowcount or 0
        )
        for model in (
            M.VehicleOdometerSnapshot, M.DataGap, M.TripMerge, M.MergeDecision, M.BusinessJourney, M.FuelRecord,
            M.ChargingRecord, M.ExportJob,
        ):
            db.execute(delete(model).where(model.vehicle_id == vid))
        for batch in db.execute(select(M.ImportBatch).where(M.ImportBatch.vehicle_id == vid)).scalars():
            db.delete(batch)
        db.flush()
        db.execute(delete(M.Vehicle).where(M.Vehicle.id == vid))

    loc_ids = [
        i for (i,) in db.execute(
            select(M.KnownLocation.id).where(M.KnownLocation.owner_user_id == user_id, M.KnownLocation.is_demo.is_(True))
        ).all()
    ]
    if loc_ids:
        removed["locations"] = len(loc_ids)
        _clear_demo_places_on_real_trips(db, loc_ids)
        db.execute(update(M.RawTrip).where(M.RawTrip.start_location_id.in_(loc_ids)).values(start_location_id=None))
        db.execute(update(M.RawTrip).where(M.RawTrip.end_location_id.in_(loc_ids)).values(end_location_id=None))
        db.execute(delete(M.WorkplacePeriod).where(M.WorkplacePeriod.location_id.in_(loc_ids)))
        db.execute(
            delete(M.LocationPattern).where(
                M.LocationPattern.destination_location_id.in_(loc_ids) | M.LocationPattern.origin_location_id.in_(loc_ids)
            )
        )
        db.execute(delete(M.LocationAlias).where(M.LocationAlias.location_id.in_(loc_ids)))
        db.execute(delete(M.KnownLocation).where(M.KnownLocation.id.in_(loc_ids)))

    if conn is not None:
        _save_demo_cfg(conn, None)
        if conn.status == IntegrationStatus.DEMO:
            conn.status = IntegrationStatus.NOT_CONFIGURED
            conn.last_sync_at = conn.last_success_at = None
            conn.capabilities = {}
    audit.record(db, audit.user_actor(user_id), "volvo.demo_disabled", "user", user_id, after=removed, user_id=user_id)
    db.flush()
    db.expire_all()
    return removed


__all__ = ["DEMO_VIN", "SCENARIOS", "DemoClient", "disable", "enable", "is_enabled", "simulate_trip", "tick"]
