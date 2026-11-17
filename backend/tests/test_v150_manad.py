"""1.5.0: regeln om månader vars underlag redan är skapat, och felsökningsfilen för den frågan.

En resa är skyddad bara om den FINNS I månadens skapade fil: månaden har ett skapat underlag
(``exported`` eller ``stale``) och resan skapades senast när filen skapades. En resa som
tillkommit efter filen (t.ex. när novembers fil skapats i förväg) får starttid ur motorstarten,
K4 och förbrukning som vanligt. Samma regel för alla tre stegen (``month_close.protection``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.api.routes import volvo as volvo_routes
from app.models.enums import ExportOrigin, MonthCloseState, TimeQuality, TripSource, ValueQuality
from app.services.audit import SYSTEM
from app.services.export import month_close
from app.services.trips import pipeline, start_time
from app.services.trips import segments as seg
from app.services.volvo import consumption, detection
from tests.conftest import reload_trip
from tests.test_v121_starttid import live_raw
from tests.test_v150_forbrukning import A, B, C, by_km, m, owner_car, replay

VIN = "YV1TESTVIN0000001"
COORD_RE = re.compile(r"(?<![:\d])\d{1,3}\.\d{4,}")
IN_ADVANCE = m(-1300)  # novembers fil skapad i förväg, dagen före resorna


def close_month(db, user, vehicle, *, made: datetime | None, month: str = "2026-11",
                state: str = MonthCloseState.EXPORTED) -> M.MonthClose:
    """Månadens underlag skapat (``made`` = när filen skapades; ``None`` = filen saknas)."""
    job_id = None
    if made is not None:
        job = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, created_at=made, status="done",
                          origin=ExportOrigin.MONTH_CLOSE)
        db.add(job)
        db.flush()
        job_id = job.id
    record = M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month=month, state=state, export_id=job_id,
                          rows_fingerprint="avtryck", notice_count=0)
    db.add(record)
    db.flush()
    return record


def run_pipeline(db, user, vehicle):
    return pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=SYSTEM,
                                        notify=False, use_llm=False, isolate_steps=True)


def fetch_with_pipeline(db, user, vehicle) -> dict[str, M.RawTrip]:
    """Ägarens förlopp ur tidslinjen, och hämtningen som skapar resorna kör efterbearbetningen."""
    obs = owner_car(db, vehicle)
    obs[-1].last_seen_at, obs[-1].seen_count = m(3.7), 2
    db.flush()
    detection.process_vehicle(db, vehicle, user_id=user.id, now=m(4.0), run_pipeline=True,
                              settings={"poll_interval_seconds": 120})
    raws = list(db.execute(select(M.RawTrip).where(M.RawTrip.vehicle_id == vehicle.id)
                           .order_by(M.RawTrip.odometer_start)).scalars())
    for raw in raws:
        db.refresh(raw)
    return by_km(raws)


def assert_owner_values(long: M.RawTrip) -> None:
    # Bilens egen tidsstämpel för motorstarten: 06:15 svensk tid, kvalitet reported.
    assert long.started_at == datetime(2026, 11, 17, 5, 15, tzinfo=UTC)
    assert long.start_time_quality == TimeQuality.REPORTED
    assert long.start_estimate_basis["method"] == "engine_start"
    assert long.fuel_quality == ValueQuality.ESTIMATED
    assert long.fuel_liters == pytest.approx(12.0, abs=1.0)


# ----------------------------------------------------------------------------- ägarens förlopp


@pytest.mark.parametrize("state", [None, MonthCloseState.EXPORTED, MonthCloseState.STALE])
def test_file_made_in_advance_new_trip_gets_start_and_fuel_in_the_same_fetch(db, user, vehicle, state):
    """Novembers fil skapad i förväg den 16:e; resan den 17:e finns inte i den och får starttid
    ur motorstarten och bensin i samma hämtning som den skapas – precis som utan någon rad i
    månadsskiftet (som i dag) och lika för en inaktuell (stale) månad."""
    if state is not None:
        record = close_month(db, user, vehicle, made=IN_ADVANCE, state=state)
    trips = fetch_with_pipeline(db, user, vehicle)
    long = trips["long"]
    assert_owner_values(long)
    locks = month_close.protection(db, vehicle.id)
    assert not locks.in_file(long)
    if state is not None:
        assert locks.files == {"2026-11": IN_ADVANCE}
        db.refresh(record)
        assert record.state == state  # regeln ändrar aldrig månadens läge eller fil


def test_trip_in_the_file_stays_untouched_and_a_trip_after_the_file_is_fixed_afterwards(db, user, vehicle):
    """Resorna skapades av 1.4.1 utan starttid och bensin. Skapades filen EFTER resorna finns de
    i den och lämnas som de är; skapades den FÖRE (i förväg) rättas de i efterhand."""
    trips = by_km(replay(db, user, vehicle))
    long = trips["long"]
    assert (long.started_at, long.fuel_liters) == (None, None)
    record = close_month(db, user, vehicle, made=long.created_at + timedelta(seconds=1))
    run_pipeline(db, user, vehicle)
    db.refresh(long)
    assert (long.started_at, long.start_time_quality) == (None, TimeQuality.BOUNDED)
    assert (long.fuel_liters, long.fuel_quality, long.fuel_basis) == (None, ValueQuality.UNKNOWN, None)
    assert month_close.protection(db, vehicle.id).in_file(long)
    assert start_time.engine_plan(db, vehicle.id) == []
    assert consumption.fill_vehicle(db, vehicle.id) == 0
    # Lika när filens tid inte går att se (ingen fil kopplad: som före 1.5.0, skyddad).
    job_id, record.export_id = record.export_id, None
    db.flush()
    assert month_close.protection(db, vehicle.id).files == {"2026-11": None}
    assert month_close.protection(db, vehicle.id).in_file(long)
    assert consumption.fill_vehicle(db, vehicle.id) == 0

    # Filen skapades i förväg, före resorna: ägarens morgonresa rättas vid nästa efterbearbetning.
    record.export_id = job_id
    db.get(M.ExportJob, job_id).created_at = IN_ADVANCE
    db.flush()
    out = run_pipeline(db, user, vehicle)
    assert out["start_from_engine"] >= 1 and out["consumption_estimated"] >= 1
    db.refresh(long)
    assert_owner_values(long)


def test_a_new_file_later_protects_the_trip_with_the_values_it_already_has(db, user, vehicle):
    """(a) Resan tillkom efter filen och fick sina värden i samma hämtning. Senare skapas en ny
    fil för månaden (stale → exported): då finns resan i den nya filen och rörs inte mer."""
    record = close_month(db, user, vehicle, made=IN_ADVANCE)
    long = fetch_with_pipeline(db, user, vehicle)["long"]
    assert_owner_values(long)
    record.state = MonthCloseState.STALE  # resan ändrade raderna
    db.flush()
    assert not month_close.protection(db, vehicle.id).in_file(long)
    new = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, created_at=datetime.now(UTC) + timedelta(seconds=1),
                      status="done", origin=ExportOrigin.MONTH_CLOSE)
    db.add(new)
    db.flush()
    record.state, record.export_id = MonthCloseState.EXPORTED, new.id
    db.flush()
    assert month_close.protection(db, vehicle.id).in_file(long)
    before = (long.started_at, long.start_time_quality, long.fuel_liters, long.fuel_basis)
    vehicle.usable_battery_kwh = 9.1  # även en kapacitetsändring lämnar resan i filen
    db.flush()
    run_pipeline(db, user, vehicle)
    consumption.refill_after_capacity_change(db, vehicle.id)
    db.refresh(long)
    assert (long.started_at, long.start_time_quality, long.fuel_liters, long.fuel_basis) == before
    assert (long.energy_kwh, long.energy_quality) == (None, ValueQuality.UNKNOWN)


# ----------------------------------------------------------------------------- K4 och undantagen


NOV = datetime(2026, 11, 16, 15, 42, tzinfo=UTC)


def k4_raw(db, vehicle, user, *, ended: datetime, created: datetime, odo: float) -> M.RawTrip:
    raw = live_raw(db, vehicle, user, km=9.0, ended=ended, odo_start=odo)
    raw.created_at = created
    seg.create_logical_for_raw(db, raw)
    db.flush()
    return raw


def test_k4_follows_the_same_rule(db, user, vehicle):
    made = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    close_month(db, user, vehicle, made=made)
    in_file = k4_raw(db, vehicle, user, ended=NOV - timedelta(days=1), created=made - timedelta(minutes=1), odo=50000)
    after = k4_raw(db, vehicle, user, ended=NOV, created=made + timedelta(minutes=1), odo=50100)
    locks = month_close.protection(db, vehicle.id)
    assert start_time.unknown_start_reasons(db, in_file, locks) == ["closed_month"]
    assert start_time.unknown_start_reasons(db, after, locks) == ["no_readings", "pending"]
    assert start_time.fill_from_distance(db, vehicle.id) == 1
    db.refresh(in_file)
    db.refresh(after)
    assert (in_file.started_at, in_file.start_time_quality) == (None, TimeQuality.BOUNDED)
    assert after.start_time_quality == TimeQuality.ESTIMATED
    assert after.start_estimate_basis["method"] == "distance_speed"
    assert start_time.unknown_start_reasons(db, after, locks) == []


def test_a_new_trip_that_would_move_into_another_closed_month_is_left(db, user, vehicle):
    """Regeln "eller som skulle flytta dit" gäller oförändrad: framme 00:05 den 1 november med
    oktobers fil skapad – starttiden skulle flytta resan till oktober och lämnas, även om resan
    tillkom efter oktobers fil."""
    made = datetime(2026, 11, 1, 0, 1, tzinfo=UTC)
    close_month(db, user, vehicle, made=made, month="2026-10")
    raw = k4_raw(db, vehicle, user, ended=datetime(2026, 10, 31, 23, 5, tzinfo=UTC),
                 created=made + timedelta(minutes=5), odo=50000)
    locks = month_close.protection(db, vehicle.id)
    assert locks.month_of(raw.sort_time) == "2026-11" and not locks.in_file(raw)
    assert start_time.fill_from_distance(db, vehicle.id) == 0
    assert start_time.unknown_start_reasons(db, raw, locks)[-1] == "closed_month"
    # Samma resa i en månad med skapad fil men samma månad: får sin tid.
    other = k4_raw(db, vehicle, user, ended=NOV, created=made + timedelta(minutes=5), odo=50200)
    close_month(db, user, vehicle, made=made, month="2026-11")
    assert start_time.fill_from_distance(db, vehicle.id) == 1
    db.refresh(other)
    assert other.start_time_quality == TimeQuality.ESTIMATED


def test_imported_trips_and_the_owners_start_time_are_never_touched(db, user, vehicle):
    """(c) Importerade resor och ägarens angivna starttid rörs aldrig, som i dag – även när resan
    tillkom efter filen."""
    made = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    close_month(db, user, vehicle, made=made)
    owned = k4_raw(db, vehicle, user, ended=NOV, created=made + timedelta(minutes=1), odo=50000)
    trip = db.execute(select(M.LogicalTrip).join(M.TripSegment).where(M.TripSegment.raw_trip_id == owned.id)).scalar_one()
    start_time.set_override(db, reload_trip(db, trip.id), NOV - timedelta(minutes=20), actor=SYSTEM)
    imported = live_raw(db, vehicle, user, km=9.0, ended=NOV + timedelta(hours=3), odo_start=50100,
                        source=TripSource.VOLVO_IMPORT)
    imported.created_at = made + timedelta(minutes=1)
    seg.create_logical_for_raw(db, imported)
    db.flush()
    locks = month_close.protection(db, vehicle.id)
    assert start_time.unknown_start_reasons(db, owned, locks) == ["owner_set"]
    assert start_time.unknown_start_reasons(db, imported, locks) == ["imported"]
    start_time.fill_from_distance(db, vehicle.id)
    db.refresh(imported)
    assert (imported.started_at, imported.start_time_quality) == (None, TimeQuality.BOUNDED)
    assert reload_trip(db, trip.id).started_at == NOV - timedelta(minutes=20)
    assert consumption.unknown_reasons(db, vehicle, [imported], locks)[imported.id] == ("imported", "imported")


def test_without_a_month_close_row_nothing_is_protected(db, user, vehicle):
    locks = month_close.protection(db, vehicle.id)
    assert locks.files == {} and locks.tz is None
    raw = k4_raw(db, vehicle, user, ended=NOV, created=NOV, odo=50000)
    assert not locks.in_file(raw) and not locks.blocks(raw, NOV - timedelta(days=30))
    # En månad som väntar eller är tom har ingen fil: skyddar ingenting.
    close_month(db, user, vehicle, made=None, state=MonthCloseState.WAITING)
    assert month_close.protection(db, vehicle.id).files == {}


# ----------------------------------------------------------------------------- felsökningsfilen


def diag(db, vehicle) -> dict:
    data = volvo_routes.build_diagnostics(db, vehicle, days=14, now=m(10))
    return json.loads(json.dumps(data, ensure_ascii=False))


def test_diagnostics_months_and_reasons_for_a_trip_in_the_file(db, user, vehicle):
    vehicle.vin, vehicle.registration_number = VIN, "ABC123"
    trips = by_km(replay(db, user, vehicle))
    close_month(db, user, vehicle, made=trips["long"].created_at + timedelta(seconds=1))
    close_month(db, user, vehicle, made=None, month="2026-09", state=MonthCloseState.EMPTY)
    run_pipeline(db, user, vehicle)
    data = diag(db, vehicle)
    sept, nov = data["months"]
    assert sept == {"month": "2026-09", "state": "empty", "closed_early": False, "export_created_at": None}
    assert nov["month"] == "2026-11" and nov["state"] == "exported" and nov["closed_early"] is True
    assert nov["export_created_at"].startswith("20")
    long = next(t for t in data["trips"] if t["distance_km"] > 100)
    assert long["protected_by_month"] is True
    assert (long["start_unknown_reason"], long["fuel_unknown_reason"], long["energy_unknown_reason"]) == (
        "closed_month", "closed_month", "closed_month")
    assert_no_leak(db, user, vehicle, data)


def test_diagnostics_reasons_for_a_trip_after_the_file(db, user, vehicle):
    vehicle.vin = VIN
    replay(db, user, vehicle)
    close_month(db, user, vehicle, made=IN_ADVANCE)
    data = diag(db, vehicle)
    long = next(t for t in data["trips"] if t["distance_km"] > 100)
    # Före efterbearbetningen: steget ur motorstarten och bensinen skulle ge värden.
    assert long["protected_by_month"] is False
    assert (long["start_unknown_reason"], long["start_unknown_steps"]) == ("pending", ["pending"])
    assert long["fuel_unknown_reason"] == "pending"
    assert long["energy_unknown_reason"] == "no_capacity"  # årsmodell 2021: kapaciteten okänd
    run_pipeline(db, user, vehicle)
    vehicle.usable_battery_kwh = 9.1
    db.flush()
    data = diag(db, vehicle)
    rows = sorted(data["trips"], key=lambda t: t["odometer_start"])
    out, back, long = rows
    assert (long["start_unknown_reason"], long["start_method"], long["fuel_unknown_reason"]) == (
        None, "engine_start", None)
    assert long["energy_unknown_reason"] == "pending"  # kapaciteten finns nu: fylls i vid nästa körning
    assert back["fuel_unknown_reason"] == "unchanged_level"  # 25 → 25 l ger aldrig 0 l
    assert out["energy_unknown_reason"] == "unchanged_level"  # 100 → 100 %
    assert all(t["protected_by_month"] is False for t in rows)
    assert_no_leak(db, user, vehicle, data)


def test_consumption_reasons_from_the_calculation():
    """Skälen kommer ur beräkningen själv (events i estimate_for_raw)."""
    from tests.test_v150_forbrukning import est, raw_trip, rd

    raw = raw_trip(10.0, 0, 20, odo=1000)
    before = [rd(-30, fuel_liters=30.0, odometer_km=1000.0)]
    refuel = est(raw, before, [rd(21, fuel_liters=45.0, odometer_km=1010.0)])
    assert (refuel.fuel_liters, refuel.fuel_reason) == (None, "refuel")
    nothing = est(raw, before, [])
    assert nothing.fuel_reason == "no_after_level" and nothing.energy_reason == "no_capacity"
    no_before = est(raw, [], [rd(21, fuel_liters=29.0, odometer_km=1010.0)])
    assert no_before.fuel_reason == "no_before_level"
    charged = est(raw, [rd(-30, battery_percent=50.0, odometer_km=1000.0)],
                  [rd(21, battery_percent=80.0, odometer_km=1010.0)], battery_kwh=10.0)
    assert charged.energy_reason == "charging"
    ok = est(raw, before, [rd(21, fuel_liters=29.0, odometer_km=1010.0)])
    assert (ok.fuel_liters, ok.fuel_reason) == (1.0, None)


def test_diagnostics_endpoint_has_the_new_keys(client, db, user, vehicle, monkeypatch):
    vehicle.vin = VIN
    replay(db, user, vehicle)
    close_month(db, user, vehicle, made=IN_ADVANCE)
    db.commit()
    monkeypatch.setattr(volvo_routes, "utcnow", lambda: m(10))
    resp = client.get("/api/v1/volvo/diagnostics?days=14")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "months" in data and data["months"][0]["month"] == "2026-11"
    for trip in data["trips"]:
        assert {"protected_by_month", "start_unknown_reason", "start_unknown_steps", "fuel_unknown_reason",
                "energy_unknown_reason", "start_method", "created_at"} <= set(trip)
    assert_no_leak(db, user, vehicle, data)


def assert_no_leak(db, user, vehicle, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False)
    places = [str(v) for p in (A, B, C) for v in p if str(v) not in ("59.0", "15.0")] + ["59.0629", "59.7428"]
    for secret in (VIN, VIN[-6:], "ABC123", "latitude", "longitude", "heading", '"raw"', str(vehicle.id),
                   str(user.id), *places):
        assert secret not in text, secret
    assert not COORD_RE.search(text)
    for trip in data["trips"]:
        for key in ("start_unknown_reason", "fuel_unknown_reason", "energy_unknown_reason", "start_method"):
            assert trip[key] is None or re.fullmatch(r"[a-z_]{3,40}", trip[key]), (key, trip[key])
    for month in data["months"]:
        assert set(month) == {"month", "state", "closed_early", "export_created_at"}
