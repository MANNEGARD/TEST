"""1.3.0 DEL 3c – knappen för AI-genomgång: planen (POST /trips/ai-review/plan) och bedömningen
av en resa i taget (POST /trips/{id}/classify) (byggkontraktet 6.D, tilläggskontraktet 1.A rad 4, 3.I)."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models as M
from app.models.enums import (
    ApprovalSource,
    Category,
    DecisionSource,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TripSource,
)
from app.security import passwords
from app.services.classify import engine
from app.services.learning import patterns, recompute
from tests.conftest import reload_trip
from tests.v130_ai_stod import (
    CUSTOMER,
    HOME,
    OFFICE,
    FakeProvider,
    answer,
    full_context,
    live,
    local,
    places,
    self_driving,
)

API = "/api/v1"


@pytest.fixture()
def world(db, user, trips):
    full_context(db, user)
    return places(trips)


def plan(client, **body):
    return client.post(f"{API}/trips/ai-review/plan", json=body)


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(engine, "get_provider", lambda db, user_id: provider)


# ----------------------------------------------------------------------------- planen


def test_plan_selected_skips_decided_and_reports_provider(client, db, user, trips, world, monkeypatch):
    open_ = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 15, 9, 0))
    decided = trips.trip(OFFICE, CUSTOMER, category=Category.WORK, purpose="Service", start_at=local(2026, 9, 14, 9))
    imported = trips.trip(OFFICE, CUSTOMER, category=Category.WORK, purpose="Service",
                          review_status=ReviewStatus.IMPORTED, start_at=local(2026, 9, 13, 9))
    db.commit()
    r = plan(client, trip_ids=[str(open_.id), str(decided.id), str(imported.id)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"trip_ids": [str(open_.id)], "total_eligible": 1, "skipped_decided": 2, "skipped_other": 0,
                    "cap": 25, "provider": None, "provider_label": None, "context_level": "full"}
    use_provider(monkeypatch, FakeProvider(answer("X")))
    body = plan(client, trip_ids=[str(open_.id)]).json()
    assert (body["provider"], body["provider_label"]) == ("anthropic", "Anthropic")


def test_plan_pending_cap_and_order(client, db, user, trips, world):
    ids = [live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 1, 8) + timedelta(hours=i)).id for i in range(30)]
    trips.trip(OFFICE, CUSTOMER, category=Category.WORK, purpose="Service", start_at=local(2026, 9, 3, 8))
    db.commit()
    body = plan(client, pending=True).json()
    assert body["total_eligible"] == 30 and body["cap"] == 25
    assert body["trip_ids"] == [str(i) for i in ids[:25]]  # de äldsta först


def test_plan_pending_includes_unconfirmed_purpose_with_open_item(client, db, user, vehicle, trips, world):
    t = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 15, 9, 0), category=Category.WORK,
             review_status=ReviewStatus.AUTO, purpose="Förslag", purpose_confirmed=False)
    t.review_status = ReviewStatus.APPROVED  # avgjord kategori men obekräftat ärende räknas inte (godkänd)
    db.add(M.ReviewItem(vehicle_id=vehicle.id, kind=ReviewKind.PURPOSE, dedupe_key=f"purpose:{t.id}",
                        logical_trip_id=t.id, title="Syfte", status=ReviewState.OPEN))
    db.commit()
    assert plan(client, pending=True).json()["trip_ids"] == []  # godkända rörs aldrig


def test_plan_validation_and_ownership(client, db, user, trips, world):
    assert plan(client).status_code == 400 and plan(client).json()["code"] == "invalid_selection"
    t = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 15, 9, 0))
    db.commit()
    both = plan(client, trip_ids=[str(t.id)], pending=True)
    assert both.status_code == 400 and both.json()["detail"] == "Välj resor eller alla som väntar i Granskning."
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password("x" * 12))
    db.add(other)
    db.flush()
    ov = M.Vehicle(owner_user_id=other.id, name="Annans bil", make="Volvo", model="V60", is_default=True)
    db.add(ov)
    db.flush()
    from tests.conftest import TripFactory

    foreign = TripFactory(db, other, ov).trip("Annanväg 1, Annanby", "Annanväg 2, Annanby")
    db.commit()
    assert plan(client, trip_ids=[str(foreign.id)]).status_code == 404


def test_plan_excludes_gap_fill(client, db, user, trips, world):
    t = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 15, 9, 0))
    t.source = TripSource.GAP_FILL
    db.commit()
    assert plan(client, pending=True).json()["trip_ids"] == []
    assert plan(client, trip_ids=[str(t.id)]).json()["skipped_other"] == 1


# ----------------------------------------------------------------------------- en resa i taget


def test_classify_skips_decided_trip_without_touching_it(client, db, user, trips, world, monkeypatch):
    fake = FakeProvider(answer("Något annat"))
    use_provider(monkeypatch, fake)
    t = trips.trip(OFFICE, CUSTOMER, category=Category.WORK, purpose="Service", start_at=local(2026, 9, 14, 9))
    db.commit()
    body = client.post(f"{API}/trips/{t.id}/classify").json()
    assert body["outcome"] == "skipped_decided" and body["changed"] is False and body["ai_used"] is False
    assert body["note"].startswith("Resan är redan avgjord")
    assert fake.documents == [] and reload_trip(db, t.id).purpose == "Service"


def test_classify_updated_unchanged_and_unavailable(client, db, user, trips, world, monkeypatch, job_queue):
    use_provider(monkeypatch, FakeProvider(answer("Kundbesök hos Exempel Åkeri AB")))
    t = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 15, 9, 0))
    db.commit()
    body = client.post(f"{API}/trips/{t.id}/classify").json()
    assert body["outcome"] == "updated" and body["ai_used"] is True and "assessment" in body
    assert body["trip"]["purpose"] == "Kundbesök hos Exempel Åkeri AB."
    assert recompute.JOB in [n for n, _ in job_queue]  # övriga resors förslag räknas om efteråt
    again = client.post(f"{API}/trips/{t.id}/classify").json()
    assert again["outcome"] == "unchanged" and "changed" in again and again["note"] is None
    use_provider(monkeypatch, FakeProvider(fail=True))
    t2 = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 16, 9, 0))
    db.commit()
    assert client.post(f"{API}/trips/{t2.id}/classify").json()["outcome"] == "ai_unavailable"


def test_owner_written_purpose_is_not_overwritten(client, db, user, trips, world, monkeypatch):
    use_provider(monkeypatch, FakeProvider(answer("Kundbesök hos Exempel Åkeri AB")))
    t = live(trips, OFFICE, CUSTOMER, start_at=local(2026, 9, 15, 9, 0), category=Category.WORK,
             review_status=ReviewStatus.PROPOSED, purpose="Mitt eget ärende", purpose_confirmed=True,
             category_source=DecisionSource.USER)
    db.commit()
    client.post(f"{API}/trips/{t.id}/classify")
    t = reload_trip(db, t.id)
    assert t.purpose == "Mitt eget ärende" and t.purpose_confirmed


def test_classify_auto_approves_in_self_driving_and_cancels_question(client, db, user, vehicle, trips, world,
                                                                     monkeypatch):
    self_driving(db, user)
    use_provider(monkeypatch, None)
    patterns.set_manual_rule(db, user.id, origin_id=None, dest_id=world["customer"].id, category=Category.WORK,
                             purpose="Service hos Exempel Åkeri AB")
    t = live(trips, HOME, CUSTOMER, start_at=local(2026, 9, 15, 9, 0))
    db.add(M.NotificationRequest(user_id=user.id, kind="classify", public_id="aiq1", logical_trip_id=t.id,
                                 vehicle_id=vehicle.id, title="Fråga", message="?", status="sent",
                                 dedupe_key=f"classify:{t.id}"))
    db.commit()
    body = client.post(f"{API}/trips/{t.id}/classify").json()
    assert body["outcome"] == "auto_approved" and body["ai_used"] is False
    t = reload_trip(db, t.id)
    assert (t.review_status, t.approval_source) == (ReviewStatus.APPROVED, ApprovalSource.RULE)
    db.expire_all()
    assert db.query(M.NotificationRequest).filter_by(public_id="aiq1").one().status == "cancelled"
