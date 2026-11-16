"""S1e: månadsskiftet och nedladdningslänken (byggkontraktet C.6 och E.2).

Varje blockerare och upplysning, att uppskjutna frågor blockerar, flödet waiting → exported,
att ett oförändrat avtryck aldrig ger en ny fil, att en ändrad rad ger "stale" och en
uppdaterad fil med skillnaden i notisen, den signerade länken med alla avvisningsfall och
loggmaskeringen genom uvicorns AccessFormatter. Inga nätverksanrop; alla data är påhittade.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

import io
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.logging_setup import RedactingFilter, redact
from app.models.enums import (
    ApprovalSource,
    Category,
    GeocodeStatus,
    IntegrationProvider,
    IntegrationStatus,
    LocationType,
    NotificationKind,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TimeQuality,
    TripSource,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.export import links, month_close
from app.services.export import service as export_svc
from tests.test_ha_support import setup_pull

pytestmark = pytest.mark.new_defaults

API = "/api/v1"
OCT = date(2026, 10, 1)
END = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # 1 november 00:00 svensk tid
AFTER = END + timedelta(hours=8)  # 1 november 08:00 svensk tid, efter väntetiden på 6 h
DAY = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)  # en onsdag i oktober
HOME = "Hemvägen 1, 111 11 Exempelstad"
CUSTOMER = "Kundvägen 5, 222 22 Exempelby"
OTHER = "Lagergatan 9, 333 33 Annanstad"


# ----------------------------------------------------------------------------- hjälpare


@pytest.fixture()
def sd(db, user):
    """Självgående läge med active_since före testresorna och notiser via utkorgen."""
    setup_pull(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": "2020-01-01T00:00:00+00:00"},
                                allow_read_only=True)
    db.flush()


def obs(db, vehicle, *, fetched: datetime, last_seen: datetime | None = None, odo: float | None = None,
        odo_at: datetime | None = None, processed: bool = True) -> M.VehicleObservation:
    row = M.VehicleObservation(
        vehicle_id=vehicle.id, fetched_at=fetched, last_seen_at=last_seen or fetched, odometer_km=odo,
        odometer_reported_at=odo_at, content_hash=uuid.uuid4().hex, processed_at=fetched if processed else None,
    )
    db.add(row)
    db.flush()
    return row


def final_fetch(db, vehicle) -> M.VehicleObservation:
    """En lyckad hämtning efter månadsslutet där bilens mätarställning är rapporterad efter slutet."""
    return obs(db, vehicle, fetched=END + timedelta(hours=2), odo=99999, odo_at=END + timedelta(hours=1))


def work(trips, *, at: datetime = DAY, km: float = 20.0, source: str = TripSource.VOLVO_API, **kw: Any):
    return trips.trip(HOME, CUSTOMER, category=Category.WORK, purpose="Kundbesök Exempel AB", km=km,
                      start_at=at, source=source, **kw)


def ev(db, user, vehicle, now: datetime = AFTER) -> month_close.MonthStatus:
    return month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=OCT, now=now)


def run(db, user, vehicle, now: datetime = AFTER, **kw: Any) -> month_close.MonthStatus:
    return month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=OCT, now=now, **kw)


def codes(items: list[month_close.Blocker]) -> dict[str, month_close.Blocker]:
    return {b.code: b for b in items}


def exports(db) -> list[M.ExportJob]:
    return list(db.execute(select(M.ExportJob).order_by(M.ExportJob.created_at)).scalars())


def monthly(db) -> list[M.NotificationRequest]:
    return list(db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.MONTHLY)
        .order_by(M.NotificationRequest.created_at)
    ).scalars())


def record(db, vehicle) -> M.MonthClose:
    return db.execute(select(M.MonthClose).where(M.MonthClose.vehicle_id == vehicle.id)).scalar_one()


def item(db, vehicle, trip, kind: str = ReviewKind.CATEGORY, status: str = ReviewState.OPEN) -> M.ReviewItem:
    row = M.ReviewItem(vehicle_id=vehicle.id, kind=kind, dedupe_key=f"{kind}:{trip.id}", logical_trip_id=trip.id,
                       title="Fråga", status=status,
                       snoozed_until=AFTER + timedelta(days=2) if status == ReviewState.SNOOZED else None)
    db.add(row)
    db.flush()
    return row


# ----------------------------------------------------------------------------- blockerare


def test_month_not_over_and_force(db, user, vehicle, trips):
    work(trips)
    early = END + timedelta(hours=5)  # före månadsslut + 6 h
    status = run(db, user, vehicle, now=early)
    assert status.state == "open" and "month_not_over" in codes(status.blockers)
    assert exports(db) == [] and db.execute(select(M.MonthClose)).first() is None  # inget sparas
    # force hoppar bara över väntan på månadsslutet.
    status = run(db, user, vehicle, now=early, force=True)
    assert status.state == "exported" and status.blockers == []
    (job,) = exports(db)
    assert job.origin == "month_close" and job.rows_fingerprint and job.status == "done"


def test_waiting_final_fetch(db, user, vehicle, trips):
    t = work(trips)
    # Fordon utan uppgifter från bilen (bara import) väntar inte på bilen.
    assert "waiting_final_fetch" not in codes(ev(db, user, vehicle).blockers)
    # Bara hämtningar före månadsslutet: väntar.
    obs(db, vehicle, fetched=END - timedelta(days=1), odo=t.odometer_end, odo_at=END - timedelta(days=1))
    b = codes(ev(db, user, vehicle).blockers)["waiting_final_fetch"]
    assert b.text == "Väntar på bilens sista uppgifter"
    assert ev(db, user, vehicle).state == "waiting"
    # Hämtning efter slutet, men bilen har rapporterat en annan mätarställning före slutet: väntar.
    late = obs(db, vehicle, fetched=END + timedelta(hours=1), odo=t.odometer_end + 7, odo_at=END - timedelta(hours=3))
    assert "waiting_final_fetch" in codes(ev(db, user, vehicle).blockers)
    # Samma mätarställning som vid sista resans slut: bilen har stått still – klart.
    late.odometer_km = t.odometer_end
    db.flush()
    assert "waiting_final_fetch" not in codes(ev(db, user, vehicle).blockers)
    # Samma innehåll som sågs igen efter slutet (last_seen_at) räknas också.
    late.odometer_km, late.fetched_at = t.odometer_end + 7, END - timedelta(hours=2)
    late.odometer_reported_at, late.last_seen_at = END + timedelta(minutes=30), END + timedelta(hours=1)
    db.flush()
    assert "waiting_final_fetch" not in codes(ev(db, user, vehicle).blockers)
    # Obearbetade avläsningar från före slutet: resor kan saknas.
    obs(db, vehicle, fetched=END - timedelta(hours=1), processed=False)
    assert "waiting_final_fetch" in codes(ev(db, user, vehicle).blockers)


def test_volvo_disconnected(db, user, vehicle, trips):
    work(trips)
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.VOLVO)
    conn.status = IntegrationStatus.NEEDS_REAUTH
    db.flush()
    assert codes(ev(db, user, vehicle).blockers)["volvo_disconnected"].text == "Volvo behöver ny inloggning"
    conn.status = IntegrationStatus.CONNECTED
    db.flush()
    assert "volvo_disconnected" not in codes(ev(db, user, vehicle).blockers)


def test_open_questions_snoozed_and_mixed(db, user, vehicle, trips):
    work(trips)
    q = trips.trip(CUSTOMER, OTHER, km=12.0, start_at=DAY + timedelta(hours=3), source=TripSource.VOLVO_API)
    assert q.review_status == ReviewStatus.NEEDS_REVIEW
    it = item(db, vehicle, q)
    b = codes(ev(db, user, vehicle).blockers)["open_questions"]
    assert (b.text, b.count, b.trip_ids, b.web_only) == ("1 resa väntar på ditt svar", 1, [str(q.id)], False)
    # En uppskjuten fråga blockerar också.
    it.status, it.snoozed_until = ReviewState.SNOOZED, AFTER + timedelta(days=3)
    db.flush()
    assert "open_questions" in codes(ev(db, user, vehicle).blockers)
    # Oavgjorda km utan ärende blockerar.
    it.status = ReviewState.RESOLVED
    db.flush()
    assert codes(ev(db, user, vehicle).blockers)["open_questions"].trip_ids == [str(q.id)]
    # Fördelningen av en blandad resa går bara att göra i webben.
    q.category, q.review_status = Category.WORK, ReviewStatus.PROPOSED
    q.purpose, q.purpose_confirmed = "Leverans", True
    q.work_km, q.undecided_km = q.distance_km, 0.0
    for s in q.segments:
        s.category = Category.WORK
    item(db, vehicle, q, ReviewKind.MIXED_ALLOCATION)
    blockers = [b for b in ev(db, user, vehicle).blockers if b.code == "open_questions"]
    assert len(blockers) == 1 and blockers[0].web_only and "öppna webben" in blockers[0].text
    # En godkänd resa blockerar inte, även om ett gammalt ärende ligger kvar.
    q.review_status = ReviewStatus.APPROVED
    db.flush()
    assert "open_questions" not in codes(ev(db, user, vehicle).blockers)


def test_unapproved_work(db, user, vehicle, trips):
    work(trips)
    p = work(trips, at=DAY + timedelta(days=1), review_status=ReviewStatus.PROPOSED)
    b = codes(ev(db, user, vehicle).blockers)["unapproved_work"]
    assert (b.text, b.trip_ids) == ("1 tjänsteresa är inte godkänd", [str(p.id)])


def test_assumption_veto_open_and_assumed_note(db, user, vehicle, trips):
    t = work(trips)
    t.approval_source = ApprovalSource.ASSUMED
    t.approval_basis = {"reason": "Antaget kundbesök", "veto_until": (AFTER + timedelta(hours=5)).isoformat()}
    db.flush()
    status = ev(db, user, vehicle)
    b = codes(status.blockers)["assumption_veto_open"]
    assert b.trip_ids == [str(t.id)] and b.text == "Väntar till 1/11 kl. 13:00 på ditt veto"
    assert codes(status.notes)["assumed"].count == 1
    status = ev(db, user, vehicle, now=AFTER + timedelta(hours=6))
    assert "assumption_veto_open" not in codes(status.blockers)
    note = codes(status.notes)["assumed"]
    assert note.trip_ids == [str(t.id)] and "antagen rad" in note.text


def test_addresses_pending_only_for_48_hours(db, user, vehicle, trips):
    t = work(trips)
    loc = db.get(M.KnownLocation, t.end_location_id)
    loc.geocode_status = GeocodeStatus.PENDING
    db.flush()
    assert codes(ev(db, user, vehicle).blockers)["addresses_pending"].text == "Adresser slås upp"
    loc.geocode_status = GeocodeStatus.FAILED
    db.flush()
    assert "addresses_pending" in codes(ev(db, user, vehicle).blockers)
    assert "addresses_pending" not in codes(ev(db, user, vehicle, now=END + timedelta(hours=49)).blockers)


def test_export_errors_including_private_address(db, user, vehicle, trips):
    t = work(trips)
    t.purpose = None
    db.flush()
    b = codes(ev(db, user, vehicle).blockers)["export_errors"]
    assert b.text == "Syfte saknas för tjänsteresor" and b.trip_ids == [str(t.id)]
    t.purpose = "Kundbesök Exempel AB"
    loc = db.get(M.KnownLocation, t.end_location_id)
    loc.type, loc.confirmed = LocationType.PRIVATE, True
    db.flush()
    b = codes(ev(db, user, vehicle).blockers)["export_errors"]
    assert b.text == "Privat adress i underlaget" and b.trip_ids == [str(t.id)]
    # Hemmet får stå som start eller mål.
    loc.type = LocationType.HOME
    db.flush()
    assert "export_errors" not in codes(ev(db, user, vehicle).blockers)


def test_private_address_check_in_export_checks(db, user, vehicle, trips):
    from app.services.export import checks as checks_mod

    live = work(trips)
    imported = work(trips, at=DAY + timedelta(days=1), source=TripSource.VOLVO_IMPORT)
    for t in (live, imported):
        db.get(M.KnownLocation, t.end_location_id).type = LocationType.PRIVATE
    db.flush()
    start, end = month_close._bounds(OCT, settings_svc.user_timezone(db, user.id))
    result = {c["code"]: c for c in checks_mod.run_checks(db, vehicle=vehicle, start=start, end=end,
                                                          tz=settings_svc.user_timezone(db, user.id), now=AFTER)}
    assert result["private_address_in_report"]["severity"] == "error"
    # Bara resor från bilen; importerade resor bedöms som förut.
    assert result["private_address_in_report"]["trip_ids"] == [str(live.id)]


# ----------------------------------------------------------------------------- upplysningar


def test_notes(db, user, vehicle, trips):
    final_fetch(db, vehicle)
    est = work(trips)
    raw = est.segments[0].raw_trip
    est.start_time_quality = raw.start_time_quality = TimeQuality.ESTIMATED
    unknown = work(trips, at=DAY + timedelta(days=1), start_known=False)
    noaddr = work(trips, at=DAY + timedelta(days=2))
    noaddr.end_address = None
    missed = work(trips, at=DAY + timedelta(days=3))
    missed.quality_flags = ["possible_missed_trips"]
    rule = work(trips, at=DAY + timedelta(days=4))
    rule.approval_source = ApprovalSource.RULE
    ret = work(trips, at=DAY + timedelta(days=5))
    ret.approval_source = ApprovalSource.RETURN_TRIP
    commute = trips.trip(HOME, OTHER, category=Category.PRIVATE, km=8.0, start_at=DAY + timedelta(days=6),
                         source=TripSource.VOLVO_API)
    commute.approval_source = ApprovalSource.COMMUTE
    looked = db.get(M.KnownLocation, rule.end_location_id)
    looked.created_from, looked.geocode_status = "api", GeocodeStatus.OK
    db.add(M.LocationPattern(owner_user_id=user.id, pattern_type="destination", pattern_key="d:test",
                             destination_location_id=looked.id, is_manual_rule=True, manual_category="work",
                             updated_at=DAY))
    db.flush()

    status = ev(db, user, vehicle)
    assert status.blockers == [] and status.state == "open"
    notes = {}
    for n in status.notes:
        notes.setdefault(n.code, []).append(n)
    assert notes["estimated_times"][0].trip_ids == [str(est.id)]
    assert notes["missing_start_time"][0].text == "1 resa saknar starttid"
    assert notes["missing_start_time"][0].trip_ids == [str(unknown.id)]
    assert notes["missing_address"][0].trip_ids == [str(noaddr.id)]
    assert notes["possible_missed_trips"][0].trip_ids == [str(missed.id)]
    assert {(n.text, n.count) for n in notes["auto_approved"]} == {
        ("Din regel: 1 resa", 1), ("Hemresa efter tjänsteresa: 1 resa", 1)}
    assert [(n.count, n.trip_ids) for n in notes["quiet_private"]] == [(1, [str(commute.id)])]
    assert notes["addresses_auto"][0].count == 1
    assert notes["new_rules"][0].count == 1
    assert "assumed" not in notes
    # Upplysningarna stoppar inte: månaden går att exportera.
    assert run(db, user, vehicle).state == "exported"


# ----------------------------------------------------------------------------- flödet


def test_run_due_waiting_then_exported(db, user, vehicle, trips, sd):
    final_fetch(db, vehicle)
    work(trips)
    q = trips.trip(CUSTOMER, OTHER, km=12.0, start_at=DAY + timedelta(hours=3), source=TripSource.VOLVO_API)
    it = item(db, vehicle, q)

    assert month_close.run_due(db, END + timedelta(hours=3)) == {}  # före månadsslut + 6 h
    out = month_close.run_due(db, AFTER)
    assert out == {"checked": 1, "waiting": 1}
    mc = record(db, vehicle)
    assert mc.state == "waiting" and [b["code"] for b in mc.blockers] == ["open_questions"]
    (notice,) = monthly(db)
    assert notice.title == "Oktober väntar" and "1 resa väntar på ditt svar" in notice.message
    assert exports(db) == []
    # Högst en kontroll i timmen.
    assert month_close.run_due(db, AFTER + timedelta(minutes=30)) == {}

    # Ägaren svarar: privat.
    q.category, q.review_status, q.private_km, q.undecided_km = Category.PRIVATE, ReviewStatus.APPROVED, 12.0, 0.0
    for s in q.segments:
        s.category = Category.PRIVATE
    it.status = ReviewState.RESOLVED
    db.flush()
    out = month_close.run_due(db, AFTER + timedelta(hours=2))
    assert out == {"checked": 1, "exported": 1}
    mc = record(db, vehicle)
    (job,) = exports(db)
    assert mc.state == "exported" and mc.export_id == job.id and mc.rows_fingerprint == job.rows_fingerprint
    assert job.origin == "month_close" and job.summary["trips"] == 1
    ready = monthly(db)[-1]
    assert ready.title == "Oktober klart" and ready.context["export_id"] == str(job.id)
    assert ready.actions[0]["uri"] == f"export-link:{job.id}"


def test_manual_mode_and_import_only_vehicles_are_left_alone(db, user, vehicle, trips):
    work(trips)
    final_fetch(db, vehicle)
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    db.flush()
    assert month_close.run_due(db, AFTER) == {}
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": "2026-11-01T06:00:00+00:00"},
                                allow_read_only=True)
    db.flush()
    # Oktober tog slut innan självgående läge började gälla.
    assert month_close.run_due(db, AFTER) == {}


def test_waiting_notice_every_third_day_at_most_three_times(db, user, vehicle, trips, sd):
    final_fetch(db, vehicle)
    work(trips, review_status=ReviewStatus.PROPOSED)
    for days in (0, 1, 2, 3, 4, 6, 9, 12):
        run(db, user, vehicle, now=AFTER + timedelta(days=days), notify_waiting=True)
    sent = [n for n in monthly(db) if ":waiting:" in n.dedupe_key]
    assert len(sent) == 3
    assert [n.created_at for n in sent] == [AFTER, AFTER + timedelta(days=3), AFTER + timedelta(days=6)]
    assert record(db, vehicle).notice_count == 3
    # En kontroll från webben skickar ingen väntenotis.
    assert all("1 tjänsteresa är inte godkänd" in n.message for n in sent)


def test_empty_month(db, user, vehicle, trips, sd):
    final_fetch(db, vehicle)
    trips.trip(HOME, OTHER, category=Category.PRIVATE, km=8.0, start_at=DAY, source=TripSource.VOLVO_API)
    status = run(db, user, vehicle)
    assert status.state == "empty" and exports(db) == []
    (notice,) = monthly(db)
    assert notice.title == "Oktober: inga tjänsteresor"
    run(db, user, vehicle, now=AFTER + timedelta(hours=2))
    assert len(monthly(db)) == 1 and record(db, vehicle).state == "empty"


def test_unchanged_fingerprint_never_gives_a_new_file(db, user, vehicle, trips, sd):
    final_fetch(db, vehicle)
    work(trips)
    assert run(db, user, vehicle).state == "exported"
    for hours in (2, 30, 80):
        assert run(db, user, vehicle, now=AFTER + timedelta(hours=hours)).state == "exported"
    assert len(exports(db)) == 1 and len(monthly(db)) == 1


def test_changed_row_gives_stale_then_updated_file(db, user, vehicle, trips, sd):
    final_fetch(db, vehicle)
    work(trips)
    run(db, user, vehicle)
    (first,) = exports(db)
    url_before = links.make_token(first, user, AFTER + timedelta(days=7))

    work(trips, at=DAY + timedelta(days=2), km=34.0)  # en resa till i oktober
    status = run(db, user, vehicle, now=AFTER + timedelta(hours=2))
    assert status.state == "stale" and record(db, vehicle).state == "stale"
    assert len(exports(db)) == 1  # nästa kontroll skapar filen

    status = run(db, user, vehicle, now=AFTER + timedelta(hours=3))
    old, new = exports(db)
    assert status.state == "exported" and status.export_id == new.id
    assert old.superseded_by_id == new.id and new.superseded_by_id is None
    mc = record(db, vehicle)
    assert mc.export_id == new.id and mc.rows_fingerprint == new.rows_fingerprint != old.rows_fingerprint
    # Den gamla länken säger att en nyare fil finns.
    with pytest.raises(links.LinkExpired) as exc:
        links.verify(db, url_before, AFTER + timedelta(hours=4))
    assert exc.value.message == links.SUPERSEDED_TEXT
    # Uppdateringsnotisen högst 1 per dygn: den första notisen skickades för 3 h sedan.
    assert len(monthly(db)) == 1
    run(db, user, vehicle, now=AFTER + timedelta(hours=25))
    update = monthly(db)[-1]
    assert update.title == "Oktober uppdaterad" and update.message.startswith("+1 resa, +34 km")
    assert len(exports(db)) == 2


def test_stale_mark_from_assumption_keeps_file_when_rows_are_unchanged(db, user, vehicle, trips, sd):
    final_fetch(db, vehicle)
    work(trips)
    run(db, user, vehicle)
    record(db, vehicle).state = "stale"  # som autopilot._on_assumed_correction gör
    db.flush()
    assert run(db, user, vehicle, now=AFTER + timedelta(hours=2)).state == "stale"
    assert run(db, user, vehicle, now=AFTER + timedelta(hours=4)).state == "stale"
    assert len(exports(db)) == 1


def test_month_close_route_run_with_force(client, db, user, vehicle, trips):
    work(trips, at=datetime.now(UTC) - timedelta(hours=2))
    current = datetime.now(UTC).astimezone(settings_svc.user_timezone(db, user.id))
    key = f"{current.year:04d}-{current.month:02d}"
    db.commit()
    r = client.post(f"{API}/month-close/{key}/run", params={"force": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "exported" and r.json()["export_id"]
    r = client.get(f"{API}/exports")
    (out,) = [e for e in r.json() if e["origin"] == "month_close"]
    assert out["link_downloads"] == 0


# ----------------------------------------------------------------------------- länken


@pytest.fixture()
def job(db, user, vehicle, trips, actor) -> M.ExportJob:
    work(trips, source=TripSource.VOLVO_IMPORT)
    start, end = month_close._bounds(OCT, settings_svc.user_timezone(db, user.id))
    return export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=start, end=end, actor=actor)


EXP = AFTER + timedelta(days=7)


def test_token_format_and_verify(db, user, job):
    token = links.make_token(job, user, EXP)
    job_hex, exp, sig = token.split(".")
    assert job_hex == job.id.hex and int(exp) == int(EXP.timestamp()) and len(sig) == 43
    assert links.verify(db, token, AFTER).id == job.id
    assert links.make_url(job, user, EXP).endswith(f"/api/v1/public/exports/{token}")
    assert job.origin == "manual" and job.rows_fingerprint


def test_no_link_without_sha256(db, user, job):
    job.sha256 = None
    assert links.make_token(job, user, EXP) is None and links.make_url(job, user, EXP) is None


@pytest.mark.parametrize("case", ["signature", "sha256", "expired", "deleted", "superseded", "downloads", "epoch",
                                  "format", "missing_file", "other_user"])
def test_link_rejections(db, user, job, case, actor):
    token = links.make_token(job, user, EXP)
    now = AFTER
    expected: type[Exception] = links.LinkInvalid
    if case == "signature":
        token = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    elif case == "sha256":
        job.sha256 = "f" * 64
    elif case == "expired":
        now, expected = EXP + timedelta(seconds=1), links.LinkExpired
    elif case == "deleted":
        export_svc.delete_export(db, job, actor=actor)
    elif case == "superseded":
        job.superseded_by_id, expected = uuid.uuid4(), links.LinkExpired
    elif case == "downloads":
        job.link_downloads = links.LINK_MAX_DOWNLOADS
    elif case == "epoch":
        links.revoke_all(db, user)
    elif case == "format":
        token = token.replace(".", "-", 1)
    elif case == "missing_file":
        export_svc.export_path(job).unlink()
    elif case == "other_user":
        token = f"{uuid.uuid4().hex}.{token.split('.', 1)[1]}"
    db.flush()
    with pytest.raises(expected):
        links.verify(db, token, now)


def test_public_route_counts_and_stops_at_ten(anon_client, db, user, job):
    exp = datetime.now(UTC) + timedelta(days=7)
    token = links.make_token(job, user, exp)
    db.commit()
    for _ in range(links.LINK_MAX_DOWNLOADS):
        r = anon_client.get(f"{API}/public/exports/{token}")
        assert r.status_code == 200 and r.content.startswith(b"PK")
        assert r.headers["cache-control"] == "no-store"
    assert anon_client.get(f"{API}/public/exports/{token}").status_code == 404  # den 11:e hämtningen
    db.expire_all()
    assert db.get(M.ExportJob, job.id).link_downloads == links.LINK_MAX_DOWNLOADS
    events = db.execute(select(M.AuditEvent).where(M.AuditEvent.action == "export.link_download")).scalars().all()
    assert len(events) == 10 and all(token not in str(e.after) for e in events)


def test_public_route_expired_and_revoked(anon_client, db, user, job):
    old = links.make_token(job, user, datetime.now(UTC) - timedelta(seconds=5))
    token = links.make_token(job, user, datetime.now(UTC) + timedelta(days=7))
    db.commit()
    r = anon_client.get(f"{API}/public/exports/{old}")
    assert r.status_code == 410 and "gått ut" in r.json()["detail"]
    links.revoke_all(db, db.get(M.User, user.id))
    db.commit()
    r = anon_client.get(f"{API}/public/exports/{token}")
    assert r.status_code == 404 and r.json()["detail"] == links.INVALID_TEXT


# ----------------------------------------------------------------------------- loggmaskeringen


def _logger(name: str, formatter: logging.Formatter) -> tuple[logging.Logger, io.StringIO]:
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, out


def test_access_log_masks_link_without_breaking_uvicorn():
    from uvicorn.logging import AccessFormatter

    token = f"{'a' * 32}.1900000000.{'S' * 43}"
    logger, out = _logger("test.s1e.access", AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logger.info('%s - "%s %s HTTP/%s" %d', "192.0.2.10:51000", "GET", f"/api/v1/public/exports/{token}", "1.1", 200)
    line = out.getvalue()
    assert '192.0.2.10:51000 - "GET /api/v1/public/exports/[DOLD] HTTP/1.1" 200' in line
    assert token not in line and "Traceback" not in line


def test_redaction_keeps_args_and_masks_joined_secrets():
    logger, out = _logger("test.s1e.plain", logging.Formatter("%(message)s"))
    logger.info("hämtade %s (%d byte)", "/api/v1/public/exports/abc.123.hemlig?x=1", 42)
    logger.info("%s=%s", "password", "hemligt-värde")
    logger.info("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    logger.info("värden %(a)s", {"a": "/public/exports/xyz.1.sig"})
    lines = out.getvalue().splitlines()
    assert lines[0] == "hämtade /api/v1/public/exports/[DOLD]?x=1 (42 byte)"
    assert lines[1] == "password=[DOLD]"
    assert "abcdefghijklmnop" not in lines[2]
    assert lines[3] == "värden /public/exports/[DOLD]"
    assert redact("GET /api/v1/public/exports/a.b.c HTTP/1.1") == "GET /api/v1/public/exports/[DOLD] HTTP/1.1"
