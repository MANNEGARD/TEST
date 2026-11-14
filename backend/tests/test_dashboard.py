"""Dashboard, rapporter och tidszoner: summor mot handräknade värden (spec 20.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.models as M
from app.models.enums import Category, GapKind, GapStatus, ReviewStatus, ValueQuality
from app.services.stats import common, dashboard, reports
from app.services.trips import merge
from app.services.trips import segments as seg
from tests.conftest import BASE_TIME

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SEPT = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
A, B, C = "Kundvägen 1, Storstad", "Hemvägen 2, Byn", "Lagergatan 3, Storstad"


def summary(db, vehicle, period=SEPT):
    return dashboard.summary(db, vehicle=vehicle, start=period.start, end=period.end, tz=TZ, now=NOW)


def build_week(db, trips):
    """Handräknat underlag: 12,4 tjänst + 7,6 privat + 5,0 ej avgjort + 3,0 preliminär tjänst."""
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    t2 = trips.trip(C, B, category=Category.PRIVATE, km=7.6, gap_minutes=30)
    t3 = trips.trip(B, A, category=Category.UNDECIDED, km=5.0, gap_minutes=60)
    t4 = trips.trip(A, C, category=Category.WORK, purpose="Möte", km=3.0, gap_minutes=60, review_status=ReviewStatus.AUTO)
    # Resa 3 saknar uppmätt bensin (okänd förbrukning).
    raw3 = t3.segments[0].raw_trip
    raw3.fuel_liters, raw3.fuel_quality = None, ValueQuality.UNKNOWN
    seg.refresh_logical(db, t3)
    db.flush()
    return t1, t2, t3, t4


def test_summary_matches_hand_computed_values(db, user, vehicle, trips):
    build_week(db, trips)
    out = summary(db, vehicle)
    d = out["distance"]
    assert d["total"] == {"km": 28.0, "mil": 2.8}
    assert d["work"] == {"km": 15.4, "mil": 1.54}
    assert d["private"] == {"km": 7.6, "mil": 0.76}
    assert d["undecided"] == {"km": 5.0, "mil": 0.5}
    assert d["preliminary_work"]["km"] == 3.0  # "varav ej godkända"
    assert out["trips"]["total"] == 4 and out["trips"]["preliminary"] == 1 and out["trips"]["needs_review"] == 1
    assert out["trips"]["by_category"] == {"work": 2, "private": 1, "undecided": 1, "mixed": 0}

    fuel = out["fuel"]
    verified_l = round(12.4 * 0.07, 2) + round(7.6 * 0.07, 2) + round(3.0 * 0.07, 2)
    assert fuel["verified"] == pytest.approx(verified_l)
    assert fuel["verified_km"] == pytest.approx(23.0) and fuel["unknown_km"] == pytest.approx(5.0)
    assert fuel["coverage"] == pytest.approx(round(23.0 / 28.0, 4))
    # l/100 km bara över den verifierade sträckan – aldrig över hela sträckan.
    assert fuel["per_100km"] == pytest.approx(round(verified_l / 23.0 * 100, 2))
    assert out["electricity"]["coverage"] == pytest.approx(1.0)
    assert out["coverage"]["status"] == "incomplete" and not out["coverage"]["private_total_derivable"]


def test_empty_period(db, user, vehicle, trips):
    build_week(db, trips)
    period = common.resolve_period("custom", tz=TZ, date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
    out = summary(db, vehicle, period)
    assert out["distance"]["total"] == {"km": 0.0, "mil": 0.0}
    assert out["trips"]["total"] == 0 and out["fuel"]["coverage"] is None and out["fuel"]["per_100km"] is None
    assert out["coverage"]["status"] == "no_data"
    ts = dashboard.timeseries(db, vehicle=vehicle, start=period.start, end=period.end, tz=TZ, bucket="day")
    assert len(ts["points"]) == 31 and all(p["total_km"] == 0 for p in ts["points"])
    assert ts["points"][0]["bucket"] == "2026-03-01" and ts["points"][-1]["bucket"] == "2026-03-31"
    assert dashboard.destinations(db, vehicle=vehicle, start=period.start, end=period.end, tz=TZ) == []
    weekly = reports.weekly_report(db, vehicle=vehicle, tz=TZ, week_start=date(2026, 3, 2), now=NOW)
    assert weekly["trips"]["total"] == 0 and "0,0 mil i tjänsten" in weekly["message"]
    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 3, 2), now=NOW)
    assert daily["message"] == "Inga resor registrerade idag." and not daily["all_clear"]


def test_timezone_boundary_late_evening_trip(db, user, vehicle, trips):
    # 23:30 svensk tid den 30 september = 21:30 UTC → september, inte oktober.
    late = trips.trip(A, C, category=Category.WORK, purpose="Sen leverans", km=10.0,
                      start_at=datetime(2026, 9, 30, 21, 30, tzinfo=UTC))
    # 00:30 svensk tid den 1 oktober = 22:30 UTC den 30 september → oktober.
    early = trips.trip(C, A, category=Category.PRIVATE, km=4.0, start_at=datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
    oct_period = common.resolve_period("custom", tz=TZ, date_from=date(2026, 10, 1), date_to=date(2026, 10, 31))
    assert summary(db, vehicle)["distance"]["total"]["km"] == 10.0
    assert summary(db, vehicle, oct_period)["distance"]["total"]["km"] == 4.0

    q3 = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 10, 31))
    days = {p["bucket"]: p for p in dashboard.timeseries(db, vehicle=vehicle, start=q3.start, end=q3.end, tz=TZ)["points"]}
    assert days["2026-09-30"]["total_km"] == 10.0 and days["2026-10-01"]["total_km"] == 4.0
    months = dashboard.timeseries(db, vehicle=vehicle, start=q3.start, end=q3.end, tz=TZ, bucket="month")["points"]
    assert [(p["label"], p["total_km"]) for p in months] == [("sep 2026", 10.0), ("okt 2026", 4.0)]

    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 9, 30), now=NOW)
    assert [t["id"] for t in daily["trips"]] == [str(late.id)]
    assert daily["trips"][0]["local_start"] == "2026-09-30 23:30"
    comparison = dashboard.monthly_comparison(db, vehicle=vehicle, tz=TZ, year=2026, now=NOW)
    assert comparison["months"][8]["current"]["total_km"] == 10.0
    assert comparison["months"][9]["current"]["total_km"] == 4.0
    del early


def test_resolve_period_handles_dst_and_validation():
    p = common.resolve_period("custom", tz=TZ, date_from=date(2026, 10, 25), date_to=date(2026, 10, 25))
    assert (p.end - p.start) == timedelta(hours=25)  # sommartiden slutar
    year = common.resolve_period("year", tz=TZ, year=2026)
    assert year.start == datetime(2025, 12, 31, 23, 0, tzinfo=UTC) and year.label == "2026"
    prev = common.resolve_period("prev_month", tz=TZ, now=datetime(2026, 1, 15, tzinfo=UTC))
    assert prev.label == "2025-12"
    with pytest.raises(common.PeriodError):
        common.resolve_period("custom", tz=TZ, date_from=date(2026, 2, 1), date_to=date(2026, 1, 1))
    with pytest.raises(common.PeriodError):
        common.resolve_period("decade", tz=TZ)


def test_open_gaps_are_reported_and_private_is_not_derived(db, user, vehicle, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0)
    trips.skip(hours=2, km=60)
    trips.trip(C, A, category=Category.WORK, purpose="Retur", km=20.0)
    db.add(M.DataGap(vehicle_id=vehicle.id, kind=GapKind.ODOMETER, dedupe_key="odo:10020:10080", odometer_start=10020,
                     odometer_end=10080, km=60.0, from_time=BASE_TIME + timedelta(minutes=20),
                     to_time=BASE_TIME + timedelta(hours=2, minutes=20), status=GapStatus.OPEN))
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.start, odometer_km=10000, source="manual"))
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.end, odometer_km=10100, source="manual"))
    db.flush()
    out = summary(db, vehicle)
    assert out["unjournaled"]["open_gap_km"] == 60.0 and out["unjournaled"]["open_gap_count"] == 1
    # Mätarställningen visar 100 km, journalen 40 km tjänst – privat är INTE 60 km.
    assert out["distance"]["private"]["km"] == 0.0
    assert out["coverage"]["odometer_delta_km"] == 100.0 and out["coverage"]["unreconciled_km"] == 60.0
    assert not out["coverage"]["reconciled"] and not out["coverage"]["private_total_derivable"]
    weekly = reports.weekly_report(db, vehicle=vehicle, tz=TZ, week_start=date(2026, 9, 16), now=NOW)
    assert weekly["data_gaps"] == {"count": 1, "km": 60.0}
    assert "1 dataluckor (60 km) saknar journalpost" in weekly["message"]


def test_reconciled_period_is_complete(db, user, vehicle, trips):
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.start, odometer_km=10000, source="manual"))
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0)
    trips.trip(C, A, category=Category.PRIVATE, km=20.0, gap_minutes=60)
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.end, odometer_km=10040, source="manual"))
    db.flush()
    cov = summary(db, vehicle)["coverage"]
    assert cov["status"] == "complete" and cov["private_total_derivable"]


def test_merged_trip_counts_once_in_dashboard(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=4.2)
    t2 = trips.trip(C, B, category=Category.WORK, purpose="Kundbesök", km=37.9, gap_minutes=8)
    merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)
    out = summary(db, vehicle)
    assert out["distance"]["total"]["km"] == 42.1 and out["trips"]["total"] == 1
    assert out["fuel"]["verified_km"] == pytest.approx(42.1)


def test_mixed_trip_split_in_dashboard(db, user, vehicle, trips):
    t = trips.trip(A, B, category=Category.WORK, purpose="Kundbesök", km=42.0)
    seg.allocate_raw_trip(db, t, t.segments[0].raw_trip_id,
                          [{"distance_km": 30.0, "category": "work"}, {"distance_km": 12.0, "category": "private"}], "test")
    out = summary(db, vehicle)
    assert out["distance"]["work"]["km"] == 30.0 and out["distance"]["private"]["km"] == 12.0
    assert out["trips"]["by_category"]["mixed"] == 1
    # Energin räknas per fysisk resa – en gång, inte per delsträcka.
    assert out["fuel"]["verified_km"] == pytest.approx(42.0)


def test_destinations_and_routes_hide_private_places(db, user, vehicle, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0)
    trips.trip(C, B, category=Category.PRIVATE, km=8.0, gap_minutes=60)
    trips.trip(B, C, category=Category.WORK, purpose="Kundbesök", km=8.0, gap_minutes=600)
    dest = dashboard.destinations(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ)
    assert dest[0]["address"] == C and dest[0]["trips"] == 2 and dest[0]["total_km"] == 18.0
    private = next(d for d in dest if d["label"].startswith("Privat resa"))
    assert private["address"] is None and private["label"] == "Privat resa (Byn)"
    rts = dashboard.routes(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ)
    assert sum(r["trips"] for r in rts) == 3
    private_route = next(r for r in rts if r["private_km"] > 0)
    assert private_route["destination"] == "Privat resa (Byn)" and "Lagergatan" not in private_route["origin"]


def test_weekly_and_daily_reports(db, user, vehicle, trips):
    build_week(db, trips)
    weekly = reports.weekly_report(db, vehicle=vehicle, tz=TZ, week_start=date(2026, 9, 17), now=NOW)
    assert weekly["week"] == {"year": 2026, "week": 38, "start": "2026-09-14", "end": "2026-09-20"}
    assert weekly["distance"]["work"]["km"] == 15.4
    assert weekly["message"].startswith("Veckans körjournal: 1,5 mil i tjänsten, 0,8 mil privat och 2 resor som behöver granskas.")
    assert "0,5 mil är inte klassade" in weekly["message"]
    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 9, 14), now=NOW)
    assert daily["trip_count"] == 4 and len(daily["problems"]) == 2
    issues = {p["purpose"] or "": p["issues"] for p in daily["problems"]}
    assert issues[""] == ["Kategori är inte avgjord"]
    assert issues["Möte"] == ["Preliminär klassning är inte godkänd"]
