"""Vecko- och dagsrapporter: saknade syften, preliminära klassningar och problemlistor."""

from __future__ import annotations

from datetime import UTC, date, datetime

import app.models as M
from app.models.enums import Category, ReviewKind, ReviewStatus
from app.services.stats import reports
from app.services.trips import review
from app.services.trips import segments as seg

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
DAY = date(2026, 9, 14)


def daily(db, vehicle, tz, day=DAY):
    return reports.daily_reconciliation(db, vehicle=vehicle, tz=tz, day=day, now=NOW)


def test_all_clear_day(db, user, vehicle, trips):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Stockholm")
    trips.trip(category=Category.WORK, purpose="Kundbesök", km=12.0)
    trips.trip(category=Category.PRIVATE, km=8.0, gap_minutes=60)
    out = daily(db, vehicle, tz)
    assert out["all_clear"] and out["problems"] == []
    assert out["message"] == "Dagens 2 resor (2,0 mil) är klara."


def test_daily_problem_list(db, user, vehicle, trips):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Stockholm")
    missing = trips.trip(category=Category.WORK, km=10.0)
    placeholder = trips.trip(category=Category.WORK, purpose="Besök hos kund – syfte behöver bekräftas.", km=5.0,
                             gap_minutes=60, purpose_confirmed=False)
    proposed = trips.trip(category=Category.WORK, purpose="Möte", km=5.0, gap_minutes=60, purpose_confirmed=False,
                          review_status=ReviewStatus.PROPOSED)
    mixed = trips.trip(category=Category.WORK, purpose="Kundbesök", km=20.0, gap_minutes=60)
    seg.allocate_raw_trip(db, mixed, mixed.segments[0].raw_trip_id,
                          [{"distance_km": 15.0, "category": "work"}, {"distance_km": 5.0, "category": "private"}], "test")
    mixed.review_status = ReviewStatus.PROPOSED
    taxed = trips.trip(category=Category.WORK, purpose="Resa till arbetsplatsen", km=7.0, gap_minutes=60)
    taxed.tax_note, taxed.review_status = "Kan vara resa bostad–arbetsplats", ReviewStatus.NEEDS_REVIEW
    review.upsert(db, vehicle_id=vehicle.id, kind=ReviewKind.PURPOSE, dedupe_key="purpose:x", title="Syfte",
                  logical_trip_id=missing.id)
    db.flush()

    out = daily(db, vehicle, tz)
    by_id = {p["id"]: p["issues"] for p in out["problems"]}
    assert "Syfte saknas" in by_id[str(missing.id)]
    assert "Syfte saknas" in by_id[str(placeholder.id)]  # platshållaren räknas som saknat syfte
    assert by_id[str(proposed.id)] == ["Preliminär klassning är inte godkänd", "Föreslaget syfte är inte bekräftat"]
    assert "Blandad privat/tjänst – kontrollera fördelningen" in by_id[str(mixed.id)]
    assert by_id[str(taxed.id)] == ["Klassningen behöver granskas", "Skattemässig bedömning krävs"]
    assert out["missing_purpose"] == 2 and out["open_review_items"] == 1
    assert out["message"] == "Dagens körjournal: 5 resor, 4,7 mil. 5 behöver åtgärdas, varav 2 saknar syfte."
    assert not out["all_clear"]


def test_weekly_counts_unreviewed_and_unconfirmed(db, user, vehicle, trips):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Stockholm")
    trips.trip(category=Category.WORK, purpose="Kundbesök", km=25.0)
    trips.trip(category=Category.WORK, purpose="Förslag", km=10.0, gap_minutes=60, purpose_confirmed=False)
    trips.trip(category=Category.PRIVATE, km=6.0, gap_minutes=60, review_status=ReviewStatus.AUTO)
    out = reports.weekly_report(db, vehicle=vehicle, tz=tz, now=datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
    assert out["week"]["week"] == 38
    assert out["unreviewed_trips"] == 1 and out["missing_purpose_trips"] == 1 and out["trips_to_review"] == 2
    assert out["message"] == "Veckans körjournal: 3,5 mil i tjänsten, 0,6 mil privat och 2 resor som behöver granskas."
    assert out["distance"]["preliminary_private"]["km"] == 6.0
    assert out["top_destinations"][0]["trips"] == 2  # den privata resans mål redovisas anonymt för sig
    assert reports.sv_number(48.62) == "48,6"
    assert M.LogicalTrip is not None
