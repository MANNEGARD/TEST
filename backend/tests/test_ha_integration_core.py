"""Enhetstester för custom-integrationens rena hjälplogik (utan Home Assistant installerat).

``custom_components/smgd_drivelog/__init__.py`` importerar Home Assistant, så modulen
``core.py`` laddas direkt från fil. Repo-roten läggs dessutom på sys.path.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from app.services.ha import actions as A

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT = REPO_ROOT / "custom_components" / "smgd_drivelog"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"smgd_drivelog_{name}", COMPONENT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


core = _load("core")
const = _load("const")

PID = "0123456789abcdef"


# ----------------------------------------------------------------------------- åtgärds-id


def test_action_ids_match_backend():
    for key in (A.WORK, A.PRIVATE, A.REVIEW, A.OPEN, A.LATER, A.MERGE_YES, A.MERGE_NO, A.PURPOSE_TEXT, "PURPOSE2",
                A.TEST_OK):
        action = A.action_id(key, PID)
        assert core.parse_action_id(action) == (key, PID) == A.parse_action_id(action)
    assert core.ACTION_PREFIX == A.PREFIX


@pytest.mark.parametrize("value", [None, 5, "", "WORK", "OTHER_WORK_0123456789abcdef", "DRIVELOG_", "DRIVELOG_WORK",
                                   "DRIVELOG_work_0123456789abcdef", "DRIVELOG_WORK_ABC", "DRIVELOG_WORK_x!"])
def test_foreign_actions_are_ignored(value):
    assert core.parse_action_id(value) is None
    assert core.is_drivelog_action({"action": value}) is False


def test_callback_payload_from_ios_event():
    event = {"action": f"DRIVELOG_PURPOSETEXT_{PID}", "reply_text": "Kundmöte", "action_data": {"drivelog_id": PID}}
    assert core.build_callback_payload(event, "01JABCDEFGHJKMNPQRSTVWXYZ0") == {
        "public_id": PID, "action": f"DRIVELOG_PURPOSETEXT_{PID}", "event_id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "reply_text": "Kundmöte",
    }
    # Utan action_data och utan text (knapp).
    payload = core.build_callback_payload({"action": f"DRIVELOG_WORK_{PID}"}, "event-0001")
    assert payload == {"public_id": PID, "action": f"DRIVELOG_WORK_{PID}", "event_id": "event-0001"}
    # Andra integrationers notiser ignoreras.
    assert core.build_callback_payload({"action": "ALARM_OFF"}, "event-0002") is None
    # Motsägelsefull händelse skickas inte.
    assert core.build_callback_payload({"action": f"DRIVELOG_WORK_{PID}", "action_data": {"drivelog_id": "fff111"}},
                                       "event-0003") is None
    with pytest.raises(ValueError):
        core.build_callback_payload({"action": f"DRIVELOG_WORK_{PID}"}, "kort")


def test_callback_payload_matches_backend_schema():
    from app.api.routes.ha import CallbackIn

    payload = core.build_callback_payload({"action": f"DRIVELOG_WORK_{PID}", "reply_text": "x" * 5000}, "e" * 26)
    model = CallbackIn(**payload)
    assert len(model.reply_text) == 2000 and model.public_id == PID


# ----------------------------------------------------------------------------- konfiguration


def test_normalize_base_url():
    assert core.normalize_base_url(" https://drivelog.example.se/ ") == "https://drivelog.example.se"
    assert core.normalize_base_url("http://10.0.0.5:8000/api/v1/") == "http://10.0.0.5:8000"
    assert core.normalize_base_url("https://x.se/drivelog") == "https://x.se/drivelog"
    for bad in ("", "ftp://x", "drivelog.local", "https://user:pw@x.se", "https://x.se/?a=1"):
        with pytest.raises(ValueError):
            core.normalize_base_url(bad)


def test_paths_exist_in_backend_routes():
    from app.main import create_app

    paths = create_app().openapi()["paths"]
    routes = {(m.upper(), path) for path, ops in paths.items() for m in ops}
    for method, path in (("GET", core.PATH_WHOAMI), ("GET", core.PATH_SUMMARY), ("GET", core.PATH_OUTBOX),
                         ("POST", core.PATH_OUTBOX_ACK), ("POST", core.PATH_HEARTBEAT), ("POST", core.PATH_CALLBACK)):
        assert (method, path) in routes, path


def test_notify_services():
    assert core.notify_service_name("notify.mobile_app_simons_iphone") == "mobile_app_simons_iphone"
    assert core.notify_service_name("notify.persistent_notification") is None
    assert core.notify_service_name(None) is None
    assert core.mobile_app_services(["mobile_app_b", "notify.mobile_app_a", "tv", "mobile_app_b"]) == [
        "mobile_app_a", "mobile_app_b"]
    assert core.select_service(None, "notify.mobile_app_a") == "mobile_app_a"
    assert core.select_service("mobile_app_x", "mobile_app_a") == "mobile_app_x"
    assert core.select_service(None, None) is None
    # 1.1.0: inget valt men exakt en telefon i HA – den används. Med flera gissas aldrig.
    assert core.select_service(None, None, available=["notify.mobile_app_a", "persistent_notification"]) == "mobile_app_a"
    assert core.select_service(None, None, available=["mobile_app_a", "mobile_app_b"]) is None
    assert core.select_service(None, "mobile_app_b", available=["mobile_app_a"]) == "mobile_app_b"


def test_token_scope():
    assert core.token_has_scope({"scopes": ["ha"]})
    assert core.token_has_scope({"scopes": ["*"]})
    assert not core.token_has_scope({"scopes": ["export"]})
    assert not core.token_has_scope(None)


def test_heartbeat_matches_backend_schema():
    from app.api.routes.ha import HeartbeatIn

    body = core.build_heartbeat(ha_version="2026.11.1", integration_version="1.0.0",
                                services=["mobile_app_iphone", "persistent_notification"],
                                selected="notify.mobile_app_iphone", queue_length=3)
    assert body == {"ha_version": "2026.11.1", "integration_version": "1.0.0",
                    "notify_services": ["mobile_app_iphone"], "notify_service": "mobile_app_iphone", "queue_length": 3}
    HeartbeatIn(**body)


# ----------------------------------------------------------------------------- utkorg


def test_parse_outbox_and_ack():
    body = {"delivery_mode": "pull", "items": [
        {"id": "aaaaaa1", "type": "notify", "service": "mobile_app_iphone",
         "data": {"title": "Ny resa", "message": "Ny resa: 4,2 mil.", "data": {"tag": "drivelog-aaaaaa1"}}},
        {"id": "bbbbbb2", "type": "clear", "service": None,
         "data": {"message": "clear_notification", "data": {"tag": "drivelog-bbbbbb2"}}},
        {"id": "cccccc3", "type": "notify", "service": None, "data": {"title": "x"}},
        "skräp",
    ]}
    deliveries, failed = core.parse_outbox(body, "notify.mobile_app_ipad")
    assert [(d.id, d.type, d.service) for d in deliveries] == [
        ("aaaaaa1", "notify", "mobile_app_iphone"), ("bbbbbb2", "clear", "mobile_app_ipad")]
    assert [f["id"] for f in failed] == ["cccccc3"]
    _, failed = core.parse_outbox(body, None)
    assert {f["id"] for f in failed} == {"bbbbbb2", "cccccc3"}
    assert core.parse_outbox({}, None) == ([], [])

    from app.api.routes.ha import AckIn

    batches = core.build_ack([f"id{i:05d}" for i in range(150)], [{"id": "x" * 40, "error": "e" * 400}])
    assert len(batches) == 2 and len(batches[0]["delivered"]) == 100 and len(batches[1]["delivered"]) == 50
    for b in batches:
        AckIn(**b)
    assert core.build_ack([], []) == []


def test_outbox_items_from_backend_parse(db, user, vehicle, trips):
    """Utkorgens format från backend tolkas av integrationen."""
    from app.services.ha import notifier
    from tests.test_ha_support import after_batch, enqueue_classify, recent_trip, setup_pull

    setup_pull(db, user)
    enqueue_classify(db, user, vehicle, recent_trip(trips))
    items = notifier.outbox(db, user.id, now=after_batch())
    deliveries, failed = core.parse_outbox({"items": items}, None)
    assert failed == [] and len(deliveries) == 1
    actions = deliveries[0].data["data"]["actions"]
    assert all(core.parse_action_id(a["action"]) for a in actions)


def test_feedback_message():
    assert core.feedback_message({"notify_user": True, "public_id": PID, "message": "Frågan har gått ut."}) == \
        "Frågan har gått ut."
    assert core.feedback_message({"notify_user": True, "public_id": None, "message": "Frågan finns inte."}) is None
    assert core.feedback_message({"notify_user": False, "public_id": PID, "message": "ok"}) is None
    assert core.feedback_message(None) is None


# ----------------------------------------------------------------------------- svarskö


def test_backoff():
    assert [core.backoff_seconds(n) for n in range(0, 5)] == [0, 15, 30, 60, 120]
    assert core.backoff_seconds(50) == core.BACKOFF_MAX_SECONDS


def test_queue_retry_backoff_and_persistence():
    q = core.ResponseQueue()
    p1 = {"public_id": PID, "action": f"DRIVELOG_WORK_{PID}", "event_id": "event-0001"}
    p2 = {"public_id": PID, "action": f"DRIVELOG_PURPOSETEXT_{PID}", "event_id": "event-0002", "reply_text": "hemligt"}
    assert q.add(p1, 1000.0) and q.add(p2, 1000.0)
    assert not q.add(p1, 1001.0)  # samma händelse läggs inte in två gånger
    assert [i.event_id for i in q.due(1000.0)] == ["event-0001", "event-0002"]

    # DriveLog nere: inget tappas, nästa försök senare och senare.
    q.failed("event-0001", 1000.0, "nere")
    q.failed("event-0002", 1000.0, "nere")
    assert q.due(1000.0) == [] and len(q) == 2
    assert q.next_due() == 1015.0
    q.failed("event-0001", 1015.0, "nere")
    assert q.items[0].next_attempt == 1045.0 and q.items[0].attempts == 2

    # Överlever omstart (Store sparar JSON).
    restored = core.ResponseQueue.from_dict(json.loads(json.dumps(q.to_dict())))
    assert [i.to_dict() for i in restored.items] == [i.to_dict() for i in q.items]
    assert core.ResponseQueue.from_dict(None).items == []
    assert core.ResponseQueue.from_dict({"items": [{"trasig": 1}, "x"]}).items == []

    restored.retry_all_now(1020.0)
    assert len(restored.due(1020.0)) == 2
    restored.succeeded("event-0001")
    assert [i.event_id for i in restored.items] == ["event-0002"]

    # Diagnostik visar aldrig svarstexten.
    diag = restored.diagnostics()
    assert diag == [{"action": "PURPOSETEXT", "attempts": 1, "has_reply_text": True, "last_error": "nere"}]
    assert "hemligt" not in json.dumps(diag)


def test_queue_limits_and_prune():
    q = core.ResponseQueue()
    for i in range(core.MAX_QUEUE_ITEMS + 5):
        q.add({"event_id": f"event-{i:05d}", "action": "x"}, float(i))
    assert len(q) == core.MAX_QUEUE_ITEMS and q.items[0].event_id == "event-00005"
    assert q.prune(core.MAX_QUEUE_AGE_SECONDS + 10.0) == 5  # skapade vid 5–9 är för gamla
    assert len(q) == core.MAX_QUEUE_ITEMS - 5


def test_classify_status():
    assert core.classify_status(200) == "ok"
    assert core.classify_status(401) == core.classify_status(403) == "auth"
    assert core.classify_status(503) == core.classify_status(429) == "retry"
    assert core.classify_status(422) == core.classify_status(404) == "reject"


def test_parse_outbox_uses_the_only_phone_and_counts_missing():
    body = {"items": [{"id": "n1", "type": "notify", "service": None, "data": {"message": "Ny resa"}}]}
    deliveries, failed = core.parse_outbox(body, None, available=["mobile_app_enda"])
    assert [d.service for d in deliveries] == ["mobile_app_enda"] and failed == []
    deliveries, failed = core.parse_outbox(body, None, available=["mobile_app_a", "mobile_app_b"])
    assert deliveries == [] and core.missing_phone_count(failed) == 1
    assert core.missing_phone_count([{"id": "x", "error": "Notisen saknar text."}]) == 0


def test_phone_issue_action():
    assert core.phone_issue_action(has_service=False, waiting=1, issue_open=False) == "create"
    assert core.phone_issue_action(has_service=False, waiting=1, issue_open=True) is None
    assert core.phone_issue_action(has_service=False, waiting=0, issue_open=False) is None
    assert core.phone_issue_action(has_service=True, waiting=0, issue_open=True) == "delete"
    assert core.phone_issue_action(has_service=True, waiting=3, issue_open=False) is None


def test_outage_tracker_notifies_once_per_outage():
    t = core.OutageTracker()
    assert t.failure(0.0) is False
    assert t.failure(30 * 60) is False  # exakt 30 minuter: inte än
    assert t.failure(30 * 60 + 1) is True
    assert t.failure(40 * 60) is False  # en gång per avbrott
    assert "30 minuter" in t.message()
    assert t.success() is True and t.success() is False
    assert t.failure(100_000.0, auth=True) is True  # 401: direkt
    assert "API-token" in t.message()
    assert t.failure(100_060.0) is False


# ----------------------------------------------------------------------------- filer


def test_core_has_no_homeassistant_imports():
    source = (COMPONENT / "core.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+homeassistant", source, re.MULTILINE)


def test_manifest_translations_and_hacs():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == const.DOMAIN == "smgd_drivelog"
    assert manifest["name"] == "SMGD DriveLog" and manifest["version"] == const.INTEGRATION_VERSION == "1.1.0"
    assert manifest["config_flow"] is True and manifest["integration_type"] == "service"
    assert manifest["iot_class"] in ("local_polling", "cloud_polling")
    for key in ("codeowners", "documentation", "issue_tracker"):
        assert manifest[key]
    hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs == {"name": "SMGD DriveLog", "homeassistant": "2024.11.0"}

    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))

    def keys(d, prefix=""):
        out = set()
        for k, v in d.items():
            out |= keys(v, f"{prefix}{k}.") if isinstance(v, dict) else {f"{prefix}{k}"}
        return out

    for lang in ("sv", "en"):
        tr = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        assert keys(tr) == keys(strings), lang
    sv = json.loads((COMPONENT / "translations" / "sv.json").read_text(encoding="utf-8"))
    assert sv["entity"]["sensor"]["review_count"]["name"] == "Resor att granska"
    assert sv["issues"]["no_phone"]["title"] == "DriveLog har frågor men ingen telefon är vald"
    assert "flush_queue" in (COMPONENT / "services.yaml").read_text(encoding="utf-8")
