"""Sammanfogningen av 1.1.0: hela kedjan med telefonens svar (byggkontraktet F, "hela kedjan").

Bilens avläsningar (FakeCar) → resa → adressen ur en låtsad karttjänst (MockTransport) →
plats → beslutet: fråga i telefonen, svar från telefonen, hemresa (T), historik (H), egen
regel (R) och antagande (A) → månadsskiftet → Excel → signerad länk som öppnas utan
inloggning. Inga nätverksanrop. Alla adresser och namn är påhittade.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx
import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.models as M
from app.models.enums import ApprovalSource, Category, LocationType, NotificationKind, ReviewStatus
from app.services import settings as settings_svc
from app.services.classify import autopilot
from app.services.export import links, month_close
from app.services.export.service import export_path
from app.services.ha import notifier, responses
from app.services.places import geocode
from app.services.places import registry as places
from app.services.stats import common
from tests.test_ha_support import setup_pull
from tests.test_s1_adresser import DAGAB, PROVIDER, Recorder
from tests.test_volvo_support import T0, A, B, C_, FakeCar, connect, poll, poll_every

pytestmark = pytest.mark.new_defaults

API = "/api/v1"
HOME_ADDRESS = "Hemvägen 1, 111 11 Exempelstad"
SHOP = {
    "place_id": 2,
    "category": "shop",
    "type": "doityourself",
    "name": "Exempelbygg",
    "display_name": "Exempelbygg, 9, Verkstadsgatan, Exempelstad, 123 46, Sverige",
    "address": {"house_number": "9", "road": "Verkstadsgatan", "city": "Exempelstad", "postcode": "123 46",
                "country": "Sverige"},
}
OFFICE_SUPPLY = {
    "place_id": 3,
    "category": "shop",
    "type": "stationery",
    "name": "Exempelpapper",
    "display_name": "Exempelpapper, 2, Pappersvägen, Exempelstad, 123 47, Sverige",
    "address": {"house_number": "2", "road": "Pappersvägen", "city": "Exempelstad", "postcode": "123 47",
                "country": "Sverige"},
}
D_ = (59.000, 15.180)
PURPOSE = "Inköp av byggmaterial till kund"
RULE_PURPOSE = "Inköp av kontorsmaterial"


def day(n: int, hour: float = 9) -> datetime:
    """Dag n i oktober (T0 = måndag 5 oktober 07:00 UTC = 09:00 svensk tid)."""
    return T0 + timedelta(days=n - 5, hours=hour - 9)


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(geocode, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(geocode, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(geocode, "_last_call", None)

    def no_dns(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - ska aldrig anropas
        raise AssertionError("DNS-uppslag i testet")

    monkeypatch.setattr(geocode, "_getaddrinfo", no_dns)


def drive(db, vehicle, car: FakeCar, start: datetime, to: tuple[float, float]) -> datetime:
    """En resa på 14 km med mätarställning under färd. Returnerar ankomsttiden."""
    poll_every(db, vehicle, car, start, start + timedelta(minutes=5))
    car.moving(5, start + timedelta(minutes=9.5))
    poll(db, vehicle, car, start + timedelta(minutes=10))
    car.moving(6, start + timedelta(minutes=14.5))
    poll(db, vehicle, car, start + timedelta(minutes=15))
    car.drive_to(to, 3, start + timedelta(minutes=18))
    poll_every(db, vehicle, car, start + timedelta(minutes=20), start + timedelta(minutes=40))
    return start + timedelta(minutes=18)


def lookup(db, monkeypatch, body: dict[str, Any], now: datetime) -> Recorder:
    db.commit()
    rec = Recorder(body)
    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(rec))
    geocode.run_pending(now=now)
    db.expire_all()
    return rec


def last_trip(db) -> M.LogicalTrip:
    return db.execute(select(M.LogicalTrip).order_by(M.LogicalTrip.sort_time.desc())).scalars().first()


def ask(db, user, now: datetime) -> M.NotificationRequest:
    """Tick-stegen för frågor: skapa (ensure_open_questions) och leverera."""
    notifier.ensure_open_questions(db, now)
    notifier.deliver_user(db, user.id, now)
    db.flush()
    (req,) = db.execute(
        select(M.NotificationRequest).where(
            M.NotificationRequest.kind.in_((NotificationKind.CLASSIFY, NotificationKind.PURPOSE)),
            M.NotificationRequest.status.in_(notifier.OPEN_STATUSES),
        )
    ).scalars().all()
    return req


def answer(db, user, req, action: str, now: datetime, text: str | None = None, *, source: str = "ha",
           always: bool = False) -> dict[str, Any]:
    out = responses.handle(db, user_id=user.id, public_id=req.public_id, action=action, reply_text=text,
                           idempotency_key=f"{req.public_id}-{action}", source=source, now=now, always=always)
    db.flush()
    db.expire_all()
    return out


def test_whole_chain_with_phone_answers(db, user, vehicle, engine, monkeypatch):
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True, "geocoder_url": PROVIDER})
    settings_svc.update_section(db, user.id, "automation", {"active_since": (T0 - timedelta(days=1)).isoformat()},
                                allow_read_only=True)
    home = places.resolve_address(db, user.id, HOME_ADDRESS, created_from="user")
    home.type, home.confirmed, home.name = LocationType.HOME, True, "Hemmet"
    home.latitude, home.longitude, home.coordinate_source = A[0], A[1], "user"
    setup_pull(db, user)
    connect(db, user, vehicle)
    car = FakeCar()

    # 1. Måndag: hem → byggvaruhuset. Adressen slås upp, ingen regel gäller → EN fråga.
    arrived = drive(db, vehicle, car, day(5), B)
    rec = lookup(db, monkeypatch, SHOP, arrived + timedelta(minutes=5))
    assert len(rec.requests) == 1
    out = last_trip(db)
    assert out.end_address == "Verkstadsgatan 9, 123 46 Exempelstad"
    assert out.review_status != ReviewStatus.APPROVED
    req = ask(db, user, arrived + timedelta(minutes=30))
    assert req.kind == NotificationKind.CLASSIFY and req.logical_trip_id == out.id
    assert len(req.actions) <= 6 and "WORKTEXT" in [a["key"] for a in req.actions]

    # 2. Svaret från telefonen: tjänst med eget ärende.
    res = answer(db, user, req, "WORKTEXT", arrived + timedelta(minutes=40), PURPOSE)
    assert res["outcome"] == "applied", res
    out = db.get(M.LogicalTrip, out.id)
    assert (out.category, out.purpose, out.review_status) == (Category.WORK, PURPOSE, ReviewStatus.APPROVED)
    assert out.approval_source == ApprovalSource.USER

    # 3. Hemresan samma dag godkänns av sig själv (T).
    drive(db, vehicle, car, day(5, 13), A)
    back = last_trip(db)
    assert back.review_status == ReviewStatus.APPROVED and back.approval_source == ApprovalSource.RETURN_TRIP
    assert back.category == Category.WORK

    # 4. Tisdag och onsdag: samma resa, samma svar från telefonen (ingen ny kartfråga).
    for n in (6, 7):
        arrived = drive(db, vehicle, car, day(n), B)
        assert not lookup(db, monkeypatch, SHOP, arrived + timedelta(minutes=5)).requests
        req = ask(db, user, arrived + timedelta(minutes=30))
        assert answer(db, user, req, "WORKTEXT", arrived + timedelta(minutes=40), PURPOSE)["outcome"] == "applied"
        drive(db, vehicle, car, day(n, 13), A)
        assert last_trip(db).approval_source == ApprovalSource.RETURN_TRIP

    # 5. Torsdag: tredje gången räcker – historiken (H) avgör utan fråga.
    arrived = drive(db, vehicle, car, day(8), B)
    lookup(db, monkeypatch, SHOP, arrived + timedelta(minutes=5))
    h_trip = last_trip(db)
    assert h_trip.review_status == ReviewStatus.APPROVED and h_trip.approval_source == ApprovalSource.HISTORY
    assert (h_trip.category, h_trip.purpose) == (Category.WORK, PURPOSE)
    assert "3 senaste" in (h_trip.approval_basis or {}).get("reason", "")
    drive(db, vehicle, car, day(8, 13), A)

    # 6. Fredag: en ny plats; svaret "Gör alltid så här" från webben ger en egen regel (R).
    arrived = drive(db, vehicle, car, day(9), C_)
    lookup(db, monkeypatch, OFFICE_SUPPLY, arrived + timedelta(minutes=5))
    req = ask(db, user, arrived + timedelta(minutes=30))
    res = answer(db, user, req, "WORKTEXT", arrived + timedelta(minutes=40), RULE_PURPOSE, source="web", always=True)
    assert res["outcome"] == "applied", res
    drive(db, vehicle, car, day(9, 13), A)
    # Måndagen därpå avgör regeln resan dit.
    arrived = drive(db, vehicle, car, day(12), C_)
    lookup(db, monkeypatch, OFFICE_SUPPLY, arrived + timedelta(minutes=5))
    r_trip = last_trip(db)
    assert r_trip.review_status == ReviewStatus.APPROVED and r_trip.approval_source == ApprovalSource.RULE
    assert (r_trip.category, r_trip.purpose) == (Category.WORK, RULE_PURPOSE)
    drive(db, vehicle, car, day(12, 13), A)

    # 7. Tisdag: ett lager med namn inom kontorstid → antaget kundbesök (A).
    arrived = drive(db, vehicle, car, day(13), D_)
    lookup(db, monkeypatch, DAGAB, arrived + timedelta(minutes=5))
    a_trip = last_trip(db)
    assert a_trip.approval_source == ApprovalSource.ASSUMED and a_trip.purpose == "Kundbesök Dagab"
    drive(db, vehicle, car, day(13, 13), A)

    # Inga frågor står öppna: allt annat är avgjort.
    notifier.ensure_open_questions(db, day(13, 15))
    open_q = db.execute(
        select(M.NotificationRequest).where(
            M.NotificationRequest.kind.in_((NotificationKind.CLASSIFY, NotificationKind.PURPOSE,
                                            NotificationKind.COMPANY)),
            M.NotificationRequest.status.in_(notifier.OPEN_STATUSES),
        )
    ).scalars().all()
    assert open_q == []

    # 8. Månadsskiftet: sista hämtningen efter månadsslutet, vetotiden har gått → underlaget skapas.
    tz = settings_svc.user_timezone(db, user.id)
    end = common.local_midnight(date(2026, 11, 1), tz)
    trips = list(db.execute(select(M.LogicalTrip).order_by(M.LogicalTrip.sort_time)).scalars())
    assert len(trips) == 14 and all(t.review_status == ReviewStatus.APPROVED for t in trips)
    vetoes = [datetime.fromisoformat(t.approval_basis["veto_until"]) for t in trips
              if t.approval_source == ApprovalSource.ASSUMED]
    check_at = max(end + timedelta(hours=7), *(v + timedelta(minutes=1) for v in vetoes))
    poll(db, vehicle, car, end + timedelta(hours=6, minutes=30))
    status = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1), now=check_at)
    assert status.state == "exported", status.blockers
    job = db.get(M.ExportJob, status.export_id)
    assert job.origin == "month_close" and job.summary["trips"] == 14

    rows = openpyxl.load_workbook(export_path(job)).active.iter_rows(values_only=True)
    purposes = [r[4] for r in rows if r and isinstance(r[4], str)]
    assert purposes.count(PURPOSE) == 4 and purposes.count(RULE_PURPOSE) == 2
    assert "Kundbesök Dagab" in purposes

    # Oförändrat avtryck ger ingen ny fil.
    again = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1),
                                now=check_at + timedelta(hours=2))
    assert again.export_id == job.id

    # 9. Den signerade länken öppnas utan inloggning; en ändrad token avvisas.
    token = links.make_token(job, db.get(M.User, user.id), end + timedelta(days=7))
    assert token and autopilot.is_active(db, user.id)
    db.commit()
    from app.main import create_app

    with TestClient(create_app()) as anon:
        r = anon.get(f"{API}/public/exports/{token}")
        assert r.status_code == 200, r.text
        assert "attachment" in r.headers["content-disposition"] and r.content[:2] == b"PK"
        assert anon.get(f"{API}/public/exports/{token[:-2]}xx").status_code == 404
