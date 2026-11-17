"""1.5.0: starttiden ur bilens egen rapport om motorstarten, och den ligger fast.

Ägarens bil rapporterar mätarställning och position bara när tändningen slås av, men "motorn
igång" direkt, med egen tidsstämpel. Efterbearbetningen letar upp den sista övergången "stod
still" → "motorn igång" före ankomsten: starttiden blir bilens tidsstämpel (eller minuten mitt
emellan två hämtningar som ligger nära varandra), visas som bilens egen tid och ändras sedan
aldrig av sig själv.

Förloppen följer den avidentifierade tidslinjen ur ägarens bil (minuter relativt ankomsten,
fågelväg A–B 7 km och A–C 144 km). Koordinater, tider och adresser är påhittade.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.models as M
from app.api import serializers
from app.models.enums import Category, ReviewStatus, TimeQuality, TripSource
from app.services.ha import messages
from app.services.trips import pipeline, start_time
from app.services.trips import segments as seg
from app.services.volvo import detection
from tests.conftest import reload_trip
from tests.test_v121_starttid import known_trips, live_trip
from tests.test_volvo_support import FakeCar, connect, poll

TZ = ZoneInfo("Europe/Stockholm")
# Framme 08:16 svensk tid (07:16 UTC), som i ägarens skärmbild.
ARR = datetime(2026, 11, 17, 7, 16, tzinfo=UTC)
A = (59.000, 15.000)
C = (60.295, 15.000)  # 144 km fågelväg rakt norr om A


def at(minutes: float) -> datetime:
    return ARR + timedelta(minutes=minutes)


def local(dt: datetime | None) -> str:
    assert dt is not None
    return dt.astimezone(TZ).strftime("%H:%M")


def row(fetched: float, last_seen: float | None = None, *, engine: bool | None, engine_ts: float | None = None,
        base: datetime = ARR):
    """En avläsning för de rena funktionerna (minuter relativt ``base``)."""
    return SimpleNamespace(
        fetched_at=base + timedelta(minutes=fetched),
        last_seen_at=base + timedelta(minutes=last_seen if last_seen is not None else fetched),
        engine_running=engine,
        engine_reported_at=base + timedelta(minutes=engine_ts) if engine_ts is not None else None,
    )


def obs(db, vehicle, fetched: float, last_seen: float | None = None, *, engine: bool | None,
        engine_ts: float | None = None, base: datetime = ARR) -> M.VehicleObservation:
    r = row(fetched, last_seen, engine=engine, engine_ts=engine_ts, base=base)
    o = M.VehicleObservation(
        vehicle_id=vehicle.id, fetched_at=r.fetched_at, last_seen_at=r.last_seen_at, seen_count=1,
        engine_running=engine, engine_reported_at=r.engine_reported_at, content_hash=uuid.uuid4().hex,
        raw={}, errors={}, processed_at=r.fetched_at,
    )
    db.add(o)
    db.flush()
    return o


def estimate(rows, *, km: float, earliest: float | None = -600.0, latest: float | None = None, base=ARR):
    if latest is None:
        latest_dt = base - timedelta(hours=km / detection.MAX_AVG_SPEED_KMH)
    else:
        latest_dt = base + timedelta(minutes=latest)
    return start_time.engine_estimate(
        rows, earliest=base + timedelta(minutes=earliest) if earliest is not None else None, latest=latest_dt,
        ended_at=base, distance_km=km,
    )


# ----------------------------------------------------------------------------- ägarens tre starter


def test_owner_194_km_engine_start_is_the_start():
    """Bekräftad av ägaren: motorn igång 120,6 min före ankomsten, hämtningen före 122,4 min."""
    rows = [row(-483.2, -122.4, engine=False, engine_ts=-1169.1), row(-120.4, -0.3, engine=True, engine_ts=-120.6)]
    kind, when, _, basis = estimate(rows, km=194.0, earliest=-1169.6)
    assert kind == "set" and local(when) == "06:15"
    assert basis["method"] == "engine_start" and basis["window_minutes"] == 2.0
    assert basis["avg_speed_kmh"] == pytest.approx(96.5, abs=0.1)


def test_owner_8_km_starts_are_plausible():
    """8 km på 12,3 min (39 km/h) och 8 km på 7,4 min (65 km/h): båda under taket 70 km/h för 3–10 km."""
    first = [row(-1355.3 + 1186.2, -1203.1 + 1186.2, engine=False), row(-11.9, -1.9, engine=True, engine_ts=-12.3)]
    kind, when, _, basis = estimate(first, km=8.0, earliest=-1000.0)
    assert kind == "set" and basis["avg_speed_kmh"] == pytest.approx(39.0, abs=0.1)
    second = [row(-14.5, -8.5, engine=False), row(-6.5, -0.5, engine=True, engine_ts=-7.4)]
    kind, when, _, basis = estimate(second, km=8.0, earliest=-16.6)
    assert kind == "set" and basis["avg_speed_kmh"] == pytest.approx(64.9, abs=0.1)
    assert start_time.plausible_speed_kmh(8.0) == 70.0
    assert start_time.plausible_speed_kmh(194.0) == detection.MAX_AVG_SPEED_KMH


# ----------------------------------------------------------------------------- reglerna


def test_without_timestamp_the_minute_between_the_polls():
    rows = [row(-40, -20.5, engine=False), row(-18.5, -1, engine=True, engine_ts=None)]
    kind, when, _, basis = estimate(rows, km=12.0)
    assert kind == "set" and when == at(-19)  # mitt emellan −20,5 och −18,5, avrundat
    assert basis["method"] == "poll_midpoint" and basis["window_minutes"] == 2.0
    # En tidsstämpel långt utanför fönstret (t.ex. förra starten) räknas som saknad.
    rows = [row(-40, -20.5, engine=False), row(-18.5, -1, engine=True, engine_ts=-300)]
    assert estimate(rows, km=12.0)[3]["method"] == "poll_midpoint"


def test_long_window_gives_no_start_but_narrows_the_latest():
    """Uppehåll i hämtningen: 40 minuter mellan hämtningarna och ingen tidsstämpel."""
    rows = [row(-90, -60, engine=False), row(-20, -1, engine=True)]
    kind, when, latest, basis = estimate(rows, km=10.0)
    assert kind == "narrow" and when is None and latest == at(-20)
    assert basis["method"] == "engine_window"


def test_last_transition_counts():
    """Motorn startas och stängs av utan att bilen rör sig, sedan start på riktigt."""
    rows = [
        row(-60, -40, engine=False),
        row(-38, -30, engine=True, engine_ts=-38.5),
        row(-28, -14, engine=False, engine_ts=-29),
        row(-12, -1, engine=True, engine_ts=-12.4),
    ]
    kind, when, _, basis = estimate(rows, km=7.0)
    assert kind == "set" and when == at(-12) and basis["method"] == "engine_start"
    # Sågs bilen med motorn av efter starten (före ankomsten) syns ingen pågående körning.
    rows.append(row(-0.8, -0.5, engine=False, engine_ts=-5))
    assert estimate(rows, km=7.0)[0] == "none"


def test_implausibly_late_engine_start_is_only_the_latest_start():
    """Laddhybrid på el: motorn igång 4 min före ankomsten efter 9 km (135 km/h) – för sent."""
    rows = [row(-30, -6, engine=False), row(-4, -1, engine=True, engine_ts=-4.2)]
    kind, when, latest, basis = estimate(rows, km=9.0)
    assert kind == "narrow" and when is None and latest == at(-4.2)
    assert basis["method"] == "engine_late" and basis["max_speed_kmh"] == 70.0


def test_no_engine_start_seen_keeps_todays_rules():
    assert estimate([row(-60, -1, engine=False)], km=5.0)[0] == "none"
    assert estimate([row(-60, -1, engine=None)], km=5.0)[0] == "none"
    # Motorn gick redan i första avläsningen: övergången syns inte.
    assert estimate([row(-60, -1, engine=True, engine_ts=-61)], km=5.0)[0] == "none"
    # Övergången före resans tidigaste gräns hör till en annan körning.
    assert estimate([row(-60, -50, engine=False), row(-48, -1, engine=True, engine_ts=-48.5)], km=30.0,
                    earliest=-30)[0] == "none"


def test_car_that_reports_while_driving():
    """En bil som rapporterar under färd: flera rader med motorn igång (mätaren ökar)."""
    rows = [row(-30, -16, engine=False), row(-14, engine=True, engine_ts=-15.2), row(-9, engine=True, engine_ts=-15.2),
            row(-4, engine=True, engine_ts=-15.2), row(1, engine=False, engine_ts=0.3)]
    kind, when, _, basis = estimate(rows, km=11.0, latest=-9)
    assert kind == "set" and when == at(-15) and basis["method"] == "engine_start"


# ----------------------------------------------------------------------------- ägarens förlopp genom insamlingen


def owner_drive(db, vehicle, car: FakeCar) -> list[dict]:
    """Tidslinjen för 194-kilometersresan, hämtning var 2:a minut."""
    car.lat, car.lon, car.pos_ts = A[0], A[1], at(-1169.6)
    car.odometer, car.odo_ts = 50016, at(-1169.1)
    car.engine, car.engine_ts = "STOPPED", at(-1169.1)
    results = []
    t = -130.4
    while t <= -122.4:
        results.append(poll(db, vehicle, car, at(t)))
        t += 2
    car.engine, car.engine_ts = "RUNNING", at(-120.6)
    t = -120.4
    while t <= -0.3:
        results.append(poll(db, vehicle, car, at(t)))
        t += 2
    car.odometer += 194
    car.odo_ts = at(0.5)
    car.lat, car.lon, car.pos_ts = C[0], C[1], at(0)
    car.engine, car.engine_ts = "STOPPED", at(0.5)
    t = 1.7
    while t <= 12:
        results.append(poll(db, vehicle, car, at(t)))
        t += 2
    return results


@pytest.fixture()
def car(db, user, vehicle) -> FakeCar:
    connect(db, user, vehicle)
    return FakeCar()


def the_trip(db, vehicle) -> M.LogicalTrip:
    (trip,) = seg.active_trips(db, vehicle.id)
    return reload_trip(db, trip.id)


def test_owner_trip_gets_the_engine_start_when_it_appears(db, user, vehicle, car, actor):
    owner_drive(db, vehicle, car)
    trip = the_trip(db, vehicle)
    assert trip.distance_km == pytest.approx(194, abs=0.5)
    assert trip.start_time_quality == TimeQuality.REPORTED
    assert local(trip.started_at) == "06:15" and local(trip.ended_at) == "08:16"
    raw = seg.ordered_raws(trip)[0]
    assert raw.start_estimate_basis["method"] == "engine_start"
    # Visas som bilens egen tid: utan "≈" i appen och på telefonen, och räknas som känd.
    assert start_time.display(trip, TZ) == "06:15"
    assert messages.fmt_trip_times(trip, TZ) == "06:15–08:16"
    assert start_time.known_start(trip) == trip.started_at
    (out,) = serializers.trips_out(db, [trip])
    assert out.start_time_source == "car" and out.start_time_text == "06:15"
    assert out.start_estimate_basis["method"] == "engine_start"


def test_existing_owner_trip_is_corrected_afterwards(db, user, vehicle, car, monkeypatch):
    """Resan skapades i 1.4.2 utan starttid; första efterbearbetningen efter uppdateringen rättar den."""
    with monkeypatch.context() as mp:
        mp.setattr(start_time, "fill_from_engine", lambda db, vehicle_id: 0)
        mp.setattr(start_time, "engine_plan", lambda db, vehicle_id: [])
        owner_drive(db, vehicle, car)
    trip = the_trip(db, vehicle)
    assert trip.start_time_quality in (TimeQuality.BOUNDED, TimeQuality.ESTIMATED)
    assert trip.started_at is None or local(trip.started_at) != "06:15"
    # Ingen ny resa: insamlingen ser att det finns något att rätta och kör efterbearbetningen.
    assert start_time.has_distance_candidates(db, vehicle.id)
    poll(db, vehicle, car, at(30))
    trip = the_trip(db, vehicle)
    assert trip.start_time_quality == TimeQuality.REPORTED and local(trip.started_at) == "06:15"
    events = db.execute(select(M.AuditEvent).where(
        M.AuditEvent.entity_type == "logical_trip", M.AuditEvent.entity_id == str(trip.id),
        M.AuditEvent.action == "trip.start_time_engine",
    )).scalars().all()
    assert len(events) == 1 and events[0].after["start_time_quality"] == TimeQuality.REPORTED
    # Inget mer att göra: nästa hämtning kör inte efterbearbetningen för starttidens skull.
    assert start_time.engine_plan(db, vehicle.id) == []


# ----------------------------------------------------------------------------- ligger fast


def engine_trip(db, vehicle, user, *, km: float = 8.0, ended: datetime = ARR, engine_ts: float | None = -12.3,
                **kw) -> M.LogicalTrip:
    obs(db, vehicle, -40, -14.1, engine=False, base=ended)
    obs(db, vehicle, -12.1, -0.4, engine=True, engine_ts=engine_ts, base=ended)
    obs(db, vehicle, 1.7, engine=False, engine_ts=0.5, base=ended)
    return live_trip(db, vehicle, user, km=km, ended=ended, earliest=ended - timedelta(hours=3), **kw)


def run_pipeline(db, user, vehicle, actor):
    return pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False,
                                        use_llm=False, isolate_steps=True)


def test_engine_start_is_fixed(db, user, vehicle, actor, trips):
    trip = engine_trip(db, vehicle, user)
    out = run_pipeline(db, user, vehicle, actor)
    assert out["start_from_engine"] == 1
    trip = reload_trip(db, trip.id)
    fixed = trip.started_at
    assert local(fixed) == local(at(-12)) and trip.start_time_quality == TimeQuality.REPORTED
    # Ändrad historik: egna resor med kända tider (K4 skulle nu ge en annan tid) och nya avläsningar.
    known_trips(trips, 6, km=8.0, minutes=30, first=datetime(2026, 10, 1, 6, 0, tzinfo=UTC))
    obs(db, vehicle, -11, -3, engine=False, engine_ts=-11.5)
    obs(db, vehicle, -2.5, -1, engine=True, engine_ts=-2.5)
    for _ in range(5):
        out = run_pipeline(db, user, vehicle, actor)
        assert out["start_from_engine"] == 0
        assert start_time.fill_from_distance(db, vehicle.id) == 0
    trip = reload_trip(db, trip.id)
    seg.refresh_logical(db, trip)
    assert trip.started_at == fixed and trip.start_time_quality == TimeQuality.REPORTED
    assert not start_time.engine_plan(db, vehicle.id)
    # Bara ägaren kan ändra den, och då gäller ägarens tid.
    own = fixed - timedelta(minutes=4)
    start_time.set_override(db, trip, own, actor=actor)
    run_pipeline(db, user, vehicle, actor)
    trip = reload_trip(db, trip.id)
    assert trip.started_at == own and trip.start_time_quality == TimeQuality.EXACT


def test_late_engine_start_lets_k4_estimate(db, user, vehicle, actor):
    trip = engine_trip(db, vehicle, user, km=9.0)
    # Motorn igång 4,2 min före ankomsten efter 9 km: för sent; tidsstämpeln i fönstret.
    db.execute(M.VehicleObservation.__table__.update().where(M.VehicleObservation.engine_running.is_(True))
               .values(fetched_at=at(-4), engine_reported_at=at(-4.2)))
    db.execute(M.VehicleObservation.__table__.update().where(
        M.VehicleObservation.engine_running.is_(False), M.VehicleObservation.fetched_at < ARR)
        .values(last_seen_at=at(-6)))
    run_pipeline(db, user, vehicle, actor)
    trip = reload_trip(db, trip.id)
    assert trip.start_time_quality == TimeQuality.ESTIMATED and start_time.from_distance(trip)
    raw = seg.ordered_raws(trip)[0]
    assert raw.start_latest <= at(-4.2) + timedelta(seconds=1)


def test_corrections_afterwards_respect_the_locks(db, user, vehicle, actor):
    # Ägaren har angett starttiden: rörs inte.
    owned = engine_trip(db, vehicle, user)
    start_time.set_override(db, owned, at(-20), actor=actor)
    assert start_time.engine_plan(db, vehicle.id) == []
    # Importerad resa: rörs inte.
    other = ARR - timedelta(days=1)
    imported = engine_trip(db, vehicle, user, ended=other, source=TripSource.VOLVO_IMPORT, odo_start=60000)
    assert start_time.engine_plan(db, vehicle.id) == []
    assert reload_trip(db, imported.id).started_at is None
    # Stängd månad: rörs inte, inte heller en resa som skulle flytta dit.
    oct_end = datetime(2026, 10, 31, 23, 5, tzinfo=UTC)  # 00:05 den 1 november svensk tid
    db.add(M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-10", state="exported"))
    db.flush()
    in_closed = engine_trip(db, vehicle, user, ended=oct_end, odo_start=70000)
    assert start_time.engine_plan(db, vehicle.id) == []
    assert reload_trip(db, in_closed.id).started_at is None


def test_k4_estimate_is_replaced_afterwards(db, user, vehicle, actor):
    """En starttid uppskattad ur sträckan (K4, "≈") ersätts av motorstarten när den syns."""
    trip = engine_trip(db, vehicle, user)
    assert start_time.fill_from_distance(db, vehicle.id) == 1
    assert reload_trip(db, trip.id).start_time_quality == TimeQuality.ESTIMATED
    assert start_time.fill_from_engine(db, vehicle.id) == 1
    trip = reload_trip(db, trip.id)
    assert trip.start_time_quality == TimeQuality.REPORTED and local(trip.started_at) == local(at(-12))
    assert start_time.display(trip, TZ) == local(at(-12))


def test_merge_keeps_the_fixed_start(db, user, vehicle, actor):
    trip = engine_trip(db, vehicle, user)
    start_time.fill_from_engine(db, vehicle.id)
    trip = reload_trip(db, trip.id)
    fixed = trip.started_at
    raw = seg.ordered_raws(trip)[0]
    # En omräkning av journalresan ur delresorna ger samma tid.
    trip.category = Category.WORK
    trip.review_status = ReviewStatus.APPROVED
    seg.refresh_logical(db, trip)
    assert trip.started_at == fixed and raw.start_time_quality == TimeQuality.REPORTED
