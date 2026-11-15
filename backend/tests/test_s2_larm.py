"""S2b: larm och Home Assistant-kopplingen (byggkontraktet C.5).

Varje larmvillkor, en push per episod (en kalender som felar i flera dygn ger ett larm),
ingen push när felet försvinner, högst två larm per dygn och tysta timmar. Automatiskt
telefonval och ``phone_missing``. Nya försök i kalendersynken och omklassning efter den.
Integrationens del (en enda telefon, reparationsärende och lokal notis vid avbrott)
prövas med Home Assistant ersatt av små attrapper.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import IntegrationProvider, IntegrationStatus, NotificationKind, NotificationStatus
from app.services import integrations
from app.services import settings as settings_svc
from app.services.calendar import sync as cal_sync
from app.services.ha import alerts, connection, notifier
from tests.test_ha_support import NOW, SERVICE, fake_ha, setup_pull, setup_push  # noqa: F401

API = "/api/v1"


@pytest.fixture(autouse=True)
def _reset_alert_clock(monkeypatch):
    monkeypatch.setattr(alerts, "_last_run_at", None)


@pytest.fixture()
def phone(db, user, fake_ha):
    setup_push(db, user)
    return fake_ha


def volvo(db, user, *, status=IntegrationStatus.CONNECTED, last_success=None, grant_started=None):
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.VOLVO)
    conn.status = status
    conn.last_success_at = last_success
    cfg: dict[str, Any] = {"mode": "oauth"}
    if grant_started is not None:
        cfg["grant_started_at"] = grant_started.isoformat()
    integrations.update_config(conn, cfg)
    db.flush()
    return conn


def calendar(db, user, *, status=IntegrationStatus.CONNECTED, last_success=None):
    conn = integrations.get_or_create(db, user.id, IntegrationProvider.MICROSOFT)
    conn.status = status
    conn.last_success_at = last_success
    db.flush()
    return conn


def observe(db, vehicle, reported_at):
    db.add(M.VehicleObservation(
        vehicle_id=vehicle.id, fetched_at=reported_at, last_seen_at=reported_at, odometer_km=1000.0,
        odometer_reported_at=reported_at, content_hash=f"h-{reported_at.isoformat()}",
    ))
    db.flush()


def alert_rows(db, code: str | None = None) -> list[M.NotificationRequest]:
    q = select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.ALERT)
    if code:
        q = q.where(M.NotificationRequest.dedupe_key.like(f"alert:{code}:%"))
    return list(db.execute(q.order_by(M.NotificationRequest.created_at)).scalars())


def tick(db, user, now) -> dict:
    """Som bakgrundsjobbet: larmkontrollen och sedan leveransen."""
    out = alerts.run(db, now=now, force=True)
    notifier.deliver_user(db, user.id, now)
    db.flush()
    return out


# ----------------------------------------------------------------------------- larmvillkoren


def test_failing_calendar_for_days_gives_one_push_and_none_on_recovery(db, user, phone):
    last = NOW - timedelta(days=2)
    conn = calendar(db, user, status=IntegrationStatus.ERROR, last_success=last)
    now = NOW
    for _ in range(4 * 24):  # fyra dygn, var 15:e minut... glesare räcker: var timme
        tick(db, user, now)
        conn.last_error_at = now  # felet upprepas – episoden får inte följa felets tid
        now += timedelta(hours=1)
    rows = alert_rows(db, alerts.CALENDAR_BROKEN)
    assert len(rows) == 1 and rows[0].status == NotificationStatus.SENT
    assert rows[0].title == "DriveLog: Kalendern synkas inte"
    assert len(phone.sent) == 1 and phone.sent[0]["title"] == "DriveLog: Kalendern synkas inte"

    # Felet försvinner: larmet rensas på telefonen, men ingen ny push skickas.
    conn.status = IntegrationStatus.CONNECTED
    conn.last_success_at = now
    tick(db, user, now)
    tick(db, user, now + timedelta(hours=1))
    db.refresh(rows[0])
    assert rows[0].status == NotificationStatus.CANCELLED
    assert len(phone.sent) == 1
    assert len(phone.clears) == 1


def test_calendar_stale_over_24h_while_connected(db, user, phone):
    calendar(db, user, last_success=NOW - timedelta(hours=23))
    tick(db, user, NOW)
    assert alert_rows(db) == []
    calendar(db, user, last_success=NOW - timedelta(hours=25))
    tick(db, user, NOW)
    rows = alert_rows(db, alerts.CALENDAR_BROKEN)
    assert len(rows) == 1 and "inte synkats sedan" in rows[0].message


def test_calendar_turned_off_or_never_connected_gives_no_alert(db, user, phone):
    calendar(db, user, status=IntegrationStatus.CONFIGURED, last_success=None)
    tick(db, user, NOW)
    assert alert_rows(db) == []
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=3))
    settings_svc.update_section(db, user.id, "calendar", {"enabled": False})
    tick(db, user, NOW)
    assert alert_rows(db) == []


def test_volvo_needs_reauth(db, user, phone):
    conn = volvo(db, user, status=IntegrationStatus.NEEDS_REAUTH, last_success=NOW - timedelta(hours=2))
    tick(db, user, NOW)
    tick(db, user, NOW + timedelta(days=1))
    rows = alert_rows(db, alerts.VOLVO_REAUTH)
    assert len(rows) == 1 and rows[0].title == "DriveLog: Volvo behöver ny inloggning"
    assert alert_rows(db, alerts.VOLVO_FETCH_FAILING) == []  # samma fel larmas inte två gånger
    assert len(phone.sent) == 1
    # Ny inloggning: larmet tas bort utan push.
    conn.status = IntegrationStatus.CONNECTED
    conn.last_success_at = NOW + timedelta(days=1, hours=1)
    tick(db, user, NOW + timedelta(days=1, hours=1))
    db.refresh(rows[0])
    assert rows[0].status == NotificationStatus.CANCELLED and len(phone.sent) == 1


def test_volvo_fetch_failing_after_6h(db, user, phone):
    conn = volvo(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(hours=5))
    tick(db, user, NOW)
    assert alert_rows(db) == []
    tick(db, user, NOW + timedelta(hours=2))
    rows = alert_rows(db, alerts.VOLVO_FETCH_FAILING)
    assert len(rows) == 1 and "försöker igen" in rows[0].message
    # Fortsatt fel med samma senaste lyckade hämtning: inget nytt larm.
    tick(db, user, NOW + timedelta(hours=8))
    assert len(alert_rows(db, alerts.VOLVO_FETCH_FAILING)) == 1
    # En lyckad hämtning avslutar episoden; ett nytt avbrott blir en ny episod.
    conn.status = IntegrationStatus.CONNECTED
    conn.last_success_at = NOW + timedelta(hours=9)
    tick(db, user, NOW + timedelta(hours=9))
    assert alert_rows(db, alerts.VOLVO_FETCH_FAILING)[0].status == NotificationStatus.CANCELLED
    conn.status = IntegrationStatus.ERROR
    tick(db, user, NOW + timedelta(hours=22))  # utanför tysta timmar
    assert len(alert_rows(db, alerts.VOLVO_FETCH_FAILING)) == 2
    assert len(phone.sent) == 2


def test_volvo_reauth_soon_at_14_and_3_days(db, user, phone):
    grant = NOW - timedelta(days=80)  # behörigheten gäller i högst 3 månader
    volvo(db, user, last_success=NOW, grant_started=grant)
    from app.services.volvo import oauth

    due = oauth.reauth_due_at(integrations.get(db, user.id, IntegrationProvider.VOLVO))
    tick(db, user, due - timedelta(days=15))
    assert alert_rows(db) == []

    t14 = due - timedelta(days=13)
    volvo(db, user, last_success=t14, grant_started=grant)
    tick(db, user, t14)
    tick(db, user, t14 + timedelta(days=1))
    rows = alert_rows(db, alerts.VOLVO_REAUTH_SOON)
    assert len(rows) == 1 and rows[0].dedupe_key.endswith(":14")
    assert "Logga in på Volvo igen senast" in rows[0].message

    t3 = due - timedelta(days=2)
    volvo(db, user, last_success=t3, grant_started=grant)
    tick(db, user, t3)
    rows = alert_rows(db, alerts.VOLVO_REAUTH_SOON)
    assert [r.dedupe_key[-2:] for r in rows] == ["14", ":3"]
    assert rows[0].status == NotificationStatus.CANCELLED  # det äldre larmet ersätts tyst
    assert len(phone.sent) == 2

    # Ny inloggning: ny behörighet, larmet försvinner utan push.
    volvo(db, user, last_success=t3, grant_started=t3)
    tick(db, user, t3 + timedelta(hours=1))
    assert all(r.status == NotificationStatus.CANCELLED for r in alert_rows(db, alerts.VOLVO_REAUTH_SOON))
    assert len(phone.sent) == 2


def test_vehicle_silent_says_it_is_normal_and_has_no_vin(db, user, vehicle, phone):
    vehicle.vin = "YV1TESTVIN0000001"
    volvo(db, user, last_success=NOW - timedelta(minutes=5))
    observe(db, vehicle, NOW - timedelta(days=3))
    tick(db, user, NOW)
    assert alert_rows(db) == []
    tick(db, user, NOW + timedelta(days=2))
    rows = alert_rows(db, alerts.VEHICLE_SILENT)
    assert len(rows) == 1
    assert "normalt när bilen står" in rows[0].message
    assert vehicle.vin not in rows[0].message and "ABC123" not in rows[0].message
    tick(db, user, NOW + timedelta(days=3))
    assert len(alert_rows(db, alerts.VEHICLE_SILENT)) == 1
    # Bilen hör av sig: larmet rensas.
    observe(db, vehicle, NOW + timedelta(days=3, hours=1))
    tick(db, user, NOW + timedelta(days=3, hours=2))
    assert alert_rows(db, alerts.VEHICLE_SILENT)[0].status == NotificationStatus.CANCELLED
    assert len(phone.sent) == 1


def test_vehicle_silent_needs_working_fetch_and_ignores_demo(db, user, vehicle, phone):
    observe(db, vehicle, NOW - timedelta(days=10))
    volvo(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(hours=1))
    tick(db, user, NOW)
    assert alert_rows(db, alerts.VEHICLE_SILENT) == []
    vehicle.is_demo = True
    volvo(db, user, last_success=NOW - timedelta(minutes=1))
    tick(db, user, NOW)
    assert alert_rows(db, alerts.VEHICLE_SILENT) == []


def test_alert_texts_have_no_addresses(db, user, vehicle, phone):
    volvo(db, user, status=IntegrationStatus.NEEDS_REAUTH, last_success=NOW - timedelta(days=1))
    calendar(db, user, status=IntegrationStatus.NEEDS_REAUTH, last_success=NOW - timedelta(days=1))
    tick(db, user, NOW)
    for row in alert_rows(db):
        assert row.title.startswith("DriveLog: ")
        assert [a["key"] for a in row.actions] == ["OPEN"]
        assert "gatan" not in row.message and "vägen" not in row.message and "ABC123" not in row.message


# ----------------------------------------------------------------------------- gränser


def test_at_most_two_alerts_per_day(db, user, vehicle, phone):
    volvo(db, user, status=IntegrationStatus.NEEDS_REAUTH, last_success=NOW - timedelta(days=1))
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=2))
    tick(db, user, NOW)
    assert len(alert_rows(db)) == 2
    # Ett tredje fel samma dygn väntar.
    vconn = integrations.get(db, user.id, IntegrationProvider.VOLVO)
    vconn.status = IntegrationStatus.ERROR
    vconn.last_success_at = NOW - timedelta(hours=7)
    out = tick(db, user, NOW + timedelta(hours=1))
    assert out["deferred_cap"] >= 1
    assert len(alert_rows(db, alerts.VOLVO_FETCH_FAILING)) == 0
    # Nästa dygn får det plats.
    tick(db, user, NOW + timedelta(days=1, minutes=1))
    assert len(alert_rows(db, alerts.VOLVO_FETCH_FAILING)) == 1


def test_run_is_throttled_to_every_15_minutes(db, user, phone):
    assert "skipped" not in alerts.run(db, now=NOW)
    assert alerts.run(db, now=NOW + timedelta(minutes=10)) == {"skipped": 1}
    assert "skipped" not in alerts.run(db, now=NOW + timedelta(minutes=15))


def test_quiet_hours_delay_the_push(db, user, phone):
    night = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)  # 23:00 i Stockholm
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=night - timedelta(days=2))
    tick(db, user, night)
    assert len(alert_rows(db)) == 1 and phone.sent == []
    tick(db, user, night + timedelta(hours=8))  # 07:00 lokal tid
    assert len(phone.sent) == 1


def test_no_alert_without_channel_or_with_notifications_off(db, user):
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=2))
    alerts.run(db, now=NOW, force=True)
    assert alert_rows(db) == []
    setup_pull(db, user, enabled=False)
    alerts.run(db, now=NOW, force=True)
    assert alert_rows(db) == []
    settings_svc.update_section(db, user.id, "notifications", {"enabled": True})
    alerts.run(db, now=NOW, force=True)
    assert len(alert_rows(db)) == 1


def test_alert_waits_in_pull_outbox_without_phone(db, user):
    """Utan vald telefon i pull-läget ligger larmet kvar i utkorgen (C.4)."""
    conn = setup_pull(db, user)
    integrations.update_config(conn, {"notify_service": None})
    calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=2))
    tick(db, user, NOW)
    items = notifier.outbox(db, user.id, NOW)
    assert len(items) == 1 and items[0]["service"] is None
    notifier.outbox_ack(db, user.id, delivered=[], failed=[{"id": items[0]["id"], "error": "Ingen notify.mobile_app-tjänst är vald."}])
    assert len(notifier.outbox(db, user.id, NOW + timedelta(minutes=1))) == 1


# ----------------------------------------------------------------------------- telefonval


def _heartbeat(anon_client, token, services, selected=None):
    body = {"ha_version": "2026.11.1", "integration_version": "1.1.0", "notify_services": services}
    if selected:
        body["notify_service"] = selected
    r = anon_client.post(f"{API}/ha/heartbeat", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def ha_token(client) -> str:
    resp = client.post(f"{API}/auth/tokens", json={"name": "Home Assistant", "scopes": ["ha"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def test_exactly_one_phone_is_selected_automatically(client, anon_client, ha_token, db, user):
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull"})
    db.commit()
    out = _heartbeat(anon_client, ha_token, ["notify.mobile_app_telefon", "notify.persistent_notification"])
    assert out["notify_service"] == "mobile_app_telefon"
    status = client.get(f"{API}/integrations/home-assistant").json()
    assert status["notify_service"] == "mobile_app_telefon" and status["phone_missing"] is False
    # Ett senare val ändras inte när en andra telefon dyker upp.
    out = _heartbeat(anon_client, ha_token, ["mobile_app_telefon", "mobile_app_platta"])
    assert out["notify_service"] == "mobile_app_telefon"


def test_two_phones_are_never_guessed(client, anon_client, ha_token, db, user):
    settings_svc.update_section(db, user.id, "notifications", {"delivery_mode": "pull"})
    db.commit()
    out = _heartbeat(anon_client, ha_token, ["mobile_app_telefon", "mobile_app_platta"])
    assert out["notify_service"] is None
    status = client.get(f"{API}/integrations/home-assistant").json()
    assert status["phone_missing"] is True
    # Integrationens eget val räcker.
    out = _heartbeat(anon_client, ha_token, ["mobile_app_telefon", "mobile_app_platta"], selected="notify.mobile_app_platta")
    assert out["notify_service"] == "mobile_app_platta"
    assert client.get(f"{API}/integrations/home-assistant").json()["phone_missing"] is False


def test_phone_missing_rules(db, user):
    assert connection.public_status(db, user.id)["phone_missing"] is False  # HA används inte
    conn = setup_pull(db, user)
    integrations.update_config(conn, {"notify_service": None, "heartbeat": {"notify_services": []}})
    assert connection.public_status(db, user.id)["phone_missing"] is True
    integrations.update_config(conn, {"heartbeat": {"notify_services": ["mobile_app_telefon"]}})
    assert connection.public_status(db, user.id)["phone_missing"] is False  # väljs vid nästa heartbeat
    assert connection.auto_select_phone(conn) == "mobile_app_telefon"
    assert connection.auto_select_phone(conn) is None  # redan valt
    integrations.update_config(conn, {"notify_service": None, "heartbeat": {"notify_services": ["a", "b"]}})
    assert connection.public_status(db, user.id)["phone_missing"] is True
    conn.status = IntegrationStatus.DEMO
    assert connection.public_status(db, user.id)["phone_missing"] is False


# ----------------------------------------------------------------------------- kalendersynken


def test_calendar_sync_retries_error_connections_and_reclassifies(db, user, vehicle, monkeypatch):
    from app.services.calendar import matching
    from app.services.learning import feedback

    conn = calendar(db, user, status=IntegrationStatus.ERROR, last_success=NOW - timedelta(days=1))
    calls: list[str] = []

    def fake_sync(db_, user_id, *, now=None, transport=None):
        calls.append("sync")
        c = integrations.get(db_, user_id, IntegrationProvider.MICROSOFT)
        integrations.mark_success(c, now)
        return {"status": "ok"}

    monkeypatch.setattr(cal_sync, "sync_user", fake_sync)
    monkeypatch.setattr(matching, "match_vehicle", lambda db_, *, user_id, vehicle, since=None: calls.append("match") or 0)
    monkeypatch.setattr(feedback, "after_decisions", lambda db_, *, user_id, vehicle_id, actor: calls.append("reclassify"))

    conn.last_sync_at = NOW - timedelta(minutes=45)  # glesare för ERROR: inte än
    db.flush()
    assert cal_sync.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 0}
    conn.last_sync_at = NOW - timedelta(hours=3)
    db.flush()
    assert cal_sync.sync_all_due(db, now=NOW) == {"synced": 1, "failed": 0}
    assert calls == ["sync", "match", "reclassify"]
    db.refresh(conn)
    assert conn.status == IntegrationStatus.CONNECTED


def test_calendar_sync_skips_needs_reauth(db, user, monkeypatch):
    calendar(db, user, status=IntegrationStatus.NEEDS_REAUTH, last_success=NOW - timedelta(days=1))
    monkeypatch.setattr(cal_sync, "sync_user", lambda *a, **k: pytest.fail("ska inte synkas"))
    assert cal_sync.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 0}


def test_calendar_sync_failure_is_counted_and_others_continue(db, user, monkeypatch):
    calendar(db, user, status=IntegrationStatus.CONNECTED, last_success=NOW - timedelta(days=1))

    def failing(db_, user_id, *, now=None, transport=None):
        raise cal_sync.CalendarError("nere", "graph_error")

    monkeypatch.setattr(cal_sync, "sync_user", failing)
    assert cal_sync.sync_all_due(db, now=NOW) == {"synced": 0, "failed": 1}


# ----------------------------------------------------------------------------- integrationen i HA

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT = REPO_ROOT / "custom_components" / "smgd_drivelog"


class _Errors(Exception):
    pass


def _fake_ha_modules() -> dict[str, types.ModuleType]:
    """Minsta möjliga attrapper av de Home Assistant-moduler som manager.py importerar."""
    mods: dict[str, types.ModuleType] = {}

    def mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        mods[name] = m
        return m

    mod("homeassistant")
    mod("homeassistant.helpers")
    mod("homeassistant.util")
    mod("homeassistant.config_entries").ConfigEntry = object
    mod("homeassistant.const").__version__ = "2026.11.1"
    core_mod = mod("homeassistant.core")
    core_mod.Event = object
    core_mod.HomeAssistant = object
    core_mod.callback = lambda fn: fn
    exc = mod("homeassistant.exceptions")
    exc.HomeAssistantError = _Errors

    class Store:
        def __init__(self, *a, **k):
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    mod("homeassistant.helpers.storage").Store = Store
    issues = mod("homeassistant.helpers.issue_registry")
    issues.created = []
    issues.deleted = []
    issues.IssueSeverity = types.SimpleNamespace(WARNING="warning")
    issues.async_create_issue = lambda hass, domain, issue_id, **kw: issues.created.append((issue_id, kw))
    issues.async_delete_issue = lambda hass, domain, issue_id: issues.deleted.append(issue_id)
    mods["homeassistant.helpers"].issue_registry = issues
    mod("homeassistant.util.dt").utcnow = lambda: datetime.now(UTC)
    mods["homeassistant.util"].dt = mods["homeassistant.util.dt"]
    return mods


@pytest.fixture()
def manager_mod(monkeypatch):
    for name, m in _fake_ha_modules().items():
        monkeypatch.setitem(sys.modules, name, m)
    pkg = types.ModuleType("smgd_drivelog_s2b")
    pkg.__path__ = [str(COMPONENT)]
    monkeypatch.setitem(sys.modules, "smgd_drivelog_s2b", pkg)

    api = types.ModuleType("smgd_drivelog_s2b.api")  # api.py kräver aiohttp, som inte finns här

    class DriveLogError(Exception):
        pass

    class DriveLogAuthError(DriveLogError):
        pass

    class DriveLogConnectionError(DriveLogError):
        pass

    class DriveLogRejectedError(DriveLogError):
        pass

    api.DriveLogError, api.DriveLogAuthError = DriveLogError, DriveLogAuthError
    api.DriveLogConnectionError, api.DriveLogRejectedError = DriveLogConnectionError, DriveLogRejectedError
    api.DriveLogApiClient = object
    monkeypatch.setitem(sys.modules, "smgd_drivelog_s2b.api", api)
    for name in ("const", "core", "manager"):
        spec = importlib.util.spec_from_file_location(f"smgd_drivelog_s2b.{name}", COMPONENT / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return sys.modules["smgd_drivelog_s2b.manager"]


class _Services:
    def __init__(self, notify: list[str]) -> None:
        self.notify = notify
        self.calls: list[tuple[str, str, dict]] = []

    def async_services(self):
        return {"notify": {name: {} for name in self.notify}}

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class _Client:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.body: dict = {"delivery_mode": "pull", "items": []}
        self.acks: list[dict] = []

    async def outbox(self):
        if self.error:
            raise self.error
        return self.body

    async def ack(self, body):
        self.acks.append(body)
        return {}


def _manager(manager_mod, notify: list[str], *, configured: str | None = None):
    hass = types.SimpleNamespace(services=_Services(notify))
    entry = types.SimpleNamespace(entry_id="post1", options={}, data={"notify_service": configured} if configured else {})
    client = _Client()
    return manager_mod.DriveLogManager(hass, entry, client), hass, client


def _item(i: int) -> dict:
    return {"id": f"n{i}", "type": "notify", "service": None, "data": {"title": "DriveLog", "message": "Ny resa"}}


def test_integration_uses_the_only_phone(manager_mod):
    mgr, hass, client = _manager(manager_mod, ["mobile_app_telefon", "persistent_notification"])
    client.body = {"items": [_item(1)]}
    assert asyncio.run(mgr.async_poll_outbox()) == 1
    assert hass.services.calls[0][:2] == ("notify", "mobile_app_telefon")
    assert client.acks == [{"delivered": ["n1"], "failed": []}]


def test_repair_issue_without_phone_and_removed_when_one_appears(manager_mod):
    issues = sys.modules["homeassistant.helpers.issue_registry"]
    mgr, hass, client = _manager(manager_mod, ["mobile_app_telefon", "mobile_app_platta"])
    client.body = {"items": [_item(1)]}
    assert asyncio.run(mgr.async_poll_outbox()) == 0
    assert hass.services.calls == []
    assert len(issues.created) == 1 and issues.created[0][0] == "no_phone_post1"
    assert issues.created[0][1]["translation_key"] == "no_phone"
    assert client.acks[0]["failed"][0]["id"] == "n1"  # ligger kvar i DriveLogs utkorg
    asyncio.run(mgr.async_poll_outbox())
    assert len(issues.created) == 1  # ett ärende, inte ett per hämtning
    # Ägaren väljer telefon i DriveLog.
    mgr.server_notify_service = "mobile_app_platta"
    assert asyncio.run(mgr.async_poll_outbox()) == 1
    assert issues.deleted == ["no_phone_post1"]


def test_local_notice_after_30_minutes_once_per_outage(manager_mod, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(manager_mod.time, "time", lambda: clock["t"])
    api = sys.modules["smgd_drivelog_s2b.api"]
    mgr, hass, client = _manager(manager_mod, ["mobile_app_telefon"])
    client.error = api.DriveLogConnectionError("nere")
    for _ in range(30):  # 29 minuter: ingen notis
        asyncio.run(mgr.async_poll_outbox())
        clock["t"] += 60
    assert hass.services.calls == []
    clock["t"] += 120
    for _ in range(20):
        asyncio.run(mgr.async_poll_outbox())
        clock["t"] += 60
    notes = [c for c in hass.services.calls if c[2].get("title") == "SMGD DriveLog nås inte"]
    assert len(notes) == 1 and notes[0][1] == "mobile_app_telefon"
    assert "30 minuter" in notes[0][2]["message"]
    # DriveLog nås igen – avbrottet är slut. Ett nytt avbrott ger en ny notis.
    client.error = None
    asyncio.run(mgr.async_poll_outbox())
    client.error = api.DriveLogConnectionError("nere igen")
    for _ in range(40):
        asyncio.run(mgr.async_poll_outbox())
        clock["t"] += 60
    notes = [c for c in hass.services.calls if c[2].get("title") == "SMGD DriveLog nås inte"]
    assert len(notes) == 2


def test_local_notice_immediately_on_401(manager_mod, monkeypatch):
    api = sys.modules["smgd_drivelog_s2b.api"]
    mgr, hass, client = _manager(manager_mod, ["mobile_app_telefon"])
    reauth: list[Any] = []
    mgr.entry.async_start_reauth = lambda hass_: reauth.append(True)
    client.error = api.DriveLogAuthError("HTTP 401")
    asyncio.run(mgr.async_poll_outbox())
    asyncio.run(mgr.async_poll_outbox())
    notes = [c for c in hass.services.calls if c[2].get("title") == "SMGD DriveLog nås inte"]
    assert len(notes) == 1 and "API-token" in notes[0][2]["message"]
    assert reauth == [True]


def test_integration_never_logs_or_stores_payload(manager_mod, caplog):
    mgr, hass, client = _manager(manager_mod, ["mobile_app_a", "mobile_app_b"])
    secret = "Kundbesök Hemlig AB"
    client.body = {"items": [{"id": "n1", "type": "notify", "service": None,
                              "data": {"title": secret, "message": secret}}]}
    with caplog.at_level("DEBUG"):
        asyncio.run(mgr.async_poll_outbox())
    assert secret not in caplog.text
    assert secret not in repr(mgr.diagnostics())
    assert secret not in repr(mgr.queue.to_dict())
