"""1.3.0 DEL 2: uppehåll i hämtningen från Volvo – scenarier hela vägen.

Hämtningen ligger nere medan bilen kör: när den är tillbaka har mätaren ökat och DriveLog
kan bara se en lång resa. Varningen säger när hämtningen var borta och hur många km mätaren
ökade, i webben (ärendet) och i telefonen (frågan och larmet). Importeras Volvos körjournal
ersätts den rekonstruerade resan av originalresorna – utan dubbletter, med rätt kilometrar,
och ägarens beslut förs över eller står i klartext i ett ärende.

Tiderna i testerna är svensk tid (måndag 5 oktober 2026, sommartid).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    GapStatus,
    LocationType,
    LogicalState,
    NotificationStatus,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TripSource,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.export import service as export_svc
from app.services.ha import actions as A
from app.services.ha import alerts, messages, notifier, responses
from app.services.importer import service as imp
from app.services.learning import feedback
from app.services.trips import pipeline
from app.services.trips import segments as seg
from app.services.volvo import detection
from tests.conftest import reload_trip
from tests.fixtures import volvo_exports as fx
from tests.test_ha_support import setup_pull
from tests.test_volvo_support import A as POS_HOME
from tests.test_volvo_support import B as POS_OFFICE
from tests.test_volvo_support import FakeCar, connect, poll, poll_every

TZ = ZoneInfo("Europe/Stockholm")
HOME = "Hemvägen 1, 111 11 Exempelstad"
OFFICE = "Kontorsgatan 2, 111 22 Exempelstad"
ACME = "Industrivägen 10, 222 33 Verkstadsby"
BETA = "Lagergatan 4, 333 44 Norrby"
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def L(h: int, m: int, day: int = 5) -> datetime:
    """Svensk lokal tid den 5 oktober 2026 (som UTC)."""
    return datetime(2026, 10, day, h, m, tzinfo=TZ).astimezone(UTC)


def naive(h: int, m: int) -> datetime:
    return datetime(2026, 10, 5, h, m)


@pytest.fixture()
def car(db, user, vehicle, trips) -> FakeCar:
    """Riktig bil (inte demo) med kända platser, självgående läge och en telefon."""
    connect(db, user, vehicle)
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    trips.location(HOME, name="Hemmet", type=LocationType.HOME, lat=POS_HOME[0], lon=POS_HOME[1])
    trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE, lat=POS_OFFICE[0], lon=POS_OFFICE[1])
    trips.location(ACME, name="Exempel Åkeri AB", type=LocationType.CUSTOMER, company="Exempel Åkeri AB")
    trips.location(BETA, name="Beta AB", type=LocationType.CUSTOMER, company="Beta AB")
    setup_pull(db, user, batch_window_minutes=0, quiet_start="00:00", quiet_end="00:00")
    c = FakeCar()
    c.odometer = 10000
    c.odo_ts = c.pos_ts = c.engine_ts = L(7, 0)
    db.flush()
    return c


def standing_at_home(db, vehicle, car: FakeCar) -> None:
    poll_every(db, vehicle, car, L(7, 55), L(8, 10))


def back_after_gap(db, vehicle, car: FakeCar, *, km: float, dest=POS_OFFICE, at: tuple[int, int] = (12, 30)) -> None:
    """Hämtningen låg nere 08:10–12:40; under tiden körde bilen ``km`` km."""
    if dest is None:  # rundresa: ingen ny position, bara mätaren (bekräftas efter längre väntan)
        car.odometer += km
        car.odo_ts = L(*at)
        car.engine, car.engine_ts = "STOPPED", L(*at)
        poll_every(db, vehicle, car, L(12, 40), L(13, 20))
        return
    car.drive_to(dest, km, L(*at))
    for t in (L(12, 40), L(12, 45), L(12, 55)):
        poll(db, vehicle, car, t)


def live_raws(db) -> list[M.RawTrip]:
    return list(db.execute(
        select(M.RawTrip).where(M.RawTrip.source == TripSource.VOLVO_API).order_by(M.RawTrip.odometer_start)
    ).scalars())


def trip_for(db, raw: M.RawTrip) -> M.LogicalTrip:
    trip = db.execute(
        select(M.LogicalTrip)
        .join(M.TripSegment, M.TripSegment.logical_trip_id == M.LogicalTrip.id)
        .where(M.TripSegment.raw_trip_id == raw.id)
        .order_by(M.LogicalTrip.created_at.desc())
    ).scalars().first()
    return reload_trip(db, trip.id)


def item(db, trip_id: uuid.UUID, kind: str) -> M.ReviewItem | None:
    return db.execute(
        select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == trip_id, M.ReviewItem.kind == kind)
    ).scalar_one_or_none()


def is_open(db, trip_id: uuid.UUID, kind: str) -> bool:
    found = item(db, trip_id, kind)
    return found is not None and found.status == ReviewState.OPEN


def asks(db, trip_id: uuid.UUID) -> bool:
    """Ett öppet ärende som ställer frågan Tjänst eller privat (kategori, eller den skattemässiga
    bedömningen som ersätter det när resan går mellan bostaden och tjänstestället)."""
    return is_open(db, trip_id, ReviewKind.CATEGORY) or is_open(db, trip_id, ReviewKind.TAX_UNCERTAIN)


def classify_questions(db) -> list[M.NotificationRequest]:
    return list(db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.kind.in_(notifier.SD_QUESTION_KINDS))
    ).scalars())


def three_rows() -> list[fx.Row]:
    """Volvos körjournal för förmiddagen: 40 + 25 + 30 km."""
    return [
        fx.Row(naive(8, 20), naive(8, 50), 10000, 10040, HOME, ACME, 1800, 40.0, 3.0, 0, 0,
               "Kundbesök Exempel Åkeri AB", "Arbete"),
        fx.Row(naive(9, 30), naive(9, 55), 10040, 10065, ACME, BETA, 1500, 25.0, 2.0, 0, 0, "", "Arbete"),
        fx.Row(naive(11, 50), naive(12, 30), 10065, 10095, BETA, OFFICE, 2400, 30.0, 2.0, 0, 0, "", "Privat"),
    ]


def import_rows(db, user, vehicle, actor, rows: list[fx.Row], name: str = "oktober.csv"):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[(name, fx.build_csv(rows))], actor=actor)
    return batch, imp.commit(db, batch, actor=actor)


# ----------------------------------------------------------------------------- S2.1


@pytest.mark.parametrize("answered", [True, False], ids=["agaren_svarade", "inget_svar"])
def test_s21_uppehall_matarhopp_fraga_och_import_av_tre_originalresor(db, user, vehicle, actor, car, answered):
    standing_at_home(db, vehicle, car)
    back_after_gap(db, vehicle, car, km=95)

    # 1–2. En resa Hemmet → Kontoret på 95 km, med uppehållet sparat.
    (raw,) = live_raws(db)
    assert raw.distance_km == 95.0
    assert {"collection_gap", "detour_or_multiple_trips"} <= set(raw.quality_flags) and raw.possible_missed_trips
    gap = detection.parse_collection_gap(raw.collection_gap)
    assert gap is not None and gap.odometer_km == 95.0
    assert gap.start == L(8, 10) and gap.end == L(12, 40)

    # 3. Inte godkänd (P "Resa till arbetet" gäller inte – spärr 1 först), ärendena öppna.
    trip = trip_for(db, raw)
    assert trip.review_status != ReviewStatus.APPROVED and trip.approval_source is None
    assert asks(db, trip.id) and is_open(db, trip.id, ReviewKind.DATA_GAP)
    data_gap = item(db, trip.id, ReviewKind.DATA_GAP)
    assert data_gap.title.startswith("Uppehåll i hämtningen:")
    for text in ("08:10", "12:40", "95 km", "Importera Volvos körjournal"):
        assert text in data_gap.detail
    db.commit()

    # 5. En fråga i telefonen med uppehållstexten, utan "Tjänst: …"-knapp.
    notifier.ensure_open_questions(db, now=L(13, 10))
    (question,) = classify_questions(db)
    assert "Volvo-hämtningen låg nere 08:10–12:40 och mätaren ökade 95 km" in question.message
    assert "Importera gärna Volvos körjournal" in question.message
    assert not any(a["key"].startswith(A.WORK_PICK) for a in question.actions)

    # 6. Variant a: ägaren svarar med ett ärende. Variant b: inget svar.
    if answered:
        out = responses.handle(db, user_id=user.id, public_id=question.public_id, action=A.WORK_TEXT,
                               reply_text="Kundbesök Exempel Åkeri AB", idempotency_key="svar-1", source="ha",
                               now=L(13, 15))
        assert out["outcome"] == "applied", out
        assert reload_trip(db, trip.id).category == Category.WORK

    # 7. Import av tre originalresor.
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("okt.csv", fx.build_csv(three_rows()))],
                               actor=actor)
    assert batch.summary["rows_new"] == 3 and batch.summary["rows_duplicate"] == 0
    assert batch.summary["rows_restored"] == 0
    result = imp.commit(db, batch, actor=actor)
    assert result["imported"] == 3 and result["reconciled_api_trips"]["superseded"] == 1

    # 8. Den rekonstruerade resan är ersatt, dess ärenden stängda och frågan avbruten (F3).
    db.expire_all()
    assert raw.voided_at is not None and raw.void_reason == detection.VOID_REASON_SUPERSEDED
    assert db.get(M.LogicalTrip, trip.id).state == LogicalState.VOIDED
    assert not asks(db, trip.id) and not is_open(db, trip.id, ReviewKind.DATA_GAP)
    if not answered:
        assert db.get(M.NotificationRequest, question.id).status == NotificationStatus.CANCELLED
    active = [t for t in seg.active_trips(db, vehicle.id) if t.odometer_start is not None and t.odometer_start >= 10000]
    assert len(active) == 3 and round(sum(t.distance_km for t in active), 1) == 95.0
    open_gaps = db.execute(select(M.DataGap).where(
        M.DataGap.vehicle_id == vehicle.id, M.DataGap.status == GapStatus.OPEN,
        M.DataGap.odometer_start >= 10000, M.DataGap.odometer_end <= 10095,
    )).scalars().all()
    assert open_gaps == []

    # 9. Ägarens beslut kunde inte föras över (1:N) – det står i klartext i ett ärende (F8).
    problems = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).scalars().all()
    if answered:
        (problem,) = problems
        assert "Beslutet kunde inte föras över automatiskt" in problem.detail
        assert "Ditt beslut var: Arbete – Kundbesök Exempel Åkeri AB" in problem.detail
        assert problem.data["previous_category"] == Category.WORK
        assert problem.data["previous_purpose"] == "Kundbesök Exempel Åkeri AB"
        first = min(active, key=lambda t: t.odometer_start)
        assert problem.logical_trip_id == first.id
    else:
        assert problems == []

    # 10. Samma fil igen: bara dubbletter.
    again = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("okt.csv", fx.build_csv(three_rows()))],
                               actor=actor)
    assert (again.summary["rows_duplicate"], again.summary["rows_new"], again.summary["rows_restored"]) == (3, 0, 0)

    # 11. Underlaget: två tjänsterader (40 + 25 km), ingen rad på 95 km.
    journal, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=L(0, 0, day=1), end=L(0, 0, day=31), tz=TZ)
    assert sorted(r.km for r in journal.rows) == [25.0, 40.0]


def test_s21_trip_out_has_the_gap(client, db, user, vehicle, car):
    standing_at_home(db, vehicle, car)
    back_after_gap(db, vehicle, car, km=95)
    trip = trip_for(db, live_raws(db)[0])
    db.commit()
    body = client.get(f"/api/v1/trips/{trip.id}").json()
    gap = body["trip"]["collection_gap"]
    assert gap["odometer_km"] == 95.0
    assert datetime.fromisoformat(gap["from"]) == L(8, 10) and datetime.fromisoformat(gap["to"]) == L(12, 40)
    assert body["trip"]["deletable"] is None  # resor från bilen kan inte tas bort
    listed = client.get("/api/v1/trips").json()["items"][0]
    assert listed["collection_gap"]["odometer_km"] == 95.0


# ----------------------------------------------------------------------------- S2.2


def test_s22_one_to_one_keeps_the_owners_decision(db, user, vehicle, actor, car):
    standing_at_home(db, vehicle, car)
    car.drive_to((59.03, 15.0), 40, L(12, 30))  # en plats som inte är känd sedan tidigare
    for t in (L(12, 40), L(12, 45), L(12, 55)):
        poll(db, vehicle, car, t)
    (raw,) = live_raws(db)
    trip = trip_for(db, raw)
    feedback.apply_decision(db, trip, user_id=user.id, actor=actor, channel="ha", category=Category.WORK,
                            purpose="Kundbesök Exempel Åkeri AB")
    db.flush()

    row = fx.Row(naive(8, 20), naive(8, 55), 10000, 10040, HOME, ACME, 2100, 40.0, 3.0, 0, 0, "", "Privat")
    _, result = import_rows(db, user, vehicle, actor, [row])
    assert result["reconciled_api_trips"]["transferred"] == 1
    imported = [t for t in seg.active_trips(db, vehicle.id) if t.odometer_start == 10000]
    assert len(imported) == 1
    assert imported[0].category == Category.WORK and imported[0].purpose == "Kundbesök Exempel Åkeri AB"
    transferred = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "trip.decision_transferred")).scalars()
    assert len(list(transferred)) == 1
    problem = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.IMPORT_PROBLEM)).scalar_one()
    assert "annan kategori" in problem.detail


# ----------------------------------------------------------------------------- S2.3


def test_s23_round_trip_during_gap_self_driving_asks_and_is_never_silently_private(db, user, vehicle, car):
    standing_at_home(db, vehicle, car)
    back_after_gap(db, vehicle, car, km=80, dest=None)
    (raw,) = live_raws(db)
    assert {"collection_gap", "round_trip_unobserved"} <= set(raw.quality_flags) and raw.possible_missed_trips
    trip = trip_for(db, raw)
    assert "possible_missed_trips" in trip.quality_flags
    assert trip.start_location_id == trip.end_location_id

    decision = autopilot.decide(db, trip, user_id=user.id, ctx=None, rules=None, cfg=None)
    assert decision.outcome == "ask" and decision.reason == autopilot.MULTI_TRIP_TEXT
    assert trip.review_status != ReviewStatus.APPROVED
    assert is_open(db, trip.id, ReviewKind.CATEGORY) and is_open(db, trip.id, ReviewKind.DATA_GAP)
    db.commit()
    notifier.ensure_open_questions(db, now=L(13, 40))
    (question,) = classify_questions(db)
    assert "kan vara flera körningar" in question.message


def test_s23_data_gap_stays_open_for_a_private_proposal(db, user, vehicle, trips):
    """F1: motorns privata förslag döljer aldrig "kan vara flera körningar"."""
    trip = trips.trip(HOME, OFFICE, category=Category.PRIVATE, review_status=ReviewStatus.PROPOSED,
                      source=TripSource.VOLVO_API, quality_flags=["collection_gap", "possible_missed_trips"],
                      possible_missed_trips=True)
    pipeline.sync_trip_review_items(db, trip)
    assert seg.is_private_only(trip)
    assert is_open(db, trip.id, ReviewKind.DATA_GAP)

    # Ett automatiskt godkännande räcker inte heller.
    trip.review_status, trip.approval_source = ReviewStatus.APPROVED, "private_place"
    pipeline.sync_trip_review_items(db, trip)
    assert is_open(db, trip.id, ReviewKind.DATA_GAP)

    # Ägarens eget svar Privat stänger ärendet (journalens omfång).
    trip.approval_source = "user"
    pipeline.sync_trip_review_items(db, trip)
    closed = item(db, trip.id, ReviewKind.DATA_GAP)
    assert closed.status != ReviewState.OPEN
    assert closed.resolution.get("reason")


def test_s23_manual_mode_low_threshold_is_never_auto(db, user, vehicle, car):
    """F2: possible_missed_trips ger aldrig en tyst preliminär klassning."""
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    settings_svc.update_section(db, user.id, "ai", {"auto_threshold": 0.6, "review_threshold": 0.5})
    standing_at_home(db, vehicle, car)
    back_after_gap(db, vehicle, car, km=80, dest=None)
    trip = trip_for(db, live_raws(db)[0])
    assert trip.review_status != ReviewStatus.AUTO
    assert is_open(db, trip.id, ReviewKind.CATEGORY) and is_open(db, trip.id, ReviewKind.DATA_GAP)


def test_s23_owner_answers_private_in_the_web_and_bulk(client, db, user, vehicle, car):
    standing_at_home(db, vehicle, car)
    back_after_gap(db, vehicle, car, km=80, dest=None)
    trip = trip_for(db, live_raws(db)[0])
    db.commit()
    assert is_open(db, trip.id, ReviewKind.DATA_GAP)  # syns före beslutet
    r = client.patch(f"/api/v1/trips/{trip.id}", json={"category": "private", "approve": True})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert not is_open(db, trip.id, ReviewKind.DATA_GAP)
    journal, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=L(0, 0, day=1), end=L(0, 0, day=31), tz=TZ)
    assert journal.rows == []


def test_s23_bulk_approval_counts_as_the_owners_decision(db, user, vehicle, trips, actor):
    trip = trips.trip(HOME, HOME, category=Category.PRIVATE, review_status=ReviewStatus.PROPOSED,
                      source=TripSource.VOLVO_API, quality_flags=["collection_gap", "possible_missed_trips"],
                      possible_missed_trips=True)
    pipeline.sync_trip_review_items(db, trip)
    assert is_open(db, trip.id, ReviewKind.DATA_GAP)
    feedback.bulk_apply(db, [trip], user_id=user.id, actor=actor, approve=True)
    assert reload_trip(db, trip.id).approval_source == "bulk"
    assert not is_open(db, trip.id, ReviewKind.DATA_GAP)


def test_s23_missing_odometer_on_private_proposal_is_unchanged(db, user, vehicle, trips):
    """Regression: MISSING_ODOMETER hanteras som förut (en saknad mätarställning döljer inga körningar)."""
    trip = trips.trip(HOME, OFFICE, category=Category.PRIVATE, review_status=ReviewStatus.PROPOSED,
                      source=TripSource.VOLVO_API, quality_flags=["missing_odometer"])
    pipeline.sync_trip_review_items(db, trip)
    assert item(db, trip.id, ReviewKind.MISSING_ODOMETER) is None


# ----------------------------------------------------------------------------- S2.4


def test_s24_import_before_the_car_is_back_gives_no_trip_and_no_question(db, user, vehicle, actor, car):
    standing_at_home(db, vehicle, car)
    import_rows(db, user, vehicle, actor, three_rows())
    back_after_gap(db, vehicle, car, km=95)
    assert live_raws(db) == []
    db.commit()
    notifier.ensure_open_questions(db, now=L(13, 10))
    assert classify_questions(db) == []
    assert db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.DATA_GAP)).first() is None


# ----------------------------------------------------------------------------- S2.5 texter


def gap_of(start: datetime, end: datetime, km: float | None) -> detection.CollectionGap:
    return detection.CollectionGap(start=start, end=end, odometer_km=km)


def test_s25_texts_same_day_overnight_and_without_km():
    same = gap_of(L(8, 10), L(12, 40), 95.0)
    assert detection.collection_gap_text(same, TZ, form="long") == (
        "Hämtningen från Volvo låg nere mellan mån 5/10 08:10 och 12:40. Under tiden ökade mätaren med 95 km, "
        "så resan kan vara flera körningar. Importera Volvos körjournal för dagen under Import, eller dela upp "
        "resan i tjänst och privat."
    )
    assert detection.collection_gap_text(same, TZ, form="short") == (
        "Volvo-hämtningen låg nere 08:10–12:40 och mätaren ökade 95 km – kan vara flera körningar. "
        "Importera gärna Volvos körjournal."
    )
    night = gap_of(L(22, 10), L(6, 40, day=6), 12.5)
    assert "mellan mån 5/10 22:10 och tis 6/10 06:40" in detection.collection_gap_text(night, TZ, form="long")
    assert "12,5 km" in detection.collection_gap_text(night, TZ, form="long")
    assert detection.collection_gap_text(night, TZ, form="short").startswith("Volvo-hämtningen låg nere mån 22:10–tis 06:40")
    no_km = gap_of(L(8, 10), L(12, 40), None)
    assert "Resan kan vara flera körningar." in detection.collection_gap_text(no_km, TZ, form="long")
    assert detection.collection_gap_text(no_km, TZ, form="short") == (
        "Volvo-hämtningen låg nere 08:10–12:40 – kan vara flera körningar. Importera gärna Volvos körjournal."
    )


def test_s25_flag_without_saved_data_gives_the_fallback(db, user, vehicle, trips):
    trip = trips.trip(HOME, OFFICE, source=TripSource.VOLVO_API, quality_flags=["collection_gap", "possible_missed_trips"])
    assert detection.collection_gap_of(trip) is None
    assert detection.collection_gap_text(None, TZ, form="long").startswith(
        "Hämtningen från Volvo hade ett uppehåll före resan")
    assert messages.collection_gap_short(trip, TZ) == "Kan vara flera körningar."
    pipeline.sync_trip_review_items(db, trip)
    data_gap = item(db, trip.id, ReviewKind.DATA_GAP)
    assert data_gap.title.startswith("Uppehåll i hämtningen:")
    assert "hade ett uppehåll före resan" in data_gap.detail


def test_s25_manual_mode_question_carries_the_short_text(db, user, vehicle, trips):
    trip = trips.trip(HOME, OFFICE, source=TripSource.VOLVO_API, quality_flags=["collection_gap"],
                      collection_gap={"from": L(8, 10).isoformat(), "to": L(12, 40).isoformat(), "odometer_km": 95.0})
    built = messages.classify(db, trip, "abc123", hide_locations=True)
    assert "Volvo-hämtningen låg nere 08:10–12:40 och mätaren ökade 95 km" in built["message"]


def test_s25_fetch_failing_alert_mentions_volvos_journal(db, user, vehicle):
    conn = connect(db, user, vehicle)
    conn.last_success_at = L(6, 0)
    db.flush()
    found = alerts._volvo_alerts(db, user.id, L(13, 0), TZ)[alerts.VOLVO_FETCH_FAILING]
    assert len(found) == 1
    assert "Kör du under tiden kan resorna behöva kompletteras med Volvos körjournal (Import)." in found[0].message


def test_rule_based_classification_marks_requires_review(db, user, vehicle, trips):
    """F2 direkt i regelmotorn: även med en egen regel som stöd kräver resan granskning."""
    from app.services.classify import engine
    from app.services.classify import signals as sig
    from app.services.learning import patterns

    office = trips.location(OFFICE, name="Kontoret", type=LocationType.CUSTOMER, company="Kontoret AB")
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=office.id, category=Category.WORK,
                             purpose="Kundbesök")
    settings_svc.update_section(db, user.id, "ai", {"auto_threshold": 0.6, "review_threshold": 0.5})
    clean = trips.trip(ACME, OFFICE, source=TripSource.VOLVO_API)
    flagged = trips.trip(ACME, OFFICE, source=TripSource.VOLVO_API, quality_flags=["possible_missed_trips"],
                         possible_missed_trips=True)

    def evaluate(trip):
        ctx = sig.build_context(db, trip, user_id=user.id, tz=TZ, tax=settings_svc.get_section(db, user.id, "tax"),
                                ai=settings_svc.get_section(db, user.id, "ai"), abbreviations={})
        return sig.evaluate(ctx)

    assert evaluate(clean).requires_review is False  # utan flaggan avgör regeln
    assert evaluate(flagged).requires_review is True
    for trip in (clean, flagged):
        engine.classify_trip(db, trip, user_id=user.id, actor=audit.SYSTEM, provider=None, use_llm=False)
    assert reload_trip(db, clean.id).review_status == ReviewStatus.AUTO
    assert reload_trip(db, flagged.id).review_status != ReviewStatus.AUTO
