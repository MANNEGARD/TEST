"""S1c: importvägen, manuellt inlagda resor och resor från före ``active_since`` beter sig
exakt som förut när självgående läge slås på."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

import app.models as M
from app.models.enums import Category, LocationType, ReviewStatus, TripSource
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.export import simple
from app.services.importer import service as imp
from app.services.trips import pipeline
from app.services.trips import segments as seg
from tests.fixtures import volvo_exports as fx

HOME = "Hemvägen 1, 111 11 Exempelstad"
OFFICE = "Kontorsgatan 2, 111 22 Exempelstad"
ACME = "Industrivägen 10, 222 33 Verkstadsby"
MON = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Stockholm")


def _self_driving(db, user, since: str = "2020-01-01T00:00:00+00:00") -> None:
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": since}, allow_read_only=True)
    db.flush()


def _state(db, vehicle) -> dict:
    trips = seg.active_trips(db, vehicle.id)
    items = db.execute(select(M.ReviewItem.dedupe_key, M.ReviewItem.status)).all()
    return {
        "trips": sorted(
            (str(t.sort_time), t.category, t.review_status, t.purpose, t.purpose_confirmed, t.approval_source,
             round(t.distance_km, 3), t.segment_count)
            for t in trips
        ),
        "items": sorted((k, s) for k, s in items),
    }


def _run(db, user, vehicle, origin: str = "live") -> None:
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False,
                                 origin=origin, use_llm=False)
    db.flush()


def test_imported_and_manual_trips_are_unchanged_by_self_driving(db, user, vehicle, trips, actor):
    trips.location(HOME, name="Hemmet", type=LocationType.HOME)
    trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE)
    trips.location(ACME, name="Acme AB", type=LocationType.CUSTOMER, company="Acme AB")
    for i in range(3):  # ägarens historik på rutten (skulle ge H för en live-resa)
        trips.trip(HOME, ACME, category=Category.WORK, purpose="Service av maskin", km=20.0,
                   source=TripSource.VOLVO_IMPORT, review_status=ReviewStatus.IMPORTED,
                   start_at=MON + timedelta(days=i))
    trips.trip(HOME, ACME, km=20.0, source=TripSource.VOLVO_IMPORT, start_at=MON + timedelta(days=3))
    trips.trip(HOME, OFFICE, km=8.0, source=TripSource.VOLVO_IMPORT, start_at=MON + timedelta(days=4))
    trips.trip(OFFICE, ACME, km=15.0, source=TripSource.VOLVO_IMPORT, gap_minutes=5)  # kort stopp
    trips.trip(HOME, ACME, km=20.0, source=TripSource.MANUAL, start_at=MON + timedelta(days=5))
    trips.trip(ACME, HOME, km=20.0, source=TripSource.MANUAL, gap_minutes=60)

    _run(db, user, vehicle, origin="import")
    _run(db, user, vehicle)
    before = _state(db, vehicle)
    rows_before = [r.as_dict() for r in simple.build_journal(
        vehicle=vehicle, trips=seg.active_trips(db, vehicle.id), start=None, end=None, tz=TZ).rows]

    _self_driving(db, user)
    _run(db, user, vehicle, origin="import")
    _run(db, user, vehicle)
    assert _state(db, vehicle) == before
    rows_after = [r.as_dict() for r in simple.build_journal(
        vehicle=vehicle, trips=seg.active_trips(db, vehicle.id), start=None, end=None, tz=TZ).rows]
    assert rows_after == rows_before
    assert db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.auto_approved")).first() is None
    assert all(t.approval_source is None for t in seg.active_trips(db, vehicle.id))
    # Kort stopp mellan importerade resor: frågan om sammanslagning finns kvar som förut.
    assert any(k.startswith("merge:") for k, _ in before["items"])


def test_real_import_in_self_driving_mode_matches_manual(db, user, vehicle, actor):
    files = fx.sample_files()
    _self_driving(db, user)
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    result = imp.commit(db, batch, actor=actor, apply_merge_rules=False)
    assert result["imported"] > 0
    trips = seg.active_trips(db, vehicle.id)
    assert trips and all(t.review_status == ReviewStatus.IMPORTED for t in trips)
    assert all(t.category == Category.WORK and t.approval_source is None for t in trips)
    assert all(not autopilot.eligible(t, active_since=settings_svc.active_since(db, user.id)) for t in trips)


def test_live_trips_from_before_active_since_are_never_touched(db, user, vehicle, trips):
    trips.location(HOME, name="Hemmet", type=LocationType.HOME)
    trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE)
    t = trips.trip(HOME, OFFICE, source=TripSource.VOLVO_API)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    _self_driving(db, user, since=future)
    _run(db, user, vehicle)
    t = seg.load_trip(db, t.id)
    assert t.approval_source is None and t.review_status != ReviewStatus.APPROVED
