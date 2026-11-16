"""Precisionsändring 3 (1.3.0): notiserna visar hela adressen.

Okända platser visas med gata och husnummer (postnumret tas bort), bostaden med sin adress,
privata platser med sitt namn eller sin adress. Bekräftade platser med namn som förut. Med
"dölj platser på låsskärmen" syns ingen plats alls på låsskärmen. Larm och månadsnotiser har
fortfarande inga platser.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import LocationType, NotificationKind, ReviewKind, ReviewState, TripSource
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.ha import messages, notifier
from app.services.ha.messages import place_label
from tests.test_ha_support import HOME, JONKOPING, NOW, OREBRO, fake_ha, recent_trip, setup_push  # noqa: F401

pytestmark = pytest.mark.new_defaults

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


# ----------------------------------------------------------------------------- etiketten


def test_unknown_place_shows_the_whole_address_without_postcode():
    street = M.KnownLocation(type="unknown", name="Lagergatan 7, 553 03 Jönköping",
                             address="Lagergatan 7, 553 03 Jönköping", confirmed=False)
    assert place_label(street, street.address) == "Lagergatan 7, Jönköping"
    assert place_label(None, OREBRO) == "Drottninggatan 12, Örebro"
    assert place_label(None, "Storgatan 5, SE-553 21 Jönköping") == "Storgatan 5, Jönköping"
    assert place_label(None, "Storgatan 5") == "Storgatan 5"
    assert place_label(None, None) == "okänd plats"


def test_unknown_place_with_map_name_keeps_name_and_whole_address():
    poi = M.KnownLocation(type="unknown", name="Dagab", company="Dagab", address="Lagergatan 7, 553 03 Jönköping",
                          city="Jönköping", confirmed=False)
    assert place_label(poi, poi.address) == "Dagab, Lagergatan 7, Jönköping"
    only_city = M.KnownLocation(type="unknown", name="Dagab", company="Dagab", address=None, city="Jönköping",
                                confirmed=False)
    assert place_label(only_city, None) == "Dagab, Jönköping"


def test_home_shows_its_address_or_its_name():
    home = M.KnownLocation(type="home", name="Hemmet", address=HOME, confirmed=True)
    assert place_label(home, HOME) == "Hemvägen 1, Hemby"
    no_address = M.KnownLocation(type="home", name="Hemmet", address=None, confirmed=True)
    assert place_label(no_address, None) == "Hemmet"
    unnamed = M.KnownLocation(type="home", name="", address=None, confirmed=True)
    assert place_label(unnamed, None) == "Hemmet"


def test_private_place_shows_its_name_or_its_address():
    named = M.KnownLocation(type="private", name="Förskolan", address=JONKOPING, confirmed=True)
    assert place_label(named, JONKOPING) == "Förskolan"
    unnamed = M.KnownLocation(type="private", name=JONKOPING, address=JONKOPING, confirmed=True)
    assert place_label(unnamed, JONKOPING) == "Östra Storgatan 5, Jönköping"


def test_confirmed_place_with_name_is_unchanged():
    confirmed = M.KnownLocation(type="customer", name="Acme AB", address=OREBRO, confirmed=True)
    assert place_label(confirmed, OREBRO) == "Acme AB"


# ----------------------------------------------------------------------------- i notisen


@pytest.fixture()
def sd(db, user, fake_ha):
    setup_push(db, user)
    settings_svc.update_section(db, user.id, "automation", {"active_since": SINCE.isoformat()}, allow_read_only=True)
    db.flush()
    return fake_ha


def live_trip(db, trips, start: str = HOME, end: str = OREBRO) -> M.LogicalTrip:
    trip = recent_trip(trips, start, end, source=TripSource.VOLVO_API, hours_ago=1.0)
    for s in trip.segments:
        s.raw_trip.created_at = trip.ended_at or NOW
    db.add(M.ReviewItem(vehicle_id=trip.vehicle_id, kind=ReviewKind.CATEGORY, dedupe_key=f"category:{trip.id}",
                        logical_trip_id=trip.id, title="Fråga", status=ReviewState.OPEN))
    db.flush()
    return trip


def question(db) -> M.NotificationRequest:
    (req,) = db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.kind.in_(notifier.SD_QUESTION_KINDS))
    ).scalars()
    return req


def test_question_shows_home_address_and_whole_address_of_unknown_place(db, user, trips, sd, monkeypatch):
    monkeypatch.setattr(autopilot, "purpose_candidates", lambda db, trip, *, user_id, limit=2: [])
    monkeypatch.setattr(autopilot, "can_remember", lambda db, trip, text: False)
    trips.location(HOME, name="Hemmet", type=LocationType.HOME)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    req = question(db)
    assert "Hemvägen 1, Hemby → Drottninggatan 12, Örebro" in req.message
    assert "123 45" not in req.message and "702 10" not in req.message


def test_hidden_on_lock_screen_shows_no_place(db, user, trips, sd, monkeypatch):
    monkeypatch.setattr(autopilot, "purpose_candidates", lambda db, trip, *, user_id, limit=2: [])
    monkeypatch.setattr(autopilot, "can_remember", lambda db, trip, text: False)
    settings_svc.update_section(db, user.id, "notifications", {"hide_locations_on_lock_screen": True})
    trips.location(HOME, name="Hemmet", type=LocationType.HOME)
    live_trip(db, trips)
    notifier.ensure_open_questions(db, NOW)
    req = question(db)
    assert req.message == "tis 15/9 · 4,2 mil. Tjänst eller privat?"
    for text in ("Hemvägen", "Hemby", "Drottninggatan", "Örebro"):
        assert text not in req.message
    assert "Drottninggatan 12, Örebro" in req.context["detail_message"]


def test_alerts_and_month_notices_still_have_no_places():
    alert = messages.alert("Hämtningen från Volvo fungerar inte", "Ingen ny data sedan i går.")
    month = messages.month_waiting("2026-11", [], [])
    for built in (alert, month):
        assert "gatan" not in built["message"] and not any(ch.isdigit() for ch in built["title"].split(":")[-1])
    assert NotificationKind.ALERT and "plats" not in alert["message"]
