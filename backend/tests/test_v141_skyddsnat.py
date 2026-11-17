"""1.4.1: skyddsnätet kring hämtningen från Volvo.

Bakgrund: i 1.4.0 låg avläsningen, skapandet av resan och hela efterbearbetningen i samma
transaktion. Ett ohanterat undantag i ett enda delsteg rullade tillbaka allt – också avläsningen
– vid varje hämtning, och "Synka nu" svarade 500 utan att något syntes under "Senaste fel".

Här prövas, i det toleranta läget (``internal_errors.tolerant_mode``), att

* avläsningen alltid sparas, även när bearbetningen kraschar;
* resan skapas och syns i journalen även när ett delsteg efter skapandet kraschar, och att
  stegets halvfärdiga skrivningar (även oskrivna ändringar i sessionen) är borta;
* ett fel i själva detekteringen lämnar avläsningarna obearbetade, och nästa hämtning skapar resan;
* felet antecknas, syns under "Senaste fel" och i felsökningsfilen – utan hemligheter – och raden
  försvinner när bearbetningen lyckas igen;
* "Synka nu" svarar 200 med en tydlig uppgift om att bearbetningen misslyckades;

och i det stränga läget (testmiljöns standard) att samma undantag kastas vidare som förut.
Sparpunkterna prövas också direkt, på SQLite och PostgreSQL (DRIVELOG_TEST_DATABASE_URL).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.models as M
from app.models.enums import ReviewKind
from app.services import audit, internal_errors
from app.services import settings as settings_svc
from app.services.calendar import matching as calendar_matching
from app.services.classify import autopilot
from app.services.classify import engine as classify_engine
from app.services.ha import notifier
from app.services.learning import patterns
from app.services.places import registry as places_registry
from app.services.stats import anomalies
from app.services.trips import gaps, journeys, merge, pipeline, review, start_time
from app.services.trips import segments as seg
from app.services.volvo import collector, consumption, detection
from tests.test_volvo_support import T0, VIN, B, FakeCar, connect, poll

API = "/api/v1/integrations/volvo"
HALF = "HALVFÄRDIGT-STEG"
API_KEY = "hemlig-nyckel-123456"
CAL_LINK = "webcal://kalender.example.com/delad/min-kalender.ics"
SECRET_MESSAGE = f"Steget gick sönder för {VIN} med api_key={API_KEY}, {CAL_LINK} vid 59.0001234 och ABC123"
COORD_RE = re.compile(r"(?<![:\d])\d{1,3}\.\d{4,}")


class Boom(RuntimeError):
    """Det påhittade felet i ett delsteg."""


def m(minutes: float):
    return T0 + timedelta(minutes=minutes)


@pytest.fixture()
def car(db, user, vehicle) -> FakeCar:
    connect(db, user, vehicle)
    return FakeCar()


@pytest.fixture()
def tolerant():
    with internal_errors.tolerant_mode(True):
        yield


def broken_step(*args, **kwargs):
    """Ett delsteg som hinner skriva (och ändra utan att skriva) innan det kraschar."""
    db = args[0]
    vehicle = db.execute(select(M.Vehicle)).scalars().first()
    for trip in seg.active_trips(db, vehicle.id):
        trip.purpose = HALF
    audit.record(db, audit.SYSTEM, "test.halvfardigt", "vehicle", vehicle.id)
    db.flush()
    vehicle.name = HALF  # ändrad i sessionen men aldrig skriven
    raise Boom(SECRET_MESSAGE)


def drive_until_processed(db, vehicle, car) -> dict:
    """Bilen står still, kör, anländer och står still. Pollar tills hämtningen bearbetade en
    avslutad rörelse (en ny resa eller ett fel i bearbetningen) och returnerar den hämtningen."""
    poll(db, vehicle, car, m(0))
    poll(db, vehicle, car, m(5))
    car.moving(5, m(9.5))
    poll(db, vehicle, car, m(10))
    car.moving(6, m(14.5))
    poll(db, vehicle, car, m(15))
    car.drive_to(B, 3, m(18))
    t = 20
    while t <= 90:
        result = poll(db, vehicle, car, m(t))
        if result.get("new_trips") or result.get("processing_error"):
            return result
        t += 5
    raise AssertionError("ingen hämtning bearbetade resan")


# (steg, modul, funktion) – varje delsteg efter att resan skapats.
STEPS = [
    ("start_time", start_time, "fill_from_distance"),
    ("consumption", consumption, "fill_vehicle"),
    ("merge", merge, "evaluate_vehicle"),
    ("gaps", gaps, "recompute"),
    ("places", places_registry, "recount_visits"),
    ("patterns", patterns, "rebuild"),
    ("calendar", calendar_matching, "match_vehicle"),
    ("classify", classify_engine, "classify_pending"),
    ("journeys", journeys, "detect"),
    ("autopilot", autopilot, "run_vehicle"),
    ("review_items", pipeline, "sync_trip_review_items"),
    ("close_orphaned", review, "close_orphaned"),
    ("anomalies", anomalies, "close_noise"),
    ("notify", notifier, "enqueue_for_changes"),
]


def break_step(monkeypatch, module, attr) -> None:
    monkeypatch.setattr(module, attr, broken_step)
    if module is autopilot:
        # Autopiloten körs bara i självgående läge.
        monkeypatch.setattr(autopilot, "is_active", lambda db, user_id: True)


def assert_no_secrets(text: str) -> None:
    for secret in (VIN, VIN[-6:], API_KEY, CAL_LINK, "kalender.example.com", "59.0001234", "ABC123", HALF):
        assert secret not in text, secret
    assert not COORD_RE.search(text)


# ----------------------------------------------------------------------------- varje delsteg


@pytest.mark.parametrize(("name", "module", "attr"), STEPS, ids=[s[0] for s in STEPS])
def test_crashing_step_is_skipped_and_the_trip_is_kept(name, module, attr, client, db, user, vehicle, car,
                                                       monkeypatch, tolerant):
    break_step(monkeypatch, module, attr)
    result = drive_until_processed(db, vehicle, car)
    db.commit()

    # Hämtningen lyckades, resan skapades och felet rapporteras med steget.
    assert result["status"] in ("ok", "partial") and result["observation"] in ("new", "duplicate")
    assert result["new_trips"] == 1
    error = result["processing_error"]
    assert error["step"] == f"pipeline.{name}" and error["steps"] == [f"pipeline.{name}"]
    assert internal_errors.step_label(f"pipeline.{name}") in error["message"]
    assert "Ladda ner felsökningsfilen" in error["message"]

    # Avläsningarna är sparade och bearbetade, resan finns.
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(M.VehicleObservation)) == 4  # stilla, 2 under färd, framme
    assert db.scalar(select(func.count()).select_from(M.VehicleObservation)
                     .where(M.VehicleObservation.processed_at.is_(None))) == 0
    (trip,) = seg.active_trips(db, vehicle.id)
    assert trip.distance_km == 14

    # Stegets halvfärdiga skrivningar är borta – i databasen och i sessionen.
    assert trip.purpose != HALF
    assert db.get(M.Vehicle, vehicle.id).name == "Testbil"
    assert db.scalar(select(func.count()).select_from(M.AuditEvent)
                     .where(M.AuditEvent.action == "test.halvfardigt")) == 0
    assert db.scalar(select(func.count()).select_from(M.AuditEvent)
                     .where(M.AuditEvent.action == "trip.detected")) == 1

    # Resan syns i journalen.
    items = client.get("/api/v1/trips").json()["items"]
    assert [i["id"] for i in items] == [str(trip.id)]

    # Felet är antecknat (utan hemligheter) …
    stored = internal_errors.entries(db, user.id)
    # Ärendena: först alla resor i en sparpunkt, sedan resan för sig – två poster för samma steg.
    assert len(stored) == (2 if name == "review_items" else 1)
    assert {e["step"] for e in stored} == {f"pipeline.{name}"}
    entry = stored[0]
    assert entry["type"].endswith("Boom") and entry["origin"] == "poll"
    assert entry["message"].startswith("Steget gick sönder för") and len(entry["message"]) <= 300
    assert_no_secrets(json.dumps(entry))
    assert any(w.startswith("app/services/trips/pipeline.py:") for w in entry["where"])

    # … syns under "Senaste fel" …
    status = client.get(f"{API}/status").json()
    assert status["last_error"] == error["message"]
    assert status["is_connected"] is True

    # … och i felsökningsfilen, utan hemligheter.
    resp = client.get("/api/v1/volvo/diagnostics")
    assert resp.status_code == 200
    assert_no_secrets(resp.text)
    diag = resp.json()["internal_errors"][0]
    assert len(resp.json()["internal_errors"]) == len(stored)
    assert diag["step"] == f"pipeline.{name}" and diag["type"] == entry["type"]
    assert diag["where"] == entry["where"] and diag["message"] == entry["message"]
    # Hämtningen räknas fortfarande som lyckad.
    assert resp.json()["sync_runs"][-1]["status"] in ("ok", "partial")


@pytest.mark.parametrize(("name", "module", "attr"), STEPS, ids=[s[0] for s in STEPS])
def test_strict_mode_raises_the_same_exception(name, module, attr, db, user, vehicle, car, monkeypatch):
    assert internal_errors.is_tolerant() is False  # testmiljön är sträng som standard
    break_step(monkeypatch, module, attr)
    with pytest.raises(Boom):
        drive_until_processed(db, vehicle, car)


def drive_another_trip(db, vehicle, car, start: float = 130) -> dict:
    """En andra resa efter drive_until_processed; returnerar hämtningen som skapade den."""
    car.moving(4, m(start))
    poll(db, vehicle, car, m(start + 1))
    car.drive_to((59.0, 15.0), 6, m(start + 10))
    t, result = start + 15, {}
    while t < start + 130 and not (result.get("new_trips") or result.get("processing_error")):
        result = poll(db, vehicle, car, m(t))
        t += 5
    assert result.get("new_trips") == 1, result
    return result


@pytest.mark.parametrize(("module", "attr", "label"), [
    (autopilot, "run_vehicle", "autopiloten"),
    (notifier, "enqueue_for_changes", "notiser"),
], ids=["autopilot", "notify"])
def test_line_stays_until_processing_succeeds_again(module, attr, label, client, db, user, vehicle, car, monkeypatch,
                                                    tolerant):
    """Granskningsfynd (1.4.2): raden försvann vid nästa lyckade hämtning, även när ingenting
    hade bearbetats om – resan saknade fortfarande sin fråga (eller autopilot) men panelen visade
    "Senaste fel: Inga". Nu står raden kvar tills en efterbearbetning har körts utan fel."""
    real = getattr(module, attr)
    break_step(monkeypatch, module, attr)
    drive_until_processed(db, vehicle, car)
    db.commit()
    line = client.get(f"{API}/status").json()["last_error"]
    assert label in line
    # Nästa hämtning: bilen står still, ingen ny resa, steget är fortfarande trasigt.
    result = poll(db, vehicle, car, m(100))
    db.commit()
    assert result["status"] in ("ok", "partial") and "processing_error" not in result
    status = client.get(f"{API}/status").json()
    assert status["last_error"] == line and status["is_connected"] is True
    # Steget är lagat men ingenting har bearbetats om: raden står kvar.
    monkeypatch.setattr(module, attr, real)
    poll(db, vehicle, car, m(110))
    db.commit()
    assert client.get(f"{API}/status").json()["last_error"] == line
    # En ny resa bearbetas utan fel: raden försvinner.
    result = drive_another_trip(db, vehicle, car)
    db.commit()
    assert "processing_error" not in result
    assert client.get(f"{API}/status").json()["last_error"] is None
    assert len(internal_errors.entries(db, user.id)) == 1  # posten ligger kvar för felsökningsfilen


def test_a_new_error_in_the_fetch_replaces_the_processing_line(client, db, user, vehicle, car, monkeypatch, tolerant):
    break_step(monkeypatch, notifier, "enqueue_for_changes")
    drive_until_processed(db, vehicle, car)
    db.commit()
    monkeypatch.setattr(car, "fetch", lambda name, vin: collector.ApiResult("error", 500, message="Volvo svarar inte"))
    poll(db, vehicle, car, m(100))
    db.commit()
    assert "Volvo svarar inte" in client.get(f"{API}/status").json()["last_error"]


def _fetch_500(name, vin):
    return collector.ApiResult("error", 500, message="Volvo svarar inte (påhittat)")


def _fetch_429(name, vin):
    return collector.ApiResult("throttled", 429, message="kvot", retry_after=60)


def _fetch_raises(name, vin):
    raise collector.VolvoError("Nätverksfel (påhittat)")


def _fetch_raises_429(name, vin):
    raise collector.vc.VolvoThrottled(60)


@pytest.mark.parametrize("bad", [None, _fetch_500, _fetch_429, _fetch_raises, _fetch_raises_429],
                         ids=["kontroll", "alla500", "429svar", "volvoerror", "429undantag"])
def test_processing_line_comes_back_after_a_temporary_fetch_error(bad, client, db, user, vehicle, car, monkeypatch,
                                                                   tolerant):
    """Granskningsfynd (1.4.2): ett tillfälligt fel i hämtningen skrev över raden, och vid nästa
    lyckade hämtning försvann den fast inget hade bearbetats om. Nu kommer den tillbaka."""
    break_step(monkeypatch, notifier, "enqueue_for_changes")
    drive_until_processed(db, vehicle, car)
    db.commit()
    line = client.get(f"{API}/status").json()["last_error"]
    assert line.startswith(internal_errors.PROCESSING_PREFIX)
    real = car.fetch
    if bad is not None:
        monkeypatch.setattr(car, "fetch", bad)
        poll(db, vehicle, car, m(100))
        db.commit()
        assert client.get(f"{API}/status").json()["last_error"] != line  # hämtningsfelet går före
    monkeypatch.setattr(car, "fetch", real)
    poll(db, vehicle, car, m(130))  # bilen står still, steget är fortfarande trasigt
    db.commit()
    assert client.get(f"{API}/status").json()["last_error"] == line
    # En ny resa bearbetas utan fel: raden försvinner, också den sparade.
    monkeypatch.undo()
    drive_another_trip(db, vehicle, car, start=140)
    db.commit()
    assert client.get(f"{API}/status").json()["last_error"] is None
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection)).scalars().one()
    assert collector.UNRESOLVED_PROCESSING not in (conn.config or {})


def test_detection_line_is_cleared_when_detection_succeeds(client, db, user, vehicle, car, monkeypatch, tolerant):
    real = seg.create_logical_for_raw
    monkeypatch.setattr(seg, "create_logical_for_raw", broken_step)
    drive_until_processed(db, vehicle, car)
    db.commit()
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection)).scalars().one()
    saved = conn.config[collector.UNRESOLVED_PROCESSING]
    assert saved["message"] == internal_errors.DETECTION_MESSAGE
    real_fetch = car.fetch
    monkeypatch.setattr(car, "fetch", _fetch_500)
    poll(db, vehicle, car, m(150))
    db.commit()
    monkeypatch.setattr(car, "fetch", real_fetch)
    monkeypatch.setattr(seg, "create_logical_for_raw", real)
    again = poll(db, vehicle, car, m(200))
    db.commit()
    assert again["new_trips"] == 1 and "processing_error" not in again
    assert client.get(f"{API}/status").json()["last_error"] is None
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection)).scalars().one()
    assert collector.UNRESOLVED_PROCESSING not in (conn.config or {})


def test_demo_vehicle_never_saves_the_processing_line(db, user, vehicle, car, monkeypatch, tolerant):
    vehicle.is_demo = True
    db.flush()
    break_step(monkeypatch, notifier, "enqueue_for_changes")
    result = drive_until_processed(db, vehicle, car)
    db.commit()
    assert result["processing_error"]["steps"] == ["pipeline.notify"]
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection)).scalars().one()
    assert collector.UNRESOLVED_PROCESSING not in (conn.config or {})


def test_processing_line_from_before_the_upgrade_is_kept(client, db, user, vehicle, car, monkeypatch, tolerant):
    """En rad som skrevs innan nyckeln fanns (bara i last_error) står kvar som förut och sparas
    sedan på det nya sättet, så att den också överlever ett tillfälligt hämtningsfel."""
    break_step(monkeypatch, notifier, "enqueue_for_changes")
    drive_until_processed(db, vehicle, car)
    db.commit()
    line = client.get(f"{API}/status").json()["last_error"]
    conn = db.execute(select(M.IntegrationConnection)).scalars().one()
    from app.services import integrations
    integrations.update_config(conn, {collector.UNRESOLVED_PROCESSING: None})
    db.commit()
    poll(db, vehicle, car, m(100))
    db.commit()
    assert client.get(f"{API}/status").json()["last_error"] == line
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection)).scalars().one()
    assert conn.config[collector.UNRESOLVED_PROCESSING]["message"] == line
    real = car.fetch
    monkeypatch.setattr(car, "fetch", _fetch_500)
    poll(db, vehicle, car, m(110))
    db.commit()
    monkeypatch.setattr(car, "fetch", real)
    poll(db, vehicle, car, m(130))
    db.commit()
    assert client.get(f"{API}/status").json()["last_error"] == line


def test_review_items_fall_back_to_one_trip_at_a_time(db, user, vehicle, car, monkeypatch, tolerant):
    """Fäller en enda resa avstämningen av ärenden får de andra resorna ändå sina ärenden."""
    real = pipeline.sync_trip_review_items
    first = drive_until_processed(db, vehicle, car)
    assert "processing_error" not in first
    (old_trip,) = seg.active_trips(db, vehicle.id)
    seen: list = []

    def flaky(db_, trip, **kwargs):
        seen.append(trip.id)
        if trip.id != old_trip.id:
            raise Boom("trasig resa")
        return real(db_, trip, **kwargs)

    monkeypatch.setattr(pipeline, "sync_trip_review_items", flaky)
    car.moving(4, m(130))
    poll(db, vehicle, car, m(131))
    car.drive_to((59.0, 15.0), 6, m(140))
    t, result = 145, {}
    while t < 260 and not result.get("new_trips"):
        result = poll(db, vehicle, car, m(t))
        t += 5
    assert result["new_trips"] == 1
    assert set(result["processing_error"]["steps"]) == {"pipeline.review_items"}
    trips = seg.active_trips(db, vehicle.id)
    assert len(trips) == 2
    new_trip = next(t for t in trips if t.id != old_trip.id)
    # Först alla resor i en sparpunkt, sedan (när den nya resan fällde den) en i taget.
    assert seen.count(old_trip.id) == 2 and seen.count(new_trip.id) == 2
    items = db.execute(select(M.ReviewItem.logical_trip_id).where(M.ReviewItem.kind == ReviewKind.CATEGORY)).scalars().all()
    assert old_trip.id in items and new_trip.id not in items
    # Resan blockerar ändå månadsskiftet: den är oavgjord.
    assert new_trip.review_status == "needs_review" and new_trip.undecided_km > 0


# ----------------------------------------------------------------------------- detekteringen


def test_detection_failure_keeps_readings_unprocessed_and_next_poll_creates_the_trip(
    client, db, user, vehicle, car, monkeypatch, tolerant
):
    real = seg.create_logical_for_raw
    monkeypatch.setattr(seg, "create_logical_for_raw", broken_step)
    result = drive_until_processed(db, vehicle, car)
    db.commit()
    assert result["status"] in ("ok", "partial") and result["observation"] in ("new", "duplicate")
    assert result["new_trips"] == 0 and result["processing_error"]["step"] == "detection"
    assert "resedetekteringen" in result["processing_error"]["message"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(M.RawTrip)) == 0
    assert db.scalar(select(func.count()).select_from(M.LogicalTrip)) == 0
    unprocessed = db.scalar(select(func.count()).select_from(M.VehicleObservation)
                            .where(M.VehicleObservation.processed_at.is_(None)))
    assert unprocessed >= 2
    # Hämtningen själv är sparad och räknas som lyckad.
    run = db.execute(select(M.IntegrationSyncRun).order_by(M.IntegrationSyncRun.started_at.desc())).scalars().first()
    assert run.status in ("ok", "partial") and run.stats["processing_error"] == "detection"
    assert db.get(M.Vehicle, vehicle.id).name == "Testbil"
    assert "resedetekteringen" in client.get(f"{API}/status").json()["last_error"]
    assert internal_errors.entries(db, user.id)[0]["step"] == "detection"

    # Felet är borta: nästa hämtning (bilen står kvar) skapar resan och raden försvinner.
    monkeypatch.setattr(seg, "create_logical_for_raw", real)
    again = poll(db, vehicle, car, m(200))
    db.commit()
    assert again["new_trips"] == 1 and "processing_error" not in again
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(M.VehicleObservation)
                     .where(M.VehicleObservation.processed_at.is_(None))) == 0
    (trip,) = seg.active_trips(db, vehicle.id)
    assert trip.distance_km == 14
    assert [i["id"] for i in client.get("/api/v1/trips").json()["items"]] == [str(trip.id)]
    assert client.get(f"{API}/status").json()["last_error"] is None


def test_detection_failure_is_raised_in_strict_mode(db, user, vehicle, car, monkeypatch):
    monkeypatch.setattr(seg, "create_logical_for_raw", broken_step)
    with pytest.raises(Boom):
        drive_until_processed(db, vehicle, car)


# ----------------------------------------------------------------------------- "Synka nu"


def test_sync_now_answers_without_500_when_processing_fails(client, db, user, vehicle, car, monkeypatch, tolerant):
    db.commit()
    real = detection.process_vehicle
    monkeypatch.setattr(collector, "client_for", lambda *a, **k: car)
    monkeypatch.setattr(detection, "process_vehicle", broken_step)
    resp = client.post(f"{API}/sync", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    result = body["result"]
    # Webben visar ett misslyckat "Synka nu" med meddelandet (status "error" + message).
    assert result["status"] == "error" and result["fetch_status"] in ("ok", "partial")
    assert result["message"] == result["processing_error"]["message"]
    assert "resedetekteringen" in result["message"] and "Ladda ner felsökningsfilen" in result["message"]
    assert result["observation"] == "new" and result["new_trips"] == 0
    assert body["last_observation_at"] is not None
    assert_no_secrets(resp.text)

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(M.VehicleObservation)) == 1
    assert db.get(M.Vehicle, vehicle.id).name == "Testbil"
    assert client.get(f"{API}/status").json()["last_error"] == result["message"]
    diag = client.get("/api/v1/volvo/diagnostics")
    assert_no_secrets(diag.text)
    (entry,) = diag.json()["internal_errors"]
    assert entry["step"] == "detection" and entry["origin"] == "poll" and entry["type"].endswith("Boom")
    assert any(w.startswith("app/services/volvo/collector.py:") for w in entry["where"])

    # Felet är rättat: nästa "Synka nu" lyckas och raden försvinner.
    monkeypatch.setattr(detection, "process_vehicle", real)
    ok = client.post(f"{API}/sync", json={})
    assert ok.status_code == 200 and ok.json()["result"]["status"] in ("ok", "partial")
    assert "processing_error" not in ok.json()["result"]
    assert client.get(f"{API}/status").json()["last_error"] is None


def test_unexpected_error_outside_the_steps_is_recorded(client, db, user, vehicle, car, monkeypatch, tolerant):
    """Ett fel före bearbetningen (t.ex. i lagringen) ger fortfarande 500, men syns nu."""
    db.commit()
    monkeypatch.setattr(collector, "client_for", lambda *a, **k: car)
    monkeypatch.setattr(collector, "store_observation", broken_step)
    with pytest.raises(Boom):  # TestClient kastar serverns undantag vidare; i drift blir det 500
        client.post(f"{API}/sync", json={})
    db.expire_all()
    assert "hämtningen" in client.get(f"{API}/status").json()["last_error"]
    (entry,) = internal_errors.entries(db, user.id)
    assert entry["step"] == "volvo.poll" and entry["origin"] == "sync"
    assert_no_secrets(json.dumps(entry))


def test_poll_due_records_unexpected_errors(db, user, vehicle, car, monkeypatch):
    monkeypatch.setattr(collector, "client_for", lambda *a, **k: car)
    monkeypatch.setattr(collector, "store_observation", broken_step)
    db.commit()
    out = collector.poll_due(db, now=m(0))
    assert out["errors"] == 1 and out["polled"] == 0
    db.expire_all()
    (entry,) = internal_errors.entries(db, user.id)
    assert entry["step"] == "volvo.poll" and entry["origin"] == "poll"
    conn = db.execute(select(M.IntegrationConnection)).scalar_one()
    assert "hämtningen" in conn.last_error


# ----------------------------------------------------------------------------- sparpunkterna


def _driver(db, user) -> str:
    db.expire_all()
    return settings_svc.get_section(db, user.id, "general")["driver_name"]


def test_savepoint_rolls_back_only_the_step_and_outer_transaction_commits(db, user, tolerant):
    db.commit()  # ingen transaktion pågår: sparpunkten får inte bli den yttersta (SQLite)

    def step(db_):
        settings_svc.update_section(db_, user.id, "general", {"driver_name": "Steget"})
        raise Boom("nej")

    assert internal_errors.run_step(db, "test.step", step, db, default="hoppat") == "hoppat"
    settings_svc.update_section(db, user.id, "general", {"employer_name": "Yttre"})
    db.commit()
    assert _driver(db, user) == ""
    assert settings_svc.get_section(db, user.id, "general")["employer_name"] == "Yttre"
    (failure,) = internal_errors.take_failures(db)
    assert failure["step"] == "test.step" and failure["type"].endswith("Boom")


def test_savepoint_release_does_not_commit_the_outer_transaction(db, user, tolerant):
    db.commit()

    def step(db_):
        settings_svc.update_section(db_, user.id, "general", {"driver_name": "Steget"})
        return "klart"

    assert internal_errors.run_step(db, "test.step", step, db) == "klart"
    db.rollback()  # den yttre transaktionen ångras: stegets skrivning ska följa med
    assert _driver(db, user) == ""


def test_failed_flush_inside_a_step_leaves_the_session_usable(db, user, tolerant):
    settings_svc.update_section(db, user.id, "general", {"employer_name": "Före"})
    db.flush()

    def step(db_):
        db_.add(M.Setting(user_id=user.id, key="general", value={}))  # dubblett: unik nyckel
        db_.flush()

    internal_errors.run_step(db, "test.step", step, db)
    (failure,) = internal_errors.take_failures(db)
    assert failure["type"] == IntegrityError.__module__ + "." + IntegrityError.__qualname__
    settings_svc.update_section(db, user.id, "general", {"driver_name": "Efter"})
    db.commit()
    general = settings_svc.get_section(db, user.id, "general")
    assert general["employer_name"] == "Före" and general["driver_name"] == "Efter"


def test_strict_run_step_raises_and_uses_no_savepoint(db, user):
    def step(db_):
        raise Boom("strängt")

    with pytest.raises(Boom):
        internal_errors.run_step(db, "test.step", step, db)
    assert internal_errors.take_failures(db) == []


def test_failed_step_drops_its_queued_follow_up_work(db, user, vehicle, tolerant):
    from app.services.learning import recompute

    db.flush()

    def step(db_):
        recompute.request(db_, vehicle.id)
        db_.info.setdefault("drivelog_deferred_jobs", []).append(("test.jobb", {}))
        raise Boom("nej")

    internal_errors.run_step(db, "test.step", step, db)
    assert "drivelog_recompute_vehicles" not in db.info and "drivelog_deferred_jobs" not in db.info
    db.expire_all()
    assert db.get(M.Vehicle, vehicle.id).recompute_requested == 0


# ----------------------------------------------------------------------------- lagringen


def test_at_most_20_entries_are_kept_newest_first(db, user):
    for i in range(25):
        internal_errors.save_failures(db, user.id, [internal_errors.describe(f"test.{i}", Boom(f"fel {i}"))])
    stored = internal_errors.entries(db, user.id)
    assert len(stored) == internal_errors.MAX_ENTRIES == 20
    assert stored[0]["step"] == "test.24" and stored[-1]["step"] == "test.5"


def test_message_is_cleaned_and_truncated():
    text = internal_errors.clean_message(
        f"{SECRET_MESSAGE} Authorization: Bearer abcdefghijklmnopqrstuvwxyz eyJa.eyJb.cc " + "x" * 400,
        extra=("Testbil",),
    )
    assert len(text) <= internal_errors.MAX_MESSAGE
    assert_no_secrets(text)
    assert "abcdefghijklmnop" not in text and "eyJa.eyJb" not in text


def test_public_entries_drop_unknown_forms(db, user, vehicle):
    settings_svc.update_section(db, user.id, "system", {"internal_errors": [{
        "at": "2026-10-05T07:00:00+00:00", "step": "Fri text med mellanslag", "type": "a b c",
        "origin": "poll", "message": f"{VIN} vid {CAL_LINK}",
        "where": ["app/services/x.py:12:f", "/etc/passwd:1:x", "app/../../hemligt.py:1:<lambda>; rm"],
    }, "inte en post"]}, allow_read_only=True)
    (entry,) = internal_errors.public_entries(db, user.id, scrub=(vehicle.registration_number,))
    assert entry["step"] is None and entry["type"] is None and entry["origin"] == "poll"
    assert entry["where"] == ["app/services/x.py:12:f"]
    assert_no_secrets(json.dumps(entry))


def test_settings_api_does_not_show_or_accept_internal_errors(client, db, user):
    internal_errors.save_failures(db, user.id, [internal_errors.describe("test.x", Boom("fel"))])
    db.commit()
    body = client.get("/api/v1/settings").json()
    assert "internal_errors" not in body["settings"]["system"]
    assert "internal_errors" not in body["defaults"]["system"]
    assert "last_version" in body["settings"]["system"]
    resp = client.patch("/api/v1/settings/system", json={"internal_errors": []})
    assert resp.status_code == 400
    assert len(internal_errors.entries(db, user.id)) == 1


# ----------------------------------------------------------------------------- granskningen av 1.4.1


def _recording_autopilot(monkeypatch) -> list:
    """Självgående läge, och autopiloten bara noterar att den körts (godkänner inget)."""
    ran: list = []
    monkeypatch.setattr(autopilot, "is_active", lambda db, user_id: True)
    monkeypatch.setattr(autopilot, "run_vehicle", lambda db, **kw: ran.append(True) or {})
    return ran


def _crash(*args, **kwargs):
    raise Boom("steget kraschade")


# Steg före autopiloten vars utfall den bygger på. merge = det tidigare regelverket (by_category
# av), som ägarens installation har sedan migreringen 0004: då körs sammanslagningen före.
AUTOPILOT_INPUTS = [
    ("start_time", start_time, "fill_from_distance"),
    ("merge", merge, "evaluate_vehicle"),
    ("gaps", gaps, "recompute"),
    ("places", places_registry, "recount_visits"),
    ("patterns", patterns, "rebuild"),
    ("calendar", calendar_matching, "match_vehicle"),
    ("classify", classify_engine, "classify_pending"),
    ("journeys", journeys, "detect"),
]


@pytest.mark.parametrize(("name", "module", "attr"), AUTOPILOT_INPUTS, ids=[s[0] for s in AUTOPILOT_INPUTS])
def test_autopilot_is_skipped_when_a_step_it_builds_on_failed(name, module, attr, db, user, vehicle, car,
                                                              monkeypatch, tolerant):
    """Granskningsfynd (1.4.2): autopiloten kördes fast t.ex. kalendersteget hade hoppats över.
    Utan kalendermatchningen finns ingen kalenderkonflikt, och resan kunde godkännas automatiskt.
    Nu körs autopiloten inte; resan förblir oavgjord och får sitt ärende i Granskning."""
    if name == "merge":
        settings_svc.update_section(db, user.id, "merge", {"by_category": False})
    ran = _recording_autopilot(monkeypatch)
    monkeypatch.setattr(module, attr, _crash)
    result = drive_until_processed(db, vehicle, car)
    db.commit()
    assert result["new_trips"] == 1
    assert f"pipeline.{name}" in result["processing_error"]["steps"]
    assert "pipeline.autopilot" not in result["processing_error"]["steps"]
    assert ran == []
    (trip,) = seg.active_trips(db, vehicle.id)
    assert trip.review_status == "needs_review" and trip.approval_source is None
    if name != "journeys":  # utan resekedjor finns ändå ärendet (det skapas av granskningsärendena)
        items = db.execute(select(M.ReviewItem.logical_trip_id).where(M.ReviewItem.kind == ReviewKind.CATEGORY))
        assert trip.id in items.scalars().all()


def test_autopilot_still_runs_when_only_consumption_failed(db, user, vehicle, car, monkeypatch, tolerant):
    ran = _recording_autopilot(monkeypatch)
    monkeypatch.setattr(consumption, "fill_vehicle", _crash)
    result = drive_until_processed(db, vehicle, car)
    assert result["processing_error"]["steps"] == ["pipeline.consumption"]
    assert ran == [True]


def test_autopilot_runs_when_nothing_failed(db, user, vehicle, car, monkeypatch, tolerant):
    ran = _recording_autopilot(monkeypatch)
    result = drive_until_processed(db, vehicle, car)
    assert "processing_error" not in result and ran == [True]


class Unprintable(Exception):
    """Ett undantag vars text inte går att läsa."""

    def __str__(self) -> str:
        raise TypeError("går inte att skriva ut")


def test_unprintable_exception_does_not_break_the_safety_net(client, db, user, vehicle, car, monkeypatch, tolerant):
    """Granskningsfynd (1.4.2): str(exc) i describe kastade, run_step kastade vidare och hela
    resan rullades tillbaka. Nu hoppas bara steget över, och posten säger att texten inte gick att läsa."""
    def broken(*args, **kwargs):
        raise Unprintable()

    monkeypatch.setattr(gaps, "recompute", broken)
    result = drive_until_processed(db, vehicle, car)
    db.commit()
    assert result["new_trips"] == 1 and result["processing_error"]["steps"] == ["pipeline.gaps"]
    assert len(seg.active_trips(db, vehicle.id)) == 1
    (entry,) = internal_errors.entries(db, user.id)
    assert entry["type"].endswith("Unprintable") and "texten kunde inte läsas" in entry["message"]
    assert entry["where"]


def test_unprintable_exception_in_detection_and_outside_the_steps(client, db, user, vehicle, car, monkeypatch,
                                                                  tolerant):
    def broken(*args, **kwargs):
        raise Unprintable()

    db.commit()
    monkeypatch.setattr(collector, "client_for", lambda *a, **k: car)
    monkeypatch.setattr(detection, "process_vehicle", broken)
    resp = client.post(f"{API}/sync", json={})
    assert resp.status_code == 200 and resp.json()["result"]["processing_error"]["step"] == "detection"
    # Utanför stegen: fortfarande 500 (TestClient kastar vidare), men felet antecknas.
    monkeypatch.setattr(collector, "store_observation", broken)
    with pytest.raises(Unprintable):
        client.post(f"{API}/sync", json={})
    db.expire_all()
    steps = [e["step"] for e in internal_errors.entries(db, user.id)]
    assert steps == ["volvo.poll", "detection"]


def test_note_failure_never_raises_even_if_describe_fails(db, monkeypatch, tolerant):
    def broken_frames(*args, **kwargs):
        raise RuntimeError("kodställena gick inte att läsa")

    monkeypatch.setattr(internal_errors, "_frames", broken_frames)
    assert internal_errors.run_step(db, "test.step", _crash, default="hoppat") == "hoppat"
    (failure,) = internal_errors.take_failures(db)
    assert failure["step"] == "test.step" and failure["type"].endswith("Boom") and failure["where"] == []


def _raise_here(message: str) -> Boom:
    try:
        raise Boom(message)
    except Boom as exc:
        return exc


def test_repeated_error_takes_one_place_and_keeps_older_errors(db, user):
    """Granskningsfynd (1.4.2): ett fel som upprepas vid varje hämtning fyllde listan på 20 och
    trängde ut äldre fel. Nu tar samma fel (steg, typ, kodställen) en plats, med antal och första tid."""
    internal_errors.save_failures(db, user.id, [internal_errors.describe("pipeline.gaps", _raise_here("en gång"))])
    first = internal_errors.entries(db, user.id)[0]["at"]
    for i in range(25):
        internal_errors.save_failures(
            db, user.id, [internal_errors.describe("pipeline.start_time", _raise_here(f"ihållande {i}"))], origin="poll"
        )
    stored = internal_errors.entries(db, user.id)
    assert [e["step"] for e in stored] == ["pipeline.start_time", "pipeline.gaps"]
    assert stored[0]["count"] == 25 and stored[0]["message"] == "ihållande 24" and stored[0]["first_at"]
    assert stored[1]["count"] == 1 and stored[1]["at"] == first
    (newest, older) = internal_errors.public_entries(db, user.id)
    assert newest["count"] == 25 and newest["first_at"] == stored[0]["first_at"] and older["count"] == 1
    # Ett annat fel i samma steg (annan typ) får en egen plats.
    internal_errors.save_failures(db, user.id, [internal_errors.describe("pipeline.start_time", ValueError("x"))])
    assert len(internal_errors.entries(db, user.id)) == 3


def test_poll_due_waits_its_interval_after_an_error_outside_the_steps(db, user, vehicle, car, monkeypatch, tolerant):
    """Granskningsfynd (1.4.2): tillbakarullningen ångrade last_sync_at, så schemaläggaren anropade
    Volvo vid varje tick (60 s). Nu väntar den sitt intervall (300 s), och felet tar en plats."""
    monkeypatch.setattr(collector, "client_for", lambda *a, **k: car)
    monkeypatch.setattr(collector, "store_observation", _crash)
    db.commit()
    polls = 0
    for minute in range(25):
        before = len(car.calls)
        out = collector.poll_due(db, now=m(minute))
        polls += len(car.calls) > before
        assert out["errors"] == (1 if minute % 5 == 0 else 0)
    assert polls == 5
    db.expire_all()
    (entry,) = internal_errors.entries(db, user.id)
    assert entry["step"] == "volvo.poll" and entry["count"] == 5
    conn = db.execute(select(M.IntegrationConnection)).scalar_one()
    assert conn.last_success_at is None  # "Senast synkad" påstår ingen lyckad hämtning
    assert "hämtningen" in conn.last_error


LEAKS = {
    "adress": "Storgatan 12, 702 10 Örebro",
    "token": "AT-gammal-0000000000",
    "vin_gemener": VIN.lower(),
    "regnr_gemener": "abc123",
    "kalender": "https://example.com/feeds/x?token=abcdef0123456789",
    "koordinat_kort": "59.090,15.000",
    "koordinat_mellanrum": "59.090, 15.180",
    "plats": "Sommarstugan Bäckaskog",
    "platsadress": "Bäckaskog 5",
    "klienthemlighet": "klienthemlighet-xyz",
}


def test_message_leaks_are_masked_in_storage_and_diagnostics(client, db, user, vehicle, car, monkeypatch, tolerant):
    """Granskningsfynd (1.4.2): adress, token utan nyckelnamn, VIN och regnr med gemener och korta
    koordinater följde med till felsökningsfilen. Nu tvättas också användarens kända värden
    (oavsett skiftläge) och adress-, regnr- och koordinatmönster."""
    vehicle.registration_number = "ABC123"
    db.add(M.KnownLocation(owner_user_id=user.id, name=LEAKS["plats"], address="Bäckaskog 5, 291 94 Kristianstad"))
    db.flush()

    def broken(*args, **kwargs):
        raise RuntimeError("Fel vid " + " | ".join(LEAKS.values()))

    monkeypatch.setattr(gaps, "recompute", broken)
    drive_until_processed(db, vehicle, car)
    db.commit()
    stored = json.dumps(internal_errors.entries(db, user.id), ensure_ascii=False)
    diag = client.get("/api/v1/volvo/diagnostics").text
    for text in (stored, diag, json.dumps(json.loads(diag), ensure_ascii=False)):
        leaked = {k: v for k, v in LEAKS.items() if v.lower() in text.lower()}
        assert leaked == {}, leaked
    assert json.loads(diag)["internal_errors"][0]["message"].startswith("Fel vid ")


def test_clean_message_masks_patterns_but_keeps_ordinary_text():
    text = internal_errors.clean_message(
        "Kungsvägen 3B, 111 22 Stockholm; Lilla Torget 4; yv1pza3test000042; abc123; 59.090,15.000 – got 100 rows "
        "in 1.234 s at 12:30:45.123, version 1.4.1"
    )
    for leak in ("Kungsvägen 3B", "111 22 Stockholm", "Torget 4", "yv1pza3test000042", "abc123", "59.090"):
        assert leak not in text, leak
    assert "got 100 rows" in text and "1.234 s" in text and "12:30:45.123" in text and "1.4.1" in text


@pytest.mark.parametrize("text", [
    "a-" * 50_000,
    "svar-" * 20_000,
    "trip-segment-merge-candidate-" * 700,
    "x" * 30 + "-" + "y" * 30 + "gatan 5",
], ids=["a-", "svar-", "kebab", "langt-ord"])
def test_clean_message_is_fast_for_long_hyphenated_text(text):
    """Granskningsfynd (1.4.2): adressmönstret tog kvadratisk tid på långa följder av ord och
    bindestreck (minuter för 100 000 tecken) och höll GIL medan hämtningens transaktion var öppen."""
    import time

    started = time.perf_counter()
    internal_errors.clean_message(text)
    internal_errors.describe("notify", Boom(text))
    assert time.perf_counter() - started < 3.0


def test_street_pattern_still_masks_ordinary_addresses():
    cases = {
        "Karl-Johans-gatan 5B": "[DOLD]",
        "x-y-Långholmsgatan 7": "[DOLD]",
        "123 Main Street 4": "123 Main [DOLD]",
        "Storgatan 5": "[DOLD]",
        "Påhittarvägen 12B": "[DOLD]",
        "Lilla Torget 4": "Lilla [DOLD]",
    }
    for address, masked in cases.items():
        assert internal_errors.clean_message(f"Kunde inte tolka {address}; okänt svar") == (
            f"Kunde inte tolka {masked}; okänt svar"
        ), address


# ----------------------------------------------------------------------------- utan fel: som 1.4.0


def _statements_for_a_trip(engine, *, tolerant_on: bool) -> tuple[list[str], int]:
    """Samma körning på en tom databas; returnerar SQL-satserna (utan sparpunkterna) i ordning."""
    from sqlalchemy import event

    from app import db as dbm
    from app.db import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = dbm.get_sessionmaker()()
    statements: list[str] = []

    def listen(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    try:
        u = M.User(email="forare@example.com", display_name="Test Förare", password_hash="x", is_admin=True)
        session.add(u)
        session.flush()
        v = M.Vehicle(owner_user_id=u.id, name="Testbil", make="Volvo", model="V90", is_default=True)
        session.add(v)
        session.flush()
        connect(session, u, v)
        session.commit()
        event.listen(engine, "before_cursor_execute", listen)
        with internal_errors.tolerant_mode(tolerant_on):
            result = drive_until_processed(session, v, FakeCar())
            session.commit()
        event.remove(engine, "before_cursor_execute", listen)
        assert result["new_trips"] == 1 and "processing_error" not in result
    finally:
        session.close()
    savepoints = [s for s in statements if s.upper().startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "BEGIN"))]
    return [s for s in statements if s not in savepoints], len(savepoints)


def test_without_errors_the_same_statements_run_as_before(engine):
    strict, strict_sp = _statements_for_a_trip(engine, tolerant_on=False)
    tolerant_, tolerant_sp = _statements_for_a_trip(engine, tolerant_on=True)
    assert strict_sp == 0  # strängt läge: inga sparpunkter alls (som 1.4.0)
    assert tolerant_sp > 0
    # Samma frågor, lika många. En sparpunkt skriver först det som väntar i sessionen
    # (Session.begin_nested gör alltid det), så en väntande INSERT/UPDATE kan komma något tidigare.
    assert len(tolerant_) == len(strict)
    assert sorted(tolerant_) == sorted(strict)
    # Läsfrågorna kommer i samma ordning.
    assert [q for q in tolerant_ if q.startswith("SELECT")] == [q for q in strict if q.startswith("SELECT")]


# ----------------------------------------------------------------------------- antecknandet fäller aldrig sparandet


def test_recording_the_error_never_undoes_the_reading_and_the_trip(client, db, user, vehicle, car, monkeypatch, tolerant):
    """Granskningsfynd (1.4.2): antecknandet av ett fel kördes oskyddat efter bearbetningen. Gick
    det sönder rullades hela hämtningen tillbaka – avläsningen och resan med. Nu körs det i en egen
    sparpunkt: resan finns, raden under "Senaste fel" visas, bara posten i listan saknas."""
    break_step(monkeypatch, gaps, "recompute")

    def cannot_save(db_, user_id, failures, *, origin=None):
        audit.record(db_, audit.SYSTEM, "test.halvfardigt", "vehicle", vehicle.id)
        db_.flush()
        raise Boom("listan gick inte att spara")

    monkeypatch.setattr(internal_errors, "save_failures", cannot_save)
    result = drive_until_processed(db, vehicle, car)
    db.commit()

    assert result["new_trips"] == 1 and result["processing_error"]["step"] == "pipeline.gaps"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(M.VehicleObservation)
                     .where(M.VehicleObservation.processed_at.is_(None))) == 0
    (trip,) = seg.active_trips(db, vehicle.id)
    assert trip.distance_km == 14
    # Det antecknandet hann skriva är borta, och listan är tom.
    assert db.scalar(select(func.count()).select_from(M.AuditEvent)
                     .where(M.AuditEvent.action == "test.halvfardigt")) == 0
    assert internal_errors.entries(db, user.id) == []
    assert client.get(f"{API}/status").json()["last_error"] == result["processing_error"]["message"]


def test_save_failures_safely_reports_whether_it_saved(db, user, monkeypatch):
    entry = internal_errors.describe("pipeline.gaps", _raise_here("en gång"))
    assert internal_errors.save_failures_safely(db, user.id, [], origin="poll") is True
    assert internal_errors.save_failures_safely(db, user.id, [entry], origin="poll") is True
    assert [e["step"] for e in internal_errors.entries(db, user.id)] == ["pipeline.gaps"]

    def boom(*args, **kwargs):
        raise Boom("går inte")

    monkeypatch.setattr(internal_errors, "save_failures", boom)
    assert internal_errors.save_failures_safely(db, user.id, [entry], origin="poll") is False
    db.commit()  # sessionen går att använda efteråt
    assert len(internal_errors.entries(db, user.id)) == 1


@pytest.mark.parametrize("where", [5, "app/x.py:1:f", None, {"a": 1}, [1, None]])
def test_a_stored_entry_in_unexpected_form_does_not_break_the_merging(db, user, where):
    """Granskningsfynd (1.4.2): en sparad post vars ``where`` inte är en lista fällde sammanslagningen
    av samma fel (och därmed hämtningen). Nu räknas den som en post utan kodställen."""
    settings_svc.update_section(
        db, user.id, internal_errors.SECTION,
        {internal_errors.KEY: [{"at": "2026-11-01T10:00:00+00:00", "step": "pipeline.gaps", "type": "Boom", "where": where},
                               "inte en post", 7]},
        allow_read_only=True,
    )
    db.flush()
    internal_errors.save_failures(db, user.id, [internal_errors.describe("pipeline.gaps", _raise_here("ny"))], origin="poll")
    stored = internal_errors.entries(db, user.id)
    assert [e["step"] for e in stored] == ["pipeline.gaps", "pipeline.gaps"] and stored[0]["count"] == 1
    assert len(internal_errors.public_entries(db, user.id)) == 2
