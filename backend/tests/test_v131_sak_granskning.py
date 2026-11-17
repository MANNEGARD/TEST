"""1.3.1, säkerhet – rättningar efter granskningen av säkerhetsrättningarna.

* indata-7 (resten): radering av historik (exporter och importfiler) raderar aldrig en fil
  utanför export- respektive importkatalogen.
* logik-3 (Graph-delen): osynliga tecken och bidi-tecken i en Outlook-rubrik eller plats sparas
  inte; synlig text är kvar.
* utgaende-5 följd: när Graph-läsningen avbryts (främmande nästa-sida eller sidtaket) markeras
  inga händelser som borttagna, som i kalenderlänkens synk.
* homeassistant-6 lämnas orättad: syftesknapparna i manuellt läge visar syftet som i 1.3.0.
* Skyddet mot DNS-ombindning prövas genom hela den riktiga transporten, så att ett namnbyte i
  httpx/httpcore inte kan få det att sluta verka i tysthet.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.config import get_config
from app.security import ratelimit
from app.services import privacy
from app.services.calendar import graph, sync
from app.services.importer import service as imp
from app.services.places import geocode
from tests.fixtures import volvo_exports as fx
from tests.test_calendar import connect, graph_event, graph_transport

# ----------------------------------------------------------------------------- indata-7


def test_delete_exports_never_removes_files_outside_the_export_folder(db, user, vehicle, actor, tmp_path):
    outside = tmp_path / "utanfor" / "viktig.db"
    outside.parent.mkdir()
    outside.write_bytes(b"data")
    exports = get_config().exports_dir
    exports.mkdir(parents=True, exist_ok=True)
    inside = exports / "underlag.xlsx"
    inside.write_bytes(b"xlsx")
    for path in (outside, inside):
        db.add(M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, status="done", file_path=str(path)))
    db.flush()

    removed = privacy.delete_history(db, user_id=user.id, scope="exports", vehicle=vehicle, actor=actor)
    assert removed["exports"] == 2
    assert outside.exists()
    assert not inside.exists()  # vanliga exportfiler raderas som förut


def test_delete_trips_never_removes_import_files_outside_the_upload_folder(db, user, vehicle, actor, tmp_path):
    outside = tmp_path / "utanfor" / "viktig.db"
    outside.parent.mkdir()
    outside.write_bytes(b"data")
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    inside = Path(batch.files[1].stored_path)
    assert inside.exists()
    batch.files[0].stored_path = str(outside)
    db.flush()

    removed = privacy.delete_history(db, user_id=user.id, scope="trips", vehicle=vehicle, actor=actor)
    assert outside.exists()
    assert not inside.exists()
    assert removed["import_files"] == 1


# ----------------------------------------------------------------------------- logik-3 (Graph)


def test_graph_sync_strips_hidden_characters_from_subject_and_location(db, user):
    connect(db, user)
    now = datetime.now(UTC)
    raw = graph_event(1, now, subject="Kundbesök​ Exempel‮BA⁦ AB\x07")
    raw["location"]["displayName"] = "Exempel‮ kontoret​"
    raw["location"]["address"]["street"] = "Påhittad​ gata 1"
    sync.sync_user(db, user.id, now=now, transport=graph_transport([[raw]], []))
    ev = db.execute(select(M.CalendarEvent)).scalar_one()
    assert ev.subject == "Kundbesök ExempelBA AB"
    assert ev.location_name == "Exempel kontoret"
    assert ev.location_address == "Påhittad gata 1, Sjöstad"


def test_graph_sync_keeps_ordinary_text():
    raw = graph_event(1, datetime(2026, 3, 2, 8, 0, tzinfo=UTC), subject="Möte – Åkeri & Söner (Göteborg)")
    values = sync._event_values(raw, include_body=False)
    assert values["subject"] == "Möte – Åkeri & Söner (Göteborg)"
    assert values["location_name"] == "Sjöstadskontoret"
    assert values["location_address"] == "Verkstadsvägen 1, Sjöstad"


# ----------------------------------------------------------------------------- avbruten Graph-läsning


def _foreign_next_transport(first: list[dict], calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/me/calendars"):
            return httpx.Response(200, json={"value": [{"id": "cal-1", "isDefaultCalendar": True}]})
        if request.url.host != "graph.microsoft.com":
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={"value": first, "@odata.nextLink": "https://angripare.example/sida2"})

    return httpx.MockTransport(handler)


def test_graph_sync_does_not_remove_events_when_paging_was_cut(db, user):
    connect(db, user)
    now = datetime.now(UTC)
    sync.sync_user(db, user.id, now=now, transport=graph_transport([[graph_event(1, now)], [graph_event(2, now)]], []))
    calls: list[httpx.Request] = []
    result = sync.sync_user(db, user.id, now=now, transport=_foreign_next_transport([graph_event(1, now)], calls))
    assert all(c.url.host != "angripare.example" for c in calls)
    assert result["removed"] == 0
    events = {e.external_id: e for e in db.execute(select(M.CalendarEvent)).scalars()}
    assert events["id-2"].removed_at is None  # inte sedd, men inte heller borttagen


def test_graph_sync_does_not_remove_events_past_the_page_cap(db, user, monkeypatch):
    connect(db, user)
    now = datetime.now(UTC)
    sync.sync_user(db, user.id, now=now, transport=graph_transport([[graph_event(1, now)], [graph_event(2, now)]], []))
    monkeypatch.setattr(graph, "MAX_PAGES", 1)
    result = sync.sync_user(db, user.id, now=now, transport=graph_transport([[graph_event(1, now)], [graph_event(2, now)]], []))
    assert result["removed"] == 0
    events = {e.external_id: e for e in db.execute(select(M.CalendarEvent)).scalars()}
    assert events["id-2"].removed_at is None


def test_graph_sync_still_removes_events_after_a_complete_read(db, user):
    connect(db, user)
    now = datetime.now(UTC)
    sync.sync_user(db, user.id, now=now, transport=graph_transport([[graph_event(1, now)], [graph_event(2, now)]], []))
    result = sync.sync_user(db, user.id, now=now + timedelta(minutes=1),
                            transport=graph_transport([[graph_event(1, now)], []], []))
    assert result["removed"] == 1


# ----------------------------------------------------------------------------- homeassistant-6 (orättad)


def test_manual_purpose_buttons_are_unchanged_with_hidden_locations(db, user, vehicle, trips):
    from app.db import utcnow
    from app.services.ha import actions as A
    from app.services.ha import messages

    trip = trips.trip("Hemvägen 1, Hemby", "Kundgatan 2, Exempelstad", km=42.0, minutes=30,
                      start_at=utcnow() - timedelta(hours=1))
    previous = ["Kundbesök Exempel AB", "Möte hos Påhittat AB i Exempelstad"]
    data = messages.purpose(db, trip, "pid-1", hide_locations=True, previous=previous)
    picks = [a for a in data["actions"] if a["key"].startswith(A.PURPOSE_PICK) and a["key"] != A.PURPOSE_TEXT]
    assert [a["title"] for a in picks] == previous
    assert [a["purpose"] for a in picks] == previous


# ----------------------------------------------------------------------------- övrigt


def test_real_transport_checks_the_address_through_the_whole_client(monkeypatch):
    """Hela vägen genom httpx: når anropet en annan nätverksbackend än DriveLogs ansluts det
    till namnet, och då syns det här."""
    connects: list[tuple] = []
    monkeypatch.setattr(geocode, "_getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
    ])

    def create(addr, *a, **k):
        connects.append(addr)
        raise OSError("ingen anslutning i testet")

    monkeypatch.setattr(socket, "create_connection", create)
    with httpx.Client(transport=geocode._real_transport()) as client:
        with pytest.raises(geocode.GeocodeError):
            client.get("http://kalender.example.com/kalender.ics")
    assert connects == []


def test_non_atomic_rate_limit_helpers_are_gone():
    """Bara try_acquire/hit: check följt av record är inte atomärt och ska inte kunna användas."""
    assert not hasattr(ratelimit, "check")
    assert not hasattr(ratelimit, "record")
