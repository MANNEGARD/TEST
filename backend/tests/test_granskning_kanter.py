"""Granskningsfynd i området "kanter" (1.1.0): låsskärmens knappar, månadsnotisen med den
fullständiga arbetsboken och "ingen telefon vald" i läget "Jag godkänner allt själv".

Inga nätverksanrop; alla data är påhittade.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.enums import Category, IntegrationStatus, NotificationKind, NotificationStatus, TripSource
from app.services import integrations
from app.services import settings as settings_svc
from app.services.ha import notifier
from tests.test_ha_support import NOW, fake_ha, setup_pull  # noqa: F401
from tests.test_s1_manadsskifte import (
    CUSTOMER,
    DAY,
    OTHER,
    exports,
    final_fetch,
    monthly,
    run,
    sd,  # noqa: F401
    work,
)
from tests.test_s2_larm import calendar, tick
from tests.test_s2_telefon import home, keys, live_trip, questions, suggest
from tests.test_s2_telefon import sd as sd_push  # noqa: F401

# ----------------------------------------------------------------------------- 1. låsskärmen


@pytest.mark.new_defaults
def test_lock_screen_hides_suggestion_in_classify_buttons(db, user, trips, sd_push, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Dagab"])
    settings_svc.update_section(db, user.id, "notifications", {"hide_locations_on_lock_screen": True})
    home(trips)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    assert "WORKP0" in keys(req) and "WORKA0" in keys(req)
    phone = notifier.service_data(req, db=db)
    assert "Dagab" not in repr(phone)
    # Det som sparas och det som webbappen visar är fortfarande det riktiga ärendet.
    picks = {a["key"]: a for a in req.actions}
    assert picks["WORKP0"]["purpose"] == "Kundbesök Dagab"
    web = {a["key"]: a for a in notifier.resolved_actions(db, req, web=True)}
    assert web["WORKP0"]["title"] == "Tjänst: Kundbesök Dagab"


@pytest.mark.new_defaults
def test_lock_screen_hides_candidates_in_purpose_buttons(db, user, trips, sd_push, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Dagab", "Montage Acme"])
    settings_svc.update_section(db, user.id, "notifications", {"hide_locations_on_lock_screen": True})
    from app.models.enums import ReviewKind

    live_trip(db, trips, kinds=(ReviewKind.PURPOSE,), category=Category.WORK)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    phone = notifier.service_data(req, db=db)
    assert "Dagab" not in repr(phone) and "Acme" not in repr(phone)
    web = [a["title"] for a in notifier.resolved_actions(db, req, web=True)]
    assert "Kundbesök Dagab" in web and "Montage Acme" in web


@pytest.mark.new_defaults
def test_lock_screen_off_keeps_suggestion_in_buttons(db, user, trips, sd_push, monkeypatch):
    suggest(monkeypatch, ["Kundbesök Dagab"])
    settings_svc.update_section(db, user.id, "notifications", {"hide_locations_on_lock_screen": False})
    home(trips)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    (req,) = questions(db)
    assert "Tjänst: Kundbesök Dagab" in repr(notifier.service_data(req, db=db))


# ----------------------------------------------------------------------------- 2. månadsnotisen


@pytest.mark.new_defaults
def test_month_ready_counts_work_trips_with_detailed_layout(db, user, vehicle, trips, sd):
    settings_svc.update_section(db, user.id, "export", {"layout": "detailed"})
    final_fetch(db, vehicle)
    work(trips)
    for h in (3, 5):
        trips.trip(CUSTOMER, OTHER, category=Category.PRIVATE, km=12.0, start_at=DAY + timedelta(hours=h),
                   source=TripSource.VOLVO_API)
    assert run(db, user, vehicle).state == "exported"
    (job,) = exports(db)
    assert job.summary["layout"] == "detailed"
    ready = monthly(db)[-1]
    assert ready.title == "Oktober klart"
    assert ready.message.startswith("1 tjänsteresa, 2 mil"), ready.message


@pytest.mark.new_defaults
def test_month_updated_shows_km_difference_with_detailed_layout(db, user, vehicle, trips, sd):
    settings_svc.update_section(db, user.id, "export", {"layout": "detailed"})
    final_fetch(db, vehicle)
    work(trips)
    from tests.test_s1_manadsskifte import AFTER

    run(db, user, vehicle)
    work(trips, at=DAY + timedelta(days=2), km=34.0)
    for hours in (2, 3, 25):
        run(db, user, vehicle, now=AFTER + timedelta(hours=hours))
    update = monthly(db)[-1]
    assert update.title == "Oktober uppdaterad" and update.message.startswith("+1 resa, +34 km"), update.message


# ----------------------------------------------------------------------------- 3. ingen telefon, manuellt läge


def test_no_phone_never_drops_alert_in_manual_mode(db, user):
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    conn = setup_pull(db, user)
    integrations.update_config(conn, {"notify_service": None})
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=2))
    tick(db, user, NOW)
    error = "Ingen notify.mobile_app-tjänst är vald i DriveLog eller i integrationen."
    for i in range(notifier.MAX_PULL_FAILURES + 5):
        items = notifier.outbox(db, user.id, NOW + timedelta(seconds=30 * i))
        assert len(items) == 1, f"försvann efter {i} hämtningar"
        notifier.outbox_ack(db, user.id, delivered=[], failed=[{"id": items[0]["id"], "error": error}])
    integrations.update_config(conn, {"notify_service": "mobile_app_x"})
    items = notifier.outbox(db, user.id, NOW + timedelta(minutes=15))
    assert len(items) == 1 and items[0]["service"] == "mobile_app_x"
    from sqlalchemy import select

    import app.models as M

    (row,) = db.execute(select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.ALERT)).scalars()
    assert row.status != NotificationStatus.FAILED and (row.delivery_attempts or 0) == 0


def test_other_pull_errors_still_count_in_manual_mode(db, user):
    settings_svc.update_section(db, user.id, "automation", {"mode": "manual"})
    conn = setup_pull(db, user)
    integrations.update_config(conn, {"notify_service": None})
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=2))
    tick(db, user, NOW)
    for i in range(notifier.MAX_PULL_FAILURES):
        items = notifier.outbox(db, user.id, NOW + timedelta(seconds=30 * i))
        notifier.outbox_ack(db, user.id, delivered=[], failed=[{"id": items[0]["id"], "error": "Tjänsten svarade inte."}])
    assert notifier.outbox(db, user.id, NOW + timedelta(minutes=15)) == []

