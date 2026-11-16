"""1.2.1: starttid ur sträckan (K4) när bilen bara rapporterar efter ankomsten.

Körtiden räknas ur resans uppmätta sträcka och en snittfart: i första hand ägarens egna resor
med kända tider (samma rutt, annars liknande sträcka), annars en enkel tabell. Starttiden blir
ankomsten minus körtiden, hållen inom det möjliga fönstret, och märks som uppskattad precis som
K2/K3. Ägarens angivna starttid rörs aldrig. Alla resor, platser och tider är påhittade.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, DistanceQuality, ReviewStatus, TimeQuality, TripSource
from app.services.export import checks as checks_mod
from app.services.export import service as export_svc
from app.services.export import simple
from app.services.ha import messages
from app.services.places import registry as places
from app.services.stats import common
from app.services.trips import segments as seg
from app.services.trips import start_time
from app.services.volvo import detection
from tests.conftest import reload_trip
from tests.test_s1_starttid import StatsCar, silent_trip
from tests.test_volvo_support import connect

TZ = ZoneInfo("Europe/Stockholm")
NBSP = " "
HOME_ADDR, WORK_ADDR = "Påhittvägen 3, 111 22 Låtsasby", "Exempelgatan 9, 333 44 Fiktivstad"
# Ankomst en tisdag 16:42 svensk tid (14:42 UTC); fönstret börjar kvällen före.
ARRIVAL = datetime(2026, 9, 15, 14, 42, tzinfo=UTC)


def local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def live_raw(db, vehicle, user, *, km: float = 9.0, ended: datetime = ARRIVAL,
             earliest: datetime | None = None, latest: datetime | None = None,
             start: str | None = HOME_ADDR, end: str | None = WORK_ADDR,
             odometer: bool = True, end_quality: str = TimeQuality.REPORTED,
             source: str = TripSource.VOLVO_API, odo_start: float = 50000.0) -> M.RawTrip:
    """En resa från bilen som i 1.2.0: bara ankomst och sträcka, starttiden okänd (bounded)."""
    s_loc = places.resolve_address(db, user.id, start, created_from="import") if start else None
    e_loc = places.resolve_address(db, user.id, end, created_from="import") if end else None
    if latest is None:
        latest = ended - timedelta(hours=km / detection.MAX_AVG_SPEED_KMH)
    raw = M.RawTrip(
        vehicle_id=vehicle.id, source=source, source_key=uuid.uuid4().hex,
        started_at=None, ended_at=ended, start_time_quality=TimeQuality.BOUNDED, end_time_quality=end_quality,
        start_earliest=earliest if earliest is not None else ended - timedelta(hours=17, minutes=37),
        start_latest=latest,
        odometer_start=odo_start if odometer else None, odometer_end=odo_start + km if odometer else None,
        odometer_source="api" if odometer else "none",
        distance_km=km, distance_quality=DistanceQuality.ODOMETER if odometer else DistanceQuality.ESTIMATED,
        duration_seconds=None, start_address=start, end_address=end,
        start_location_id=s_loc.id if s_loc else None, end_location_id=e_loc.id if e_loc else None,
        is_complete=odometer, quality_flags=[] if odometer else ["missing_odometer"], updated_by="system",
    )
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    return raw


def live_trip(db, vehicle, user, **kw) -> M.LogicalTrip:
    raw = live_raw(db, vehicle, user, **kw)
    return seg.create_logical_for_raw(db, raw, category=Category.UNDECIDED, review_status=ReviewStatus.NEEDS_REVIEW)


def known_trips(trips, n: int, *, start: str = HOME_ADDR, end: str = WORK_ADDR, km: float = 20.0,
                minutes: int = 20, first: datetime = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)) -> None:
    """Ägarens egna resor med kända tider (importerade med start- och sluttid)."""
    for i in range(n):
        trips.trip(start, end, km=km, minutes=minutes, start_at=first + timedelta(days=i))


# ----------------------------------------------------------------------------- den rena uträkningen


def test_table_speed_by_distance():
    assert start_time.table_speed(0.5) == 25
    assert start_time.table_speed(2.9) == 25
    assert start_time.table_speed(9.0) == 35
    assert start_time.table_speed(25) == 50
    assert start_time.table_speed(60) == 65
    assert start_time.table_speed(250) == 80


def test_estimate_from_table_inside_a_long_window():
    """9 km, framme 16:42, fönstret från kvällen före: 9 km i 35 km/h = 15,4 min → ≈ 16:27 (±5 min)."""
    E, L = ARRIVAL - timedelta(hours=17, minutes=37), ARRIVAL - timedelta(minutes=3)
    est = start_time.estimate_from_distance(
        ended_at=ARRIVAL, distance_km=9.0, earliest=E, latest=L, speed=start_time.Speed(35.0, "table", 0),
    )
    assert est.method == "distance_speed"
    assert local(est.at) == "16:27"
    assert start_time.margin_text(est.basis) == "±5 min"
    assert est.basis["method"] == "distance_speed" and est.basis["speed_source"] == "table"
    assert est.basis["speed_kmh"] == 35.0 and est.basis["trip_km"] == 9.0
    assert est.basis["duration_minutes"] == pytest.approx(15.4, abs=0.05)
    assert est.earliest == E and est.latest == L


def test_estimate_is_held_inside_the_window():
    # Mycket lång resa: 300 km i 80 km/h är 3 h 45 min, men bilen stod still för 2 h sedan.
    E = ARRIVAL - timedelta(hours=2)
    est = start_time.estimate_from_distance(ended_at=ARRIVAL, distance_km=300, earliest=E,
                                            latest=ARRIVAL - timedelta(minutes=110), speed=start_time.Speed(80, "table", 0))
    assert est.at == E
    # Senaste möjliga start före den uträknade: tiden blir den senaste möjliga.
    L = ARRIVAL - timedelta(minutes=12)
    est = start_time.estimate_from_distance(ended_at=ARRIVAL, distance_km=2.0, earliest=ARRIVAL - timedelta(hours=3),
                                            latest=L, speed=start_time.Speed(25, "table", 0))
    assert est.at == L


def test_very_short_trip_gets_at_least_one_minute():
    est = start_time.estimate_from_distance(ended_at=ARRIVAL, distance_km=0.3, earliest=ARRIVAL - timedelta(hours=1),
                                            latest=ARRIVAL, speed=start_time.Speed(25, "table", 0))
    assert est.at == ARRIVAL - timedelta(minutes=1)
    assert est.basis["margin_min"] >= 3


def test_no_estimate_without_distance_or_with_contradicting_window():
    speed = start_time.Speed(35, "table", 0)
    assert start_time.estimate_from_distance(ended_at=ARRIVAL, distance_km=0, earliest=None,
                                             latest=ARRIVAL, speed=speed).at is None
    assert start_time.estimate_from_distance(ended_at=ARRIVAL, distance_km=5, earliest=ARRIVAL,
                                             latest=ARRIVAL - timedelta(minutes=5), speed=speed).at is None
    assert start_time.estimate_from_distance(ended_at=None, distance_km=5, earliest=None, latest=None,
                                             speed=speed).at is None


# ----------------------------------------------------------------------------- snittfarten


def test_speed_from_own_trips_on_the_same_route(db, user, vehicle, trips):
    known_trips(trips, 3, km=20.0, minutes=24)  # 50 km/h
    trip = live_trip(db, vehicle, user, km=20.0)
    assert start_time.fill_from_distance(db, vehicle.id) == 1
    trip = reload_trip(db, trip.id)
    basis = trip.segments[0].raw_trip.start_estimate_basis
    assert basis["speed_source"] == "route" and basis["samples"] == 3 and basis["speed_kmh"] == 50.0
    assert local(trip.started_at) == "16:18"  # 20 km i 50 km/h = 24 min före 16:42
    assert trip.start_time_quality == TimeQuality.ESTIMATED


def test_route_counts_both_directions(db, user, vehicle, trips):
    known_trips(trips, 2, km=20.0, minutes=24)
    known_trips(trips, 1, start=WORK_ADDR, end=HOME_ADDR, km=20.0, minutes=24,
                first=datetime(2026, 9, 5, 15, 0, tzinfo=UTC))
    trip = live_trip(db, vehicle, user, km=20.0)
    start_time.fill_from_distance(db, vehicle.id)
    assert reload_trip(db, trip.id).segments[0].raw_trip.start_estimate_basis["speed_source"] == "route"


def test_speed_from_trips_of_similar_length_when_the_route_is_new(db, user, vehicle, trips):
    for i in range(5):
        known_trips(trips, 1, start=f"Annanväg {i}, Låtsasby", end=f"Bortaväg {i}, Fiktivstad", km=10.0, minutes=15,
                    first=datetime(2026, 9, 1, 6, 0, tzinfo=UTC) + timedelta(days=i))  # 40 km/h
    known_trips(trips, 2, km=9.0, minutes=9, first=datetime(2026, 9, 7, 6, 0, tzinfo=UTC))  # bara 2 på rutten
    trip = live_trip(db, vehicle, user, km=9.0)
    start_time.fill_from_distance(db, vehicle.id)
    basis = reload_trip(db, trip.id).segments[0].raw_trip.start_estimate_basis
    # 2 på rutten räcker inte; 7 resor på 9–10 km (±30 %) ger medianen 40 km/h.
    assert basis["speed_source"] == "similar" and basis["samples"] == 7 and basis["speed_kmh"] == 40.0


def test_too_few_own_trips_gives_the_table(db, user, vehicle, trips):
    known_trips(trips, 2, km=9.0, minutes=9)
    trip = live_trip(db, vehicle, user, km=9.0)
    start_time.fill_from_distance(db, vehicle.id)
    trip = reload_trip(db, trip.id)
    basis = trip.segments[0].raw_trip.start_estimate_basis
    assert basis["speed_source"] == "table" and basis["speed_kmh"] == 35
    assert local(trip.started_at) == "16:27"


def test_estimated_and_unknown_starts_never_count_as_own_trips(db, user, vehicle, trips):
    """Bara verkliga tider räknas: uppskattade starttider blir aldrig underlag för nya."""
    for i in range(3):
        live_trip(db, vehicle, user, km=9.0, ended=ARRIVAL - timedelta(days=i + 1), odo_start=40000 + i * 100)
    trip = live_trip(db, vehicle, user, km=9.0)
    assert start_time.fill_from_distance(db, vehicle.id) == 4
    assert reload_trip(db, trip.id).segments[0].raw_trip.start_estimate_basis["speed_source"] == "table"


# ----------------------------------------------------------------------------- vilka resor som får uppskattningen


def test_only_live_trips_with_odometer_and_known_arrival(db, user, vehicle):
    imported = live_trip(db, vehicle, user, source=TripSource.VOLVO_IMPORT, odo_start=10000)
    no_odo = live_trip(db, vehicle, user, odometer=False, odo_start=20000)
    unsure_end = live_trip(db, vehicle, user, end_quality=TimeQuality.BOUNDED, odo_start=30000)
    assert start_time.fill_from_distance(db, vehicle.id) == 0
    for t in (imported, no_odo, unsure_end):
        t = reload_trip(db, t.id)
        assert t.started_at is None and t.start_time_quality == TimeQuality.BOUNDED


def test_owner_start_time_is_never_touched(db, user, vehicle, actor):
    trip = live_trip(db, vehicle, user)
    own = ARRIVAL - timedelta(minutes=31)
    start_time.set_override(db, trip, own, actor=actor)
    start_time.fill_from_distance(db, vehicle.id)
    trip = reload_trip(db, trip.id)
    assert trip.start_time_override == own and trip.started_at == own
    assert trip.start_time_quality == TimeQuality.EXACT
    from app.api.serializers import start_time_source

    assert start_time_source(trip) == "user"
    # Tas den angivna tiden bort gäller uppskattningen.
    start_time.set_override(db, trip, None, actor=actor)
    trip = reload_trip(db, trip.id)
    assert trip.start_time_quality == TimeQuality.ESTIMATED and local(trip.started_at) == "16:27"


def test_trip_over_midnight(db, user, vehicle, actor, trips):
    """Framme 00:10: 20 km i 50 km/h = 24 min → ≈ 23:46 dagen före; underlaget skriver (+1)."""
    arrival = datetime(2026, 9, 15, 22, 10, tzinfo=UTC)  # 00:10 svensk tid den 16:e
    trip = live_trip(db, vehicle, user, km=20.0, ended=arrival)
    start_time.fill_from_distance(db, vehicle.id)
    trip = reload_trip(db, trip.id)
    assert trip.started_at.astimezone(TZ).strftime("%d %H:%M") == "15 23:46"
    assert simple.time_text(trip, TZ) == f"ca{NBSP}23:46–00:10 (+1)"
    trip.category, trip.review_status, trip.purpose, trip.purpose_confirmed = (
        Category.WORK, ReviewStatus.APPROVED, "Jourutryckning", True)
    seg.refresh_logical(db, trip)
    row = simple.build_row(trip, TZ)
    assert row.date == date(2026, 9, 15)


# ----------------------------------------------------------------------------- befintliga resor efter uppgraderingen


def test_existing_trips_get_the_estimate_on_the_next_collection(db, user, vehicle):
    """En resa från 1.2.0 med okänd starttid får uppskattningen vid nästa insamling, även när
    insamlingen inte hittar någon ny resa."""
    connect(db, user, vehicle)
    trip = live_trip(db, vehicle, user)
    db.commit()
    assert reload_trip(db, trip.id).start_time_quality == TimeQuality.BOUNDED
    assert detection.process_vehicle(db, vehicle, user_id=user.id) == []
    trip = reload_trip(db, trip.id)
    assert trip.start_time_quality == TimeQuality.ESTIMATED and local(trip.started_at) == "16:27"
    assert "start_time_uncertain" not in trip.quality_flags
    # Nästa insamling ändrar ingenting (idempotent).
    before = trip.segments[0].raw_trip.start_estimate_basis
    detection.process_vehicle(db, vehicle, user_id=user.id)
    assert reload_trip(db, trip.id).segments[0].raw_trip.start_estimate_basis == before


def test_silent_trip_from_the_car_gets_the_estimate(db, user, vehicle):
    """Bilen rapporterar först efter ankomsten (07:42, 14 km): K1 är avstängd, K2/K3 räcker inte,
    så starttiden uppskattas ur sträckan: 14 km i 50 km/h = 16,8 min → ≈ 07:25 UTC."""
    connect(db, user, vehicle)
    car = StatsCar()
    silent_trip(db, vehicle, car)
    raw = db.execute(select(M.RawTrip)).scalars().one()
    assert raw.start_time_quality == TimeQuality.ESTIMATED
    assert raw.start_estimate_basis["method"] == "distance_speed"
    assert raw.started_at.astimezone(UTC).strftime("%H:%M") == "07:25"
    assert raw.start_earliest <= raw.started_at <= raw.start_latest


# ----------------------------------------------------------------------------- nedströms, som K2/K3


def _work(db, trip: M.LogicalTrip) -> M.LogicalTrip:
    trip.category, trip.review_status, trip.purpose, trip.purpose_confirmed = (
        Category.WORK, ReviewStatus.APPROVED, "Kundbesök", True)
    seg.refresh_logical(db, trip)
    db.flush()
    return trip


def test_downstream_is_the_same_as_for_k2_and_k3(db, user, vehicle, actor):
    trip = live_trip(db, vehicle, user)
    start_time.fill_from_distance(db, vehicle.id)
    trip = _work(db, reload_trip(db, trip.id))
    # Ingen flagga "Osäker starttid", och därmed inget tak för osäkra uppgifter i klassningen.
    assert "start_time_uncertain" not in trip.quality_flags
    # Telefonen och appen: "≈".
    assert messages.fmt_trip_times(trip, TZ) == "≈ 16:27–16:42"
    assert start_time.display(trip, TZ) == "≈ 16:27"
    # Månadens kontroller: "Uträknade tider" i stället för "Starttid saknas".
    codes = {c["code"] for c in checks_mod.reported_quality_checks(db, [trip])}
    assert "estimated_times" in codes and "missing_start_time" not in codes
    # Underlaget: "ca", fotnoten och grunden i den detaljerade arbetsboken.
    period = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=period.start, end=period.end,
                                   actor=actor, tz=TZ, now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
    ws = openpyxl.load_workbook(Path(job.file_path), data_only=True).worksheets[0]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    assert f"ca{NBSP}16:27–16:42" in texts and simple.FOOTNOTE_TIMES in texts
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=period.start, end=period.end,
                                   actor=actor, tz=TZ, now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC), layout="detailed")
    book = openpyxl.load_workbook(Path(job.file_path), data_only=True)
    notes = [str(c.value) for ws in book.worksheets for row in ws.iter_rows() for c in row if c.value is not None]
    assert any("Starttiden är uppskattad ur sträckan" in n and "±5 min" in n for n in notes)


def test_api_shows_estimated_source_and_basis(client, db, user, vehicle):
    trip = live_trip(db, vehicle, user)
    db.commit()
    start_time.fill_from_distance(db, vehicle.id)
    db.commit()
    body = client.get(f"/api/v1/trips/{trip.id}").json()
    t = body.get("trip", body)
    assert t["start_time_source"] == "estimated" and t["start_time_text"] == "≈ 16:27"
    assert t["start_estimate_basis"]["method"] == "distance_speed"
