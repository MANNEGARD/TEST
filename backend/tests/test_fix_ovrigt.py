"""Regressionstester för rättningar som inte hör till någon enskild modul."""

from __future__ import annotations

from sqlalchemy import select
from starlette.requests import Request

import app.models as M
from app.api.deps import client_ip
from app.models.enums import Category, DecisionSource, ReviewStatus
from app.services.learning import feedback
from app.services.trips import merge
from app.services.trips import segments as seg
from tests.conftest import reload_trip

IMPORTED = dict(category_source=DecisionSource.IMPORT, review_status=ReviewStatus.IMPORTED)


def _request(headers: dict[str, str], client: tuple[str, int] | None = ("192.0.2.10", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": client})


def test_client_ip_uses_the_value_added_by_the_proxy() -> None:
    # Klienten skickar själv ett påhittat värde; proxyn lägger till den verkliga adressen sist.
    request = _request({"X-Forwarded-For": "10.9.9.9, 203.0.113.7"})
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_single_value_and_fallback() -> None:
    assert client_ip(_request({"X-Forwarded-For": "203.0.113.7"})) == "203.0.113.7"
    assert client_ip(_request({})) == "192.0.2.10"
    assert client_ip(_request({}, client=None)) == ""


def test_undo_split_keeps_decisions_on_every_part(db, user, vehicle, actor, trips):
    a = trips.trip("A1, Stad", "B1, Stad", km=10, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    b = trips.trip("B1, Stad", "C1, Stad", km=10, gap_minutes=60, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    c = trips.trip("C1, Stad", "D1, Stad", km=10, gap_minutes=60, category=Category.WORK, purpose="Kundbesök", **IMPORTED)
    m = merge.merge_trips(db, [a, b, c], trigger="manual", actor=actor, acknowledge_gap=True)
    db.flush()
    left, right = merge.split_trip(db, reload_trip(db, m.id), after_index=1, actor=actor)
    db.flush()
    left, right = reload_trip(db, left.id), reload_trip(db, right.id)
    # Användaren fördelar en delsträcka i vänstra delen och anger att högra delen var privat.
    raw_b = seg.ordered_raws(left)[1]
    seg.allocate_raw_trip(db, left, raw_b.id, [{"distance_km": 4, "category": "private"},
                                               {"distance_km": 6, "category": "work"}], "user")
    feedback.apply_decision(db, right, user_id=user.id, actor=actor, category=Category.PRIVATE)
    raw_c = seg.ordered_raws(right)[0].id
    db.commit()
    db.expire_all()
    op = db.execute(select(M.TripMerge).where(M.TripMerge.operation == "split")).scalar_one()
    (src,) = merge.undo_operation(db, op, actor=actor)
    db.flush()
    src = reload_trip(db, src.id)
    c_cat = {seg.segment_category(src, s) for s in src.segments if s.raw_trip_id == raw_c}
    # Privat sträcka får aldrig bli tjänst för att en uppdelning ångras – oavsett vilken del.
    assert c_cat == {Category.PRIVATE}, c_cat
