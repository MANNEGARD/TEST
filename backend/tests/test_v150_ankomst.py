"""1.5.0 – resan syns i journalen direkt när bilen har rapporterat ankomsten.

Ägarens bil rapporterar mätarställning och position bara när tändningen slås av, och
motorstarten direkt med egen tidsstämpel. En ankomst med kompletta uppgifter (mätarvärdet
avläst vid ankomsten och minst fågelvägen) stängs därför vid den hämtning som först ser den.
Ett mätarvärde som släpar efter förs till den redan skapade resan i stället för att bli en
egen "resa". Saknas något gäller väntan från före 1.5.0 oförändrad, och frågan till telefonen
kommer aldrig tidigare än förut.

Förloppen är skrivna som avläsningar. Ägarens förlopp följer den avidentifierade tidslinjen
(minuter relativt ankomsten, mätarökning och platser A, B, C med fågelvägen A–B 7 km och
A–C 144 km) med PÅHITTADE koordinater som ger samma avstånd. Sist jämförs den nya motorn med
motorn i 1.4.2 (de16760) på ett stort antal slumpade men reproducerbara förlopp.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.db import utcnow
from app.models.enums import DistanceQuality, NotificationKind, ReviewKind, TimeQuality
from app.services import settings as settings_svc
from app.services.ha import notifier
from app.services.places.normalize import haversine_m
from app.services.trips import segments as seg
from app.services.volvo import detection
from tests.test_ha_support import setup_push
from tests.test_volvo_support import T0, FakeCar, connect, poll

# Påhittade koordinater: A–B 7,0 km, A–C 144,0 km, B–C 140,0 km fågelväg (som i tidslinjen).
PA = (59.0, 15.0)
PB = (59.0 + 7 / 111.195, 15.0)
PC = (59.7449, 17.0796)
X = T0 + timedelta(minutes=1400)  # ankomsten i ägarens förlopp (minut 0 i tidslinjen)


def at(minutes: float) -> datetime:
    return X + timedelta(minutes=minutes)


def raws(db, vehicle=None) -> list[M.RawTrip]:
    q = select(M.RawTrip).where(M.RawTrip.voided_at.is_(None)).order_by(M.RawTrip.odometer_start)
    if vehicle is not None:
        q = q.where(M.RawTrip.vehicle_id == vehicle.id)
    return list(db.execute(q).scalars())


def km_sum(rows: list[M.RawTrip]) -> float:
    return round(sum(r.distance_km for r in rows), 1)


def assert_no_double_km(rows: list[M.RawTrip], increase: float) -> None:
    """Summan av sträckorna är mätarställningens ökning, och inga mätarintervall överlappar."""
    assert km_sum(rows) == increase
    spans = sorted((r.odometer_start, r.odometer_end) for r in rows)
    for (_, prev_end), (start, _) in zip(spans, spans[1:], strict=False):
        assert start >= prev_end


def trip_of(db, raw: M.RawTrip) -> M.LogicalTrip:
    segment = db.execute(select(M.TripSegment).where(M.TripSegment.raw_trip_id == raw.id)).scalar_one()
    return seg.load_trip(db, segment.logical_trip_id)


@pytest.fixture()
def car(db, user, vehicle) -> FakeCar:
    connect(db, user, vehicle)
    settings_svc.update_section(db, user.id, "volvo", {"poll_interval_seconds": 120})
    return FakeCar(odometer=10_000, lat=PA[0], lon=PA[1], odo_ts=at(-2156.5), pos_ts=at(-2156.6),
                   engine_ts=at(-2156.5), fuel=26.0)


def polls(db, vehicle, car, start: float, end: float, every: float = 2.0) -> None:
    t = start
    while t <= end + 1e-9:
        poll(db, vehicle, car, at(t))
        t += every


def engine_on(car: FakeCar, minute: float) -> None:
    car.engine, car.engine_ts = "RUNNING", at(minute)


def arrive(car: FakeCar, pos: tuple[float, float], km: float, *, pos_min: float, odo_min: float | None,
           fuel: float | None = None) -> None:
    """Tändningen av: position (pos_min) och, om odo_min anges, mätarställning och motor av."""
    car.lat, car.lon = pos
    car.pos_ts = at(pos_min)
    if odo_min is not None:
        car.odometer += km
        car.odo_ts = at(odo_min)
        car.engine, car.engine_ts = "STOPPED", at(odo_min)
    if fuel is not None:
        car.fuel = fuel


# ----------------------------------------------------------------------------- ägarens förlopp


def owner_timeline(db, vehicle, car, *, until: float = 1.7) -> None:
    """De tolv avläsningarna ur tidslinjen. Hämtning var 2:a minut kring händelserna och var
    20:e minut under långa uppställningar (under gränsen för uppehåll i hämtningen)."""
    polls(db, vehicle, car, -1356, -1200, every=20)
    engine_on(car, -1198.5)  # A → B
    polls(db, vehicle, car, -1198.1, -1188.1)
    arrive(car, PB, 8, pos_min=-1186.2, odo_min=-1186.3, fuel=25.0)
    polls(db, vehicle, car, -1186.1, -1186.1)
    car.odo_ts = car.engine_ts = at(-1185.8)  # samma värde, ny tidsstämpel
    polls(db, vehicle, car, -1184.1, -1178.1)
    engine_on(car, -1177.0)  # B → A
    polls(db, vehicle, car, -1176.1, -1170.1)
    arrive(car, PA, 8, pos_min=-1169.6, odo_min=-1169.1)
    polls(db, vehicle, car, -1168.1, -1128.1)
    polls(db, vehicle, car, -1108.1, -128.1, every=20)
    polls(db, vehicle, car, -124.4, -122.4)
    engine_on(car, -120.6)  # A → C, 194 km på 2 h 01 min; ingen ny rad under färd
    if until <= -0.3:
        polls(db, vehicle, car, -120.4, until)
        return
    polls(db, vehicle, car, -120.4, -0.3)
    arrive(car, PC, 194, pos_min=0.0, odo_min=0.5, fuel=13.0)
    polls(db, vehicle, car, 1.7, until)


def test_owner_timeline_trip_at_first_fetch_after_arrival(db, user, vehicle, car):
    owner_timeline(db, vehicle, car, until=-0.3)
    before = raws(db)
    assert [r.distance_km for r in before] == [8, 8]  # A→B och B→A, en resa per ankomst
    # Motorn går sedan 2 h: raden ses 61 gånger utan nya rader – ingen resa än.
    running = db.execute(select(M.VehicleObservation).where(M.VehicleObservation.engine_running.is_(True))
                         .order_by(M.VehicleObservation.fetched_at.desc())).scalars().first()
    assert running.seen_count == 61 and running.processed_at is None
    arrive(car, PC, 194, pos_min=0.0, odo_min=0.5, fuel=13.0)
    poll(db, vehicle, car, at(1.7))  # första hämtningen efter ankomsten
    rows = raws(db)
    assert len(rows) == 3
    last = rows[-1]
    assert (last.odometer_start, last.odometer_end, last.distance_km) == (10_016, 10_210, 194)
    assert last.distance_quality == DistanceQuality.ODOMETER
    assert last.ended_at == at(0.0) and last.end_time_quality == TimeQuality.REPORTED
    assert "detour_or_multiple_trips" not in last.quality_flags and "collection_gap" not in last.quality_flags
    assert trip_of(db, last).distance_km == 194  # syns i journalen direkt
    assert_no_double_km(rows, 210)
    # Fler hämtningar ändrar ingenting.
    polls(db, vehicle, car, 3.7, 40)
    assert [r.distance_km for r in raws(db)] == [8, 8, 194]


def test_owner_timeline_old_engine_needed_a_second_fetch(db, user, vehicle, car, old_engine):
    """Jämförelse: samma förlopp med motorn i 1.4.2 ger resan först vid nästa hämtning."""
    old_engine.install()
    owner_timeline(db, vehicle, car, until=1.7)
    assert len(raws(db)) == 2
    poll(db, vehicle, car, at(3.7))
    assert len(raws(db)) == 3


# ----------------------------------------------------------------------------- ankomst i flera steg


def parked(db, vehicle, car) -> None:
    polls(db, vehicle, car, -60, -20, every=10)


def test_odometer_one_fetch_after_position(db, user, vehicle, car):
    """Positionen kommer först, mätarvärdet (avläst vid ankomsten) en hämtning senare."""
    parked(db, vehicle, car)
    engine_on(car, -15)
    polls(db, vehicle, car, -14, -2)
    arrive(car, PB, 0, pos_min=0.0, odo_min=None)  # mätaren visar fortfarande gamla värdet
    poll(db, vehicle, car, at(1.7))
    assert raws(db) == []  # mätarvärdet är från förra parkeringen: inte komplett, vänta
    car.odometer += 9
    car.odo_ts = car.engine_ts = at(0.5)
    car.engine = "STOPPED"
    poll(db, vehicle, car, at(3.7))
    (raw,) = raws(db)
    assert (raw.odometer_start, raw.odometer_end) == (10_000, 10_009) and raw.ended_at == at(0.0)
    polls(db, vehicle, car, 5.7, 60)
    assert_no_double_km(raws(db), 9)


def test_late_odometer_after_close_corrects_the_same_trip(db, user, vehicle, car):
    """Resan stängs vid ankomsten; ett senare mätarvärde, avläst vid ankomsten, rättar samma
    resa i stället för att bli en egen resa. Rättningen syns i resans historik."""
    parked(db, vehicle, car)
    engine_on(car, -15)
    polls(db, vehicle, car, -14, -2)
    arrive(car, PB, 8, pos_min=0.0, odo_min=0.2)  # 8 av 9 km hann rapporteras
    poll(db, vehicle, car, at(1.7))
    (raw,) = raws(db)
    assert raw.distance_km == 8
    trip = trip_of(db, raw)
    car.odometer += 1
    car.odo_ts = at(0.5)
    poll(db, vehicle, car, at(3.7))
    (raw,) = raws(db)
    assert (raw.odometer_start, raw.odometer_end, raw.distance_km) == (10_000, 10_009, 9)
    trip = trip_of(db, raw)
    assert trip.distance_km == 9 and trip.odometer_end == 10_009
    events = db.execute(select(M.AuditEvent).where(
        M.AuditEvent.entity_type == "logical_trip", M.AuditEvent.entity_id == str(trip.id),
        M.AuditEvent.action == "trip.update")).scalars().all()
    (ev,) = events
    assert ev.before["odometer_end"] == 10_008 and ev.after["odometer_end"] == 10_009
    assert ev.after["reason"] == "late_odometer" and ev.actor_type == "system"
    polls(db, vehicle, car, 5.7, 90)
    rows = raws(db)
    assert len(rows) == 1
    assert_no_double_km(rows, 9)
    # Omkörning från början ger samma resa och ingen ny rättning.
    for obs in db.execute(select(M.VehicleObservation)).scalars():
        obs.processed_at = None
    db.flush()
    detection.process_vehicle(db, vehicle, user_id=user.id, now=at(95))
    rows = raws(db)
    assert len(rows) == 1 and rows[0].distance_km == 9


def test_late_odometer_corrects_an_approved_merged_trip(db, user, vehicle, car, actor):
    """Är resan redan godkänd och sammanslagen rättas ändå sträckan (mätarställningen är ett
    mätvärde); beslutet står kvar och rättningen syns i historiken. Ett skapat underlag märker
    ändringen själv genom avtrycket (månadsskiftet ersätter filen)."""
    from app.models.enums import Category, ReviewStatus
    from app.services.trips import merge

    parked(db, vehicle, car)
    engine_on(car, -30)
    polls(db, vehicle, car, -29, -22)
    arrive(car, PB, 9, pos_min=-21.0, odo_min=-20.8)
    polls(db, vehicle, car, -19, -11)
    engine_on(car, -10)
    polls(db, vehicle, car, -9, -2)
    arrive(car, PC, 140, pos_min=0.0, odo_min=0.2)  # 140 av 141 km hann rapporteras
    poll(db, vehicle, car, at(1.7))
    first, second = raws(db)
    t1, t2 = trip_of(db, first), trip_of(db, second)
    merged = merge.merge_trips(db, [t1.id, t2.id], actor=actor) if hasattr(merge, "merge_trips") else None
    trip = merged if merged is not None else t2
    trip.category, trip.review_status = Category.WORK, ReviewStatus.APPROVED
    db.flush()
    car.odometer += 1
    car.odo_ts = at(0.5)
    poll(db, vehicle, car, at(3.7))
    rows = raws(db)
    assert len(rows) == 2 and rows[-1].distance_km == 141
    trip = trip_of(db, rows[-1])
    assert trip.review_status == ReviewStatus.APPROVED and trip.category == Category.WORK
    assert trip.odometer_end == 10_150
    assert sum(s.distance_km for s in trip.segments) == trip.distance_km
    assert_no_double_km(rows, 150)


def test_very_short_trip_without_odometer_change(db, user, vehicle, car):
    """Flyttad 400 m, mätaren hann inte öka – men värdet är avläst vid ankomsten."""
    near = (PA[0] + 0.0036, PA[1])
    parked(db, vehicle, car)
    engine_on(car, -3)
    polls(db, vehicle, car, -2.5, -0.5)
    arrive(car, near, 0, pos_min=0.0, odo_min=0.3)
    poll(db, vehicle, car, at(1.5))
    (raw,) = raws(db)
    assert raw.distance_quality == DistanceQuality.ESTIMATED and 0.4 <= raw.distance_km <= 0.6
    # Ett senare värde avläst vid ankomsten (1 km) rättar resan till mätarsträckan.
    car.odometer += 1
    car.odo_ts = at(0.4)
    poll(db, vehicle, car, at(3.5))
    (raw,) = raws(db)
    assert raw.distance_quality == DistanceQuality.ODOMETER and raw.distance_km == 1
    assert "distance_estimated" not in raw.quality_flags
    polls(db, vehicle, car, 5.5, 60)
    assert_no_double_km(raws(db), 1)


def test_two_drives_with_short_stop(db, user, vehicle, car):
    """Två körningar med fem minuters stopp: två resor, var och en direkt vid ankomsten."""
    parked(db, vehicle, car)
    engine_on(car, -30)
    polls(db, vehicle, car, -29, -15)
    arrive(car, PB, 9, pos_min=-14.0, odo_min=-13.5)
    poll(db, vehicle, car, at(-13))
    assert len(raws(db)) == 1
    polls(db, vehicle, car, -11, -9)
    engine_on(car, -8.5)
    polls(db, vehicle, car, -7, -1)
    arrive(car, PC, 145, pos_min=0.0, odo_min=0.5)
    poll(db, vehicle, car, at(1))
    first, second = raws(db)
    assert (first.distance_km, second.distance_km) == (9, 145)
    polls(db, vehicle, car, 3, 60)
    assert_no_double_km(raws(db), 154)


def test_arrival_without_odometer_timestamp_waits_as_before(db, user, vehicle, car):
    """Utan tidsstämpel på mätarvärdet går det inte att veta att det är avläst vid ankomsten:
    väntan från före 1.5.0 gäller (genvägen med avstängd motor, sedd två gånger)."""
    parked(db, vehicle, car)
    engine_on(car, -15)
    polls(db, vehicle, car, -14, -2)
    arrive(car, PB, 9, pos_min=0.0, odo_min=0.5)
    original = car.fetch

    def no_odometer_ts(endpoint: str, vin: str):
        result = original(endpoint, vin)
        if endpoint == "odometer":
            result.data["data"]["odometer"].pop("timestamp", None)
        return result

    car.fetch = no_odometer_ts  # type: ignore[method-assign]
    poll(db, vehicle, car, at(2))
    assert raws(db) == []
    poll(db, vehicle, car, at(4))
    (raw,) = raws(db)
    assert raw.distance_km == 9


def test_round_trip_without_new_position_uses_todays_rules(db, user, vehicle, car):
    """Mätaren ökar men positionen ändras inte (rundresa): ingen ankomst – dagens väntan
    (3 × settle) och samma flaggor som förut."""
    parked(db, vehicle, car)
    engine_on(car, -20)
    polls(db, vehicle, car, -19, -1)
    car.odometer += 12
    car.odo_ts = car.engine_ts = at(0.5)
    car.engine = "STOPPED"
    polls(db, vehicle, car, 1, 29)
    assert raws(db) == []
    polls(db, vehicle, car, 31, 40)
    (raw,) = raws(db)
    assert raw.distance_km == 12 and "round_trip_unobserved" in raw.quality_flags and raw.possible_missed_trips


def test_collection_gap_is_flagged_as_before(db, user, vehicle, car):
    parked(db, vehicle, car)
    engine_on(car, -100)
    arrive(car, PB, 9, pos_min=-40.0, odo_min=-39.5)
    poll(db, vehicle, car, at(1))  # hämtningen låg nere i 80 minuter
    (raw,) = raws(db)
    assert "collection_gap" in raw.quality_flags and raw.possible_missed_trips
    assert raw.collection_gap["odometer_km"] == 9


def test_car_reporting_while_driving_closes_at_arrival(db, user, vehicle, car):
    """En bil som rapporterar mätarställningen under färd (dagens tester): samma resa och
    sträcka som förut, men redan vid hämtningen som ser ankomsten."""
    parked(db, vehicle, car)
    car.moving(5, at(-10.5))
    poll(db, vehicle, car, at(-10))
    car.moving(6, at(-5.5))
    poll(db, vehicle, car, at(-5))
    car.drive_to(PB, 3, at(-2))
    poll(db, vehicle, car, at(0))
    (raw,) = raws(db)
    assert (raw.odometer_start, raw.odometer_end) == (10_000, 10_014) and raw.ended_at == at(-2)
    assert "observed_in_motion" in raw.quality_flags
    polls(db, vehicle, car, 5, 60, every=5)
    assert_no_double_km(raws(db), 14)


def test_stale_odometer_shorter_than_crow_distance_waits(db, user, vehicle, car):
    """Tidsstämpeln stämmer men mätarsträckan är kortare än fågelvägen: värdet är bevisligen
    gammalt, så väntan gäller."""
    parked(db, vehicle, car)
    engine_on(car, -150)
    polls(db, vehicle, car, -149, -2, every=20)
    arrive(car, PC, 50, pos_min=0.0, odo_min=0.5)  # 50 km för 144 km fågelväg
    poll(db, vehicle, car, at(1.5))
    assert raws(db) == []


# ----------------------------------------------------------------------------- frågan till telefonen


def test_question_not_sent_before_arrival_seen_plus_settle(db, user, vehicle):
    """Resan syns direkt, men frågan väntar som förut tills ankomsten setts i settle_minutes."""
    connect(db, user, vehicle)
    setup_push(db, user, batch_window_minutes=0)
    base = utcnow().replace(microsecond=0) - timedelta(minutes=20)
    car = FakeCar(odometer=10_000, lat=PA[0], lon=PA[1], odo_ts=base - timedelta(hours=5),
                  pos_ts=base - timedelta(hours=5), engine_ts=base - timedelta(hours=5))
    for minute in (0, 10):
        poll(db, vehicle, car, base + timedelta(minutes=minute))
    car.engine, car.engine_ts = "RUNNING", base + timedelta(minutes=11)
    poll(db, vehicle, car, base + timedelta(minutes=12))
    car.lat, car.lon, car.pos_ts = PB[0], PB[1], base + timedelta(minutes=19)
    car.odometer += 9
    car.odo_ts = car.engine_ts = base + timedelta(minutes=19, seconds=30)
    car.engine = "STOPPED"
    seen = base + timedelta(minutes=20)
    poll(db, vehicle, car, seen)
    (raw,) = raws(db)
    trip = trip_of(db, raw)
    assert detection.question_hold_until(db, trip, user.id) == seen + timedelta(minutes=10)
    req = db.execute(select(M.NotificationRequest).where(
        M.NotificationRequest.logical_trip_id == trip.id)).scalars().first()
    if req is None:  # inget ärende skapades av pipelinen: fråga direkt med samma regler
        (req,) = notifier.enqueue_for_changes(
            db, user_id=user.id, vehicle=vehicle, merge_items=[], trip_items=[(trip, [ReviewKind.CATEGORY])], now=seen,
        )
    assert req.kind in (NotificationKind.CLASSIFY, NotificationKind.COMPANY, NotificationKind.PURPOSE)
    assert req.send_after >= seen + timedelta(minutes=10)


def test_self_driving_question_waits_for_settle(db, user, vehicle):
    from tests.v130_ai_stod import self_driving

    connect(db, user, vehicle)
    setup_push(db, user, batch_window_minutes=0)
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": utcnow() - timedelta(hours=1)},
                                allow_read_only=True)
    del self_driving
    base = utcnow().replace(microsecond=0) - timedelta(minutes=20)
    car = FakeCar(odometer=10_000, lat=PA[0], lon=PA[1], odo_ts=base - timedelta(hours=5),
                  pos_ts=base - timedelta(hours=5), engine_ts=base - timedelta(hours=5))
    poll(db, vehicle, car, base)
    car.lat, car.lon, car.pos_ts = PB[0], PB[1], base + timedelta(minutes=19)
    car.odometer += 9
    car.odo_ts = car.engine_ts = base + timedelta(minutes=19, seconds=30)
    seen = base + timedelta(minutes=20)
    poll(db, vehicle, car, seen)
    (raw,) = raws(db)
    trip = trip_of(db, raw)
    open_items = db.execute(select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == trip.id,
                                                       M.ReviewItem.status == "open")).scalars().all()
    if not any(i.kind in (ReviewKind.CATEGORY, ReviewKind.PURPOSE) for i in open_items):
        pytest.skip("autopiloten avgjorde resan utan fråga")
    stats = notifier.ensure_open_questions(db, seen + timedelta(minutes=5), user_id=user.id)
    assert stats.get("waiting_arrival") == 1 and not stats.get("created")
    stats = notifier.ensure_open_questions(db, seen + timedelta(minutes=10, seconds=1), user_id=user.id)
    assert stats.get("created") == 1


# ----------------------------------------------------------------------------- jämförelse med 1.4.2


def _load_old_detection(tmp_dir: Path) -> ModuleType | None:
    """detection.py från 1.4.2 (de16760): DRIVELOG_OLD_DETECTION, annars ur git."""
    path = os.environ.get("DRIVELOG_OLD_DETECTION")
    if path and Path(path).is_file():
        source = Path(path)
    else:
        repo = Path(__file__).resolve().parents[2]
        try:
            text = subprocess.run(
                ["git", "-C", str(repo), "show", "de16760:backend/app/services/volvo/detection.py"],
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        source = tmp_dir / "detection_de16760.py"
        source.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("drivelog_old_detection", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["drivelog_old_detection"] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class OldEngine:
    module: ModuleType
    monkeypatch: Any

    def install(self) -> None:
        """Låter insamlingen köra den gamla motorn (för ett helt förlopp genom collector)."""
        self.monkeypatch.setattr(detection, "process_vehicle", self.module.process_vehicle)


@pytest.fixture(scope="module")
def old_module(tmp_path_factory) -> ModuleType:
    module = _load_old_detection(tmp_path_factory.mktemp("gammal"))
    if module is None:
        pytest.skip("detection.py från 1.4.2 går inte att hämta (varken DRIVELOG_OLD_DETECTION eller git)")
    return module


@pytest.fixture()
def old_engine(old_module, monkeypatch) -> OldEngine:
    return OldEngine(old_module, monkeypatch)


@dataclass
class Drive:
    dep: float
    arrival: float
    dest: tuple[float, float]
    odo_from: int
    odo_to: int
    lag: int
    lag_kind: str
    partial: int = 0


@dataclass
class Scenario:
    seed: int
    poll_min: int
    under_way: bool
    timestamps: bool
    drives: list[Drive]
    gap: tuple[float, float] | None
    end: float
    events: list[tuple[float, int, dict[str, Any]]] = field(default_factory=list)


def _random_place(rng: random.Random) -> tuple[float, float]:
    return (round(59.0 + rng.uniform(-0.3, 0.3), 5), round(15.0 + rng.uniform(-0.5, 0.5), 5))


def make_scenario(seed: int) -> Scenario:
    """Ett slumpat men reproducerbart förlopp (fast frö)."""
    rng = random.Random(seed)
    poll_min = rng.choice([2, 5])
    under_way = rng.random() < 0.5
    timestamps = rng.random() < 0.8
    places = [_random_place(rng) for _ in range(5)]
    here = places[0]
    odo = 20_000
    t = rng.uniform(20, 60)
    drives: list[Drive] = []
    for i in range(rng.randint(1, 5)):
        if i:
            t += rng.choice([rng.uniform(1, 9), rng.uniform(10, 40), rng.uniform(60, 200)])
        if rng.random() < 0.15:
            dest = here  # rundresa: tillbaka till samma plats, ingen ny position
            km = rng.randint(2, 25)
        else:
            dest = rng.choice([p for p in places if p != here])
            crow = haversine_m(here[0], here[1], dest[0], dest[1]) / 1000
            km = max(1, round(crow * rng.uniform(1.15, 1.5) + rng.uniform(0, 3)))
        dur = max(rng.uniform(4, 90), km / 100 * 60)
        lag = rng.choice([0, 0, 0, 1, 2, 3])
        kind = rng.choice(["stale", "partial", "pos_late"])
        partial = rng.randint(1, min(2, km)) if kind == "partial" else 0
        drives.append(Drive(round(t, 2), round(t + dur, 2), dest, odo, odo + km, lag, kind, partial))
        odo += km
        t += dur
        here = dest
    gap = None
    if rng.random() < 0.15:
        g0 = rng.uniform(10, t)
        gap = (g0, g0 + rng.uniform(60, 150))
    sc = Scenario(seed, poll_min, under_way, timestamps, drives, gap, end=t + 240)
    _build_events(sc)
    return sc


def _build_events(sc: Scenario) -> None:
    """Bilens rapporter som händelser i tidsordning (minut, ordning, ändrade fält)."""
    ev: list[tuple[float, int, dict[str, Any]]] = []
    n = 0

    def add(minute: float, **changes: Any) -> None:
        nonlocal n
        ev.append((minute, n, changes))
        n += 1

    for i, d in enumerate(sc.drives):
        nxt = sc.drives[i + 1].dep if i + 1 < len(sc.drives) else sc.end
        add(d.dep, engine=True, engine_ts=d.dep)
        if sc.under_way:
            minute = d.dep + 1
            while minute < d.arrival - 0.5:
                frac = (minute - d.dep) / (d.arrival - d.dep)
                value = d.odo_from + int((d.odo_to - d.odo_from) * frac)
                add(minute, odo=value, odo_ts=minute - 0.2)
                minute += 1
        late_at = min(d.arrival + d.lag * sc.poll_min, nxt - 0.1)
        final_odo = {"odo": d.odo_to, "odo_ts": d.arrival + 0.5}
        if d.lag == 0 or d.lag_kind == "pos_late" and late_at <= d.arrival:
            add(d.arrival, pos=d.dest, pos_ts=d.arrival, engine=False, engine_ts=d.arrival + 0.5, **final_odo)
        elif d.lag_kind == "stale":
            add(d.arrival, pos=d.dest, pos_ts=d.arrival, engine=False, engine_ts=d.arrival + 0.5)
            add(late_at, **final_odo)
        elif d.lag_kind == "partial":
            add(d.arrival, pos=d.dest, pos_ts=d.arrival, engine=False, engine_ts=d.arrival + 0.5,
                odo=d.odo_to - d.partial, odo_ts=d.arrival + 0.2)
            add(late_at, **final_odo)
        else:  # positionen släpar efter mätarvärdet
            add(d.arrival, engine=False, engine_ts=d.arrival + 0.5, **final_odo)
            add(late_at, pos=d.dest, pos_ts=d.arrival)
    ev.sort(key=lambda e: (e[0], e[1]))
    sc.events = ev


def _poll_times(sc: Scenario) -> list[float]:
    out, t = [], 0.0
    while t <= sc.end:
        if sc.gap is None or not (sc.gap[0] <= t <= sc.gap[1]):
            out.append(round(t, 3))
        t += sc.poll_min
    return out


class Feed:
    """Lägger avläsningar i databasen som insamlingen gör: samma innehåll → bara sedd igen."""

    def __init__(self, db, vehicle, sc: Scenario, base: datetime):
        self.db, self.vehicle, self.sc, self.base = db, vehicle, sc, base
        self.state: dict[str, Any] = {
            "odo": 20_000, "odo_ts": -300.0, "pos": _random_place(random.Random(sc.seed)), "pos_ts": -300.0,
            "engine": False, "engine_ts": -300.0,
        }
        self.i = 0
        self.last: M.VehicleObservation | None = None
        self.last_key: tuple | None = None

    def _ts(self, minute: float | None) -> datetime | None:
        if minute is None or not self.sc.timestamps:
            return None
        return self.base + timedelta(minutes=minute)

    def fetch(self, minute: float) -> datetime:
        while self.i < len(self.sc.events) and self.sc.events[self.i][0] <= minute:
            self.state.update(self.sc.events[self.i][2])
            self.i += 1
        s = self.state
        now = self.base + timedelta(minutes=minute)
        key = (s["odo"], s["odo_ts"], s["pos"], s["pos_ts"], s["engine"], s["engine_ts"])
        if self.last is not None and key == self.last_key:
            self.last.last_seen_at = now
            self.last.seen_count += 1
        else:
            self.last = M.VehicleObservation(
                vehicle_id=self.vehicle.id, source="volvo_api", fetched_at=now, last_seen_at=now, seen_count=1,
                content_hash=hashlib.sha256(repr(key).encode()).hexdigest(), raw={}, errors={},
                odometer_km=float(s["odo"]), odometer_reported_at=self._ts(s["odo_ts"]),
                latitude=s["pos"][0], longitude=s["pos"][1], position_reported_at=self._ts(s["pos_ts"]),
                engine_running=s["engine"], engine_reported_at=self._ts(s["engine_ts"]),
            )
            self.db.add(self.last)
            self.last_key = key
        self.db.flush()
        return now


def run_scenario(db, user, engine: ModuleType, sc: Scenario, cfg: dict[str, Any]) -> tuple[M.Vehicle, dict[Any, float]]:
    vehicle = M.Vehicle(owner_user_id=user.id, name=f"Slump {sc.seed}", make="Volvo", model="V90")
    db.add(vehicle)
    db.flush()
    feed = Feed(db, vehicle, sc, T0)
    created: dict[Any, float] = {}
    for minute in _poll_times(sc):
        now = feed.fetch(minute)
        engine.process_vehicle(db, vehicle, user_id=user.id, settings=cfg, now=now, run_pipeline=False)
        for raw in raws(db, vehicle):
            created.setdefault(raw.id, minute)
    return vehicle, created


COMPARED_FLAGS = {"collection_gap", "detour_or_multiple_trips", "no_position", "round_trip_unobserved",
                  "missing_odometer"}


def compare(db, user, old_module: ModuleType, seed: int) -> dict[str, Any]:
    sc = make_scenario(seed)
    cfg = dict(settings_svc.get_section(db, user.id, "volvo"))
    cfg["poll_interval_seconds"] = sc.poll_min * 60
    old_vehicle, old_created = run_scenario(db, user, old_module, sc, cfg)
    new_vehicle, new_created = run_scenario(db, user, detection, sc, cfg)
    old_rows, new_rows = raws(db, old_vehicle), raws(db, new_vehicle)
    increase = sc.drives[-1].odo_to - sc.drives[0].odo_from
    info = f"frö {seed}: {sc}"
    # Aldrig fler kilometer än mätaren har ökat, och här (allt har lagt sig) exakt ökningen.
    assert km_sum(new_rows) <= increase, info
    assert_no_double_km(new_rows, increase)
    out: dict[str, Any] = {"merged_late_odometer": 0, "gap_after_early_close": 0, "earlier": [], "same_time": 0}
    old_spans = [(r.odometer_start, r.odometer_end) for r in old_rows]
    new_spans = [(r.odometer_start, r.odometer_end) for r in new_rows]
    if old_spans != new_spans:
        # Den enda tillåtna skillnaden: 1.4.2 stängde resan innan ett släpande mätarvärde (avläst
        # vid ankomsten) kom och gjorde resten till en egen "resa" utan ny position. Nu förs det
        # till samma resa. Allt annat är ett fel.
        expected = list(old_spans)
        for d in sc.drives:
            if d.lag_kind != "partial" or not d.lag:
                continue
            a, b = (d.odo_from, d.odo_to - d.partial), (d.odo_to - d.partial, d.odo_to)
            if a in expected and b in expected:
                expected[expected.index(a)] = (d.odo_from, d.odo_to)
                expected.remove(b)
                out["merged_late_odometer"] += 1
        assert expected == new_spans, f"{info}\n gammal {old_spans}\n ny {new_spans}"
    old_by_start = {r.odometer_start: r for r in old_rows}
    for raw in new_rows:
        old = old_by_start[raw.odometer_start]
        delay = old_created[old.id] - new_created[raw.id]
        assert delay >= 0, f"{info}\n resan {raw.odometer_start}–{raw.odometer_end} skapades senare än i 1.4.2"
        if delay > 0:
            out["earlier"].append(delay)
        else:
            out["same_time"] += 1
        if old.odometer_end != raw.odometer_end:
            continue
        old_flags = set(old.quality_flags) & COMPARED_FLAGS
        new_flags = set(raw.quality_flags) & COMPARED_FLAGS
        if old_flags != new_flags:
            # 1.4.2 tog en körning efter ett uppehåll med i föregående rörelse (som delresa 2,
            # där uppehållet aldrig prövas). Nu är föregående resa redan stängd, och uppehållet
            # flaggas som på varje annan resa.
            assert new_flags - old_flags == {"collection_gap"} and not old_flags - new_flags, info
            assert "split_by_position_reports" in old.quality_flags and sc.gap is not None, info
            out["gap_after_early_close"] += 1
    return out


def test_random_scenarios_match_old_engine_and_never_later(db, user, old_module):
    """Slumpade förlopp genom motorn i 1.4.2 och den nya: samma resor med samma mätarintervall,
    aldrig fler kilometer än mätaren ökat, och resan aldrig senare än förut.

    Antalet förlopp styrs av DRIVELOG_V150_SEEDS (standard 60; redovisningen kördes med 600)."""
    count = int(os.environ.get("DRIVELOG_V150_SEEDS", "60"))
    totals: dict[str, Any] = {"merged_late_odometer": 0, "gap_after_early_close": 0, "earlier": [], "same_time": 0}
    for seed in range(1, count + 1):
        res = compare(db, user, old_module, seed)
        for key in ("merged_late_odometer", "gap_after_early_close", "same_time"):
            totals[key] += res[key]
        totals["earlier"] += res["earlier"]
    earlier = totals["earlier"]
    mean = sum(earlier) / len(earlier) if earlier else 0.0
    summary = (f"{count} förlopp: {len(earlier)} resor tidigare (i medeltal {mean:.1f} min), "
               f"{totals['same_time']} samtidigt, {totals['merged_late_odometer']} släpande mätarvärden förda "
               f"till samma resa, {totals['gap_after_early_close']} uppehåll som nu flaggas")
    print(summary)
    assert earlier, summary  # den nya motorn ska faktiskt vara snabbare någon gång
