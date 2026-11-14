"""Sammanslagningsreglerna A, B och C samt uppdelning, ångra och dataluckor."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    GapStatus,
    LogicalState,
    MergeKind,
    ReviewKind,
    ReviewState,
    ReviewStatus,
)
from app.services import settings as settings_svc
from app.services.trips import gaps, merge, pipeline
from app.services.trips import segments as seg
from app.services.trips.segments import TripError
from tests.conftest import reload_trip

A, B, C, D = "A-gatan 1, Stad", "B-gatan 2, Stad", "C-gatan 3, Stad", "D-gatan 4, Stad"


def run_rules(db, user, vehicle, actor, **kw):
    cfg = settings_svc.get_section(db, user.id, "merge")
    return merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor, **kw)


def two_trips(trips, gap_minutes: float, **kw):
    t1 = trips.trip(A, B, category=Category.WORK, purpose="Kundbesök", km=12.4, **kw)
    t2 = trips.trip(B, C, category=Category.WORK, purpose="Kundbesök", km=30.6, gap_minutes=gap_minutes, **kw)
    return t1, t2


def merge_questions(db):
    return db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.MERGE)).scalars().all()


# ----------------------------------------------------------------------------- gränsvärden


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (0, "auto"),
        (14, "auto"),
        (15, "auto"),  # exakt 15 minuter = automatisk sammanslagning
        (16, "ask"),
        (30, "ask"),
        (60, "ask"),  # exakt 60 minuter = användaren tillfrågas
        (61, "none"),
        (5 * 60, "none"),
        (3 * 24 * 60, "none"),
    ],
)
def test_rule_boundaries(db, user, vehicle, actor, trips, gap, expected):
    t1, t2 = two_trips(trips, gap)
    result = run_rules(db, user, vehicle, actor)
    active = seg.active_trips(db, vehicle.id)
    if expected == "auto":
        assert result["auto_merged"] == 1 and len(active) == 1
        merged = active[0]
        assert merged.merge_kind == MergeKind.AUTO and merged.segment_count == 2
        assert merge_questions(db) == []
    elif expected == "ask":
        assert result["auto_merged"] == 0 and len(active) == 2
        (q,) = merge_questions(db)
        assert q.status == ReviewState.OPEN
        assert q.data["minutes_low"] == pytest.approx(gap)
        assert f"{gap} minuter" in q.title
    else:
        assert result["auto_merged"] == 0 and len(active) == 2
        assert merge_questions(db) == []


def test_custom_thresholds_from_settings(db, user, vehicle, actor, trips):
    settings_svc.update_section(db, user.id, "merge", {"auto_max_minutes": 5, "ask_max_minutes": 20})
    two_trips(trips, 10)
    result = run_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0 and len(merge_questions(db)) == 1


# ----------------------------------------------------------------------------- regel A


def test_auto_merge_preserves_originals_and_sums_correctly(db, user, vehicle, actor, trips):
    t1 = trips.trip("Hemmet 1, Byn", "Macken 2, Byn", category=Category.WORK, purpose="Kontorsdag", km=4.2, minutes=6)
    t2 = trips.trip("Macken 2, Byn", "Kontoret 3, Staden", category=Category.WORK, purpose="Kontorsdag", km=37.9,
                    minutes=31, gap_minutes=8)
    raw1, raw2 = t1.segments[0].raw_trip, t2.segments[0].raw_trip
    before = (raw1.distance_km, raw1.odometer_start, raw1.odometer_end, raw2.odometer_start, raw2.odometer_end)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)

    assert merged.start_address == "Hemmet 1, Byn" and merged.end_address == "Kontoret 3, Staden"
    assert merged.distance_km == pytest.approx(42.1)  # 4,2 + 37,9 – ingen dubbelräkning
    assert merged.fuel_liters == pytest.approx(raw1.fuel_liters + raw2.fuel_liters)
    assert merged.fuel_quality == "verified"
    assert merged.started_at == raw1.started_at and merged.ended_at == raw2.ended_at
    assert merged.driving_seconds == 37 * 60
    assert merged.odometer_start == raw1.odometer_start and merged.odometer_end == raw2.odometer_end
    (stop,) = merged.via
    assert stop["address"] == "Macken 2, Byn" and stop["stop_minutes"] == 8
    assert merged.purpose == "Kontorsdag" and merged.category == Category.WORK
    assert merged.review_status == ReviewStatus.APPROVED

    # Originalresorna finns kvar orörda, med separata mätarställningar.
    db.expire_all()
    r1, r2 = db.get(M.RawTrip, raw1.id), db.get(M.RawTrip, raw2.id)
    assert (r1.distance_km, r1.odometer_start, r1.odometer_end, r2.odometer_start, r2.odometer_end) == before
    assert r1.voided_at is None and r2.voided_at is None
    assert db.get(M.LogicalTrip, t1.id).state == LogicalState.MERGED
    assert db.get(M.LogicalTrip, t1.id).merged_into_id == merged.id
    op = db.execute(select(M.TripMerge)).scalar_one()
    assert op.trigger == "rule_a" and set(op.source_trip_ids) == {str(t1.id), str(t2.id)}


def test_chain_of_short_stops_becomes_one_trip(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, km=5)
    trips.trip(B, C, category=Category.WORK, km=6, gap_minutes=3)
    trips.trip(C, D, category=Category.WORK, km=7, gap_minutes=12)
    trips.trip(D, A, category=Category.WORK, km=8, gap_minutes=15)
    result = run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    assert result["auto_merged"] == 3
    assert merged.segment_count == 4 and merged.distance_km == pytest.approx(26)
    assert [s["address"] for s in merged.via] == [B, C, D]


def test_merge_across_midnight_and_dates(db, user, vehicle, actor, trips):
    from tests.conftest import BASE_TIME

    late = BASE_TIME.replace(hour=21, minute=50)  # 23:50 lokal tid
    trips.trip(A, B, category=Category.WORK, km=10, minutes=15, start_at=late)
    trips.trip(B, C, category=Category.WORK, km=20, minutes=25, gap_minutes=10)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    assert merged.started_at == late
    assert merged.ended_at == late + timedelta(minutes=15 + 10 + 25)
    assert merged.ended_at.date() != merged.started_at.astimezone().date() or merged.ended_at > merged.started_at


def test_auto_merge_can_be_undone_and_is_not_redone(db, user, vehicle, actor, trips):
    t1, t2 = two_trips(trips, 8)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    restored = merge.dissolve(db, merged, actor=actor)
    assert {t.id for t in restored} == {t1.id, t2.id}
    assert [t.id for t in seg.active_trips(db, vehicle.id)] == [t1.id, t2.id]
    assert db.get(M.LogicalTrip, merged.id).state == LogicalState.SPLIT
    # Regelmotorn ska respektera att användaren ångrade.
    again = run_rules(db, user, vehicle, actor)
    assert again["auto_merged"] == 0 and len(seg.active_trips(db, vehicle.id)) == 2
    assert merge_questions(db) == []


def test_conflicting_categories_are_grouped_but_flagged_and_never_reclassified(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.PRIVATE, km=10)
    t2 = trips.trip(B, C, category=Category.WORK, purpose="Kundbesök", km=30, gap_minutes=5)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    assert merged.segment_count == 2
    assert merged.has_mixed_categories is True
    assert merged.private_km == pytest.approx(10) and merged.work_km == pytest.approx(30)
    assert merged.review_status == ReviewStatus.NEEDS_REVIEW
    assert {s.category_override for s in merged.segments} == {Category.PRIVATE, Category.WORK}
    pipeline.sync_trip_review_items(db, merged)
    kinds = {i.kind for i in db.execute(select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == merged.id)).scalars()}
    assert ReviewKind.MIXED_ALLOCATION in kinds
    assert t1.id and t2.id


def test_work_plus_undecided_keeps_undecided_km_undecided(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10)
    trips.trip(B, C, category=Category.UNDECIDED, km=30, gap_minutes=5)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    assert merged.work_km == pytest.approx(10) and merged.undecided_km == pytest.approx(30)
    assert merged.review_status == ReviewStatus.NEEDS_REVIEW


# ----------------------------------------------------------------------------- regel B


def test_question_answer_yes_merges(db, user, vehicle, actor, trips):
    t1, t2 = two_trips(trips, 35)
    run_rules(db, user, vehicle, actor)
    (q,) = merge_questions(db)
    import uuid

    merged = merge.answer_question(
        db, vehicle_id=vehicle.id, first_raw_trip_id=uuid.UUID(q.data["first_raw_trip_id"]),
        second_raw_trip_id=uuid.UUID(q.data["second_raw_trip_id"]), accept=True, actor=actor,
    )
    assert merged is not None and merged.merge_kind == MergeKind.PROMPTED
    assert merged.distance_km == pytest.approx(43.0)
    db.refresh(q)
    assert q.status == ReviewState.RESOLVED and q.resolution["decision"] == "merged"
    assert len(seg.active_trips(db, vehicle.id)) == 1
    assert t1.id and t2.id


def test_question_answer_no_keeps_separate_and_is_not_asked_again(db, user, vehicle, actor, trips):
    import uuid

    two_trips(trips, 35)
    run_rules(db, user, vehicle, actor)
    (q,) = merge_questions(db)
    result = merge.answer_question(
        db, vehicle_id=vehicle.id, first_raw_trip_id=uuid.UUID(q.data["first_raw_trip_id"]),
        second_raw_trip_id=uuid.UUID(q.data["second_raw_trip_id"]), accept=False, actor=actor,
    )
    assert result is None
    db.refresh(q)
    assert q.status == ReviewState.RESOLVED and q.resolution["decision"] == "keep_separate"
    again = run_rules(db, user, vehicle, actor)
    assert again["questions"] == [] and len(seg.active_trips(db, vehicle.id)) == 2


def test_unanswered_question_stays_open_and_trips_stay_separate(db, user, vehicle, actor, trips):
    two_trips(trips, 40)
    first = run_rules(db, user, vehicle, actor)
    second = run_rules(db, user, vehicle, actor)  # retroaktiv omkörning skapar ingen ny fråga
    assert len(first["questions"]) == 1 and second["questions"] == []
    assert len(merge_questions(db)) == 1 and len(seg.active_trips(db, vehicle.id)) == 2


def test_retroactive_merge_when_next_trip_arrives_later(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, km=10)
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 0
    trips.trip(B, C, category=Category.WORK, km=20, gap_minutes=9)  # rapporteras i efterhand
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 1


def test_import_mode_asks_instead_of_auto_merging(db, user, vehicle, actor, trips):
    two_trips(trips, 5)
    result = run_rules(db, user, vehicle, actor, apply_auto=False)
    assert result["auto_merged"] == 0
    (q,) = merge_questions(db)
    assert q.data["rule"] == "A"


# ----------------------------------------------------------------------------- saknade tider


def test_unknown_start_time_uses_bounds_and_never_invents(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    end1 = t1.ended_at
    raw2 = trips.raw(B, C, km=20, gap_minutes=10, start_known=False)
    raw2.start_earliest, raw2.start_latest = end1 + timedelta(minutes=2), end1 + timedelta(minutes=12)
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    # Övre gränsen 12 min ≤ 15 → säkert inom regel A.
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 1
    (merged,) = seg.active_trips(db, vehicle.id)
    assert "start_time_uncertain" not in merged.quality_flags  # första delresans start är känd


def test_uncertain_gap_straddling_threshold_asks(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=30, start_known=False)
    raw2.start_earliest, raw2.start_latest = t1.ended_at + timedelta(minutes=5), t1.ended_at + timedelta(minutes=40)
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    result = run_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0
    (q,) = merge_questions(db)
    assert q.data["exact"] is False and "mellan 5 och 40 minuter" in q.title


def test_completely_unknown_times_are_left_for_manual_handling(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, km=10)
    raw2 = trips.raw(B, C, km=20, gap_minutes=10, start_known=False)
    raw2.start_earliest = raw2.start_latest = None
    seg.set_raw_sort_time(raw2)
    seg.create_logical_for_raw(db, raw2, category=Category.WORK)
    result = run_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0 and merge_questions(db) == []


def test_overlapping_trips_are_flagged_not_merged(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10, minutes=30)
    trips.trip(B, C, category=Category.WORK, km=20, start_at=t1.started_at + timedelta(minutes=10))
    result = run_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0 and result["overlaps"] == 1
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.ANOMALY)).scalar_one()
    assert item.data["type"] == "overlap"


# ----------------------------------------------------------------------------- odometerluckor


def test_odometer_gap_blocks_automatic_merge_and_question(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, km=10)
    trips.skip(km=25)  # 25 km körning saknas i journalen
    trips.trip(B, C, category=Category.WORK, km=20, gap_minutes=5)
    result = run_rules(db, user, vehicle, actor)
    assert result["auto_merged"] == 0 and merge_questions(db) == []
    info = gaps.recompute(db, vehicle.id)
    assert info["open_gaps"] == 1 and info["open_gap_km"] == pytest.approx(25)


def test_manual_merge_over_gap_requires_acknowledgement_and_never_hides_the_gap(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    trips.skip(km=25)
    t2 = trips.trip(B, C, category=Category.WORK, km=20, gap_minutes=5)
    gaps.recompute(db, vehicle.id)
    with pytest.raises(TripError) as err:
        merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)
    assert err.value.code == "gap_requires_ack" and err.value.data["gap_km"] == 25

    merged = merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor, acknowledge_gap=True)
    assert merged.has_gap is True and merged.gap_km == pytest.approx(25)
    assert merged.distance_km == pytest.approx(30)  # luckans km räknas INTE in i resan
    info = gaps.recompute(db, vehicle.id)
    assert info["open_gaps"] == 1  # luckan är fortfarande öppen


def test_odometer_rounding_within_tolerance_is_not_a_gap(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, km=10)
    trips.skip(km=-1)  # heltalsavrundning: nästa start 1 km lägre än föregående slut
    trips.trip(B, C, category=Category.WORK, km=20, gap_minutes=5)
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 1
    assert gaps.recompute(db, vehicle.id)["gaps"] == 0


# ----------------------------------------------------------------------------- regel C / manuellt


def test_manual_merge_of_long_stop(db, user, vehicle, actor, trips):
    t1, t2 = two_trips(trips, 4 * 60)
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 0
    merged = merge.merge_trips(db, [t2, t1], trigger="manual", actor=actor)  # ordningen spelar ingen roll
    assert merged.merge_kind == MergeKind.MANUAL and merged.distance_km == pytest.approx(43.0)
    assert merged.started_at == t1.started_at


def test_manual_merge_requires_adjacent_trips(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    trips.trip(B, C, category=Category.WORK, km=10, gap_minutes=120)
    t3 = trips.trip(C, D, category=Category.WORK, km=10, gap_minutes=120)
    with pytest.raises(TripError) as err:
        merge.merge_trips(db, [t1, t3], trigger="manual", actor=actor)
    assert err.value.code == "not_adjacent"


def test_manual_merge_validations(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    with pytest.raises(TripError) as err:
        merge.merge_trips(db, [t1], trigger="manual", actor=actor)
    assert err.value.code == "too_few_trips"
    with pytest.raises(TripError) as err:
        merge.merge_trips(db, [t1, t1], trigger="manual", actor=actor)
    assert err.value.code == "duplicate_trip"


def test_merged_purposes_are_combined_without_touching_originals(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, purpose="Rest mellan kontor och hotell", km=4)
    t2 = trips.trip(B, C, category=Category.WORK, purpose="Kundbesök på Exempelbolaget", km=9, gap_minutes=13)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    assert merged.purpose == "Rest mellan kontor och hotell; Kundbesök på Exempelbolaget"
    assert t1.segments[0].raw_trip.original_notes == "Rest mellan kontor och hotell"
    assert t2.segments[0].raw_trip.original_notes == "Kundbesök på Exempelbolaget"


# ----------------------------------------------------------------------------- uppdelning


def test_split_and_undo_split(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, purpose="Kundrunda", km=5)
    trips.trip(B, C, category=Category.WORK, purpose="Kundrunda", km=6, gap_minutes=3)
    trips.trip(C, D, category=Category.WORK, purpose="Kundrunda", km=7, gap_minutes=4)
    run_rules(db, user, vehicle, actor)
    (merged,) = seg.active_trips(db, vehicle.id)
    left, right = merge.split_trip(db, reload_trip(db, merged.id), after_index=0, actor=actor)
    assert left.distance_km == pytest.approx(5) and right.distance_km == pytest.approx(13)
    assert right.segment_count == 2 and right.purpose == "Kundrunda"
    assert [t.id for t in seg.active_trips(db, vehicle.id)] == [left.id, right.id]
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 0  # delningen respekteras

    op = db.execute(select(M.TripMerge).where(M.TripMerge.operation == "split")).scalar_one()
    merge.undo_operation(db, op, actor=actor)
    (back,) = seg.active_trips(db, vehicle.id)
    assert back.id == merged.id and back.distance_km == pytest.approx(18)


def test_split_requires_multiple_raw_trips(db, actor, trips):
    t = trips.trip(A, B, category=Category.WORK, km=5)
    with pytest.raises(TripError) as err:
        merge.split_trip(db, t, after_index=0, actor=actor)
    assert err.value.code == "single_raw"


def test_allocate_part_of_trip_as_private(db, user, vehicle, actor, trips):
    t = trips.trip(A, B, category=Category.WORK, purpose="Kundbesök", km=42.0)
    raw_id = t.segments[0].raw_trip_id
    with pytest.raises(TripError) as err:
        seg.allocate_raw_trip(
            db, t, raw_id, [{"distance_km": 30, "category": "work"}, {"distance_km": 10, "category": "private"}],
            actor.label,
        )
    assert err.value.code == "allocation_sum_mismatch"  # kilometer får varken försvinna eller uppstå
    seg.allocate_raw_trip(
        db, t, raw_id, [{"distance_km": 30, "category": "work"}, {"distance_km": 12, "category": "private"}],
        actor.label,
    )
    t = reload_trip(db, t.id)
    assert t.work_km == pytest.approx(30) and t.private_km == pytest.approx(12)
    assert t.distance_km == pytest.approx(42) and t.has_mixed_categories and t.segment_count == 1
    seg.clear_allocation(db, t, raw_id, actor.label)
    assert reload_trip(db, t.id).work_km == pytest.approx(42)


# ----------------------------------------------------------------------------- dataluckor


def test_gap_resolved_as_private_creates_summary_without_invented_times(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, B, category=Category.WORK, km=10)
    trips.skip(km=140, hours=30)
    t2 = trips.trip(B, C, category=Category.WORK, km=20)
    gaps.recompute(db, vehicle.id)
    (gap,) = gaps.list_gaps(db, vehicle.id)
    assert gap.km == pytest.approx(140) and gap.status == GapStatus.OPEN

    trip = gaps.resolve_as_private(db, gap, actor=actor, note="Semesterresa")
    assert trip.category == Category.PRIVATE and trip.private_km == pytest.approx(140)
    assert trip.started_at is None and trip.ended_at is None  # inga påhittade tider
    assert trip.start_time_quality == "bounded" and trip.is_complete is False
    raw = trip.segments[0].raw_trip
    assert raw.start_earliest == t1.ended_at and raw.start_latest == t2.started_at

    info = gaps.recompute(db, vehicle.id)
    assert info["open_gaps"] == 0 and info["gaps"] == 1  # luckan finns kvar som hanterad
    # Sammanfattningen slås aldrig ihop med riktiga resor.
    assert run_rules(db, user, vehicle, actor)["auto_merged"] == 0
    total = sum(t.distance_km for t in seg.active_trips(db, vehicle.id))
    assert total == pytest.approx(170)  # 10 + 140 + 20 = hela mätarsträckan

    gaps.reopen(db, gap, actor=actor)
    assert gaps.recompute(db, vehicle.id)["open_gaps"] == 1
    assert sum(t.distance_km for t in seg.active_trips(db, vehicle.id)) == pytest.approx(30)


def test_ignored_gap_stays_recorded(db, vehicle, actor, trips):
    trips.trip(A, B, category=Category.WORK, km=10)
    trips.skip(km=50, hours=5)
    trips.trip(B, C, category=Category.WORK, km=20)
    gaps.recompute(db, vehicle.id)
    (gap,) = gaps.list_gaps(db, vehicle.id)
    gaps.ignore(db, gap, actor=actor, note="Verkstadens provkörning")
    assert gaps.recompute(db, vehicle.id) | {} == {"gaps": 1, "open_gaps": 0, "open_gap_km": 0, "odometer_overlaps": 0}
    with pytest.raises(TripError):
        gaps.ignore(db, gap, actor=actor)
