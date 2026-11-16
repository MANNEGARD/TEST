"""1.3.0 DEL 6: månadens underlag i förväg ("Skapa underlaget nu").

``run_for(force=True)`` för innevarande månad hoppar bara över väntan på månadsslutet; övriga
kontroller gäller. Filen gäller hela kalendermånaden och visar när den skapades. Vid
månadsskiftet kontrolleras månaden igen: oförändrat → ingenting (ingen fil, ingen notis),
ändrat → nytt underlag och notisen "uppdaterad", och en obesvarad fråga om en tjänsteresa
ger den vanliga "väntar"-notisen.

Klockan i testerna: måndag 16 november 2026 kl. 10:12 svensk tid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, NotificationKind, ReviewKind, ReviewState, ReviewStatus, TripSource
from app.services import settings as settings_svc
from app.services.export import month_close, simple
from app.services.export import service as export_svc
from tests.conftest import reload_trip
from tests.test_ha_support import setup_pull

pytestmark = pytest.mark.new_defaults

API = "/api/v1"
NOV = date(2026, 11, 1)
NOW = datetime(2026, 11, 16, 9, 12, tzinfo=UTC)  # 10:12 svensk tid
END = datetime(2026, 11, 30, 23, 0, tzinfo=UTC)  # 1 december 00:00 svensk tid
HOME = "Hemvägen 1, 111 11 Exempelstad"
CUSTOMER = "Kundvägen 5, 222 22 Exempelby"


def at(day: int, hour: int, minute: int = 0, month: int = 12) -> datetime:
    """Svensk vintertid (UTC+1) som UTC."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=1)


@pytest.fixture()
def sd(db, user, vehicle):
    """Självgående läge, notiser via utkorgen och en bil som har skickat data."""
    setup_pull(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": "2020-01-01T00:00:00+00:00"},
                                allow_read_only=True)
    obs(db, vehicle, fetched=datetime(2026, 11, 2, 6, 0, tzinfo=UTC), odo=12000)
    db.flush()


def obs(db, vehicle, *, fetched: datetime, odo: float | None = None, odo_at: datetime | None = None):
    row = M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=fetched, last_seen_at=fetched, odometer_km=odo,
                               odometer_reported_at=odo_at or fetched, content_hash=uuid.uuid4().hex,
                               processed_at=fetched)
    db.add(row)
    db.flush()
    return row


def final_fetch(db, vehicle) -> None:
    """En lyckad hämtning efter månadsslutet (mätarställningen rapporterad efter slutet)."""
    obs(db, vehicle, fetched=END + timedelta(hours=2), odo=99999, odo_at=END + timedelta(hours=1))


def work(trips, *, day: int = 10, km: float = 20.0, **kw: Any) -> M.LogicalTrip:
    return trips.trip(HOME, CUSTOMER, category=Category.WORK, purpose="Kundbesök Exempel AB", km=km,
                      start_at=datetime(2026, 11, day, 7, 0, tzinfo=UTC), source=TripSource.VOLVO_API, **kw)


def pending_work(db, trips, *, day: int = 25) -> M.LogicalTrip:
    """En tjänsteresa som väntar på ägarens svar (öppen fråga)."""
    trip = trips.trip(HOME, CUSTOMER, category=Category.WORK, review_status=ReviewStatus.PROPOSED, km=34,
                      start_at=datetime(2026, 11, day, 7, 0, tzinfo=UTC), source=TripSource.VOLVO_API)
    db.add(M.ReviewItem(vehicle_id=trip.vehicle_id, kind=ReviewKind.CATEGORY, dedupe_key=f"category:{trip.id}",
                        logical_trip_id=trip.id, title="Godkänn förslag", status=ReviewState.OPEN))
    db.flush()
    return trip


def run(db, user, vehicle, now: datetime = NOW, **kw: Any) -> month_close.MonthStatus:
    return month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=NOV, now=now, **kw)


def ev(db, user, vehicle, now: datetime = NOW) -> month_close.MonthStatus:
    return month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=NOV, now=now)


def exports(db) -> list[M.ExportJob]:
    return list(db.execute(select(M.ExportJob).order_by(M.ExportJob.created_at)).scalars())


def monthly(db) -> list[M.NotificationRequest]:
    """Novembers månadsnotiser (oktober kontrolleras också av run_due, men är tom)."""
    return [n for n in db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.MONTHLY)
        .order_by(M.NotificationRequest.created_at)
    ).scalars() if n.title.startswith("November")]


def november(db) -> M.MonthClose:
    return db.execute(select(M.MonthClose).where(M.MonthClose.month == "2026-11")).scalar_one()


def due(db, now: datetime) -> dict[str, Any]:
    out = month_close.run_due(db, now=now)
    db.flush()
    return out


def header_cells(path: str) -> dict[str, str]:
    ws = openpyxl.load_workbook(path)["Körjournal"]
    out: dict[str, str] = {}
    for row in ws.iter_rows(min_row=1, max_row=5):
        cells = [c.value for c in row]
        for i, value in enumerate(cells[:-1]):
            if isinstance(value, str) and isinstance(cells[i + 1], str):
                out.setdefault(value, cells[i + 1])
    return out


# ----------------------------------------------------------------------------- trycket


def test_current_month_is_open_before_the_press(db, user, vehicle, trips, sd):
    work(trips)
    status = ev(db, user, vehicle)
    assert status.state == "open" and "month_not_over" in {b.code for b in status.blockers}
    assert status.closed_early is False and status.export_created_at is None


def test_press_creates_the_file_now_and_says_so(db, user, vehicle, trips, sd):
    work(trips)
    status = run(db, user, vehicle, force=True)
    assert status.state == "exported" and status.closed_early is True
    (job,) = exports(db)
    assert job.origin == "month_close" and job.created_at == NOW
    record = db.execute(select(M.MonthClose)).scalar_one()
    assert record.rows_fingerprint == job.rows_fingerprint and record.export_id == job.id
    (notice,) = monthly(db)
    assert notice.title == "November: underlaget skapat"
    assert "Skapat 16/11 innan månaden var slut – ändras något kommer ett nytt underlag efter månadsskiftet." \
        in notice.message
    # Filen visar ärligt när den skapades; perioden är hela kalendermånaden.
    cells = header_cells(job.file_path)
    assert cells["Period"] == "November 2026"
    assert cells["Skapad"] == "16 november 2026 kl. 10:12, innan månaden var slut"
    assert job.filename.endswith("november 2026.xlsx")

    # F4: kortet visar filen även innan månaden är slut.
    later = ev(db, user, vehicle, now=NOW + timedelta(hours=1))
    assert later.state == "exported" and later.closed_early is True and later.export_created_at == NOW
    assert later.export_id == job.id


def test_skapad_row_only_before_the_period_ends(db, user, vehicle, trips):
    work(trips)
    tz = settings_svc.user_timezone(db, user.id)
    start, end = month_close._bounds(NOV, tz)
    early, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz, generated_at=NOW)
    late, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz,
                                              generated_at=END + timedelta(days=1))
    assert early.created_text and late.created_text == ""
    assert export_svc.rows_fingerprint(early) == export_svc.rows_fingerprint(late)
    assert simple.created_text(NOW, start, end + timedelta(days=1), tz).endswith("innan perioden var slut")


def test_waiting_press_creates_nothing(db, user, vehicle, trips, sd):
    work(trips)
    pending_work(db, trips)
    status = run(db, user, vehicle, force=True)
    assert status.state == "waiting" and "open_questions" in {b.code for b in status.blockers}
    assert exports(db) == [] and monthly(db) == []


def test_two_presses_without_change_give_one_file_and_with_change_a_new_one(db, user, vehicle, trips, sd):
    work(trips)
    run(db, user, vehicle, force=True)
    run(db, user, vehicle, now=NOW + timedelta(minutes=5), force=True)
    assert len(exports(db)) == 1 and len(monthly(db)) == 1
    work(trips, day=12, km=34)
    status = run(db, user, vehicle, now=NOW + timedelta(minutes=10), force=True)  # F5: direkt, inte först "stale"
    first, second = exports(db)
    assert status.state == "exported" and status.export_id == second.id
    assert first.superseded_by_id == second.id


def test_api_press_for_the_current_month(client, db, user, vehicle, trips, sd):
    from app.db import utcnow

    now = utcnow()
    tz = settings_svc.user_timezone(db, user.id)
    month = now.astimezone(tz).strftime("%Y-%m")
    trips.trip(HOME, CUSTOMER, category=Category.WORK, purpose="Kundbesök Exempel AB", km=20,
               start_at=now - timedelta(minutes=90), source=TripSource.VOLVO_IMPORT)
    db.commit()
    r = client.post(f"{API}/month-close/{month}/run", params={"force": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "exported" and body["closed_early"] is True and body["export_created_at"]
    listed = client.get(f"{API}/month-close").json()["months"][0]
    assert listed["month"] == month and listed["state"] == "exported" and listed["closed_early"] is True


# ----------------------------------------------------------------------------- vid månadsskiftet


def test_month_end_unchanged_gives_nothing_more(db, user, vehicle, trips, sd):
    work(trips)
    run(db, user, vehicle, force=True)
    final_fetch(db, vehicle)
    due(db, at(20, 12, month=11))
    assert november(db).last_checked_at == NOW  # november kontrolleras inte under månaden
    due(db, at(1, 3))
    assert november(db).last_checked_at == NOW  # inom väntetiden efter månadsslutet
    due(db, at(1, 7))
    assert november(db).last_checked_at == at(1, 7) and november(db).state == "exported"
    assert len(exports(db)) == 1 and len(monthly(db)) == 1  # ingen fil i onödan, ingen dubbel "klar"-notis


def test_month_end_with_a_new_trip_gives_an_updated_file(db, user, vehicle, trips, sd):
    work(trips)
    run(db, user, vehicle, force=True)
    work(trips, day=25, km=34)
    final_fetch(db, vehicle)
    due(db, at(1, 7))
    assert november(db).state == "stale" and len(exports(db)) == 1
    due(db, at(1, 8, 1))
    first, second = exports(db)
    assert first.superseded_by_id == second.id
    notices = monthly(db)
    assert len(notices) == 2 and notices[-1].title == "November uppdaterad"
    assert notices[-1].message.startswith("+1 resa, +34 km")


def test_month_end_waits_for_the_final_fetch(db, user, vehicle, trips, sd):
    work(trips)
    run(db, user, vehicle, force=True)
    work(trips, day=25, km=34)
    due(db, at(1, 7))
    due(db, at(1, 8, 1))
    assert len(exports(db)) == 1  # väntar på bilens sista uppgifter
    final_fetch(db, vehicle)
    due(db, at(1, 9, 2))
    assert len(exports(db)) == 2


def test_empty_month_pressed_stays_quiet(db, user, vehicle, sd):
    status = run(db, user, vehicle, force=True)
    assert status.state == "empty" and exports(db) == []
    assert len(monthly(db)) == 1 and monthly(db)[0].title == "November: inga tjänsteresor"
    final_fetch(db, vehicle)
    due(db, at(1, 7))
    assert len(monthly(db)) == 1


def test_closed_early_then_an_unanswered_work_trip_gives_a_waiting_notice(db, user, vehicle, trips, sd):
    work(trips)
    run(db, user, vehicle, force=True)
    trip = pending_work(db, trips)
    final_fetch(db, vehicle)
    due(db, at(1, 7))
    assert november(db).state == "exported" and len(exports(db)) == 1
    waiting = [n for n in monthly(db) if n.title == "November väntar"]
    assert len(waiting) == 1 and "väntar på ditt svar" in waiting[0].message
    due(db, at(1, 8, 1))
    assert len([n for n in monthly(db) if n.title == "November väntar"]) == 1  # var 3:e dygn

    # Frågan besvaras: avtrycket ändras → nytt underlag och "uppdaterad".
    t = reload_trip(db, trip.id)
    t.review_status = ReviewStatus.APPROVED
    t.purpose, t.purpose_confirmed = "Kundbesök Exempel AB", True
    for item in db.execute(select(M.ReviewItem).where(M.ReviewItem.logical_trip_id == t.id)).scalars():
        item.status = ReviewState.RESOLVED
    db.flush()
    due(db, at(1, 9, 2))
    due(db, at(1, 10, 3))
    assert len(exports(db)) == 2
    assert monthly(db)[-1].title == "November uppdaterad"


def test_closed_after_month_end_gets_no_waiting_notice(db, user, vehicle, trips, sd):
    """Regression: en månad som stängdes efter månadsslutet får ingen "väntar"-notis i efterhand."""
    work(trips)
    final_fetch(db, vehicle)
    status = run(db, user, vehicle, now=at(1, 7))
    assert status.state == "exported" and status.closed_early is False
    pending_work(db, trips)
    due(db, at(1, 9))
    due(db, at(2, 9))
    assert [n.title for n in monthly(db)] == ["November klart"]
