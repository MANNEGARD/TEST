"""1.3.0 DEL 5: ta bort importerade resor (en och flera) – och få tillbaka dem vid återimport.

Borttagning är makulering med spår i historiken. Resans egna ärenden, frågor,
kalenderträffar och inlärning stängs eller ångras i anropet; luckor, mönster och förslag för
övriga resor räknas om i bakgrunden efter svaret. "Om jag importerar igen så vill jag att en
raderad resa kommer tillbaka" – samma fil (eller en annan fil med samma resa) ger tillbaka de
borttagna resorna, som de står i filen, medan resor som finns kvar är dubbletter som förut.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.db import utcnow
from app.models.enums import (
    Category,
    GapStatus,
    ImportRowStatus,
    LogicalState,
    NotificationStatus,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TripSource,
)
from app.services import settings as settings_svc
from app.services.export import month_close
from app.services.importer import service as imp
from app.services.learning import feedback, patterns, recompute
from app.services.trips import merge, pipeline, removal
from app.services.trips import segments as seg
from app.services.trips.segments import TripError
from app.services.volvo import detection
from tests.fixtures import volvo_exports as fx

API = "/api/v1"
HOME = "Hemvägen 1, 111 11 Exempelstad"
ACME = "Industrivägen 10, 222 33 Verkstadsby"
BETA = "Lagergatan 4, 333 44 Norrby"
GAMMA = "Gammagatan 3, 444 55 Söderby"


def rows3() -> list[fx.Row]:
    """Tre importerade resor den 15 september 2026 (svensk tid), sammanhängande mätarställning."""
    d = datetime(2026, 9, 15)
    return [
        fx.Row(d.replace(hour=8), d.replace(hour=8, minute=40), 20000, 20034, HOME, ACME, 2400, 34.0, 2.0, 0, 0,
               "Kundbesök Acme AB", "Arbete"),
        fx.Row(d.replace(hour=10), d.replace(hour=10, minute=30), 20034, 20059, ACME, BETA, 1800, 25.0, 2.0, 0, 0,
               "Kundbesök Beta AB", "Arbete"),
        fx.Row(d.replace(hour=15), d.replace(hour=15, minute=40), 20059, 20100, BETA, HOME, 2400, 41.0, 3.0, 0, 0,
               "Hemresa", "Arbete"),
    ]


def do_import(db, user, vehicle, actor, rows=None, name: str = "september.csv", fmt: str = "csv"):
    data = fx.build_csv(rows or rows3()) if fmt == "csv" else fx.build_v2(rows or rows3())
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[(name, data)], actor=actor)
    return batch, imp.commit(db, batch, actor=actor)


def preview(db, user, vehicle, actor, rows=None, name: str = "september.csv", fmt: str = "csv"):
    data = fx.build_csv(rows or rows3()) if fmt == "csv" else fx.build_v2(rows or rows3())
    return imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[(name, data)], actor=actor)


def active(db, vehicle) -> list[M.LogicalTrip]:
    return [t for t in seg.active_trips(db, vehicle.id) if t.odometer_start is not None]


def by_odo(db, vehicle, odo: float) -> M.LogicalTrip:
    return next(t for t in active(db, vehicle) if t.odometer_start == odo)


# ----------------------------------------------------------------------------- vad som kan tas bort


def test_kinds_and_refusals(db, user, vehicle, trips, actor):
    do_import(db, user, vehicle, actor)
    first = by_odo(db, vehicle, 20000)
    assert removal.deletable_kind(first) == "imported"

    live = trips.trip(GAMMA, HOME, source=TripSource.VOLVO_API, start_at=datetime(2026, 9, 20, 8, tzinfo=UTC))
    assert removal.deletable_kind(live) is None
    with pytest.raises(TripError) as err:
        removal.delete_trip(db, live, actor=actor)
    assert err.value.code == "not_deletable"
    assert err.value.message == ("Resor från bilen kan inte tas bort. Bara importerade och manuellt inlagda resor "
                                 "kan tas bort.")

    second, third = by_odo(db, vehicle, 20034), by_odo(db, vehicle, 20059)
    merged = merge.merge_trips(db, [second, third], trigger="manual", actor=actor)
    with pytest.raises(TripError) as err:
        removal.delete_trip(db, seg.load_trip(db, merged.id), actor=actor)
    assert err.value.code == "merged_trip" and "Dela upp den först" in err.value.message

    journey = M.BusinessJourney(vehicle_id=vehicle.id, name="Resa till Verkstadsby", status="confirmed",
                                started_at=first.started_at, ended_at=first.ended_at)
    db.add(journey)
    db.flush()
    first.journey_id = journey.id
    db.flush()
    with pytest.raises(TripError) as err:
        removal.delete_trip(db, first, actor=actor)
    assert err.value.code == "in_journey"
    assert "Resan ingår i tjänsteresan ”Resa till Verkstadsby”. Lös upp tjänsteresan först" in err.value.message

    first.journey_id = None
    removal.delete_trip(db, first, actor=actor)
    with pytest.raises(TripError) as err:
        removal.delete_trip(db, first, actor=actor)
    assert err.value.code == "not_active" and err.value.message == "Resan finns inte längre i körjournalen."


def test_manual_trip_can_be_deleted_and_never_comes_back(db, user, vehicle, trips, actor):
    manual = trips.trip(HOME, ACME, source=TripSource.MANUAL, category=Category.WORK, purpose="Möte",
                        start_at=datetime(2026, 9, 21, 8, tzinfo=UTC))
    assert removal.deletable_kind(manual) == "manual"
    removal.delete_trip(db, manual, actor=actor)
    raw = seg.ordered_raws(manual)[0]
    assert raw.voided_at is not None and raw.void_reason == removal.VOID_REASON_DELETED
    assert imp._deleted_keys(db, vehicle.id) == {}  # bara importerade resor kommer tillbaka
    event = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.manual_delete")).scalar_one()
    assert event.before["purpose"] == "Möte"


# ----------------------------------------------------------------------------- vad borttagningen gör


def test_delete_steps(db, user, vehicle, actor):
    from app.services.ha import notifier

    do_import(db, user, vehicle, actor)
    second = by_odo(db, vehicle, 20034)
    raw = seg.ordered_raws(second)[0]
    third = by_odo(db, vehicle, 20059)
    # Ägaren ändrade resan (inlärning), resan har en öppen fråga, en kalenderträff och en
    # sammanslagningsfråga med grannen.
    feedback.apply_decision(db, second, user_id=user.id, actor=actor, category=Category.PRIVATE, approve=True)
    rule = patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=second.end_location_id,
                                    category=Category.PRIVATE, purpose=None)
    category = M.ReviewItem(vehicle_id=vehicle.id, kind=ReviewKind.CATEGORY, dedupe_key=f"category:{second.id}",
                            logical_trip_id=second.id, title="Fråga", status=ReviewState.OPEN)
    merge_q = M.ReviewItem(vehicle_id=vehicle.id, kind=ReviewKind.MERGE, dedupe_key=f"merge:{raw.id}:x",
                           logical_trip_id=third.id, title="Slå samman?", status=ReviewState.OPEN,
                           data={"first_raw_trip_id": str(raw.id), "second_raw_trip_id": str(uuid.uuid4())})
    event = M.CalendarEvent(user_id=user.id, calendar_id="ics", external_id="e1", subject="Möte",
                            start_at=second.started_at, end_at=second.ended_at)
    db.add_all([category, merge_q, event])
    db.flush()
    db.add(M.CalendarMatch(logical_trip_id=second.id, calendar_event_id=event.id, relation="arrival", score=0.8,
                           time_score=0.8, geo_score=0.8, geo_basis="coordinates", is_selected=True))
    req = M.NotificationRequest(user_id=user.id, kind="classify", status=NotificationStatus.PENDING,
                                logical_trip_id=second.id, dedupe_key="classify:x", title="Ny resa", message="?",
                                public_id="abcdef123", send_after=utcnow(), expires_at=utcnow() + timedelta(days=1))
    req_merge = M.NotificationRequest(user_id=user.id, kind="merge", status=NotificationStatus.PENDING,
                                      review_item_id=merge_q.id, dedupe_key="merge:x", title="Slå samman?",
                                      message="?", public_id="abcdef124", send_after=utcnow(),
                                      expires_at=utcnow() + timedelta(days=1))
    db.add_all([req, req_merge])
    db.flush()
    assert notifier is not None

    removal.delete_trip(db, second, actor=actor)
    db.expire_all()
    assert raw.voided_at is not None and raw.void_reason == "Borttagen av användaren"
    assert db.get(M.LogicalTrip, second.id).state == LogicalState.VOIDED
    assert db.get(M.ReviewItem, category.id).status == ReviewState.RESOLVED
    assert db.get(M.ReviewItem, category.id).resolution == {"deleted": True}
    assert db.get(M.ReviewItem, merge_q.id).status == ReviewState.RESOLVED
    assert db.get(M.NotificationRequest, req.id).status == NotificationStatus.CANCELLED
    assert db.get(M.NotificationRequest, req_merge.id).status == NotificationStatus.CANCELLED
    assert db.execute(select(M.CalendarMatch).where(M.CalendarMatch.logical_trip_id == second.id)).first() is None
    assert db.get(M.CalendarEvent, event.id) is not None  # bokningen finns kvar
    fbs = feedback.history(db, second.id)
    assert fbs and all(f.undone_at is not None for f in fbs)
    assert db.get(M.LocationPattern, rule.id) is not None  # egen regel finns kvar
    audit_event = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.import_delete")).scalar_one()
    assert audit_event.before["distance_km"] == 25.0 and audit_event.before["file"] == "september.csv"
    assert audit_event.before["row_number"] is not None and audit_event.before["import_batch_id"]
    # Resan räknas inte längre i mönstren.
    patterns.rebuild(db, user.id)
    for pat in db.execute(select(M.LocationPattern).where(
            M.LocationPattern.destination_location_id == second.end_location_id)).scalars():
        assert pat.confirmed_private == 0 and pat.imported_work == 0


@pytest.mark.parametrize("scope", ["business_only", "complete"])
def test_route_requests_recompute_and_the_gap_appears_afterwards(client, db, user, vehicle, actor, job_queue,
                                                                 monkeypatch, scope):
    settings_svc.update_section(db, user.id, "general", {"journal_scope": scope})
    do_import(db, user, vehicle, actor)
    second = by_odo(db, vehicle, 20034)
    db.commit()
    before = db.get(M.Vehicle, vehicle.id).recompute_requested
    monkeypatch.setattr(pipeline, "after_trips_changed", lambda *a, **k: pytest.fail("ingen omräkning i anropet"))

    r = client.delete(f"{API}/trips/{second.id}")
    assert r.status_code == 204, r.text
    db.expire_all()
    assert db.get(M.Vehicle, vehicle.id).recompute_requested == before + 1
    assert [n for n, _ in job_queue if n == recompute.JOB] == [recompute.JOB]
    assert client.get(f"{API}/review").json()["recompute_pending"] is True

    monkeypatch.undo()
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    db.expire_all()
    gaps = db.execute(select(M.DataGap).where(
        M.DataGap.vehicle_id == vehicle.id, M.DataGap.odometer_start == 20034, M.DataGap.odometer_end == 20059
    )).scalars().all()
    # business_only: luckan är privat körning utan ärende; complete: en lucka att hantera (Luckor).
    assert len(gaps) == 1 and gaps[0].km == 25 and gaps[0].status == GapStatus.OPEN


def test_route_errors_and_bulk(client, db, user, vehicle, actor, trips, job_queue):
    do_import(db, user, vehicle, actor)
    live = trips.trip(GAMMA, HOME, source=TripSource.VOLVO_API, start_at=datetime(2026, 9, 20, 8, tzinfo=UTC))
    first, second = by_odo(db, vehicle, 20000), by_odo(db, vehicle, 20034)
    db.commit()
    r = client.delete(f"{API}/trips/{live.id}")
    assert r.status_code == 400 and r.json()["code"] == "not_deletable"
    assert client.get(f"{API}/trips/{first.id}").json()["trip"]["deletable"] == "imported"

    r = client.post(f"{API}/trips/bulk-delete", json={"trip_ids": [str(first.id), str(second.id), str(live.id)]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == 2
    assert body["errors"] == [{"trip_id": str(live.id), "code": "not_deletable",
                               "message": removal.TEXT_NOT_DELETABLE}]
    assert [n for n, _ in job_queue if n == recompute.JOB] == [recompute.JOB]  # en gång per fordon

    r = client.post(f"{API}/trips/bulk-delete", json={"trip_ids": [str(uuid.uuid4())]})
    assert r.status_code == 404


def test_other_users_trip_is_404(client, db, user, vehicle, actor):
    other = M.User(email="annan@example.com", display_name="Annan", password_hash="x")
    db.add(other)
    db.flush()
    other_vehicle = M.Vehicle(owner_user_id=other.id, name="Annan bil", make="Volvo", model="XC60")
    db.add(other_vehicle)
    db.flush()
    raw = M.RawTrip(vehicle_id=other_vehicle.id, source=TripSource.MANUAL, source_key="manual:x",
                    started_at=datetime(2026, 9, 1, 8, tzinfo=UTC), ended_at=datetime(2026, 9, 1, 9, tzinfo=UTC),
                    distance_km=10, fuel_quality="unknown", energy_quality="unknown")
    seg.set_raw_sort_time(raw)
    db.add(raw)
    db.flush()
    trip = seg.create_logical_for_raw(db, raw, category=Category.UNDECIDED, category_source="system",
                                      review_status=ReviewStatus.NEEDS_REVIEW)
    db.commit()
    assert client.delete(f"{API}/trips/{trip.id}").status_code == 404
    assert client.post(f"{API}/trips/bulk-delete", json={"trip_ids": [str(trip.id)]}).status_code == 404


# ----------------------------------------------------------------------------- återimport


@pytest.mark.parametrize("fmt", ["csv", "xlsx"], ids=["samma-fil", "annan-fil"])
def test_reimport_brings_back_the_deleted_trip_as_in_the_file(db, user, vehicle, actor, fmt):
    do_import(db, user, vehicle, actor)
    second = by_odo(db, vehicle, 20034)
    # Ägaren ändrade resan innan den togs bort: ändringen kommer inte tillbaka.
    feedback.apply_decision(db, second, user_id=user.id, actor=actor, category=Category.PRIVATE, approve=True)
    removal.delete_trip(db, second, actor=actor)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False, use_llm=False)

    batch = preview(db, user, vehicle, actor, name="september.csv" if fmt == "csv" else "export.xlsx", fmt=fmt)
    s = batch.summary
    assert (s["rows_new"], s["rows_restored"], s["rows_duplicate"] + s["rows_duplicate_differs"]) == (1, 1, 2)
    if fmt == "csv":
        assert s["files"][0]["status"] == "duplicate_file"  # filen är redan importerad – raden kommer ändå tillbaka
    row = db.execute(select(M.ImportRow).join(M.ImportedFile).where(
        M.ImportedFile.batch_id == batch.id, M.ImportRow.status == ImportRowStatus.OK)).scalar_one()
    assert "Du tog bort resan 15 september 2026. Den kommer tillbaka när du bekräftar importen." in row.messages

    result = imp.commit(db, batch, actor=actor)
    assert result["imported"] == 1 and result["restored"] == 1
    back = by_odo(db, vehicle, 20034)
    assert back.id != second.id
    assert (back.category, back.review_status, back.purpose) == (Category.WORK, ReviewStatus.IMPORTED,
                                                                 "Kundbesök Beta AB")
    assert all(f.undone_at is not None for f in feedback.history(db, second.id))  # gamla rättelser förblir ångrade
    assert len(active(db, vehicle)) == 3
    commit_event = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "import.commit")
                              .order_by(M.AuditEvent.at.desc())).scalars().first()
    assert commit_event.after["restored"] == 1


def test_preview_made_before_the_deletion_restores_nothing(db, user, vehicle, actor):
    do_import(db, user, vehicle, actor)
    early = preview(db, user, vehicle, actor)
    assert early.summary["rows_new"] == 0
    removal.delete_trip(db, by_odo(db, vehicle, 20000), actor=actor)
    assert imp.commit(db, early, actor=actor)["imported"] == 0
    assert len(active(db, vehicle)) == 2


def test_still_existing_trips_are_duplicates_and_undo_of_old_import_works(db, user, vehicle, actor):
    first_batch, _ = do_import(db, user, vehicle, actor)
    removal.delete_trip(db, by_odo(db, vehicle, 20059), actor=actor)
    result = imp.undo(db, first_batch, actor=actor)
    assert result["voided"] == 2  # den borttagna var redan makulerad
    assert active(db, vehicle) == []


def test_deleting_an_import_that_replaced_a_reconstructed_trip(db, user, vehicle, actor, trips):
    """Resan från bilen förblir ersatt; återimport ger tillbaka den importerade resan."""
    rec = trips.raw(HOME, ACME, km=34, odometer_start=20000, source=TripSource.VOLVO_API,
                    start_at=datetime(2026, 9, 15, 6, 0, tzinfo=UTC), observation_ids=["obs-1"])
    seg.create_logical_for_raw(db, rec, category=Category.UNDECIDED, category_source="system",
                               review_status=ReviewStatus.NEEDS_REVIEW)
    do_import(db, user, vehicle, actor)
    db.expire_all()
    assert rec.voided_at is not None and rec.void_reason == detection.VOID_REASON_SUPERSEDED
    removal.delete_trip(db, by_odo(db, vehicle, 20000), actor=actor)
    db.expire_all()
    assert rec.voided_at is not None and rec.void_reason == detection.VOID_REASON_SUPERSEDED
    _, result = do_import(db, user, vehicle, actor)
    assert result["restored"] == 1
    assert by_odo(db, vehicle, 20000).source != TripSource.VOLVO_API


def test_closed_month_becomes_stale_and_gets_a_new_file(db, user, vehicle, actor):
    do_import(db, user, vehicle, actor)
    now = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    status = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 9, 1), now=now)
    assert status.state == "exported", [b.code for b in status.blockers]
    first_export = status.export_id
    removal.delete_trip(db, by_odo(db, vehicle, 20034), actor=actor)
    status = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 9, 1),
                                 now=now + timedelta(hours=2))
    assert status.state == "stale"
    status = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 9, 1),
                                 now=now + timedelta(hours=4))
    assert status.state == "exported" and status.export_id != first_export
    assert db.get(M.ExportJob, first_export).superseded_by_id == status.export_id
    assert db.get(M.ExportJob, status.export_id).summary["trips"] == 2
