"""1.4.1 – lunchregeln får inte krascha på en resa utan starttid.

Bilen rapporterar ingen starttid. Lyckas uppskattningen inte har journalresan ``started_at``
tom, och lunchregeln läste då ``start_latest`` på journalresan – ett fält som bara finns på
råresan. Undantaget stoppade hela hämtningen från Volvo ("Ett oväntat fel inträffade"):
varken avläsningen eller resan sparades, och felet upprepades vid varje hämtning.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.enums import ApprovalSource, LocationType
from app.services import settings as settings_svc
from app.services.classify import lunch
from tests.conftest import reload_trip
from tests.v130_ai_stod import CAFE, OFFICE, decide, live, local, places, run, self_driving

MON = (2026, 9, 14)


@pytest.fixture()
def setup(db, user, trips):
    settings_svc.update_section(db, user.id, "lunch", {"enabled": True})
    self_driving(db, user)
    locs = places(trips)
    cafe = trips.location(CAFE, name="Kafé Exempel", type=LocationType.UNKNOWN, confirmed=False)
    cafe.osm_class, cafe.place_kind = "amenity=cafe", "never"
    db.flush()
    return locs


def without_start(db, trip, *, latest=None):
    """Resan som den ser ut när bilen inte gett någon starttid och uppskattningen uteblivit."""
    trip = reload_trip(db, trip.id)
    raw = trip.segments[0].raw_trip
    trip.started_at = None
    raw.started_at = None
    raw.start_latest = latest
    db.flush()
    return reload_trip(db, trip.id)


def test_trip_without_start_time_does_not_crash_the_lunch_rule(db, user, vehicle, trips, setup):
    u = live(trips, OFFICE, CAFE, km=3.0, minutes=4, start_at=local(*MON, 11, 52))
    u = without_start(db, u)
    # Före rättningen: AttributeError: 'LogicalTrip' object has no attribute 'start_latest'.
    assert lunch.match(db, u, user_id=user.id) is None
    assert decide(db, user, u).source != ApprovalSource.LUNCH
    run(db, user, vehicle)  # hela autopiloten går igenom med resan i journalen


def test_latest_possible_start_from_the_raw_trip_is_used(db, user, vehicle, trips, setup):
    u = live(trips, OFFICE, CAFE, km=3.0, minutes=4, start_at=local(*MON, 11, 52))
    start = reload_trip(db, u.id).started_at
    u = without_start(db, u, latest=start)
    assert lunch._start(u) == start
    assert lunch._outbound(db, u, user_id=user.id, cfg=lunch.config(db, user.id)) is not None
    # Utan någon gräns alls gäller sluttiden.
    u = without_start(db, u, latest=None)
    assert lunch._start(u) == u.ended_at
    # En känd starttid går alltid först.
    u.started_at = start - timedelta(minutes=1)
    assert lunch._start(u) == start - timedelta(minutes=1)
