"""Regressionstester för slutgranskningens fynd i backend (REG-2, B1/REG-3, D2, B2, A4, B4, D4)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

import app.models as M
from app.models.enums import Category, ReviewKind, ReviewState, TimeQuality
from app.services import settings as settings_svc
from app.services.importer import service as imp
from app.services.trips import merge
from app.services.trips import segments as seg
from tests.test_fix_data import DATE_ONLY_CSV

A, B, C = "A-gatan 1, Stad", "B-gatan 2, Stad", "C-gatan 3, Stad"


def _run_merge_rules(db, user, vehicle, actor):
    cfg = settings_svc.get_section(db, user.id, "merge")
    return merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor)


def _open_merge_items(db):
    return db.execute(
        select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.MERGE, M.ReviewItem.status == ReviewState.OPEN)
    ).scalars().all()


# ----------------------------------------------------------------------------- REG-2


def test_reg2_date_only_import_asks_no_merge_question(db, user, vehicle, actor):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("egen.csv", DATE_ONLY_CSV.encode())],
                               actor=actor)
    imp.commit(db, batch, actor=actor)
    _run_merge_rules(db, user, vehicle, actor)
    assert _open_merge_items(db) == []
    assert len(seg.active_trips(db, vehicle.id)) == 2
    # Manuell sammanslagning går fortfarande.
    merged = merge.merge_trips(db, seg.active_trips(db, vehicle.id), trigger="manual", actor=actor)
    assert len(seg.active_trips(db, vehicle.id)) == 1 and merged.distance_km == 40


def test_reg2_day_level_start_bounds_after_exact_end_ask_nothing(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=10, start_known=False)
    day = t1.ended_at.replace(hour=0, minute=0)
    raw2.start_earliest, raw2.start_latest = day - timedelta(hours=2), day + timedelta(hours=22)
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    result = _run_merge_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0 and _open_merge_items(db) == []


def test_reg2_bounded_previous_end_gives_unknown_gap(trips):
    r1 = trips.raw(A, B, km=10)
    r2 = trips.raw(B, C, km=10, gap_minutes=5)
    r1.end_time_quality = TimeQuality.BOUNDED
    assert not merge.stop_gap(r1, r2).known


def test_reg2_narrow_volvo_interval_still_asks(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=30, start_known=False)
    raw2.start_earliest, raw2.start_latest = t1.ended_at + timedelta(minutes=20), t1.ended_at + timedelta(minutes=45)
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    _run_merge_rules(db, user, vehicle, actor)
    (q,) = _open_merge_items(db)
    assert "mellan 20 och 45 minuter" in q.title
