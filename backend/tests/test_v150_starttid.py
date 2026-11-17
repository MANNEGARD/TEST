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


def test_minute_between_the_polls_is_estimated(db, user, vehicle, actor):
    """Minuten mitt emellan är uträknad: "≈" och "ca", aldrig bilens tid – men den ligger fast."""
    trip = engine_trip(db, vehicle, user, engine_ts=None)
    db.execute(M.VehicleObservation.__table__.update().where(
        M.VehicleObservation.engine_running.is_(False), M.VehicleObservation.fetched_at < ARR)
        .values(last_seen_at=at(-14.1)))
    db.execute(M.VehicleObservation.__table__.update().where(M.VehicleObservation.engine_running.is_(True))
               .values(fetched_at=at(-12.1)))
    assert start_time.fill_from_engine(db, vehicle.id) == 1
    trip = reload_trip(db, trip.id)
    raw = seg.ordered_raws(trip)[0]
    assert raw.start_estimate_basis["method"] == "poll_midpoint" and raw.start_estimate_basis["margin_min"] == 1.0
    assert trip.start_time_quality == TimeQuality.ESTIMATED and local(trip.started_at) == local(at(-13))
    assert start_time.display(trip, TZ).startswith("≈")
    assert start_time.known_start(trip) == trip.started_at
    (out,) = serializers.trips_out(db, [trip])
    assert out.start_time_source == "estimated"
    # Ligger fast: varken steget eller K4 rör den igen.
    assert start_time.engine_plan(db, vehicle.id) == []
    assert start_time.fill_from_distance(db, vehicle.id) == 0
    assert reload_trip(db, trip.id).started_at == trip.started_at


def test_unreadable_engine_status_midtrip_is_ignored():
    """En hämtning där motorstatusen inte kunde läsas mitt i resan är ingen motorstart."""
    rows = [
        row(-200, -56, engine=False),
        row(-54.5, -28, engine=True, engine_ts=-55),
        row(-26, engine=None),
        row(-24, -0.4, engine=True, engine_ts=-55),
        row(1.7, engine=False, engine_ts=0.5),
    ]
    kind, when, _, basis = estimate(rows, km=40.0)
    assert kind == "set" and when == at(-55) and basis["method"] == "engine_start"


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


# ----------------------------------------------------------------------------- "bilen används" (command-accessibility)
# Det är inte prövat att den riktiga bilen svarar CAR_IN_USE under färd: förloppen nedan är
# påhittade och prövar att koden ger rätt resultat både med och utan statusen.

IN_USE = ("UNAVAILABLE", "CAR_IN_USE")
FREE = ("AVAILABLE", None)


def srow(fetched: float, last_seen: float | None = None, *, engine: bool | None, engine_ts: float | None = None,
         status: tuple[str, str | None] | None = None, status_ts: float | None = None, base: datetime = ARR):
    """Som ``row`` men med statusen ur command-accessibility (``None`` = ingen status lästes)."""
    r = row(fetched, last_seen, engine=engine, engine_ts=engine_ts, base=base)
    r.accessibility_status, r.accessibility_reason = status if status is not None else (None, None)
    r.accessibility_reported_at = base + timedelta(minutes=status_ts) if status_ts is not None else None
    r.car_in_use = None if status is None else status == IN_USE
    return r


def sobs(db, vehicle, fetched: float, last_seen: float | None = None, *, engine: bool | None,
         engine_ts: float | None = None, status: tuple[str, str | None] | None = None,
         status_ts: float | None = None, base: datetime = ARR) -> M.VehicleObservation:
    o = obs(db, vehicle, fetched, last_seen, engine=engine, engine_ts=engine_ts, base=base)
    r = srow(fetched, last_seen, engine=engine, status=status, status_ts=status_ts, base=base)
    o.accessibility_status, o.accessibility_reason = r.accessibility_status, r.accessibility_reason
    o.accessibility_reported_at, o.car_in_use = r.accessibility_reported_at, r.car_in_use
    db.flush()
    return o


def electric_start(status: tuple[str, str | None] | None = IN_USE, *, engine_later: bool = True) -> list:
    """Start på el 20 min före ankomsten: statusen slår om direkt, bensinmotorn går igång 10 min senare
    (med ``engine_later=False`` går den aldrig igång: hela resan på el).

    Hämtning var 2:a minut (identiska avläsningar slås ihop till en rad med ``last_seen_at``).
    """
    seen = None if status is None else FREE
    used = status
    return [
        srow(-60, -21.5, engine=False, engine_ts=-300, status=seen, status_ts=-300),
        srow(-19.5, -11.5, engine=False, engine_ts=-300, status=used, status_ts=-20.2),
        srow(-9.5, -0.5, engine=engine_later, engine_ts=-10.1 if engine_later else -300, status=used,
             status_ts=-20.2),
        srow(1.5, engine=False, engine_ts=0.5, status=seen, status_ts=0.6),
    ]


def without_status(rows: list) -> list:
    """Samma avläsningar med statusen bortplockad (som en bil utan command-accessibility)."""
    out = []
    for r in rows:
        c = SimpleNamespace(**vars(r))
        c.accessibility_status = c.accessibility_reason = c.accessibility_reported_at = c.car_in_use = None
        out.append(c)
    return out


def assert_same_as_without_status(rows: list, ignored: str, **kw) -> tuple:
    """Statusen lämnas: exakt samma resultat och grund som utan statusen, plus ``status_ignored``."""
    got = estimate(rows, **kw)
    plain = estimate(without_status(rows), **kw)
    assert got[3].get("status_ignored") == ignored and "status_ignored" not in plain[3]
    assert "start_signal" not in got[3] and "in_use_reported_at" not in got[3]
    assert (got[0], got[1], got[2], {k: v for k, v in got[3].items() if k != "status_ignored"}) == plain
    return got


def test_electric_trip_the_status_alone_sets_no_start():
    """Hela resan på el: statusen är det enda beskedet. Det är inte sett hur den riktiga bilen
    rapporterar statusen, så den sätter aldrig ensam en starttid (och snävar inte in gränserna):
    samma resultat som utan statusen – ingen starttid ur avläsningarna – plus ``status_ignored``.
    (Förut, 1.5.0 under bygget, blev statusens tid starttiden med kvalitet reported.)"""
    for km in (6.0, 12.0, 60.0):
        kind, when, latest, basis = assert_same_as_without_status(
            electric_start(engine_later=False), "no_engine_start", km=km)
        assert (kind, when, latest, basis) == ("none", None, None, {"status_ignored": "no_engine_start"})
    # Utan statusen syns ingen start alls, som förut.
    assert estimate(electric_start(status=None, engine_later=False), km=12.0) == ("none", None, None, {})


def test_status_on_half_an_hour_before_an_electric_trip_gives_no_invented_time():
    """Bilen står en halvtimme med statusen påslagen (dörren öppnad, förvärmning) och körs sedan
    hela vägen på el. Det går inte att skilja från en start på el: ingen påhittad tid, med eller
    utan statusens tidsstämpel, med eller utan tidigaste gräns."""
    rows = [
        srow(-60, -42.5, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-40.5, -20.5, engine=False, engine_ts=-300, status=IN_USE, status_ts=-41),  # står still
        srow(-18.5, -6.5, engine=False, engine_ts=-300, status=IN_USE, status_ts=-41),  # kör på el
        srow(-4.5, -0.5, engine=None, status=IN_USE, status_ts=-41),  # motorstatusen kunde inte läsas
        srow(1.5, engine=False, engine_ts=0.4, status=FREE, status_ts=0.5),
    ]
    for status_ts in (-41, None):
        for r in rows[1:4]:
            r.accessibility_reported_at = at(status_ts) if status_ts is not None else None
        for earliest in (-600.0, -45.0, None):
            for latest in (None, -15.0):
                kind, when, new_latest, _ = assert_same_as_without_status(
                    rows, "no_engine_start", km=14.0, earliest=earliest, latest=latest)
                assert (kind, when, new_latest) == ("none", None, None)


def test_status_before_a_later_engine_start_leaves_the_engine_rule_alone():
    """Statusen slår om utan motorn och motorn startar en senare hämtning. Det går inte att skilja en
    start på el från en bil som stod kvar med statusen påslagen: motorstartens regel gäller ensam."""
    plain = estimate(electric_start(status=None), km=12.0)
    kind, when, _, basis = estimate(electric_start(), km=12.0)
    assert kind == "set" and when == at(-10) and basis["status_ignored"] == "engine_later"
    assert "start_signal" not in basis
    assert (kind, when, {k: v for k, v in basis.items() if k != "status_ignored"}) == (plain[0], plain[1], plain[3])
    # Granskningens förlopp: statusen 30 min före den riktiga motorstarten (dörren öppnad, bilen
    # förvärmd), med och utan statusens tidsstämpel. Förut blev statusens tid starttiden.
    rows = [
        srow(-60, -32.5, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-30.5, -12.5, engine=False, engine_ts=-300, status=IN_USE, status_ts=-31),
        srow(-10.5, -0.5, engine=True, engine_ts=-10.6, status=IN_USE, status_ts=-31),
        srow(1.5, engine=False, engine_ts=0.4, status=FREE, status_ts=0.5),
    ]
    for status_ts in (-31, None):
        for r in rows[1:3]:
            r.accessibility_reported_at = at(status_ts) if status_ts is not None else None
        kind, when, _, basis = estimate(rows, km=8.0)
        assert kind == "set" and when == at(-11) and basis["engine_reported_at"] == at(-10.6).isoformat()
        assert basis["method"] == "engine_start" and basis["status_ignored"] == "engine_later"


def test_electric_trip_through_the_pipeline_gets_no_start_from_the_status(db, user, vehicle, actor):
    """Hela resan på el genom efterbearbetningen: statusen sätter ingen starttid; resan får samma
    utfall som utan statusen (K4 ur sträckan), och felsökningsfilen visar varför statusen lämnades."""
    from app.services.export import month_close

    for r in electric_start(engine_later=False):
        sobs(db, vehicle, (r.fetched_at - ARR).total_seconds() / 60, (r.last_seen_at - ARR).total_seconds() / 60,
             engine=r.engine_running,
             engine_ts=(r.engine_reported_at - ARR).total_seconds() / 60 if r.engine_reported_at else None,
             status=(r.accessibility_status, r.accessibility_reason) if r.accessibility_status else None,
             status_ts=(r.accessibility_reported_at - ARR).total_seconds() / 60 if r.accessibility_reported_at else None)
    trip = live_trip(db, vehicle, user, km=12.0, ended=ARR, earliest=ARR - timedelta(hours=3))
    raw = seg.ordered_raws(trip)[0]
    assert start_time.engine_plan(db, vehicle.id) == []
    steps = start_time.unknown_start_reasons(db, raw, month_close.protection(db, vehicle.id))
    assert steps[:2] == ["no_engine_start", "car_in_use_ignored_no_engine_start"]
    out = run_pipeline(db, user, vehicle, actor)
    assert out["start_from_engine"] == 0
    trip = reload_trip(db, trip.id)
    raw = seg.ordered_raws(trip)[0]
    assert trip.start_time_quality != TimeQuality.REPORTED
    basis = raw.start_estimate_basis or {}
    assert basis.get("method") == "distance_speed" and "start_signal" not in basis
    assert trip.start_time_quality == TimeQuality.ESTIMATED


def test_status_left_on_during_a_stop_does_not_reach_the_previous_trip():
    """Statusen står kvar på CAR_IN_USE under ett stopp (föraren sitter kvar), men motorn rapporteras
    avstängd med ny tidsstämpel: bilen stannade. Utan tidigaste gräns blev förra resans start den här
    resans starttid, med gränsen blev den okänd – motorstarten efter stoppet är starten."""
    rows = [
        srow(-90, -70, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-68, -42, engine=True, engine_ts=-69, status=IN_USE, status_ts=-69),
        srow(-40, -21.5, engine=False, engine_ts=-40.2, status=IN_USE, status_ts=-69),  # stoppet
        srow(-19.5, -0.5, engine=True, engine_ts=-20.1, status=IN_USE, status_ts=-69),
        srow(1.5, engine=False, engine_ts=0.4, status=FREE, status_ts=0.5),
    ]
    for earliest in (None, -40.2):
        kind, when, _, basis = estimate(rows, km=12.0, earliest=earliest)
        assert kind == "set" and when == at(-20) and basis["status_ignored"] == "stop"
        assert "start_signal" not in basis
    # Statusen kvar hela natten sedan förra ankomsten (motorn avstängd −1169,1, aldrig AVAILABLE).
    night = [
        srow(-1200, -1171, engine=True, engine_ts=-1200.5, status=IN_USE, status_ts=-1200.4),
        srow(-1168.1, -122.4, engine=False, engine_ts=-1169.1, status=IN_USE, status_ts=-1200.4),
        srow(-120.4, -0.3, engine=True, engine_ts=-120.6, status=IN_USE, status_ts=-1200.4),
        srow(1.7, engine=False, engine_ts=0.5, status=FREE, status_ts=0.6),
    ]
    for earliest in (None, -1169.6):
        kind, when, _, basis = estimate(night, km=194.0, earliest=earliest)
        assert kind == "set" and when == at(-121) and basis["status_ignored"] == "stop"


def test_short_coverage_gap_in_an_electric_trip_gives_no_start():
    """Hela resan på el och en hämtning mitt i med UNAVAILABLE/NO_INTERNET (eller ingen status), motorn
    av med samma tidsstämpel som förut. Förut blev statusens första tid starttiden; nu sätter statusen
    aldrig ensam en starttid, med eller utan glapp: samma resultat som utan statusen."""
    def trip(gap_status):
        return [
            srow(-150, -122.4, engine=False, engine_ts=-1169.1, status=FREE, status_ts=-1169),
            srow(-120.4, -112.4, engine=False, engine_ts=-1169.1, status=IN_USE, status_ts=-121),
            srow(-110.4, -108.4, engine=False, engine_ts=-1169.1, status=gap_status, status_ts=-110.5),
            srow(-106.4, -0.3, engine=False, engine_ts=-1169.1, status=IN_USE, status_ts=-106.5),
            srow(1.7, engine=False, engine_ts=0.5, status=FREE, status_ts=0.6),
        ]
    for gap in (("UNAVAILABLE", "NO_INTERNET"), None, FREE):
        for km in (25.0, 60.0):
            kind, when, _, _ = assert_same_as_without_status(trip(gap), "no_engine_start", km=km)
            assert (kind, when) == ("none", None)


def test_status_and_engine_at_the_same_time():
    rows = [
        srow(-40, -14.1, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-12.1, -0.4, engine=True, engine_ts=-12.3, status=IN_USE, status_ts=-12.3),
        srow(1.7, engine=False, engine_ts=0.5, status=FREE, status_ts=0.5),
    ]
    kind, when, _, basis = estimate(rows, km=8.0)
    assert kind == "set" and when == at(-12) and basis["start_signal"] == "engine_and_car_in_use"
    # Statusen en halv minut före motorstarten: den tidigaste vinner.
    rows[1].accessibility_reported_at = at(-12.8)
    kind, when, _, basis = estimate(rows, km=8.0)
    assert kind == "set" and when == at(-13) and basis["start_signal"] == "car_in_use"
    assert basis["engine_reported_at"] == at(-12.3).isoformat()


def test_only_the_status():
    """Hela resan på el: motorn går aldrig, bara statusen slår om. Förut gav statusens tid (eller
    minuten mitt emellan hämtningarna) starttiden; nu blir det samma som utan statusen, oavsett
    statusens tidsstämpel."""
    rows = [
        srow(-40, -15.5, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-13.5, -6, engine=False, engine_ts=-300, status=IN_USE, status_ts=-14.6),
        srow(-4, -0.5, engine=None, status=IN_USE, status_ts=-14.6),  # motorstatusen kunde inte läsas
        srow(1.5, engine=False, engine_ts=0.4, status=FREE, status_ts=0.5),
    ]
    for status_ts in (-14.6, None, -300):
        for r in rows[1:3]:
            r.accessibility_reported_at = at(status_ts) if status_ts is not None else None
        kind, when, _, _ = assert_same_as_without_status(rows, "no_engine_start", km=6.0)
        assert (kind, when) == ("none", None)


def test_status_in_the_same_poll_as_the_engine_start_still_refines():
    """Fall (1) är oförändrat: statusen och motorstarten i samma hämtning, den tidigaste av bilens
    två tidsstämplar inom fönstret blir starttiden (kvalitet reported, metod engine_start)."""
    rows = electric_start(engine_later=False)
    rows[1].car_in_use, rows[1].accessibility_status, rows[1].accessibility_reason = False, "AVAILABLE", None
    rows[2].engine_running, rows[2].engine_reported_at = True, at(-9.8)
    rows[2].accessibility_reported_at = at(-10.6)
    kind, when, _, basis = estimate(rows, km=12.0)
    assert kind == "set" and when == at(-11) and basis["method"] == "engine_start"
    assert basis["start_signal"] == "car_in_use" and basis["in_use_reported_at"] == at(-10.6).isoformat()
    assert "status_ignored" not in basis
    # Utan statusen: motorstartens tid.
    plain = estimate(without_status(rows), km=12.0)
    assert plain[0] == "set" and plain[1] == at(-10) and "start_signal" not in plain[3]


def test_status_missing_entirely_gives_exactly_todays_result():
    """Kolumnerna tomma (äldre avläsningar, en bil utan stöd): samma resultat och samma grund."""
    cases = [
        ([(-483.2, -122.4, False, -1169.1), (-120.4, -0.3, True, -120.6)], 194.0, -1169.6, None),
        ([(-40, -20.5, False, None), (-18.5, -1, True, None)], 12.0, -600.0, None),
        ([(-200, -56, False, None), (-54.5, -28, True, -55), (-26, None, None, None), (-24, -0.4, True, -55),
          (1.7, None, False, 0.5)], 40.0, -600.0, None),
        ([(-90, -60, False, None), (-20, -1, True, None)], 10.0, -600.0, None),
        ([(-30, -6, False, None), (-4, -1, True, -4.2)], 9.0, -600.0, None),
        ([(-30, -16, False, None), (-14, None, True, -15.2), (-9, None, True, -15.2), (1, None, False, 0.3)],
         11.0, -600.0, -9),
    ]
    for spec, km, earliest, latest in cases:
        plain = [row(f, s, engine=e, engine_ts=t) for f, s, e, t in spec]
        empty = [srow(f, s, engine=e, engine_ts=t) for f, s, e, t in spec]
        assert estimate(empty, km=km, earliest=earliest, latest=latest) == estimate(
            plain, km=km, earliest=earliest, latest=latest)


def test_unavailable_for_another_reason_does_not_count():
    other = ("UNAVAILABLE", "POWER_SAVING_MODE")
    rows = electric_start(status=other)
    kind, when, _, basis = estimate(rows, km=12.0)
    assert kind == "set" and when == at(-10) and "start_signal" not in basis
    # Inte heller som "används" när motorstatusen saknas.
    assert estimate([srow(-60, -1, engine=None, status=other, status_ts=-30)], km=5.0)[0] == "none"


def test_available_with_unreadable_engine_midtrip_is_no_standstill():
    """AVAILABLE mitt i resan när motorstatusen inte kunde läsas: ingen ny start (bilen kanske
    inte svarar CAR_IN_USE under färd)."""
    rows = [
        srow(-60, -21.5, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-19.5, -12, engine=True, engine_ts=-20.1, status=FREE, status_ts=-300),
        srow(-10, engine=None, status=FREE, status_ts=-300),
        srow(-8, -0.5, engine=True, engine_ts=-20.1, status=FREE, status_ts=-300),
    ]
    kind, when, _, basis = estimate(rows, km=12.0)
    assert kind == "set" and when == at(-20) and "start_signal" not in basis


def test_status_that_flips_without_driving_changes_nothing(db, user, vehicle, actor):
    """Statusen slår om (t.ex. bilen låses upp och stängs igen) utan att bilen rör sig: ingen resa."""
    sobs(db, vehicle, -60, -30.5, engine=False, engine_ts=-300, status=FREE, status_ts=-300)
    sobs(db, vehicle, -28.5, -20.5, engine=False, engine_ts=-300, status=IN_USE, status_ts=-29)
    sobs(db, vehicle, -18.5, -0.5, engine=False, engine_ts=-300, status=FREE, status_ts=-19)
    assert seg.active_trips(db, vehicle.id) == []
    assert start_time.engine_plan(db, vehicle.id) == []
    assert run_pipeline(db, user, vehicle, actor)["start_from_engine"] == 0
    assert seg.active_trips(db, vehicle.id) == []
    # Som ren funktion: bilen sågs stå still efter omslaget, så ingen pågående körning vid "ankomsten".
    rows = [srow(-60, -30.5, engine=False, status=FREE, status_ts=-300),
            srow(-28.5, -20.5, engine=False, status=IN_USE, status_ts=-29),
            srow(-18.5, -0.5, engine=False, engine_ts=-19, status=FREE, status_ts=-19)]
    assert estimate(rows, km=5.0)[0] == "none"


def test_status_flip_before_a_real_trip_is_not_the_start():
    """Statusen slår om och tillbaka en stund före den riktiga starten: den sista övergången räknas."""
    rows = [
        srow(-60, -40.5, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-38.5, -32.5, engine=False, engine_ts=-300, status=IN_USE, status_ts=-39),
        srow(-30.5, -14.1, engine=False, engine_ts=-300, status=FREE, status_ts=-31),
        srow(-12.1, -0.4, engine=True, engine_ts=-12.3, status=IN_USE, status_ts=-12.6),
    ]
    kind, when, _, basis = estimate(rows, km=8.0)
    assert kind == "set" and when == at(-13) and basis["start_signal"] == "car_in_use"


def test_fixed_engine_start_is_not_changed_when_the_status_appears_later(db, user, vehicle, actor):
    trip = engine_trip(db, vehicle, user)
    assert start_time.fill_from_engine(db, vehicle.id) == 1
    fixed = reload_trip(db, trip.id).started_at
    assert local(fixed) == local(at(-12))
    # Statusen kommer i efterhand och skulle ha gett en tidigare tid (inom fönstret).
    db.execute(M.VehicleObservation.__table__.update().where(M.VehicleObservation.engine_running.is_(True))
               .values(accessibility_status="UNAVAILABLE", accessibility_reason="CAR_IN_USE",
                       accessibility_reported_at=at(-15.5), car_in_use=True))
    assert start_time.engine_plan(db, vehicle.id) == []
    assert run_pipeline(db, user, vehicle, actor)["start_from_engine"] == 0
    trip = reload_trip(db, trip.id)
    assert trip.started_at == fixed and trip.start_time_quality == TimeQuality.REPORTED
    assert "start_signal" not in seg.ordered_raws(trip)[0].start_estimate_basis


def test_status_does_not_pass_the_locks(db, user, vehicle, actor):
    """Ägarens angivna tid, importerade resor och stängda månader gäller även med statusen."""
    owned = engine_trip(db, vehicle, user)
    db.execute(M.VehicleObservation.__table__.update().where(M.VehicleObservation.engine_running.is_(True))
               .values(car_in_use=True, accessibility_status="UNAVAILABLE", accessibility_reason="CAR_IN_USE",
                       accessibility_reported_at=at(-13.5)))
    start_time.set_override(db, owned, at(-20), actor=actor)
    assert start_time.engine_plan(db, vehicle.id) == []
    other = ARR - timedelta(days=1)
    imported = engine_trip(db, vehicle, user, ended=other, source=TripSource.VOLVO_IMPORT, odo_start=60000)
    db.execute(M.VehicleObservation.__table__.update().where(M.VehicleObservation.engine_running.is_(True))
               .values(car_in_use=True, accessibility_reported_at=M.VehicleObservation.fetched_at))
    assert start_time.engine_plan(db, vehicle.id) == []
    assert reload_trip(db, imported.id).started_at is None
    oct_end = datetime(2026, 10, 31, 23, 5, tzinfo=UTC)
    db.add(M.MonthClose(user_id=user.id, vehicle_id=vehicle.id, month="2026-10", state="exported"))
    db.flush()
    in_closed = engine_trip(db, vehicle, user, ended=oct_end, odo_start=70000)
    db.execute(M.VehicleObservation.__table__.update().where(M.VehicleObservation.engine_running.is_(True))
               .values(car_in_use=True, accessibility_reported_at=M.VehicleObservation.fetched_at))
    assert start_time.engine_plan(db, vehicle.id) == []
    assert reload_trip(db, in_closed.id).started_at is None


def test_status_never_changes_which_transition_counts():
    """Granskningen: följden börjar med motorn igång, men statusen höll ihop den över en avläsning med
    motorn av utan ny tidsstämpel för avstängningen ("motorn igång" låg kvar en hämtning efter stoppet,
    eller avstängningen saknar tidsstämpel). Förut blev en tidigare motorstart starttiden, eller ingen;
    nu lämnas statusen och resultatet blir exakt detsamma som utan den."""
    # F1: motorn igång −56, sedd igång till −45; av (tidsstämpel −48, före förra hämtningen) med
    # statusen; motorn igång igen −20. Utan statusen är starten −20.
    f1 = [
        srow(-60, engine=False, engine_ts=-120, status=FREE, status_ts=-120),
        srow(-55, -45, engine=True, engine_ts=-56, status=FREE, status_ts=-120),
        srow(-43, -22, engine=False, engine_ts=-48, status=IN_USE, status_ts=-44),
        srow(-19, engine=True, engine_ts=-20, status=IN_USE, status_ts=-44),
    ]
    for earliest in (-120.0, -48.0, None):
        kind, when, _, _ = assert_same_as_without_status(f1, "other_transition", km=20.0, earliest=earliest)
        assert (kind, when) == ("set", at(-20))
    # A: motorn igång −51, sedan av utan tidsstämpel med statusen fram till ankomsten. Utan statusen
    # syns ingen pågående körning vid ankomsten: ingen starttid ur avläsningarna (förut −51).
    a = [
        srow(-60, -52, engine=False, engine_ts=-120, status=FREE, status_ts=-120),
        srow(-50, -42, engine=True, engine_ts=-51, status=FREE, status_ts=-120),
        srow(-40, -1, engine=False, status=IN_USE, status_ts=-41),
    ]
    for earliest in (-120.0, None):
        kind, when, _, basis = assert_same_as_without_status(a, "other_transition", km=20.0, earliest=earliest)
        assert (kind, when, basis) == ("none", None, {"status_ignored": "other_transition"})


def test_other_transition_shows_in_the_debug_steps(db, user, vehicle):
    from app.services.export import month_close

    sobs(db, vehicle, -60, -52, engine=False, engine_ts=-120, status=FREE, status_ts=-120)
    sobs(db, vehicle, -50, -42, engine=True, engine_ts=-51, status=FREE, status_ts=-120)
    sobs(db, vehicle, -40, -1, engine=False, status=IN_USE, status_ts=-41)
    trip = live_trip(db, vehicle, user, km=20.0, ended=ARR, earliest=ARR - timedelta(hours=3))
    raw = seg.ordered_raws(trip)[0]
    assert start_time.engine_plan(db, vehicle.id) == []
    steps = start_time.unknown_start_reasons(db, raw, month_close.protection(db, vehicle.id))
    assert steps[:2] == ["no_engine_start", "car_in_use_ignored_other_transition"]


def test_start_signal_only_names_a_timestamp_that_gave_the_time():
    """Statusen och motorstarten i samma hämtning men ingen av bilens tidsstämplar inom fönstret: tiden
    är minuten mitt emellan hämtningarna (eller bara gränser), och grunden påstår inte att bilen gav den."""
    rows = [
        srow(-30, -16, engine=False, engine_ts=-300, status=FREE, status_ts=-300),
        srow(-12, -0.5, engine=True, engine_ts=None, status=IN_USE, status_ts=None),
    ]
    kind, when, _, basis = estimate(rows, km=8.0)
    assert kind == "set" and when == at(-14) and basis["method"] == "poll_midpoint"
    assert basis["start_signal"] is None
    rows[0].last_seen_at = at(-40)  # långt fönster: bara gränser
    kind, _, _, basis = estimate(rows, km=8.0)
    assert kind == "narrow" and basis["method"] == "engine_window" and basis["start_signal"] is None
    # Motorns tid utanför fönstret, statusens inom: statusen gav tiden.
    rows[0].last_seen_at = at(-16)
    rows[1].engine_reported_at, rows[1].accessibility_reported_at = at(-200), at(-13.2)
    kind, when, _, basis = estimate(rows, km=8.0)
    assert kind == "set" and when == at(-13) and basis["start_signal"] == "car_in_use"


def test_status_long_before_the_engine_start_in_a_long_window_is_not_the_start():
    """Helhetsprovet: hämtningen låg nere (fönster 58 min), statusen slog om 20 min före motorstarten i
    samma fönster. Statusens tid får inte bli en fast starttid: motorstartens tid gäller, som utan
    statusen. Statusen högst två minuter före motorstarten förfinar fortfarande tiden."""
    rows = [
        srow(-200, -133, engine=False, engine_ts=-1000, status=FREE, status_ts=-1000),
        srow(-75, -0.5, engine=True, engine_ts=-95.3, status=IN_USE, status_ts=-115.3),
        srow(1.5, engine=False, engine_ts=0.4, status=FREE, status_ts=0.5),
    ]
    kind, when, _, basis = assert_same_as_without_status(rows, "too_early", km=100.0)
    assert kind == "set" and when == at(-95) and basis["method"] == "engine_start"
    assert basis["window_minutes"] == 58.0
    # Kort försprång (1,5 min): statusens tid förfinar som förut.
    rows[1].accessibility_reported_at = at(-96.8)
    kind, when, _, basis = estimate(rows, km=100.0)
    assert kind == "set" and when == at(-97) and basis["start_signal"] == "car_in_use"
    assert "status_ignored" not in basis


def test_debug_file_shows_which_signal_gave_the_start(db, user, vehicle):
    """Felsökningsfilen säger varför: vilket av bilens besked som gav starttiden, statusens tid och om
    statusen lämnades (här: för tidig i ett långt fönster)."""
    from app.api.routes import volvo as volvo_routes

    long_arr = ARR - timedelta(days=1)
    sobs(db, vehicle, -200, -133, engine=False, engine_ts=-1000, status=FREE, status_ts=-1000, base=long_arr)
    sobs(db, vehicle, -75, -0.5, engine=True, engine_ts=-95.3, status=IN_USE, status_ts=-115.3, base=long_arr)
    sobs(db, vehicle, 1.5, engine=False, engine_ts=0.4, status=FREE, status_ts=0.5, base=long_arr)
    live_trip(db, vehicle, user, km=100.0, ended=long_arr, earliest=long_arr - timedelta(hours=4))
    sobs(db, vehicle, -40, -14.1, engine=False, engine_ts=-300, status=FREE, status_ts=-300)
    sobs(db, vehicle, -12.1, -0.4, engine=True, engine_ts=-12.3, status=IN_USE, status_ts=-12.8)
    sobs(db, vehicle, 1.7, engine=False, engine_ts=0.5, status=FREE, status_ts=0.5)
    live_trip(db, vehicle, user, km=8.0, ended=ARR, earliest=ARR - timedelta(hours=3))
    assert start_time.fill_from_engine(db, vehicle.id) == 2
    out = volvo_routes.build_diagnostics(db, vehicle, days=14, now=ARR + timedelta(minutes=5))
    long_trip, short_trip = out["trips"]
    assert long_trip["started_at"] == (long_arr - timedelta(minutes=95)).isoformat()
    assert (long_trip["start_method"], long_trip["start_signal"], long_trip["status_ignored"],
            long_trip["in_use_reported_at"]) == ("engine_start", None, "too_early", None)
    assert short_trip["started_at"] == at(-13).isoformat()
    assert (short_trip["start_method"], short_trip["start_signal"], short_trip["status_ignored"],
            short_trip["in_use_reported_at"]) == ("engine_start", "car_in_use", None,
                                                  at(-12.8).isoformat(timespec="seconds"))


def test_history_events_from_the_engine_start_have_a_text_in_the_web():
    """Historiken visar ``AUDIT_ACTION_LABELS[action] ?? action``: händelserna som fill_from_engine
    skriver ska ha en text, inte visas som rå kod."""
    import inspect
    import re
    from pathlib import Path

    labels = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "labels.ts"
    if not labels.exists():
        pytest.skip("webben finns inte bredvid backend")
    block = labels.read_text(encoding="utf-8").split("AUDIT_ACTION_LABELS", 1)[1].split("};", 1)[0]
    actions = set(re.findall(r'"(trip\.start_[a-z_]*engine)"', inspect.getsource(start_time)))
    assert actions == {"trip.start_time_engine", "trip.start_latest_engine"}
    for action in actions:
        assert f'"{action}":' in block, action


def test_car_that_reports_while_driving_with_five_minutes_between_the_polls(db, user, vehicle):
    """En bil som rapporterar under färd: detekteringen sätter tidigaste start ur motorrapporten
    minus 2 min. Med 4–5 minuter mellan hämtningarna ligger den sista avläsningen med bilen
    stillastående före den gränsen; den ska ändå läsas, så att motorstarten blir starttiden."""
    from app.services.export import month_close

    obs(db, vehicle, -60, -22.8, engine=False, engine_ts=-300)
    obs(db, vehicle, -18, -13.5, engine=True, engine_ts=-18.4)
    obs(db, vehicle, -8.5, -1, engine=True, engine_ts=-18.4)
    obs(db, vehicle, 1.5, engine=False, engine_ts=0.4)
    trip = live_trip(db, vehicle, user, km=11.0, ended=ARR, earliest=at(-20.4), latest=at(-13.5))
    raw = seg.ordered_raws(trip)[0]
    assert start_time.unknown_start_reasons(db, raw, month_close.protection(db, vehicle.id)) == ["pending"]
    (change,) = start_time.engine_plan(db, vehicle.id)
    assert change.kind == "set" and change.at == at(-18) and change.basis["method"] == "engine_start"
