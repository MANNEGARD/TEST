"""Regressionstester för granskningsfynden om dataintegritet (sammanslagning, delning,
ångra, luckor, granskningsärenden, platser och import).

Bärande invariant: summan av aktiva journalresors km = summan av icke-makulerade
RawTrips km (per kategori enligt segmenten), ingen RawTrip i två eller noll aktiva
journalresor, och privat sträcka blir aldrig tjänst genom sammanslagning, delning
eller ångra.
"""

from __future__ import annotations

import io
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    DecisionSource,
    GapStatus,
    LocationType,
    LogicalState,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TimeQuality,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import engine as cengine
from app.services.classify import llm
from app.services.importer import parse as P
from app.services.importer import service as imp
from app.services.learning import feedback, patterns
from app.services.places import registry as places
from app.services.trips import gaps, merge, pipeline
from app.services.trips import segments as seg
from app.services.trips.segments import TripError
from tests.conftest import reload_trip
from tests.fixtures import volvo_exports as fx

API = "/api/v1"
A, B, C, D = "A-gatan 1, Stad", "B-gatan 2, Stad", "C-gatan 3, Stad", "D-gatan 4, Stad"
TZ = ZoneInfo("Europe/Stockholm")
IMPORTED = dict(category_source=DecisionSource.IMPORT, review_status=ReviewStatus.IMPORTED)


# ----------------------------------------------------------------------------- hjälp


def run_rules(db, user, vehicle, actor, **kw):
    cfg = settings_svc.get_section(db, user.id, "merge")
    return merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor, **kw)


def per_raw_km(db, vehicle) -> dict[tuple[uuid.UUID, str], float]:
    """km per (RawTrip, kategori) i de aktiva journalresorna."""
    out: dict[tuple[uuid.UUID, str], float] = defaultdict(float)
    for t in seg.active_trips(db, vehicle.id):
        for s in t.segments:
            out[(s.raw_trip_id, seg.segment_category(t, s))] += s.distance_km
    return {k: round(v, 3) for k, v in out.items() if v > 1e-9}


def assert_invariant(db, vehicle) -> None:
    db.flush()
    db.expire_all()
    active = seg.active_trips(db, vehicle.id)
    raws = db.execute(
        select(M.RawTrip).where(M.RawTrip.vehicle_id == vehicle.id, M.RawTrip.voided_at.is_(None))
    ).scalars().all()
    count = {r.id: 0 for r in raws}
    for t in active:
        for r in seg.ordered_raws(t):
            assert r.id in count, "aktiv resa innehåller makulerad RawTrip"
            count[r.id] += 1
        assert abs(t.work_km + t.private_km + t.undecided_km - t.distance_km) < 1e-3
        assert abs(sum(s.distance_km for s in t.segments) - t.distance_km) < 1e-3
    assert all(n == 1 for n in count.values()), {k: n for k, n in count.items() if n != 1}
    assert abs(sum(t.distance_km for t in active) - sum(r.distance_km for r in raws)) < 1e-3
    for r in raws:  # per RawTrip: segmentens km = uppmätt sträcka
        km = sum(s.distance_km for t in active for s in t.segments if s.raw_trip_id == r.id)
        assert abs(km - r.distance_km) < seg.KM_EPS


def open_items(db, trip_id=None, kind=None):
    q = select(M.ReviewItem).where(M.ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)))
    if trip_id is not None:
        q = q.where(M.ReviewItem.logical_trip_id == trip_id)
    if kind is not None:
        q = q.where(M.ReviewItem.kind == kind)
    return db.execute(q).scalars().all()


def import_month(db, user, vehicle, actor, **commit):
    batch = imp.create_preview(
        db, user_id=user.id, vehicle=vehicle, files=[("juni.xlsx", fx.build_v2(fx.month_rows(2026, 6, 180000)))],
        actor=actor,
    )
    return batch, imp.commit(db, batch, actor=actor, **commit)


# ----------------------------------------------------------------------------- data-2


def test_merge_work_and_undecided_leaves_undecided_leg_following_trip(db, user, vehicle, actor, trips):
    trips.trip(A, B, km=10, category=Category.WORK, purpose="Arbete", **IMPORTED)
    trips.trip(B, C, km=30, gap_minutes=5)  # oavgjord, t.ex. live-detekterad
    run_rules(db, user, vehicle, actor)
    (m,) = seg.active_trips(db, vehicle.id)
    assert [s.category_override for s in sorted(m.segments, key=lambda s: s.seq)] == [Category.WORK, None]
    assert (m.category, m.work_km, m.undecided_km) == (Category.UNDECIDED, 10, 30)
    # Ett förslag på resan (som klassificeringsmotorn sätter) gäller även den oavgjorda delen.
    m.category = Category.WORK
    seg.refresh_logical(db, m)
    assert (m.work_km, m.undecided_km, m.private_km) == (40, 0, 0)
    feedback.apply_decision(db, m, user_id=user.id, actor=actor, approve=True)
    m = reload_trip(db, m.id)
    assert (m.review_status, m.work_km, m.undecided_km) == (ReviewStatus.APPROVED, 40, 0)
    assert_invariant(db, vehicle)


def test_merge_private_and_undecided_never_makes_private_work(db, user, vehicle, actor, trips):
    trips.trip(A, B, km=10, category=Category.PRIVATE, **IMPORTED)
    trips.trip(B, C, km=30, gap_minutes=5)
    run_rules(db, user, vehicle, actor)
    (m,) = seg.active_trips(db, vehicle.id)
    m.category = Category.WORK  # förslag på resan
    seg.refresh_logical(db, m)
    assert (m.private_km, m.work_km) == (10, 30)


def test_full_pipeline_work_plus_undecided_is_not_silently_approved(db, user, vehicle, actor, trips):
    home, office, cust = "Hemvägen 1, Stad", "Kontorsgatan 2, Stad", "Kundvägen 3, Stad"
    trips.location(home, name="Hemmet", type=LocationType.HOME)
    trips.location(office, name="Kontoret", type=LocationType.OFFICE)
    trips.location(cust, name="Kund AB", type=LocationType.CUSTOMER, company="Kund AB")
    for _ in range(6):
        trips.trip(office, cust, km=30, category=Category.WORK, purpose="Kundbesök Kund AB",
                   category_source=DecisionSource.USER, start_at=trips._clock + timedelta(days=1))
        trips.skip(hours=3, km=40)
    trips.skip(days=1)
    trips.trip(home, office, km=10, category=Category.WORK, purpose="Arbete på kontoret", **IMPORTED)
    trips.trip(office, cust, km=30, gap_minutes=5)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False, use_llm=False)
    db.flush()
    m = next(t for t in seg.active_trips(db, vehicle.id) if t.segment_count == 2)
    if m.undecided_km > 0:  # motorn lämnade resan oavgjord: då ska den granskas
        assert open_items(db, m.id, ReviewKind.CATEGORY)
        with pytest.raises(TripError):
            feedback.apply_decision(db, m, user_id=user.id, actor=actor, approve=True)
    else:
        assert m.work_km == 40
    assert_invariant(db, vehicle)


def test_approve_refused_while_legs_are_undecided(db, user, vehicle, actor, trips):
    """Även en äldre resa med uttrycklig 'undecided'-del kan inte godkännas och ger ett ärende."""
    t = trips.trip(A, B, km=40, category=Category.WORK, purpose="Kundbesök", review_status=ReviewStatus.AUTO,
                   category_source=DecisionSource.RULE)
    raw = seg.ordered_raws(t)[0]
    seg.allocate_raw_trip(db, t, raw.id, [{"distance_km": 10, "category": "work"},
                                          {"distance_km": 30, "category": "undecided"}], "test")
    pipeline.sync_trip_review_items(db, t)
    assert open_items(db, t.id, ReviewKind.CATEGORY)
    with pytest.raises(TripError) as exc:
        feedback.apply_decision(db, t, user_id=user.id, actor=actor, approve=True)
    assert exc.value.code == "unallocated"


# ----------------------------------------------------------------------------- data-3


def _mixed_merged(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.PRIVATE, km=10, **IMPORTED)
    trips.trip(B, C, category=Category.WORK, purpose="Kundbesök", km=30, gap_minutes=5, **IMPORTED)
    run_rules(db, user, vehicle, actor)
    (m,) = seg.active_trips(db, vehicle.id)
    pipeline.sync_trip_review_items(db, m)
    db.commit()
    return m


def test_allocated_mixed_merged_trip_can_be_approved_via_api(db, user, vehicle, actor, trips, client):
    m = _mixed_merged(db, user, vehicle, actor, trips)
    assert m.category == Category.UNDECIDED and m.undecided_km == 0
    r = client.post(f"{API}/trips/{m.id}/approve")
    assert r.status_code == 200, r.text
    t = client.get(f"{API}/trips/{m.id}").json()["trip"]
    assert (t["work_km"], t["private_km"], t["undecided_km"], t["review_status"]) == (30, 10, 0, "approved")
    kinds = {i["kind"] for i in client.get(f"{API}/review").json()["items"]}
    assert "mixed_allocation" not in kinds and "category" not in kinds


def test_per_leg_allocation_of_undecided_merge_can_be_approved(db, user, vehicle, actor, trips, client):
    trips.trip(A, B, km=10)
    trips.trip(B, C, km=30, gap_minutes=5)
    run_rules(db, user, vehicle, actor)
    (m,) = seg.active_trips(db, vehicle.id)
    db.commit()
    raws = client.get(f"{API}/trips/{m.id}").json()["raw_trips"]
    for raw, cat in zip(raws, ("private", "work"), strict=True):
        r = client.post(f"{API}/trips/{m.id}/allocate",
                        json={"raw_trip_id": raw["id"], "parts": [{"distance_km": raw["distance_km"], "category": cat}]})
        assert r.status_code == 200, r.text
    r = client.patch(f"{API}/trips/{m.id}", json={"purpose": "Kundbesök", "approve": True})
    assert r.status_code == 200, r.text
    assert (r.json()["work_km"], r.json()["private_km"], r.json()["review_status"]) == (30, 10, "approved")


def test_undecided_single_trip_still_needs_category(db, user, vehicle, actor, trips):
    t = trips.trip(A, B, km=10)
    with pytest.raises(TripError) as exc:
        feedback.apply_decision(db, t, user_id=user.id, actor=actor, approve=True)
    assert exc.value.code == "undecided"


# ----------------------------------------------------------------------------- data-4


def test_split_result_can_be_unmerged_into_single_trips(db, user, vehicle, actor, trips):
    trips.trip(A, B, km=10, category=Category.WORK, purpose="X", **IMPORTED)
    trips.trip(B, C, km=20, gap_minutes=5, category=Category.PRIVATE, **IMPORTED)
    trips.trip(C, D, km=30, gap_minutes=5, category=Category.WORK, purpose="Y", **IMPORTED)
    run_rules(db, user, vehicle, actor)
    (m,) = seg.active_trips(db, vehicle.id)
    assert m.segment_count == 3
    before = per_raw_km(db, vehicle)
    _, right = merge.split_trip(db, m, after_index=0, actor=actor)
    parts = merge.dissolve(db, seg.load_trip(db, right.id), actor=actor)
    assert [p.segment_count for p in parts] == [1, 1]
    assert per_raw_km(db, vehicle) == before
    assert_invariant(db, vehicle)
    # Nedbrytningen loggas och kan ångras.
    op = db.execute(select(M.TripMerge).where(M.TripMerge.trigger == "unmerge")).scalar_one()
    merge.undo_operation(db, op, actor=actor)
    assert per_raw_km(db, vehicle) == before
    assert_invariant(db, vehicle)


def test_import_undo_works_after_split(db, user, vehicle, actor):
    batch, _ = import_month(db, user, vehicle, actor)
    active = seg.active_trips(db, vehicle.id)
    i = next(i for i, t in enumerate(active) if t.segment_count >= 2)
    m3 = merge.merge_trips(db, [active[i], active[i + 1]], trigger="manual", actor=actor, acknowledge_gap=True)
    _, right = merge.split_trip(db, seg.load_trip(db, m3.id), after_index=0, actor=actor)
    assert right.segment_count >= 2
    res = imp.undo(db, batch, actor=actor)
    assert res["voided"] > 0
    assert seg.active_trips(db, vehicle.id) == []
    assert_invariant(db, vehicle)


# ----------------------------------------------------------------------------- data-5


def _gap_fill_then_import(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, km=10, category=Category.WORK)  # 10000 → 10010
    trips.skip(hours=5, km=50)  # lucka 10010 → 10060
    trips.trip(B, C, km=10, category=Category.WORK)  # 10060 → 10070
    gaps.recompute(db, vehicle.id)
    gap = gaps.list_gaps(db, vehicle.id)[0]
    filled = gaps.resolve_as_private(db, gap, actor=actor, note="privat")
    trips.trip(B, D, km=30, odometer_start=10020, start_at=t1.ended_at + timedelta(hours=1),
               category=Category.WORK, **IMPORTED)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False, origin="import")
    db.flush()
    db.expire_all()
    return filled


def test_resolved_gap_is_not_double_counted_after_later_import(db, user, vehicle, actor, trips, complete_scope):
    filled = _gap_fill_then_import(db, user, vehicle, actor, trips)
    active = seg.active_trips(db, vehicle.id)
    assert sum(t.distance_km for t in active) == 50  # 10 + 30 + 10, ingen dubbelräkning
    assert db.get(M.LogicalTrip, filled.id).state == LogicalState.VOIDED
    assert_invariant(db, vehicle)
    open_gaps = [(g.odometer_start, g.odometer_end) for g in gaps.list_gaps(db, vehicle.id, only_open=True)]
    assert open_gaps == [(10010, 10020), (10050, 10060)]
    assert [i.data["type"] for i in open_items(db, kind=ReviewKind.ANOMALY)] == ["gap_fill_stale"]


def test_resolved_gap_change_gives_no_item_in_business_only(db, user, vehicle, actor, trips):
    filled = _gap_fill_then_import(db, user, vehicle, actor, trips)
    assert db.get(M.LogicalTrip, filled.id).state == LogicalState.VOIDED
    assert not open_items(db, kind=ReviewKind.ANOMALY)
    assert_invariant(db, vehicle)


def test_gap_fill_survives_unrelated_recompute(db, user, vehicle, actor, trips, complete_scope):
    trips.trip(A, B, km=10, category=Category.WORK)
    trips.skip(hours=5, km=50)
    trips.trip(B, C, km=10, category=Category.WORK)
    gaps.recompute(db, vehicle.id)
    filled = gaps.resolve_as_private(db, gaps.list_gaps(db, vehicle.id)[0], actor=actor)
    trips.trip(C, D, km=5, gap_minutes=60, category=Category.WORK)  # efter luckan
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False)
    assert db.get(M.LogicalTrip, filled.id).state == LogicalState.ACTIVE
    assert gaps.list_gaps(db, vehicle.id)[0].status == GapStatus.RESOLVED_PRIVATE


# ----------------------------------------------------------------------------- data-6


def _two_merged_work_trips(db, user, vehicle, actor, trips, km1=12, km2=30):
    trips.trip(A, B, km=km1, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    trips.trip(B, C, km=km2, gap_minutes=5, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    run_rules(db, user, vehicle, actor)
    (m,) = seg.active_trips(db, vehicle.id)
    return m


def test_unmerge_keeps_category_decided_on_merged_trip(db, user, vehicle, actor, trips):
    m = _two_merged_work_trips(db, user, vehicle, actor, trips)
    feedback.apply_decision(db, m, user_id=user.id, actor=actor, category=Category.PRIVATE)
    merge.dissolve(db, reload_trip(db, m.id), actor=actor)
    db.flush()
    db.expire_all()
    active = seg.active_trips(db, vehicle.id)
    assert len(active) == 2
    assert sum(t.private_km for t in active) == 42 and sum(t.work_km for t in active) == 0
    assert all(t.review_status == ReviewStatus.APPROVED for t in active)
    assert_invariant(db, vehicle)


def test_unmerge_keeps_allocation_made_on_merged_trip(db, user, vehicle, actor, trips):
    m = _two_merged_work_trips(db, user, vehicle, actor, trips, km1=30, km2=12)
    raw1 = seg.ordered_raws(m)[0]
    seg.allocate_raw_trip(db, m, raw1.id, [{"distance_km": 20, "category": "work"},
                                           {"distance_km": 10, "category": "private"}], "user")
    before = per_raw_km(db, vehicle)
    merge.dissolve(db, m, actor=actor)
    assert per_raw_km(db, vehicle) == before
    active = seg.active_trips(db, vehicle.id)
    assert (sum(t.work_km for t in active), sum(t.private_km for t in active)) == (32, 10)
    assert_invariant(db, vehicle)


def test_unmerge_without_changes_restores_sources_unchanged(db, user, vehicle, actor, trips):
    m = _two_merged_work_trips(db, user, vehicle, actor, trips)
    restored = merge.dissolve(db, m, actor=actor)
    assert all((t.category, t.review_status, t.category_source) == (Category.WORK, ReviewStatus.IMPORTED,
                                                                    DecisionSource.IMPORT) for t in restored)
    assert all(not any(s.category_override for s in t.segments) for t in restored)


def test_undo_split_keeps_decision_made_on_part(db, user, vehicle, actor, trips):
    m = _two_merged_work_trips(db, user, vehicle, actor, trips)
    left, _ = merge.split_trip(db, m, after_index=0, actor=actor)
    feedback.apply_decision(db, seg.load_trip(db, left.id), user_id=user.id, actor=actor,
                            category=Category.PRIVATE, rebuild=False)
    before = per_raw_km(db, vehicle)
    op = db.execute(select(M.TripMerge).where(M.TripMerge.operation == "split")).scalar_one()
    merge.undo_operation(db, op, actor=actor)
    assert per_raw_km(db, vehicle) == before
    assert_invariant(db, vehicle)


# ----------------------------------------------------------------------------- data-7


def test_items_closed_when_manual_trip_deleted(client, db):
    r = client.post(f"{API}/trips/manual", json={
        "started_at": "2026-09-14T08:00:00+02:00", "ended_at": "2026-09-14T08:30:00+02:00",
        "start_address": A, "end_address": B, "distance_km": 20})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert client.get(f"{API}/review").json()["open_total"] >= 1
    assert client.delete(f"{API}/trips/{tid}").status_code == 204
    after = client.get(f"{API}/review").json()
    assert after["open_total"] == 0, after["items"]


def test_merge_questions_closed_after_import_undo(db, user, vehicle, actor):
    batch, _ = import_month(db, user, vehicle, actor, apply_merge_rules=False)
    assert open_items(db, kind=ReviewKind.MERGE)
    imp.undo(db, batch, actor=actor)
    assert not open_items(db)


def test_merge_question_kept_while_answerable(db, user, vehicle, actor, trips):
    """En fråga om ett par vars andra resa slagits ihop med en tredje kan fortfarande besvaras."""
    trips.trip(A, B, km=10, category=Category.WORK, purpose="X", **IMPORTED)
    trips.trip(B, C, km=10, gap_minutes=30, category=Category.WORK, purpose="X", **IMPORTED)  # regel B
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False, use_llm=False)
    (q,) = open_items(db, kind=ReviewKind.MERGE)
    trips.trip(C, D, km=10, gap_minutes=5, category=Category.WORK, purpose="X", **IMPORTED)  # regel A med den andra
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False, use_llm=False)
    assert [i.id for i in open_items(db, kind=ReviewKind.MERGE)] == [q.id]


# ----------------------------------------------------------------------------- data-8


def test_location_merge_moves_manual_rule_and_feedback(db, user, vehicle, actor, trips):
    keep = trips.location("Kundvägen 3, Stad", name="Kund AB")
    other = trips.location("Kundvägen 5, Stad", name="Kund AB (parkering)")
    rule = patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=other.id, category=Category.PRIVATE,
                                    purpose=None)
    t = trips.trip(A, "Kundvägen 5, Stad", km=10, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    feedback.apply_decision(db, t, user_id=user.id, actor=actor, category=Category.PRIVATE)
    places.merge_locations(db, keep, other)
    patterns.rebuild(db, user.id)
    db.flush()
    db.expire_all()
    rule = db.get(M.LocationPattern, rule.id)
    assert rule.pattern_key == patterns.dest_key(keep.id)
    _, dest = patterns.get_patterns(db, user.id, None, keep.id)
    assert dest is not None and dest.manual_category == Category.PRIVATE
    assert dest.corrections_to_private == 1
    fb = db.execute(select(M.LearningFeedback)).scalar_one()
    assert fb.destination_location_id == keep.id


def test_location_merge_keeps_existing_rule_on_kept_place(db, user, trips):
    keep = trips.location("Kundvägen 3, Stad", name="Kund AB")
    other = trips.location("Kundvägen 5, Stad", name="Kund AB (parkering)")
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=keep.id, category=Category.WORK, purpose="Kund")
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=other.id, category=Category.PRIVATE, purpose=None)
    places.merge_locations(db, keep, other)
    rows = db.execute(select(M.LocationPattern).where(M.LocationPattern.is_manual_rule.is_(True))).scalars().all()
    assert [(r.pattern_key, r.manual_category) for r in rows] == [(patterns.dest_key(keep.id), Category.WORK)]


# ----------------------------------------------------------------------------- data-9


def test_clear_allocation_is_audited(client, db, user, vehicle, actor, trips):
    t = trips.trip(A, B, km=41.8, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    db.commit()
    raw = client.get(f"{API}/trips/{t.id}").json()["raw_trips"][0]
    r = client.post(f"{API}/trips/{t.id}/allocate", json={"raw_trip_id": raw["id"], "parts": [
        {"distance_km": 31.8, "category": "work"}, {"distance_km": 10, "category": "private"}]})
    assert r.status_code == 200, r.text
    r = client.delete(f"{API}/trips/{t.id}/allocate/{raw['id']}")
    assert r.status_code == 200, r.text
    assert (r.json()["work_km"], r.json()["private_km"]) == (41.8, 0)
    rows = db.execute(
        select(M.AuditEvent).where(M.AuditEvent.action == "trip.allocation_cleared")
    ).scalars().all()
    assert len(rows) == 1
    assert {p["category"] for p in rows[0].before["parts"]} == {"work", "private"}
    assert rows[0].after["parts"] == [{"distance_km": 41.8, "category": "work", "note": None}]


# ----------------------------------------------------------------------------- data-10


def test_split_restores_each_sources_own_purpose(db, user, vehicle, actor, trips):
    kw = dict(category=Category.WORK, **IMPORTED)
    t1 = trips.trip(A, B, km=12, purpose="Kundbesök Alfa AB", **kw)
    t2 = trips.trip(B, C, km=30, gap_minutes=40, purpose="Kundbesök Beta AB", **kw)
    m = merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)
    assert m.purpose == "Kundbesök Alfa AB; Kundbesök Beta AB"
    left, right = merge.split_trip(db, seg.load_trip(db, m.id), after_index=0, actor=actor)
    assert (left.purpose, left.purpose_confirmed) == ("Kundbesök Alfa AB", True)
    assert (right.purpose, right.purpose_confirmed) == ("Kundbesök Beta AB", True)


def test_split_after_purpose_edit_requires_confirmation(db, user, vehicle, actor, trips):
    kw = dict(category=Category.WORK, **IMPORTED)
    t1 = trips.trip(A, B, km=12, purpose="Kundbesök Alfa AB", **kw)
    t2 = trips.trip(B, C, km=30, gap_minutes=40, purpose="Kundbesök Beta AB", **kw)
    m = merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)
    feedback.apply_decision(db, m, user_id=user.id, actor=actor, purpose="Kundbesök Alfa och Beta", rebuild=False)
    left, right = merge.split_trip(db, seg.load_trip(db, m.id), after_index=0, actor=actor)
    assert left.purpose == right.purpose == "Kundbesök Alfa och Beta"
    assert not left.purpose_confirmed and not right.purpose_confirmed


# ----------------------------------------------------------------------------- data-11


def test_other_sheets_with_trips_give_warning():
    june = fx.month_rows(2026, 6, 180000)
    july = fx.month_rows(2026, 7, june[-1].odo_end + 310)
    wb = openpyxl.Workbook()
    for i, (title, rows) in enumerate((("Juni", june), ("Juli", july))):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append(fx.HEADERS_V2)
        for r in rows:
            ws.append([r.category, fx._fmt(r.start), r.odo_start, r.start_addr, fx._fmt(r.end), r.odo_end,
                       r.end_addr, fx._hm(r.drive_seconds), r.km, r.fuel, r.kwh, r.note])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = P.parse_file("tva_blad.xlsx", buf.getvalue(), TZ, max_bytes=10_000_000, max_rows=20000)
    assert len(parsed.rows) == len(june) and parsed.sheet_name == "Juni"
    assert any("Juli" in w and f"{len(july)} rader" in w for w in parsed.warnings), parsed.warnings


# ----------------------------------------------------------------------------- data-12


def test_trip_across_dst_fall_back_uses_matching_occurrence():
    hdr = ";".join(fx.HEADERS_V2)
    csv_text = "\n".join([
        hdr,
        "Arbete;2026-10-25 02:50;1000;A-gatan 1, Stad;2026-10-25 02:10;1020;B-gatan 2, Stad;00:20 h;20,0;1,0;2,0;Jour",
        "Arbete;2026-10-25 01:50;900;A-gatan 1, Stad;2026-10-25 02:20;1000;B-gatan 2, Stad;01:30 h;100,0;5,0;9,0;Jour",
        "Arbete;2026-10-25 02:10;800;A-gatan 1, Stad;2026-10-25 02:40;810;B-gatan 2, Stad;00:30 h;10,0;1,0;1,0;Jour",
    ]) + "\n"
    parsed = P.parse_file("dst.csv", csv_text.encode(), TZ, max_bytes=10_000_000, max_rows=1000)

    def minutes(r):
        return (datetime.fromisoformat(r.values["ended_at"]) - datetime.fromisoformat(r.values["started_at"])) \
            .total_seconds() / 60

    r1, r2, r3 = parsed.rows
    assert r1.ok and minutes(r1) == 20
    assert r2.ok and minutes(r2) == 90
    assert r3.ok and minutes(r3) == 30  # båda i första förekomsten – oförändrat
    assert any("andra förekomsten" in m for m in r1.messages + r2.messages)


# ----------------------------------------------------------------------------- data-13


DATE_ONLY_CSV = (
    "Startdatum;Mätarställning start;Från;Sluttid;Mätarställning slut;Till;Avstånd (km);Syfte\n"
    "2026-06-01;1000;A-gatan 1, Stad;2026-06-01;1020;B-gatan 2, Stad;20,0;Kundbesök\n"
    "2026-06-01;1020;B-gatan 2, Stad;2026-06-01;1040;A-gatan 1, Stad;20,0;Hem\n"
)


def test_date_only_times_are_flagged_and_not_exact(db, user, vehicle, actor):
    parsed = P.parse_file("egen.csv", DATE_ONLY_CSV.encode(), TZ, max_bytes=10_000_000, max_rows=100)
    assert all(r.ok and any("saknar klockslag" in m for m in r.messages) for r in parsed.rows)
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("egen.csv", DATE_ONLY_CSV.encode())],
                               actor=actor)
    imp.commit(db, batch, actor=actor)
    raws = db.execute(select(M.RawTrip)).scalars().all()
    assert len(raws) == 2
    assert {(r.start_time_quality, r.end_time_quality) for r in raws} == {(TimeQuality.BOUNDED, TimeQuality.BOUNDED)}
    first = raws[0]
    assert first.start_earliest.astimezone(TZ).hour == 0 and first.start_latest.astimezone(TZ).hour == 23
    # Okänt klockslag ger inte 0 minuters stillestånd → ingen automatisk sammanslagning.
    assert len(seg.active_trips(db, vehicle.id)) == 2


# ----------------------------------------------------------------------------- data-14


def test_import_without_merge_rules_waits_for_answers(db, user, vehicle, actor):
    _, res = import_month(db, user, vehicle, actor, apply_merge_rules=False)
    n = len(seg.active_trips(db, vehicle.id))
    q = len(open_items(db, kind=ReviewKind.MERGE))
    assert res["auto_merged"] == 0 and q > 0
    out = pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False,
                                       origin="manual", use_llm=False)
    assert out["auto_merged"] == 0
    assert len(seg.active_trips(db, vehicle.id)) == n
    assert len(open_items(db, kind=ReviewKind.MERGE)) == q


# ----------------------------------------------------------------------------- klas-4

OFFICE, CUSTOMER, GYM = "Kontorsgatan 4, Storstad", "Lagergatan 7, Storstad", "Idrottsvägen 3, Hemby"


class FakeProvider:
    name, model = "fake", "fake-1"

    def __init__(self, data):
        self.data = data

    def assess(self, document):
        return llm.LLMResult(data=llm._coerce(self.data), provider=self.name, model=self.model, usage={})


def _ai(category="work", text="Kundbesök hos Exempelbolaget"):
    return {"category": category, "confidence": 0.9, "purpose": text, "evidence": ["x"],
            "purpose_needs_confirmation": False, "requires_review": False}


def _ai_places(trips):
    trips.location(OFFICE, name="Storstadskontoret", type=LocationType.OFFICE)
    trips.location(CUSTOMER, name="Exempelbolaget", type=LocationType.CUSTOMER, company="Exempelbolaget")
    trips.location(GYM, name="Gymmet", type=LocationType.PRIVATE)


def _current_source(db, trip_id):
    return db.execute(select(M.TripClassification.source).where(
        M.TripClassification.logical_trip_id == trip_id, M.TripClassification.is_current.is_(True))).scalar_one()


def test_decision_on_other_trip_keeps_ai_proposal(db, user, vehicle, trips):
    _ai_places(trips)
    t1 = trips.trip(OFFICE, CUSTOMER)
    trips.skip(days=1)
    t2 = trips.trip(OFFICE, GYM)
    cengine.classify_pending(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, provider=FakeProvider(_ai()))
    t1 = reload_trip(db, t1.id)
    before = (t1.category, t1.purpose, t1.purpose_source, t1.category_source)
    assert t1.category_source == DecisionSource.AI
    feedback.apply_decision(db, reload_trip(db, t2.id), user_id=user.id, actor=audit.user_actor(user.id),
                            category=Category.PRIVATE)
    t1 = reload_trip(db, t1.id)
    assert (t1.category, t1.purpose, t1.purpose_source, t1.category_source) == before


def test_undo_restores_ai_proposal_and_purpose_rows(db, user, vehicle, trips):
    _ai_places(trips)
    t = trips.trip(OFFICE, CUSTOMER)
    cengine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM, provider=FakeProvider(_ai()))
    snap = (t.category, t.review_status, t.purpose, t.purpose_source, t.category_source)
    actor = audit.user_actor(user.id)
    feedback.apply_decision(db, t, user_id=user.id, actor=actor, category=Category.PRIVATE, purpose="Gym")
    fb = db.execute(select(M.LearningFeedback)).scalar_one()
    feedback.undo(db, fb, actor=actor)
    t = reload_trip(db, t.id)
    assert (t.category, t.review_status, t.purpose, t.purpose_source, t.category_source) == snap
    assert _current_source(db, t.id) == DecisionSource.AI
    cur = db.execute(select(M.TripPurpose.text).where(
        M.TripPurpose.logical_trip_id == t.id, M.TripPurpose.is_current.is_(True))).scalars().all()
    assert cur == [snap[2]]


def test_undo_purpose_only_clears_user_purpose_row(db, user, vehicle, trips):
    _ai_places(trips)
    t = trips.trip(OFFICE, CUSTOMER)
    cengine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM, use_llm=False)
    before = t.purpose
    actor = audit.user_actor(user.id)
    feedback.apply_decision(db, t, user_id=user.id, actor=actor, purpose="Avtalsgenomgång")
    fb = db.execute(select(M.LearningFeedback)).scalar_one()
    feedback.undo(db, fb, actor=actor)
    t = reload_trip(db, t.id)
    assert t.purpose == before
    cur = db.execute(select(M.TripPurpose.text).where(
        M.TripPurpose.logical_trip_id == t.id, M.TripPurpose.is_current.is_(True))).scalars().all()
    assert "Avtalsgenomgång" not in cur


# ----------------------------------------------------------------------------- scope-1


def _private_manual_without_odometer(client):
    r = client.post(f"{API}/trips/manual", json={
        "started_at": "2026-09-14T18:00:00+02:00", "ended_at": "2026-09-14T18:30:00+02:00",
        "start_address": A, "end_address": B, "distance_km": 20, "category": "private"})
    assert r.status_code == 201, r.text
    return r.json()


def test_no_missing_odometer_item_for_private_trip_in_business_only(client, db):
    t = _private_manual_without_odometer(client)
    assert t["category"] == "private"
    kinds = {i["kind"] for i in client.get(f"{API}/review").json()["items"]}
    assert "missing_odometer" not in kinds


def test_missing_odometer_item_for_private_trip_in_complete_scope(complete_scope, client, db):
    _private_manual_without_odometer(client)
    kinds = {i["kind"] for i in client.get(f"{API}/review").json()["items"]}
    assert "missing_odometer" in kinds


def test_missing_odometer_item_for_work_trip_in_business_only(db, user, vehicle, actor, trips):
    t = trips.trip(A, B, km=10, category=Category.WORK, purpose="Kundbesök", odometer_start=None, **IMPORTED)
    t.segments[0].raw_trip.odometer_start = t.segments[0].raw_trip.odometer_end = None
    seg.refresh_logical(db, t)
    pipeline.sync_trip_review_items(db, t)
    assert open_items(db, t.id, ReviewKind.MISSING_ODOMETER)


# ----------------------------------------------------------------------------- slumptest


STRUCTURAL = {"merge", "dissolve", "split", "undo_op", "rules"}


# Fröna 27–184 återskapade ett fel där en ångrad uppdelning tappade beslut på senare delar.
@pytest.mark.parametrize("seed", [*range(12), 27, 86, 88, 114, 147, 184])
def test_random_operations_keep_invariants(db, user, vehicle, actor, trips, seed):
    """Slumpade följder av slå ihop / dela / fördela / besluta / ångra.

    Efter varje steg: invarianten håller. Strukturella åtgärder (slå ihop, dela, ångra,
    regelkörning) ändrar aldrig km per RawTrip och kategori – privat blir aldrig tjänst.
    Till sist kan varje flerdelsresa lösas upp.
    """
    rnd = random.Random(seed)
    addr = [f"Gata {i}, Stad" for i in range(12)]
    for i in range(8):
        cat = rnd.choice([Category.WORK, Category.PRIVATE, Category.UNDECIDED])
        trips.trip(addr[i], addr[i + 1], km=rnd.choice([5, 12.4, 30.6, 7.5]),
                   gap_minutes=rnd.choice([3, 10, 20, 45, 90]), category=cat,
                   purpose="Syfte" if cat == Category.WORK else None)
    cfg = settings_svc.get_section(db, user.id, "merge")
    log: list[str] = []
    for _ in range(50):
        db.expire_all()
        active = seg.active_trips(db, vehicle.id)
        op = rnd.choice(["merge", "merge", "dissolve", "split", "allocate", "clear", "undo_op", "rules", "decide",
                         "undo_fb"])
        before = per_raw_km(db, vehicle)
        try:
            if op == "merge" and len(active) >= 2:
                i = rnd.randrange(len(active) - 1)
                merge.merge_trips(db, active[i:i + rnd.choice([2, 2, 3])], trigger="manual", actor=actor,
                                  acknowledge_gap=True)
            elif op == "dissolve":
                merge.dissolve(db, rnd.choice(active), actor=actor)
            elif op == "split":
                t = rnd.choice(active)
                merge.split_trip(db, t, after_index=rnd.randrange(max(1, t.segment_count - 1)), actor=actor)
            elif op == "allocate":
                t = rnd.choice(active)
                raw = rnd.choice(seg.ordered_raws(t))
                x = round(raw.distance_km * rnd.choice([0.25, 0.5, 0.4]), 3)
                seg.allocate_raw_trip(db, t, raw.id, [{"distance_km": x, "category": "private"},
                                                      {"distance_km": round(raw.distance_km - x, 3),
                                                       "category": "work"}], "user")
            elif op == "clear":
                t = rnd.choice(active)
                seg.clear_allocation(db, t, rnd.choice(seg.ordered_raws(t)).id, "user")
            elif op == "undo_op":
                ops = db.execute(select(M.TripMerge)).scalars().all()
                if ops:
                    merge.undo_operation(db, rnd.choice(ops), actor=actor)
            elif op == "rules":
                merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor)
            elif op == "decide":
                feedback.apply_decision(db, rnd.choice(active), user_id=user.id, actor=actor,
                                        category=rnd.choice([Category.WORK, Category.PRIVATE]), purpose="X",
                                        rebuild=False)
            elif op == "undo_fb":
                fbs = db.execute(
                    select(M.LearningFeedback).where(M.LearningFeedback.undone_at.is_(None))
                ).scalars().all()
                if fbs:
                    feedback.undo(db, rnd.choice(fbs), actor=actor)
            log.append(op)
        except TripError as exc:
            log.append(f"{op}:{exc.code}")
        assert_invariant(db, vehicle)
        if op in STRUCTURAL:
            assert per_raw_km(db, vehicle) == before, (seed, log[-6:])
    for _ in range(20):  # nästlade sammanslagningar löses upp en nivå i taget
        multi = [t for t in seg.active_trips(db, vehicle.id) if t.segment_count > 1]
        if not multi:
            break
        before = per_raw_km(db, vehicle)
        assert merge.dissolve(db, seg.load_trip(db, multi[0].id), actor=actor)
        assert per_raw_km(db, vehicle) == before
        assert_invariant(db, vehicle)
    assert all(t.segment_count == 1 for t in seg.active_trips(db, vehicle.id))
