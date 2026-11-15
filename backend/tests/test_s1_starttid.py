"""S1b: starttid ur bilens egna avläsningar, byggkontraktet C.2, D och A.5 (tider).

(a) Mätaren ökar under färd → uträknad starttid inom ett fönster på högst 10 minuter, "≈".
(b) Bara slutavläsning → okänd starttid (bounded) med K1 avstängd, K1 med flaggan på.
Trippmätare som avviker mer än 1,5 km ger ingen K1. Tiden kläms till [E, L]. Avstängd motor
påverkar aldrig. Ägarens starttid gäller. Underlaget skriver "ca 07:35" med hårt mellanslag
och fotnoten. Inga nätverksanrop; alla data är påhittade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, TimeQuality, TripSource
from app.services.export import service as export_svc
from app.services.export import simple
from app.services.stats import common
from app.services.trips import merge, start_time
from app.services.trips import segments as seg
from app.services.volvo import collector
from app.services.volvo.client import ApiResult
from tests.conftest import reload_trip
from tests.test_volvo_support import T0, B, FakeCar, connect, iso, poll, poll_every

TZ = ZoneInfo("Europe/Stockholm")
NBSP = "\u00a0"  # hårt mellanslag


def m(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def view(at: datetime, odo: float | None = None, *, engine: bool | None = None, fetched: datetime | None = None,
         trip_km: float | None = None, speed: float | None = None, stats_at: datetime | None = None) -> start_time.ObsView:
    return start_time.ObsView(
        fetched_at=fetched or at, odometer_km=odo, odometer_reported_at=at if odo is not None else None,
        engine_running=engine, engine_reported_at=at if engine is not None else None,
        trip_meter_auto_km=trip_km, avg_speed_auto=speed, statistics_reported_at=stats_at,
    )


def raws(db) -> list[M.RawTrip]:
    return list(db.execute(select(M.RawTrip).order_by(M.RawTrip.odometer_start)).scalars())


@dataclass
class StatsCar(FakeCar):
    """FakeCar som också rapporterar trippmätaren och snittfarten (statistics)."""

    trip_meter: float | None = None
    avg_speed: float | None = None
    stats_ts: datetime = T0 - timedelta(hours=1)

    def fetch(self, endpoint_name: str, vin: str) -> ApiResult:
        if endpoint_name != "statistics" or self.trip_meter is None:
            return super().fetch(endpoint_name, vin)
        self.calls.append(endpoint_name)
        ts = iso(self.stats_ts)
        return ApiResult("ok", 200, data={"data": {
            "tripMeterAutomatic": {"value": self.trip_meter, "unit": "km", "timestamp": ts},
            "averageSpeedAutomatic": {"value": self.avg_speed, "unit": "km/h", "timestamp": ts},
        }})


@pytest.fixture()
def car(db, user, vehicle) -> StatsCar:
    connect(db, user, vehicle)
    return StatsCar()


def observed_trip(db, vehicle, car) -> None:
    """Mätarställningen rapporteras under färd: 5 km 07:09:30, 11 km 07:14:30, framme 07:18."""
    poll_every(db, vehicle, car, m(0), m(5))
    car.moving(5, m(9.5))
    poll(db, vehicle, car, m(10))
    car.moving(6, m(14.5))
    poll(db, vehicle, car, m(15))
    car.drive_to(B, 3, m(18))
    poll_every(db, vehicle, car, m(20), m(40))


def silent_trip(db, vehicle, car, *, trip_km: float = 14, speed: float = 60) -> None:
    """Bilen rapporterar först efter ankomsten (07:42); statistiken kommer med slutavläsningen."""
    poll_every(db, vehicle, car, m(0), m(30))
    car.drive_to(B, 14, m(42))
    car.trip_meter, car.avg_speed, car.stats_ts = trip_km, speed, m(42)
    poll_every(db, vehicle, car, m(45), m(70))


# ----------------------------------------------------------------------------- insamlingen


def test_collector_stores_average_speed_and_statistics_time():
    ts = "2026-10-05T07:42:00Z"
    results = {"statistics": ApiResult("ok", 200, data={"data": {
        "tripMeterAutomatic": {"value": 14.2, "unit": "km", "timestamp": ts},
        "averageSpeedAutomatic": {"value": 61, "unit": "km/h", "timestamp": ts},
    }})}
    f = collector.parse_results(results)
    assert f["trip_meter_auto_km"] == 14.2 and f["avg_speed_auto"] == 61
    assert f["statistics_reported_at"] == datetime(2026, 10, 5, 7, 42, tzinfo=UTC)
    # Nya fält ingår i innehållskontrollen: ändrad snittfart ger en ny observationsrad.
    other = dict(f, avg_speed_auto=62)
    assert collector.content_hash(f) != collector.content_hash(other)
    # Okänd enhet ger hellre inget värde än ett fel värde; mph räknas om.
    results["statistics"].data["data"]["averageSpeedAutomatic"]["unit"] = "furlongs"
    assert collector.parse_results(results)["avg_speed_auto"] is None
    results["statistics"].data["data"]["averageSpeedAutomatic"]["unit"] = "mph"
    assert collector.parse_results(results)["avg_speed_auto"] == pytest.approx(98.2, abs=0.1)


def test_observation_row_gets_the_new_fields(db, user, vehicle, car):
    car.trip_meter, car.avg_speed, car.stats_ts = 3.0, 40.0, m(-5)
    poll(db, vehicle, car, m(0))
    obs = db.execute(select(M.VehicleObservation)).scalars().one()
    assert obs.avg_speed_auto == 40.0 and obs.statistics_reported_at == m(-5)


# ----------------------------------------------------------------------------- (a) under färd


def test_observed_trip_gets_estimated_start_within_ten_minutes(db, user, vehicle, car):
    observed_trip(db, vehicle, car)
    (raw,) = raws(db)
    assert raw.start_time_quality == TimeQuality.ESTIMATED
    assert raw.start_latest - raw.start_earliest <= timedelta(minutes=10)
    assert raw.start_earliest <= raw.started_at <= raw.start_latest
    assert raw.started_at.second == 0 and raw.started_at.microsecond == 0  # hel minut
    basis = raw.start_estimate_basis
    assert basis["method"] == "odometer_speed" and basis["speed_kmh"] == pytest.approx(72.0)
    assert 0 < basis["margin_min"] <= 5 and basis["window_minutes"] <= 10
    assert set(basis) >= {"method", "earliest", "latest", "margin_min", "trip_km", "avg_speed_kmh", "speed_kmh",
                          "engine_running_at", "window_minutes"}
    trip = db.execute(select(M.LogicalTrip)).scalar_one()
    assert trip.started_at == raw.started_at and trip.start_time_quality == TimeQuality.ESTIMATED
    assert start_time.display(trip, TZ) == "≈ 09:05"  # 07:05 UTC
    assert "start_time_uncertain" not in trip.quality_flags


def test_window_midpoint_when_only_the_window_is_narrow():
    """K3: inga två avläsningar under färd, men fönstret är högst 10 min → mitten, marginal ≤ 5 min."""
    est = start_time.estimate(
        earliest=m(0), latest=m(8), start_odometer=100.0, ended_at=m(20), distance_km=15.0,
        steps=[view(m(20), 115.0)], end=view(m(20), 115.0),
    )
    assert est.method == "window" and est.at == m(4) and est.margin_min == 4.0
    wide = start_time.estimate(
        earliest=m(0), latest=m(11), start_odometer=100.0, ended_at=m(20), distance_km=15.0,
        steps=[view(m(20), 115.0)], end=view(m(20), 115.0),
    )
    assert wide.method == "none" and wide.at is None  # 11 min är för brett


def test_engine_running_narrows_latest_start():
    est = start_time.estimate(
        earliest=m(0), latest=m(30), start_odometer=100.0, ended_at=m(40), distance_km=20.0,
        steps=[view(m(6), engine=True), view(m(40), 120.0)], end=view(m(40), 120.0),
    )
    assert est.latest == m(6) and est.method == "window" and est.at == m(3)
    assert est.basis["engine_running_at"] == m(6).isoformat()


# ----------------------------------------------------------------------------- (b) bara slutavläsning


def test_silent_trip_stays_bounded_with_k1_disabled(db, user, vehicle, car):
    assert start_time.K1_ENABLED is False
    silent_trip(db, vehicle, car)
    (raw,) = raws(db)
    assert raw.started_at is None and raw.start_time_quality == TimeQuality.BOUNDED
    assert raw.start_estimate_basis is None
    assert raw.start_earliest is not None and raw.start_latest is not None
    trip = db.execute(select(M.LogicalTrip)).scalar_one()
    assert start_time.display(trip, TZ) == "" and "start_time_uncertain" in trip.quality_flags


def test_silent_trip_uses_trip_meter_when_k1_enabled(db, user, vehicle, car, monkeypatch):
    monkeypatch.setattr(start_time, "K1_ENABLED", True)
    silent_trip(db, vehicle, car, trip_km=14, speed=60)
    (raw,) = raws(db)
    assert raw.start_time_quality == TimeQuality.ESTIMATED
    assert raw.started_at == m(28)  # 14 km i 60 km/h före ankomsten 07:42
    assert raw.start_estimate_basis["method"] == "trip_meter"
    assert raw.start_estimate_basis["margin_min"] == 5.0
    assert raw.start_estimate_basis["trip_km"] == 14 and raw.start_estimate_basis["avg_speed_kmh"] == 60


def test_trip_meter_off_by_more_than_1_5_km_gives_no_k1(db, user, vehicle, car, monkeypatch):
    monkeypatch.setattr(start_time, "K1_ENABLED", True)
    silent_trip(db, vehicle, car, trip_km=15.6, speed=60)
    (raw,) = raws(db)
    assert raw.started_at is None and raw.start_time_quality == TimeQuality.BOUNDED


@pytest.mark.parametrize(
    ("trip_km", "speed", "stats_at", "ok"),
    [
        (15.5, 60.0, m(42), True),  # precis inom 1,5 km
        (15.6, 60.0, m(42), False),  # mer än 1,5 km
        (14.0, 4.0, m(42), False),  # för låg snittfart
        (14.0, 131.0, m(42), False),  # för hög snittfart
        (14.0, 60.0, m(39), False),  # statistiken är äldre än resans slut − 2 min
        (14.0, None, m(42), False),  # snittfart saknas
    ],
)
def test_k1_conditions(monkeypatch, trip_km, speed, stats_at, ok):
    monkeypatch.setattr(start_time, "K1_ENABLED", True)
    end = view(m(42), 114.0, trip_km=trip_km, speed=speed, stats_at=stats_at)
    est = start_time.estimate(earliest=m(-60), latest=m(35), start_odometer=100.0, ended_at=m(42), distance_km=14.0,
                              steps=[end], end=end)
    assert (est.method == "trip_meter") is ok


def test_k1_is_never_used_when_disabled():
    end = view(m(42), 114.0, trip_km=14.0, speed=60.0, stats_at=m(42))
    est = start_time.estimate(earliest=m(-60), latest=m(35), start_odometer=100.0, ended_at=m(42), distance_km=14.0,
                              steps=[end], end=end)
    assert est.at is None and est.method == "none"


# ----------------------------------------------------------------------------- klämning och motor av


def test_estimate_is_clamped_to_bounds(monkeypatch):
    """K2 ger en tid före E (bilen rapporterar låg fart) → tiden kläms till E."""
    steps = [view(m(10), 110.0), view(m(20), 115.0)]  # 30 km/h → start 07:10 − 10 km/30 km/h = 06:50
    est = start_time.estimate(earliest=m(2), latest=m(14), start_odometer=100.0, ended_at=m(30), distance_km=20.0,
                              steps=steps, end=view(m(30), 120.0))
    assert est.method == "odometer_speed" and est.at == m(2)
    # Och en K1-tid efter L kläms till L.
    monkeypatch.setattr(start_time, "K1_ENABLED", True)
    end = view(m(42), 114.0, trip_km=14.0, speed=130.0, stats_at=m(42))
    est = start_time.estimate(earliest=m(0), latest=m(20), start_odometer=100.0, ended_at=m(42), distance_km=14.0,
                              steps=[end], end=end)
    assert est.method == "trip_meter" and est.at == m(20)


def test_rounding_never_leaves_the_bounds():
    est = start_time.estimate(earliest=m(0.2), latest=m(0.9), start_odometer=None, ended_at=m(20), distance_km=None,
                              steps=[], end=view(m(20)))
    assert est.method == "window" and m(0.2) <= est.at <= m(0.9)


def test_engine_stopped_never_affects_the_estimate():
    base: dict[str, Any] = {"earliest": m(0), "latest": m(9), "start_odometer": 100.0, "ended_at": m(30),
                            "distance_km": 20.0, "end": view(m(30), 120.0)}
    steps = [view(m(12), 108.0), view(m(18), 115.0)]
    without = start_time.estimate(steps=steps, **base)
    stopped = [view(m(1), engine=False), view(m(5), engine=False), *steps]
    with_stopped = start_time.estimate(steps=stopped, **base)
    assert without.at is not None and without == with_stopped
    # Inte heller när bara avstängd motor finns: då görs ingen insnävning.
    only = start_time.estimate(earliest=m(0), latest=m(30), start_odometer=100.0, ended_at=m(40), distance_km=20.0,
                               steps=[view(m(2), engine=False)], end=view(m(40), 120.0))
    assert only.at is None and only.latest == m(30)


def test_contradicting_times_give_no_estimate():
    """Mätarökning före tidigaste start (bilens klocka fel) används inte; L < E ger ingen tid."""
    est = start_time.estimate(earliest=m(10), latest=m(15), start_odometer=100.0, ended_at=m(30), distance_km=20.0,
                              steps=[view(m(5), 105.0)], end=view(m(30), 120.0))
    assert est.at is not None and est.at >= m(10)  # den motsägande avläsningen hoppas över
    est = start_time.estimate(earliest=m(10), latest=m(5), start_odometer=100.0, ended_at=m(30), distance_km=20.0,
                              steps=[], end=view(m(30), 120.0))
    assert est.at is None


# ----------------------------------------------------------------------------- ägarens starttid


def test_owner_start_time_overrides_and_can_be_removed(db, user, vehicle, car, actor):
    silent_trip(db, vehicle, car)
    trip = db.execute(select(M.LogicalTrip)).scalar_one()
    trip = reload_trip(db, trip.id)
    start_time.set_override(db, trip, m(25), actor=actor)
    trip = reload_trip(db, trip.id)
    assert trip.started_at == m(25) and trip.start_time_quality == TimeQuality.EXACT
    assert "start_time_uncertain" not in trip.quality_flags
    assert start_time.display(trip, TZ) == "09:25"
    # En ny omräkning (t.ex. efter klassning) behåller ägarens tid.
    seg.refresh_logical(db, trip)
    assert trip.started_at == m(25)
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.start_time_override")).scalars().first()
    # Råresan ändras inte: bilens uppgifter finns kvar.
    (raw,) = raws(db)
    assert raw.started_at is None and raw.start_time_quality == TimeQuality.BOUNDED
    start_time.set_override(db, trip, None, actor=actor)
    trip = reload_trip(db, trip.id)
    assert trip.started_at is None and trip.start_time_quality == TimeQuality.BOUNDED
    assert "start_time_uncertain" in trip.quality_flags


def test_owner_start_time_via_api(client, db, user, vehicle, car):
    silent_trip(db, vehicle, car)
    trip = db.execute(select(M.LogicalTrip)).scalar_one()
    db.commit()
    resp = client.patch(f"/api/v1/trips/{trip.id}", json={"start_time": m(25).isoformat()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_time_source"] == "user" and body["start_time_text"] == "09:25"
    assert datetime.fromisoformat(body["started_at"]) == m(25)


# ----------------------------------------------------------------------------- sammanslagningens stillestånd


def test_stop_gap_counts_estimated_start_with_bounds(trips):
    a = trips.raw("Ett 1, Stad", "Två 2, Stad", start_at=datetime(2026, 9, 14, 6, 0, tzinfo=UTC), minutes=20,
                  source=TripSource.VOLVO_API)
    b = trips.raw("Två 2, Stad", "Tre 3, Stad", start_at=datetime(2026, 9, 14, 6, 30, tzinfo=UTC), minutes=20,
                  source=TripSource.VOLVO_API)
    b.start_time_quality = TimeQuality.ESTIMATED
    b.start_earliest = datetime(2026, 9, 14, 6, 25, tzinfo=UTC)
    b.start_latest = datetime(2026, 9, 14, 6, 35, tzinfo=UTC)
    gap = merge.stop_gap(a, b)
    assert (gap.minutes_low, gap.minutes_high, gap.exact) == (5.0, 15.0, False)
    # Utan gränser gäller den angivna tiden som förut.
    b.start_earliest = b.start_latest = None
    gap = merge.stop_gap(a, b)
    assert (gap.minutes_low, gap.minutes_high) == (10.0, 10.0)


# ----------------------------------------------------------------------------- underlaget


class _Trip:
    def __init__(self, start, end, sq, eq, source=TripSource.VOLVO_API):
        self.started_at, self.ended_at = start, end
        self.start_time_quality, self.end_time_quality = sq, eq
        self.source = source


def test_time_text_marks_estimated_times_with_ca_and_hard_space():
    s, e = datetime(2026, 9, 14, 5, 35, tzinfo=UTC), datetime(2026, 9, 14, 6, 10, tzinfo=UTC)
    t = _Trip(s, e, TimeQuality.ESTIMATED, TimeQuality.REPORTED)
    assert simple.time_text(t, TZ) == f"ca{NBSP}07:35–08:10"  # type: ignore[arg-type]
    t = _Trip(s, e, TimeQuality.ESTIMATED, TimeQuality.ESTIMATED)
    assert simple.time_text(t, TZ) == f"ca{NBSP}07:35–ca{NBSP}08:10"  # type: ignore[arg-type]
    t = _Trip(None, e, TimeQuality.BOUNDED, TimeQuality.ESTIMATED)
    assert simple.time_text(t, TZ) == f"–ca{NBSP}08:10"  # type: ignore[arg-type]
    t = _Trip(s, e, TimeQuality.EXACT, TimeQuality.EXACT, source=TripSource.VOLVO_IMPORT)
    assert simple.time_text(t, TZ) == "07:35–08:10"  # type: ignore[arg-type]


def _export(db, user, vehicle, actor) -> Path:
    period = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=period.start, end=period.end,
                                   actor=actor, tz=TZ, now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
    return Path(job.file_path)


def _sheet_texts(path: Path) -> list[str]:
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


def test_excel_writes_ca_and_footnote_for_estimated_start(db, user, vehicle, actor, trips):
    trip = trips.trip("Ett 1, Stad", "Två 2, Stad", category=Category.WORK, purpose="Kundbesök", km=14.0,
                      start_at=datetime(2026, 9, 14, 5, 35, tzinfo=UTC), minutes=35, source=TripSource.VOLVO_API)
    raw = trip.segments[0].raw_trip
    raw.start_time_quality = TimeQuality.ESTIMATED
    raw.start_estimate_basis = {"method": "window", "margin_min": 4.0}
    seg.refresh_logical(db, trip)
    db.flush()
    texts = _sheet_texts(_export(db, user, vehicle, actor))
    assert f"ca{NBSP}07:35–08:10" in texts
    assert simple.FOOTNOTE_TIMES in texts
    assert simple.FOOTNOTE_TIMES.startswith("ca = uträknad ur bilens egna uppgifter")


def test_excel_footnote_for_unknown_start_on_live_trip(db, user, vehicle, actor, trips):
    trips.trip("Ett 1, Stad", "Två 2, Stad", category=Category.WORK, purpose="Kundbesök", km=14.0,
               start_at=datetime(2026, 9, 14, 6, 17, tzinfo=UTC), minutes=35, start_known=False,
               source=TripSource.VOLVO_API)
    texts = _sheet_texts(_export(db, user, vehicle, actor))
    assert "–08:52" in texts and simple.FOOTNOTE_TIMES in texts


def test_imported_trips_get_no_time_footnote(db, user, vehicle, actor, trips):
    """Importen är oförändrad: ingen "ca" och ingen ny fotnot, inte heller vid okänd start."""
    trips.trip("Ett 1, Stad", "Två 2, Stad", category=Category.WORK, purpose="Kundbesök", km=14.0,
               start_at=datetime(2026, 9, 14, 6, 17, tzinfo=UTC), minutes=35, start_known=False)
    texts = _sheet_texts(_export(db, user, vehicle, actor))
    assert "–08:52" in texts
    assert simple.FOOTNOTE_TIMES not in texts and not any(f"ca{NBSP}" in t for t in texts)
