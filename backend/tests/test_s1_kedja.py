"""S1e: hela kedjan i självgående läge (byggkontraktet F).

Bilens avläsningar → resa med uträknad starttid → adressen slås upp → beslutet (antaget
kundbesök och hemresan efter det) → månadsskiftet (``run_for``) → Excel med "ca" och rätt
Från/Till. Inga nätverksanrop: bilen är en FakeCar och kartan en MockTransport. Alla
adresser och namn är påhittade.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx
import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import ApprovalSource, Category, LocationType, ReviewStatus, TimeQuality
from app.services import settings as settings_svc
from app.services.export import links, month_close
from app.services.export.service import export_path
from app.services.places import geocode
from app.services.places import registry as places
from app.services.stats import common
from tests.test_s1_adresser import PROVIDER, Recorder
from tests.test_volvo_support import T0, A, B, FakeCar, connect, poll, poll_every

pytestmark = pytest.mark.new_defaults

NBSP = " "
HOME_ADDRESS = "Hemvägen 1, 111 11 Exempelstad"
DAGAB_ADDRESS = "Exempelgatan 5, 123 45 Exempelstad"
RETURN_PURPOSE = "Hemresa efter kundbesök Dagab."  # purpose.return_purpose


def m(minutes: float):
    return T0 + timedelta(minutes=minutes)  # T0 = måndag 5 oktober 07:00 UTC (09:00 svensk tid)


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(geocode, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(geocode, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(geocode, "_last_call", None)

    def no_dns(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - ska aldrig anropas
        raise AssertionError("DNS-uppslag i testet")

    monkeypatch.setattr(geocode, "_getaddrinfo", no_dns)


def drive(db, vehicle, car: FakeCar, start_min: float, to: tuple[float, float]) -> None:
    """En resa på 14 km där mätarställningen rapporteras under färd (fönster under 10 min)."""
    poll_every(db, vehicle, car, m(start_min), m(start_min + 5))
    car.moving(5, m(start_min + 9.5))
    poll(db, vehicle, car, m(start_min + 10))
    car.moving(6, m(start_min + 14.5))
    poll(db, vehicle, car, m(start_min + 15))
    car.drive_to(to, 3, m(start_min + 18))
    poll_every(db, vehicle, car, m(start_min + 20), m(start_min + 40))


def sheet_rows(path) -> list[list[Any]]:
    ws = openpyxl.load_workbook(path).active
    return [[c.value for c in row] for row in ws.iter_rows()]


def test_whole_chain_from_car_to_excel(db, user, vehicle, monkeypatch):
    # Självgående läge med karta, och hemmet som bekräftad plats med koordinat.
    settings_svc.update_section(db, user.id, "privacy", {"geocoding_enabled": True, "geocoder_url": PROVIDER})
    settings_svc.update_section(db, user.id, "automation", {"active_since": (T0 - timedelta(days=1)).isoformat()},
                                allow_read_only=True)
    home = places.resolve_address(db, user.id, HOME_ADDRESS, created_from="user")
    home.type, home.confirmed, home.name = LocationType.HOME, True, "Hemmet"
    home.latitude, home.longitude, home.coordinate_source = A[0], A[1], "user"
    connect(db, user, vehicle)
    car = FakeCar()

    # 1. Bilen kör hem → kunden. Platsen skapas utan nätverk och väntar på adress.
    drive(db, vehicle, car, 0, B)
    (out_raw,) = db.execute(select(M.RawTrip)).scalars().all()
    assert out_raw.start_time_quality == TimeQuality.ESTIMATED and out_raw.started_at is not None
    customer = db.get(M.KnownLocation, out_raw.end_location_id)
    assert customer.geocode_status == "pending" and out_raw.end_address is None

    # 2. Adressen slås upp (kartan svarar Dagab, lager) och beslutet fattas.
    db.commit()
    rec = Recorder()
    monkeypatch.setattr(geocode, "_default_transport", httpx.MockTransport(rec))
    geocode.run_pending(now=m(25))
    db.expire_all()
    assert len(rec.requests) == 1
    customer = db.get(M.KnownLocation, customer.id)
    assert (customer.address, customer.name, customer.place_kind) == (DAGAB_ADDRESS, "Dagab", "work")
    outbound = db.execute(select(M.LogicalTrip)).scalar_one()
    assert outbound.end_address == DAGAB_ADDRESS
    assert outbound.review_status == ReviewStatus.APPROVED and outbound.category == Category.WORK
    assert outbound.approval_source == ApprovalSource.ASSUMED and outbound.purpose == "Kundbesök Dagab"

    # 3. Hemresan samma dag: godkänd som hemresa efter kundbesöket (också antagen).
    drive(db, vehicle, car, 240, A)
    db.commit()
    trips = list(db.execute(select(M.LogicalTrip).order_by(M.LogicalTrip.sort_time)).scalars())
    assert len(trips) == 2
    back = trips[1]
    assert back.review_status == ReviewStatus.APPROVED and back.purpose == RETURN_PURPOSE
    assert back.approval_source == ApprovalSource.ASSUMED

    # 4. Månadsskiftet: bilen hämtas efter månadsslutet (står still) och underlaget skapas.
    # Vetotiden räknas från godkännandet (systemets klocka), så kontrollen görs när den har gått.
    tz = settings_svc.user_timezone(db, user.id)
    end = common.local_midnight(date(2026, 11, 1), tz)
    vetoes = [datetime.fromisoformat(t.approval_basis["veto_until"]) for t in trips]
    check_at = max(end + timedelta(hours=7), *(v + timedelta(minutes=1) for v in vetoes))
    early = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1),
                                 now=min(vetoes) - timedelta(minutes=1))
    assert "assumption_veto_open" in [b.code for b in early.blockers]
    before = month_close.evaluate(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1), now=check_at)
    assert [b.code for b in before.blockers] == ["waiting_final_fetch"]
    poll(db, vehicle, car, end + timedelta(hours=6, minutes=30))
    status = month_close.run_for(db, user_id=user.id, vehicle=vehicle, month=date(2026, 10, 1), now=check_at)
    assert status.state == "exported", status.blockers
    notes = {n.code: n for n in status.notes}
    assert notes["assumed"].count == 2 and notes["estimated_times"].count == 2
    assert notes["addresses_auto"].count == 1
    job = db.get(M.ExportJob, status.export_id)
    assert job.origin == "month_close" and job.summary["trips"] == 2 and job.summary["km"] == 28.0

    # 5. Excel: "ca" med hårt mellanslag, Från/Till som gatuadresser utan POI-namn.
    rows = sheet_rows(export_path(job))
    data = [r for r in rows if r and r[0] is not None and isinstance(r[1], str) and "ca" + NBSP in r[1]]
    assert len(data) == 2
    first, second = data
    assert first[1].startswith("ca" + NBSP) and first[2] == HOME_ADDRESS and first[3] == DAGAB_ADDRESS
    assert first[4] == "Kundbesök Dagab"
    assert second[2] == DAGAB_ADDRESS and second[3] == HOME_ADDRESS and second[4] == RETURN_PURPOSE
    assert not any("Dagab," in str(c) for r in data for c in r[2:4])  # aldrig "Dagab, Exempelgatan …"
    footnotes = [c for r in rows for c in r if isinstance(c, str) and c.startswith("ca = uträknad")]
    assert footnotes

    # 6. Länken till filen fungerar utan inloggning.
    exp = end + timedelta(days=7)
    token = links.make_token(job, db.get(M.User, user.id), exp)
    assert links.verify(db, token, end + timedelta(hours=8)).id == job.id
