"""1.3.1 säkerhetsrättningar i Home Assistant-integrationen och notiserna.

* homeassistant-1: integrationen släpper bara igenom det DriveLog själv bygger till notify.
* homeassistant-3: < och > i notisens rubrik och text tolkas inte som HTML på Android.
* homeassistant-4: http mot en publik adress byts till https innan token skickas.
* homeassistant-6: "Dölj platser" döljer också syftesknapparna i läget "Jag godkänner allt själv".

``core.py`` laddas direkt från fil, som i test_ha_integration_core.py.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from app.services.ha import actions as A

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT = REPO_ROOT / "custom_components" / "smgd_drivelog"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"smgd_drivelog_sak_{name}", COMPONENT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


core = _load("core")

PID = "0123456789abcdef"
BASE = "https://drivelog.example.com"


def _item(data, *, kind="notify", item_id="aaaaaa1"):
    return {"items": [{"id": item_id, "type": kind, "service": "mobile_app_telefon", "data": data}]}


def _notify(**over):
    data = A.build_notify_data(
        public_id=PID, kind="classify", title="Ny resa", message="Ny resa: 4,2 mil. Arbete eller privat?",
        actions=[
            {"key": A.WORK, "title": "Arbete", "icon": "sfsymbols:briefcase"},
            {"key": A.REVIEW, "title": "Granska", "uri": f"/m/q/{PID}"},
            {"key": A.PURPOSE_TEXT, "title": "Ange nytt syfte", "text_input": True, "placeholder": "Syfte"},
        ],
        base_url=BASE, open_path=f"/m/q/{PID}",
    )
    data.update(over)
    return data


# ----------------------------------------------------------------------------- homeassistant-1


def test_drivelogs_own_notices_pass_unchanged():
    """Allt DriveLog bygger (fråga, påminnelse, länk till export, rensning) går igenom oförändrat."""
    samples = [
        _notify(),
        A.build_notify_data(public_id=PID, kind="alert", title="DriveLog: Volvo", message="Kontrollera kopplingen.",
                            actions=[{"key": A.OPEN, "title": "Öppna", "uri": "/settings?sektion=om"}],
                            base_url="http://localhost:3000", open_path="/settings", reminder=True),
        A.build_notify_data(public_id=PID, kind="month", title="Underlaget", message="Klart.",
                            actions=[{"key": A.OPEN, "title": "Öppna underlaget",
                                      "uri": f"{BASE}/api/v1/public/exports/abc.123.def"},
                                     {"key": A.OPEN_SECOND, "title": "Se antagna", "uri": "/m/month/2026-10"}],
                            base_url=BASE, open_path="/export"),
    ]
    for data in samples:
        deliveries, failed = core.parse_outbox(_item(data), None)
        assert failed == [] and deliveries[0].data == data
    clear = A.build_clear_data(PID)
    deliveries, failed = core.parse_outbox(_item(clear, kind="clear"), None)
    assert failed == [] and deliveries[0].data == clear


def test_outbox_from_backend_passes_unchanged(db, user, vehicle, trips):
    from app.services.ha import notifier
    from tests.test_ha_support import after_batch, enqueue_classify, recent_trip, setup_pull

    setup_pull(db, user)
    enqueue_classify(db, user, vehicle, recent_trip(trips))
    items = notifier.outbox(db, user.id, now=after_batch())
    deliveries, failed = core.parse_outbox({"items": items}, None)
    assert failed == [] and [d.data for d in deliveries] == [i["data"] for i in items]


@pytest.mark.parametrize("message", [
    "command_app_lock", "command_activity", "command_broadcast_intent", "request_location_update",
    "clear_badge", "remove_channel", "update_complications", "update_widgets", "TTS", " command_dnd ",
])
def test_companion_commands_are_refused(message):
    deliveries, failed = core.parse_outbox(_item({"message": message, "data": {"app_lock_enabled": False}}), None)
    assert deliveries == [] and [f["id"] for f in failed] == ["aaaaaa1"]


def test_clear_only_for_drivelog_tags():
    ok, _ = core.parse_outbox(_item({"message": "clear_notification", "data": {"tag": "drivelog-x1"}}), None)
    assert len(ok) == 1
    for data in ({"message": "clear_notification", "data": {"tag": "annan-app"}},
                 {"message": "clear_notification", "data": {}}):
        deliveries, failed = core.parse_outbox(_item(data), None)
        assert deliveries == [] and len(failed) == 1


def test_unknown_keys_are_dropped():
    data = _notify()
    data["data"]["intent_uri"] = "intent:#Intent;action=x;end"
    data["data"]["push"]["sound"] = "x"
    data["data"]["actions"][0]["destructive"] = True
    data["data"]["action_data"]["extra"] = "x"
    data["extra"] = 1
    (delivery,), failed = core.parse_outbox(_item(data), None)
    assert failed == [] and delivery.data == _notify()


@pytest.mark.parametrize("change", [
    lambda d: d["data"].__setitem__("url", "intent:#Intent;end"),
    lambda d: d["data"]["actions"][1].__setitem__("uri", "javascript:alert(1)"),
    lambda d: d["data"]["actions"][1].__setitem__("uri", "app://com.example"),
    lambda d: d["data"]["actions"][0].__setitem__("action", "URI"),
    lambda d: d["data"]["actions"][0].__setitem__("action", "ANNAN_APP_ab12cd34"),
    lambda d: d["data"]["actions"][2].__setitem__("behavior", "annat"),
    lambda d: d["data"].__setitem__("actions", "inte en lista"),
])
def test_foreign_links_and_actions_are_refused(change):
    data = _notify()
    change(data)
    deliveries, failed = core.parse_outbox(_item(data), None)
    assert deliveries == [] and len(failed) == 1


def test_html_is_neutralized_in_the_integration_too():
    data = _notify(title="Ny <b>resa</b>", message="Kafé<br><b>tryck Privat</b>")
    (delivery,), _ = core.parse_outbox(_item(data), None)
    assert delivery.data["title"] == "Ny ‹b›resa‹/b›"
    assert delivery.data["message"] == "Kafé‹br›‹b›tryck Privat‹/b›"


# ----------------------------------------------------------------------------- homeassistant-3


def test_build_notify_data_neutralizes_html():
    data = A.build_notify_data(public_id=PID, kind="classify", title="Antaget: <i>X</i>",
                               message='Kafé<br><b>DriveLog</b><font color="#00000000">', actions=[],
                               base_url=BASE, open_path="/")
    assert "<" not in data["title"] + data["message"] and ">" not in data["title"] + data["message"]
    assert data["message"] == 'Kafé‹br›‹b›DriveLog‹/b›‹font color="#00000000"›'
    # & lämnas orört – annars visas "A & B" som "&amp;" på Android
    assert A.build_notify_data(public_id=PID, kind="k", title="A & B", message="A & B", actions=[],
                               base_url=BASE, open_path="/")["message"] == "A & B"


# ----------------------------------------------------------------------------- homeassistant-4


def _resolver(table):
    async def resolve(host):
        if host not in table:
            raise OSError("okänd värd")
        return table[host]
    return resolve


def test_public_http_is_upgraded_before_any_request():
    resolve = _resolver({
        "drivelog.example.com": ["203.0.113.10", "2001:db8::1", "8.8.8.8"],
        "drivelog.global.example": ["8.8.8.8"],
        "drivelog.hemma.example": ["192.168.1.5"],
        "a0d7b954-smgd-drivelog": ["172.30.33.4"],
        "homeassistant.local": ["192.168.1.2"],
        "drivelog.tailnet.example": ["100.101.102.103"],
        "blandad.example": ["8.8.8.8", "10.0.0.5"],
        "ensam": ["8.8.8.8"],
    })

    def run(url):
        return asyncio.run(core.secure_base_url(core.normalize_base_url(url), resolve))

    assert run("http://drivelog.global.example") == "https://drivelog.global.example"
    assert run("http://drivelog.global.example/drivelog/api/v1") == "https://drivelog.global.example/drivelog"
    # Lokala adresser, tilläggets värdnamn och namn som inte går att slå upp behåller http.
    for url in ("http://drivelog.hemma.example:8000", "http://a0d7b954-smgd-drivelog:8000",
                "http://homeassistant.local:8000", "http://192.168.1.5:8000", "http://127.0.0.1:8000",
                "http://localhost:8000", "http://drivelog.tailnet.example", "http://blandad.example",
                "http://ensam:8000", "http://okand.example", "http://[fd00::5]:8000"):
        assert run(url) == core.normalize_base_url(url), url
    assert run("http://8.8.8.8:8000") == "https://8.8.8.8:8000"
    assert run("https://drivelog.example.com") == "https://drivelog.example.com"


def test_config_flow_uses_secure_base_url():
    """Konfigurationsflödet kör kontrollen innan whoami skickas med token."""
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    validate = source[source.index("async def _validate"):source.index("class DriveLogConfigFlow")]
    assert validate.index("secure_base_url") < validate.index("DriveLogApiClient(")
