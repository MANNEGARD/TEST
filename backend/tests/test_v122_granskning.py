"""1.2.2: ett beslut i Granskning svarar direkt – omräkningen görs efter svaret.

* Ett beslut, massredigering, ett svar på en fråga och "Ångra" gör bara själva beslutet och
  resans egna ärenden i anropet, och beställer omräkningen (``learning.recompute``).
* När omräkningen är klar är resultatet detsamma som med den gamla vägen
  (``feedback.after_decisions`` direkt i anropet): samma förslag, samma automatiska
  godkännanden i självgående läge och samma ärenden.
* Beställningen ligger kvar i databasen: avbryts omräkningen (omstart) görs den vid nästa tick.
* Antalet databasfrågor för ett beslut beror inte på hur många resor fordonet har.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import event, select

import app.models as M
from app import jobs
from app.db import utcnow
from app.models.enums import Category, LogicalState, ReviewKind, ReviewState
from app.services import settings as settings_svc
from app.services.ha import notifier
from app.services.learning import feedback, recompute
from app.services.trips import segments as seg
from tests import v122_stordata as data
from tests.conftest import reload_trip
from tests.test_ha_support import setup_pull

API = "/api/v1"


# ----------------------------------------------------------------------------- hjälp


def snapshot(db, vehicle_id: uuid.UUID) -> dict[str, Any]:
    """Allt som omräkningen påverkar: resornas förslag och beslut, aktuella bedömningar,
    mönster och ärenden (utan tidsstämplar och vem som stängde ett ärende)."""
    db.expire_all()
    trips = {
        str(t.id): (t.state, t.category, t.subcategory, t.category_source, t.review_status, t.confidence, t.purpose,
                    t.purpose_source, t.purpose_confirmed, t.tax_note, t.approval_source, t.approval_basis)
        for t in db.execute(select(M.LogicalTrip).where(M.LogicalTrip.vehicle_id == vehicle_id)).scalars()
    }
    # Bedömningens resultat med underlag (indatahashen innehåller nya mönsters slumpade id:n).
    classifications = sorted(
        (str(c.logical_trip_id), c.source, c.category, c.confidence, repr((c.signals or {}).get("result")))
        for c in db.execute(
            select(M.TripClassification)
            .join(M.LogicalTrip, M.LogicalTrip.id == M.TripClassification.logical_trip_id)
            .where(M.LogicalTrip.vehicle_id == vehicle_id, M.TripClassification.is_current.is_(True))
        ).scalars()
    )
    purposes = sorted(
        (str(p.logical_trip_id), p.text, p.source)
        for p in db.execute(
            select(M.TripPurpose)
            .join(M.LogicalTrip, M.LogicalTrip.id == M.TripPurpose.logical_trip_id)
            .where(M.LogicalTrip.vehicle_id == vehicle_id, M.TripPurpose.is_current.is_(True))
        ).scalars()
    )
    patterns = sorted(
        (p.pattern_key, p.confirmed_work, p.confirmed_private, p.imported_work, p.imported_private,
         p.corrections_to_private, p.corrections_to_work, repr(p.purposes))
        for p in db.execute(select(M.LocationPattern)).scalars()
    )
    items = sorted(
        (i.dedupe_key, i.kind, i.status, i.title, i.detail or "", str(i.logical_trip_id))
        for i in db.execute(select(M.ReviewItem).where(M.ReviewItem.vehicle_id == vehicle_id)).scalars()
    )
    return {"trips": trips, "classifications": classifications, "purposes": purposes, "patterns": patterns,
            "items": items}


def open_category_trips(db, vehicle) -> list[uuid.UUID]:
    rows = db.execute(
        select(M.ReviewItem.logical_trip_id).where(
            M.ReviewItem.vehicle_id == vehicle.id, M.ReviewItem.kind == ReviewKind.CATEGORY,
            M.ReviewItem.status == ReviewState.OPEN,
        ).order_by(M.ReviewItem.created_at)
    ).scalars()
    return [r for r in rows if r is not None]


class QueryCounter:
    def __init__(self, engine) -> None:
        self.engine, self.n = engine, 0

    def __enter__(self) -> QueryCounter:
        event.listen(self.engine, "before_cursor_execute", self._count)
        self.n = 0
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._count)

    def _count(self, *_a: object, **_k: object) -> None:
        self.n += 1


@pytest.fixture()
def fleet(db, user, vehicle):
    return data.build(db, user, vehicle, imported=200)


def _old_path(db, user, vehicle, decide) -> dict[str, Any]:
    """Den gamla vägen (1.2.1): beslutet och hela omräkningen direkt, i samma transaktion."""
    decide()
    db.flush()
    state = snapshot(db, vehicle.id)
    db.rollback()
    return state


def _same_customer_trips(db, vehicle, fleet) -> list[M.LogicalTrip]:
    first = db.get(M.LogicalTrip, fleet.open_trip_ids[0])
    return [t for t in seg.active_trips(db, vehicle.id)
            if t.end_location_id == first.end_location_id and t.review_status != "imported"]


# ----------------------------------------------------------------------------- samma resultat


def test_flera_beslut_i_rad_ger_samma_resultat_som_gamla_vagen(client, db, user, vehicle, fleet, job_queue, actor):
    before = snapshot(db, vehicle.id)
    targets = [t.id for t in _same_customer_trips(db, vehicle, fleet)][:3]
    assert len(targets) == 3

    def old():
        # Flera beslut i rad räknas om en gång, som massredigeringen alltid har gjort.
        for tid in targets:
            trip = seg.load_trip(db, tid)
            feedback.apply_decision(db, trip, user_id=user.id, actor=actor, category=Category.WORK,
                                    purpose="Kundbesök Nykund", rebuild=False)
        feedback.after_decisions(db, user_id=user.id, vehicle_id=vehicle.id, actor=actor)
    expected = _old_path(db, user, vehicle, old)
    assert expected != before  # beslutet påverkar fler resor än de tre (annars prövar testet inget)

    for tid in targets:
        r = client.patch(f"{API}/trips/{tid}", json={"category": "work", "purpose": "Kundbesök Nykund"})
        assert r.status_code == 200, r.text
        assert r.json()["category"] == "work" and r.json()["review_status"] == "approved"
    # Svaret kom före omräkningen: beställd, köad och ännu inte gjord.
    assert [name for name, _ in job_queue if name == recompute.JOB] == [recompute.JOB] * 3
    assert client.get(f"{API}/review").json()["recompute_pending"] is True
    middle = snapshot(db, vehicle.id)
    for tid in targets:  # resornas egna ärenden är redan stängda
        assert all(i[2] != ReviewState.OPEN for i in middle["items"] if i[5] == str(tid))

    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    assert snapshot(db, vehicle.id) == expected
    assert client.get(f"{API}/review").json()["recompute_pending"] is False


def test_godkann_massredigering_snabbfraga_och_angra_ger_samma_resultat(
    client, db, user, vehicle, fleet, job_queue, actor
):
    """Varje beslut för sig: den gamla vägen räknade om efter varje beslut, den nya efter svaret."""
    from app.services.ha import responses

    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    open_ids = open_category_trips(db, vehicle)
    live = set(fleet.live_trip_ids)
    proposal = next(t for t in open_ids if db.get(M.LogicalTrip, t).category != Category.UNDECIDED)
    question_trip = next(t for t in open_ids if t in live and t != proposal)
    bulk_ids = [t for t in open_ids if t not in (proposal, question_trip)][:4]
    cfg = settings_svc.get_section(db, user.id, "notifications")
    req = notifier.create_classify_question(db, user_id=user.id, trip=db.get(M.LogicalTrip, question_trip),
                                            cfg=cfg, now=utcnow(), send_after=utcnow())
    assert req is not None
    db.commit()

    def old():
        feedback.apply_decision(db, seg.load_trip(db, proposal), user_id=user.id, actor=actor, approve=True)
        feedback.bulk_apply(db, [seg.load_trip(db, t) for t in bulk_ids], user_id=user.id, actor=actor,
                            category=Category.PRIVATE)
        responses.handle(db, user_id=user.id, public_id=req.public_id, action="WORK", idempotency_key="gammal",
                         source="web")
    expected = _old_path(db, user, vehicle, old)

    shown = db.get(M.LogicalTrip, proposal).category
    assert client.post(f"{API}/trips/{proposal}/approve", json={"expected_category": shown}).status_code == 200
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    r = client.post(f"{API}/trips/bulk", json={"trip_ids": [str(t) for t in bulk_ids], "category": "private"})
    assert r.status_code == 200 and r.json()["updated"] == 4
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    r = client.post(f"{API}/notifications/{req.public_id}/respond", json={"action": "WORK"})
    assert r.status_code == 200 and r.json()["outcome"] == "applied", r.text
    assert recompute.is_pending(db, vehicle.id)
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    assert snapshot(db, vehicle.id) == expected

    # Ångra: resan återställs direkt, och omräkningen efter svaret ger samma läge som den gamla vägen.
    fb = feedback.history(db, question_trip)[0]
    expected_undo = _old_path(db, user, vehicle, lambda: feedback.undo(db, db.get(M.LearningFeedback, fb.id),
                                                                       actor=actor))
    r = client.post(f"{API}/learning/feedback/{fb.id}/undo")
    assert r.status_code == 200 and r.json()["category"] != "work"
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}
    assert snapshot(db, vehicle.id) == expected_undo


def test_sjalvgaende_lage_godkanner_samma_resor(client, db, user, vehicle, job_queue, actor):
    """Tre beslut för samma kund gör att autopiloten godkänner nästa resa från bilen dit (H)."""
    fleet = data.build(db, user, vehicle, imported=60, open_imported=4, live=8)
    first = db.get(M.LogicalTrip, fleet.live_trip_ids[0])
    same = [t.id for t in seg.active_trips(db, vehicle.id)
            if t.end_location_id == first.end_location_id and t.review_status != "imported"]
    decided, waiting = same[:3], same[3:]
    assert waiting

    def old():
        for tid in decided:
            feedback.apply_decision(db, seg.load_trip(db, tid), user_id=user.id, actor=actor,
                                    category=Category.WORK, purpose="Kundbesök Nykund")
    expected = _old_path(db, user, vehicle, old)
    auto = {k for k, v in expected["trips"].items() if v[10] not in (None, "user")}
    assert auto & {str(t) for t in waiting}, "scenariot ska ge ett automatiskt godkännande"

    for tid in decided:
        assert client.patch(f"{API}/trips/{tid}", json={"category": "work", "purpose": "Kundbesök Nykund"}).status_code == 200
        recompute.run_vehicle(vehicle.id)
    assert snapshot(db, vehicle.id) == expected


# ----------------------------------------------------------------------------- omstart och samtidighet


def test_avbruten_omrakning_gors_vid_nasta_tick(client, db, user, vehicle, fleet, job_queue, actor, monkeypatch):
    from app.services.classify import autopilot
    from app.worker import tasks

    trip_id = open_category_trips(db, vehicle)[0]
    expected = _old_path(db, user, vehicle, lambda: feedback.apply_decision(
        db, seg.load_trip(db, trip_id), user_id=user.id, actor=actor, category=Category.PRIVATE))
    assert client.patch(f"{API}/trips/{trip_id}", json={"category": "private"}).status_code == 200

    real = autopilot.run_vehicle

    def crash(*_a, **_k):
        raise RuntimeError("processen avbröts")

    monkeypatch.setattr(autopilot, "run_vehicle", crash)
    with pytest.raises(RuntimeError):
        recompute.run_vehicle(vehicle.id)  # halvvägs: mönster och klassning är redan sparade
    assert recompute.is_pending(db, vehicle.id)
    monkeypatch.setattr(autopilot, "run_vehicle", real)

    # En omstart mitt i en körning lämnar låset kvar; enprocessläget släpper det vid start.
    db.execute(M.Vehicle.__table__.update().values(recompute_started_at=utcnow()))
    db.commit()
    assert tasks.tick()["recompute"] == {"vehicles": 0}  # låset hindrar en andra körning
    recompute.release_leases()
    assert tasks.tick()["recompute"] == {"vehicles": 1}
    assert not recompute.is_pending(db, vehicle.id)
    assert snapshot(db, vehicle.id) == expected


def test_nytt_beslut_under_omrakningen_ger_en_omrakning_till(db, user, vehicle, fleet, monkeypatch):
    with recompute_session(db) as s:
        recompute.request(s, vehicle.id)
    real = recompute._recompute
    calls: list[int] = []

    def with_new_decision(vehicle_id):
        real(vehicle_id)
        calls.append(1)
        if len(calls) == 1:  # ett beslut kommer medan omräkningen pågår
            with recompute_session(db) as s:
                recompute.request(s, vehicle_id)

    monkeypatch.setattr(recompute, "_recompute", with_new_decision)
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 2}
    assert not recompute.is_pending(db, vehicle.id)


def test_las_hindrar_samtidig_omrakning_men_gar_ut(db, user, vehicle, fleet):
    with recompute_session(db) as s:
        recompute.request(s, vehicle.id)
    db.execute(M.Vehicle.__table__.update().values(recompute_started_at=utcnow()))
    db.commit()
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 0}
    db.execute(M.Vehicle.__table__.update().values(recompute_started_at=utcnow() - recompute.LEASE * 2))
    db.commit()
    assert recompute.run_vehicle(vehicle.id) == {"rounds": 1}


class recompute_session:  # noqa: N801 - används som contextmanager
    """En egen session som beställer och committar (som ett anrop gör)."""

    def __init__(self, db) -> None:
        self.db = db

    def __enter__(self):
        from app.db import get_sessionmaker

        self.s = get_sessionmaker()()
        self.queue = jobs.capture_for_tests(True)
        return self.s

    def __exit__(self, *exc: object) -> None:
        self.s.commit()
        self.s.close()
        jobs.capture_for_tests(False)


def test_godkann_ett_forslag_som_hunnit_andras_vagras(client, db, user, vehicle, fleet, job_queue):
    trip_id = next(t for t in open_category_trips(db, vehicle) if db.get(M.LogicalTrip, t).category != "undecided")
    shown = db.get(M.LogicalTrip, trip_id).category
    other = "private" if shown == "work" else "work"
    r = client.post(f"{API}/trips/{trip_id}/approve", json={"expected_category": other})
    assert r.status_code == 409 and r.json()["code"] == "proposal_changed"
    assert reload_trip(db, trip_id).review_status != "approved"
    assert client.post(f"{API}/trips/{trip_id}/approve").status_code == 200  # utan förväntan som förut


def test_vanliga_anrop_raknar_om_direkt_som_forut(db, user, vehicle, fleet, actor, job_queue):
    """Tjänstekod utan recompute.defer (t.ex. inställningar, kalender) räknar om direkt."""
    trip = seg.load_trip(db, open_category_trips(db, vehicle)[0])
    feedback.apply_decision(db, trip, user_id=user.id, actor=actor, category=Category.WORK)
    assert not recompute.is_pending(db, vehicle.id) and job_queue == []


# ----------------------------------------------------------------------------- prestanda


def test_beslut_kostar_lika_lite_oavsett_antal_resor(client, engine, db, user, vehicle, job_queue):
    small = M.Vehicle(owner_user_id=user.id, name="Lilla bilen", make="Volvo", model="V60")
    db.add(small)
    db.flush()
    data.build(db, user, small, imported=40, open_imported=4, live=2, self_driving=False)
    data.build(db, user, vehicle, imported=1000)

    def cost(vehicle_id: uuid.UUID) -> dict[str, tuple[int, float]]:
        trips = open_category_trips(db, db.get(M.Vehicle, vehicle_id))
        out: dict[str, tuple[int, float]] = {}
        for label, call in (
            ("beslut", lambda: client.patch(f"{API}/trips/{trips[0]}", json={"category": "work"})),
            ("massredigering", lambda: client.post(f"{API}/trips/bulk", json={
                "trip_ids": [str(t) for t in trips[1:3]], "category": "private"})),
        ):
            with QueryCounter(engine) as qc:
                start = time.perf_counter()
                assert call().status_code == 200
                out[label] = (qc.n, time.perf_counter() - start)
        return out

    small_cost, large_cost = cost(small.id), cost(vehicle.id)
    for label, (queries, seconds) in large_cost.items():
        # 1.2.1: ca 6 500 frågor och 1–2 s per beslut med tusen resor (se matning.md).
        assert queries <= small_cost[label][0] + 10, (label, queries, small_cost[label][0])
        assert queries < 150, (label, queries)
        assert seconds < 1.0, (label, seconds)


def test_omrakningen_haller_korta_transaktioner(db, user, vehicle, monkeypatch):
    data.build(db, user, vehicle, imported=300)
    with recompute_session(db) as s:
        recompute.request(s, vehicle.id)
    steps: list[float] = []
    real = recompute._step

    from contextlib import contextmanager

    @contextmanager
    def timed(*a, **k):
        start = time.perf_counter()
        with real(*a, **k) as s:
            yield s
        steps.append(time.perf_counter() - start)

    monkeypatch.setattr(recompute, "_step", timed)
    recompute.run_vehicle(vehicle.id)
    assert len(steps) > 6  # mönster, klassning i bitar, autopilot, ärenden i bitar, luckor
    assert max(steps) < 1.0, steps


# ----------------------------------------------------------------------------- uppgraderingen


def test_migrering_0003_fran_en_121_databas_med_resor_och_arenden(tmp_path):
    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db as dbm
    from app.services.trips import review
    from tests.test_enprocess_sqlite import alembic_config

    url = f"sqlite:///{tmp_path}/v121.sqlite3"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    file_engine = create_engine(url)
    dbm.set_engine(file_engine)
    s = dbm.get_sessionmaker()()
    u = M.User(email="agare@example.com", display_name="Ägare", password_hash="x", is_admin=True)
    s.add(u)
    s.flush()
    v = M.Vehicle(owner_user_id=u.id, name="Bilen", make="Volvo", model="V90")
    s.add(v)
    s.flush()
    from tests.conftest import TripFactory

    f = TripFactory(s, u, v)
    a = f.trip("Startgatan 1, Påhittby", "Mellanvägen 2, Påhittby", km=36.0, odometer_start=197700)
    f.skip(hours=3)
    b = f.trip("Mellanvägen 2, Påhittby", "Slutgatan 3, Påhittby", km=25.0, odometer_start=197735)
    ra, rb = seg.ordered_raws(a)[0], seg.ordered_raws(b)[0]
    review.upsert(s, vehicle_id=v.id, kind=ReviewKind.ANOMALY, dedupe_key=f"odo-overlap:{ra.id}:{rb.id}",
                  title="Motstridiga mätarställningar", detail="Två resor överlappar i mätarställning "
                  "(197736 km respektive 197735 km). Det kan vara en dubblett eller ett avläsningsfel.",
                  data={"type": "odometer_overlap", "first_raw_trip_id": str(ra.id), "second_raw_trip_id": str(rb.id)})
    settings_svc.update_section(s, u.id, "merge", {"odometer_tolerance_km": 0.0})
    s.commit()
    s.close()

    command.downgrade(cfg, "0002")  # databasen som den såg ut i 1.2.1, med resor och ett öppet ärende
    assert "recompute_requested" not in {c["name"] for c in inspect(file_engine).get_columns("vehicles")}
    command.upgrade(cfg, "head")
    with file_engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT recompute_requested, recompute_done FROM vehicles").one() == (1, 0)
        assert conn.exec_driver_sql("SELECT count(*) FROM logical_trips").scalar() == 2

    from app.worker import tasks

    assert tasks.tick()["recompute"] == {"vehicles": 1}
    s = dbm.get_sessionmaker()()
    item = s.execute(select(M.ReviewItem).where(M.ReviewItem.dedupe_key.like("odo-overlap:%"))).scalar_one()
    assert item.status == ReviewState.RESOLVED and item.resolution["reason"] == review.NO_LONGER_REASON
    assert settings_svc.get_section(s, u.id, "merge")["odometer_tolerance_km"] == 0.0
    assert s.execute(select(M.LogicalTrip).where(M.LogicalTrip.state == LogicalState.ACTIVE)).scalars().all()
    s.close()
    dbm.dispose_engine()
