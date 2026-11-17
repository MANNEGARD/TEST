"""1.3.2: avvikelser i Granskning visar resan och vad ägaren gör; brus som inte rör underlaget tas bort.

* GET /review ger resan (``trip``) eller resorna (``first_trip``/``second_trip``) för varje
  avvikelse – även för ärenden från före 1.3.2 som saknar ``logical_trip_id``.
* Förbrukningsavvikelser skapas inte och öppna stängs.
* Inga avvikelser ur DriveLogs egna uppskattningar (uträknad tid, uppskattad sträcka).
* Inga resebundna avvikelser för privata resor när endast tjänsteresor redovisas.
* Körtiden larmar först vid en halvtimmes skillnad.
* Ett ärende vars resa är makulerad stängs av sig självt.
* Antalet databasfrågor i GET /review växer inte med antalet ärenden.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

import app.models as M
from app.models.enums import (
    Category,
    DistanceQuality,
    LogicalState,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TimeQuality,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.stats import anomalies
from app.services.trips import pipeline, review

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
A, B, C = "Påhittgatan 1, Exempelby", "Låtsasvägen 2, Exempelby", "Fantasitorget 3, Exempelby"


def scan(db, user, vehicle):
    return anomalies.scan_vehicle(db, vehicle, user_id=user.id, now=NOW)


def anomaly_items(db, vehicle, status=ReviewState.OPEN) -> list[M.ReviewItem]:
    return list(
        db.execute(
            select(M.ReviewItem).where(
                M.ReviewItem.vehicle_id == vehicle.id, M.ReviewItem.kind == ReviewKind.ANOMALY,
                M.ReviewItem.status == status,
            )
        ).scalars()
    )


def raw_of(trip: M.LogicalTrip) -> M.RawTrip:
    return trip.segments[0].raw_trip


def mismatch(trip: M.LogicalTrip, extra_km: float = 30) -> M.RawTrip:
    raw = raw_of(trip)
    raw.odometer_end = raw.odometer_start + raw.distance_km + extra_km
    return raw


def old_item(db, vehicle, key: str, data: dict, title: str = "Gammalt ärende") -> M.ReviewItem:
    """Ett ärende som 1.3.1 skapade: ingen logical_trip_id, resan bara i data."""
    item, _ = review.upsert(db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key=key, title=title,
                            detail="Gammal text", data=data)
    return item


def review_list(client) -> list[dict]:
    resp = client.get("/api/v1/review", params={"kind": "anomaly"})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# ----------------------------------------------------------------------------- resan följer med


def test_ny_avvikelse_far_resan_och_klartext(client, db, user, vehicle, trips):
    t = trips.trip(A, B, category=Category.WORK, purpose="Kundmöte", km=12.0)
    mismatch(t, extra_km=30)
    db.flush()
    scan(db, user, vehicle)
    (item,) = anomaly_items(db, vehicle)
    assert item.logical_trip_id == t.id  # sparas när avvikelsen skapas
    db.commit()

    (out,) = review_list(client)
    assert out["data"]["type"] == "odometer_mismatch"
    assert out["logical_trip_id"] == str(t.id)
    assert out["trip"]["id"] == str(t.id) and out["trip"]["distance_km"] == 12.0
    assert out["first_trip"] is None and out["second_trip"] is None
    assert out["kind_label"] == "Avvikelse"
    data = out["data"]
    assert data["distance_km"] == 12.0 and data["odometer_km"] == 42.0
    assert data["reported"] == "distance_km" and data["reported_km"] == 12.0
    assert data["difference_km"] == 30.0
    # Svensk decimalkomma, vilket värde underlaget använder och vad som händer om man inget gör.
    assert "12,0 km" in out["detail"] and "Underlaget redovisar körsträckan" in out["detail"]
    assert "Gör du inget redovisas 12,0 km" in out["detail"]


def test_befintliga_arenden_utan_resa_far_resan_vid_lasning(client, db, user, vehicle, trips):
    t1 = trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10.0)
    t2 = trips.trip(B, C, category=Category.WORK, purpose="Möte", km=11.0, gap_minutes=60)
    t3 = trips.trip(C, A, category=Category.WORK, purpose="Möte", km=11.0, gap_minutes=60)
    r1, r2, r3 = raw_of(t1), raw_of(t2), raw_of(t3)
    old_item(db, vehicle, f"anomaly:duration:{r1.id}", {"type": "time_conflict", "raw_trip_id": str(r1.id)})
    old_item(db, vehicle, f"overlap:{r1.id}:{r2.id}",
             {"type": "overlap", "first_raw_trip_id": str(r1.id), "second_raw_trip_id": str(r2.id)})
    ids = sorted((str(r2.id), str(r3.id)))
    old_item(db, vehicle, f"anomaly:duplicate:{ids[0]}:{ids[1]}", {"type": "possible_duplicate", "raw_trip_ids": ids})
    db.commit()

    items = {i["data"]["type"]: i for i in review_list(client)}
    one = items["time_conflict"]
    assert one["trip"]["id"] == str(t1.id) and one["logical_trip_id"] == str(t1.id)
    assert one["first_trip"] is None and one["second_trip"] is None
    two = items["overlap"]
    assert two["trip"] is None
    assert (two["first_trip"]["id"], two["second_trip"]["id"]) == (str(t1.id), str(t2.id))
    dup = items["possible_duplicate"]
    by_raw = {str(r2.id): str(t2.id), str(r3.id): str(t3.id)}
    assert (dup["first_trip"]["id"], dup["second_trip"]["id"]) == (by_raw[ids[0]], by_raw[ids[1]])
    assert dup["trip"] is None


def test_svarsschemat_beskriver_resorna(engine):
    from app.main import create_app

    spec = create_app().openapi()
    ok = spec["paths"]["/api/v1/review"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/ReviewListOut")
    props = spec["components"]["schemas"]["ReviewItemOut"]["properties"]
    assert {"trip", "first_trip", "second_trip", "kind_label", "logical_trip_id"} <= set(props)


# ----------------------------------------------------------------------------- förbrukning


def test_forbrukning_skapas_inte_och_oppna_stangs(db, user, vehicle, trips):
    raws = [raw_of(trips.trip(km=20.0, gap_minutes=60)) for _ in range(25)]
    for r in raws:
        r.energy_kwh = 20.0 * 0.06
    raws[3].energy_kwh = 20.0 * 0.496  # 49,6 kWh/100 km mot medianen 6,0
    old = old_item(db, vehicle, f"anomaly:consumption:energy_kwh:{raws[3].id}",
                   {"type": "abnormal_consumption", "metric": "energy_kwh", "raw_trip_id": str(raws[3].id),
                    "value": 49.6, "median": 6.0}, title="Onormal elförbrukning")
    db.flush()
    result = scan(db, user, vehicle)
    assert "abnormal_consumption" not in result["created"]
    assert old.status == ReviewState.RESOLVED and old.resolved_by == "system"
    assert old.resolution["auto"] is True and old.resolution["reason"] == anomalies.CONSUMPTION_REASON
    assert result["closed"]["abnormal_consumption"] == 1
    assert anomaly_items(db, vehicle) == []


def test_forbrukningsarende_stangs_redan_vid_omrakningen(db, user, vehicle, trips):
    t = trips.trip(km=20.0)
    old = old_item(db, vehicle, f"anomaly:consumption:fuel_liters:{raw_of(t).id}",
                   {"type": "abnormal_consumption", "raw_trip_id": str(raw_of(t).id)})
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False)
    assert old.status == ReviewState.RESOLVED and old.resolution["reason"] == anomalies.CONSUMPTION_REASON


def test_forsta_ticken_efter_start_rensar_kon(db, user, vehicle, trips, monkeypatch):
    from app.worker import tasks

    t = trips.trip(km=20.0)
    old = old_item(db, vehicle, f"anomaly:consumption:fuel_liters:{raw_of(t).id}",
                   {"type": "abnormal_consumption", "raw_trip_id": str(raw_of(t).id)})
    old_id = old.id
    db.commit()
    monkeypatch.setattr(tasks, "_anomalies_scanned_since_start", False)
    first = tasks.scan_anomalies_after_start()
    assert first["vehicles"] == 1 and first["closed_total"] == 1
    assert tasks.scan_anomalies_after_start() == {"skipped": True}  # bara en gång per process
    db.expire_all()
    assert db.get(M.ReviewItem, old_id).status == ReviewState.RESOLVED


# ----------------------------------------------------------------------------- uppskattade värden


def test_inga_avvikelser_ur_uppskattade_varden(db, user, vehicle, trips):
    # Körtid mot uträknad starttid.
    t1 = trips.trip(km=5.0, minutes=20, gap_minutes=600)
    r1 = raw_of(t1)
    r1.start_time_quality = TimeQuality.ESTIMATED
    r1.duration_seconds = 3 * 3600
    # Uppskattad sträcka mot mätarställningen.
    t2 = trips.trip(km=10.0, gap_minutes=600)
    r2 = mismatch(t2, extra_km=25)
    r2.distance_quality = DistanceQuality.ESTIMATED
    # Två resor med samma uträknade starttid och sträcka.
    t3 = trips.trip(km=10.0, gap_minutes=600, minutes=30)
    r3 = raw_of(t3)
    t4 = trips.trip(km=10.2, start_at=r3.started_at + timedelta(seconds=40), odometer_start=r3.odometer_end + 1)
    for r in (r3, raw_of(t4)):
        r.start_time_quality = TimeQuality.ESTIMATED
    # Ett ärende som 1.3.1 skapade ur den uträknade tiden stängs.
    old = old_item(db, vehicle, f"anomaly:duration:{r1.id}", {"type": "time_conflict", "raw_trip_id": str(r1.id)})
    db.flush()
    result = scan(db, user, vehicle)
    assert not {"time_conflict", "odometer_mismatch", "possible_duplicate", "time_overlap"} & set(result["found"])
    assert old.status == ReviewState.RESOLVED and old.resolution["reason"] == review.NO_LONGER_REASON


# ----------------------------------------------------------------------------- privata resor


def test_inga_avvikelser_for_privata_resor_nar_bara_tjanst_redovisas(db, user, vehicle, trips):
    private = trips.trip(A, B, category=Category.PRIVATE, km=10.0)
    mismatch(private)
    work = trips.trip(B, C, category=Category.WORK, purpose="Möte", km=10.0, gap_minutes=600)
    mismatch(work)
    db.flush()
    scan(db, user, vehicle)
    (item,) = [i for i in anomaly_items(db, vehicle) if i.data["type"] == "odometer_mismatch"]
    assert item.logical_trip_id == work.id


def test_privat_resa_ger_avvikelse_i_fullstandig_journal(db, user, vehicle, trips, complete_scope):
    private = trips.trip(A, B, category=Category.PRIVATE, km=10.0)
    mismatch(private)
    db.flush()
    scan(db, user, vehicle)
    (item,) = anomaly_items(db, vehicle)
    assert item.logical_trip_id == private.id


def test_arendet_stangs_nar_resan_blir_privat_och_oppnas_igen_som_tjanst(db, user, vehicle, trips):
    t = trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10.0)
    mismatch(t)
    db.flush()
    scan(db, user, vehicle)
    (item,) = anomaly_items(db, vehicle)
    # Ägaren avgör resan som privat: omräkningen stänger ärendet direkt.
    t.category, t.review_status = Category.PRIVATE, ReviewStatus.APPROVED
    db.flush()
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False)
    assert item.status == ReviewState.RESOLVED and item.resolution["reason"] == review.SCOPE_REASON
    scan(db, user, vehicle)
    assert anomaly_items(db, vehicle) == []
    # Blir resan tjänst igen blir avvikelsen aktuell igen.
    t.category = Category.WORK
    db.flush()
    scan(db, user, vehicle)
    assert item.status == ReviewState.OPEN


def test_ett_forslag_om_privat_ar_inte_avgjort(db, user, vehicle, trips):
    t = trips.trip(A, B, category=Category.PRIVATE, review_status=ReviewStatus.PROPOSED, km=10.0)
    mismatch(t)
    db.flush()
    scan(db, user, vehicle)
    assert len(anomaly_items(db, vehicle)) == 1


def test_tva_resor_racker_att_en_ar_tjanst(db, user, vehicle, trips):
    p1 = trips.trip(A, B, category=Category.PRIVATE, km=10.0, gap_minutes=600, minutes=30)
    r1 = raw_of(p1)
    trips.trip(A, B, category=Category.PRIVATE, km=10.2, start_at=r1.started_at + timedelta(seconds=40),
               odometer_start=r1.odometer_end + 1)
    w = trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10.0, gap_minutes=600, minutes=30)
    rw = raw_of(w)
    trips.trip(A, B, category=Category.PRIVATE, km=10.2, start_at=rw.started_at + timedelta(seconds=40),
               odometer_start=rw.odometer_end + 1)
    db.flush()
    result = scan(db, user, vehicle)
    assert result["found"]["possible_duplicate"] == 1  # bara paret med en tjänsteresa
    dup = next(i for i in anomaly_items(db, vehicle) if i.data["type"] == "possible_duplicate")
    assert str(rw.id) in dup.data["raw_trip_ids"]


# ----------------------------------------------------------------------------- körtiden


@pytest.mark.parametrize(("extra_minutes", "expected"), [(5, False), (29, False), (30, True), (90, True)])
def test_kortidsgransen(db, user, vehicle, trips, extra_minutes, expected):
    t = trips.trip(km=15.0, minutes=20)
    raw_of(t).duration_seconds = (20 + extra_minutes) * 60
    db.flush()
    result = scan(db, user, vehicle)
    assert ("time_conflict" in result["found"]) is expected
    if expected:
        (item,) = anomaly_items(db, vehicle)
        assert item.data["driving_minutes"] == 20 + extra_minutes and item.data["elapsed_minutes"] == 20
        assert item.data["difference_minutes"] == extra_minutes
        assert item.logical_trip_id == t.id


def test_gammalt_kortidsarende_under_gransen_stangs(db, user, vehicle, trips):
    t = trips.trip(km=15.0, minutes=20)
    r = raw_of(t)
    r.duration_seconds = 30 * 60  # tio minuter mer än tiden mellan start och slut
    old = old_item(db, vehicle, f"anomaly:duration:{r.id}", {"type": "time_conflict", "raw_trip_id": str(r.id)})
    db.flush()
    scan(db, user, vehicle)
    assert old.status == ReviewState.RESOLVED and old.resolution["reason"] == review.NO_LONGER_REASON


# ----------------------------------------------------------------------------- resan finns inte längre


def test_arende_for_makulerad_resa_stangs(db, user, vehicle, trips):
    t = trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10.0)
    r = mismatch(t)
    db.flush()
    scan(db, user, vehicle)
    (item,) = anomaly_items(db, vehicle)
    # Ett äldre ärende för samma råresa utan logical_trip_id (stängs inte av close_orphaned).
    old = old_item(db, vehicle, f"anomaly:time_negative:{r.id}", {"type": "time_conflict", "raw_trip_id": str(r.id)})
    r.voided_at, r.void_reason = NOW, "Ångrad import"
    t.state = LogicalState.VOIDED
    db.flush()
    result = anomalies.close_noise(db, vehicle.id, scope=settings_svc.SCOPE_BUSINESS_ONLY)
    assert result == {"trip_gone": 2}
    for it in (item, old):
        assert it.status == ReviewState.RESOLVED and it.resolution["reason"] == review.INACTIVE_REASON


def test_fordonsavvikelser_paverkas_inte(db, user, vehicle, trips, complete_scope):
    trips.trip(km=10.0)
    for at, km in ((NOW - timedelta(days=3), 10010), (NOW - timedelta(days=3, hours=-2), 11500)):
        db.add(M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=at, last_seen_at=at, odometer_km=km,
                                    content_hash=f"h{km}"))
    db.flush()
    result = scan(db, user, vehicle)
    assert result["created"].get("odometer_jump") == 1
    jump = next(i for i in anomaly_items(db, vehicle) if i.data["type"] == "odometer_jump")
    assert jump.logical_trip_id is None and anomalies.trip_refs(jump) == []


# ----------------------------------------------------------------------------- antal databasfrågor


def _count_queries(engine, fn) -> int:
    statements: list[str] = []

    def before(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return len(statements)


def test_antalet_fragor_vaxer_inte_med_antalet_arenden(client, engine, db, user, vehicle, trips):
    def add(n: int) -> None:
        for _ in range(n):
            t = trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10.0, gap_minutes=600)
            r = raw_of(t)
            old_item(db, vehicle, f"anomaly:odo_mismatch:{r.id}", {"type": "odometer_mismatch", "raw_trip_id": str(r.id)})
            t2 = trips.trip(A, B, category=Category.WORK, purpose="Möte", km=10.0, gap_minutes=600)
            r2 = raw_of(t2)
            old_item(db, vehicle, f"overlap:{r.id}:{r2.id}",
                     {"type": "overlap", "first_raw_trip_id": str(r.id), "second_raw_trip_id": str(r2.id)})
        db.commit()

    add(2)
    few = _count_queries(engine, lambda: review_list(client))
    add(8)
    many = _count_queries(engine, lambda: review_list(client))
    assert len(review_list(client)) == 20
    assert many == few
    assert all(i["trip"] or (i["first_trip"] and i["second_trip"]) for i in review_list(client))
