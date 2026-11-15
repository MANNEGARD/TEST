import json

import pytest
from drivelog_addon import installningar
from drivelog_addon.installningar import OgiltigaInstallningar, normalisera_doman, tolka

GRUND = {"domain": "drivelog.example.se", "strato_dyndns": False, "log_level": "info"}


def test_minsta_giltiga_installningar():
    inst = tolka(GRUND)
    assert inst.domain == "drivelog.example.se"
    assert not inst.har_adminuppgifter
    assert not inst.strato_dyndns
    assert inst.python_loggniva == 20


@pytest.mark.parametrize(
    ("in_", "ut"),
    [
        ("DriveLog.Example.se", "drivelog.example.se"),
        ("https://drivelog.example.se/", "drivelog.example.se"),
        ("  drivelog.example.se. ", "drivelog.example.se"),
        ("localhost", "localhost"),
        ("a-b.c-d.example", "a-b.c-d.example"),
    ],
)
def test_doman_normaliseras(in_, ut):
    assert normalisera_doman(in_) == ut


@pytest.mark.parametrize(
    "fel",
    ["", "drivelog", "192.168.1.10", "::1", "drivelog.example.se/app", "drivelog.example.se:8443",
     "-drivelog.example.se", "drive_log.example.se", "a..b.se", "x" * 64 + ".se", "drivelog.example.123"],
)
def test_ogiltiga_domaner(fel):
    with pytest.raises(OgiltigaInstallningar):
        normalisera_doman(fel)


def test_adminuppgifter_och_losenordslangd():
    inst = tolka({**GRUND, "admin_email": "Agare@Example.se", "admin_password": "x" * 16})
    assert inst.har_adminuppgifter
    assert inst.admin_email == "agare@example.se"
    with pytest.raises(OgiltigaInstallningar, match="minst 12"):
        tolka({**GRUND, "admin_email": "a@example.se", "admin_password": "kort"})
    with pytest.raises(OgiltigaInstallningar, match="e-postadress"):
        tolka({**GRUND, "admin_email": "inte-epost", "admin_password": "x" * 16})


def test_bara_ena_adminuppgiften_ger_varning():
    inst = tolka({**GRUND, "admin_email": "a@example.se"})
    assert not inst.har_adminuppgifter
    assert any("Både" in v for v in inst.varningar)


def test_losenord_syns_inte_i_repr():
    inst = tolka({**GRUND, "admin_email": "a@example.se", "admin_password": "hemligt-losenord-123",
                  "strato_dyndns": True, "strato_dyndns_user": "example.se",
                  "strato_dyndns_password": "strato-hemlis"})
    assert "hemligt-losenord-123" not in repr(inst)
    assert "strato-hemlis" not in repr(inst)


def test_dyndns_kraver_uppgifter():
    with pytest.raises(OgiltigaInstallningar, match="strato_dyndns_user"):
        tolka({**GRUND, "strato_dyndns": True})
    with pytest.raises(OgiltigaInstallningar, match="localhost"):
        tolka({**GRUND, "domain": "localhost", "strato_dyndns": True, "strato_dyndns_user": "x.se",
               "strato_dyndns_password": "p"})
    inst = tolka({**GRUND, "strato_dyndns": True, "strato_dyndns_user": "Example.se",
                  "strato_dyndns_password": "p"})
    assert inst.strato_dyndns_user == "example.se"
    assert inst.varningar == ()


def test_dyndns_anvandare_for_annan_doman_ger_varning():
    inst = tolka({**GRUND, "strato_dyndns": True, "strato_dyndns_user": "annan.se",
                  "strato_dyndns_password": "p"})
    assert any("strato_dyndns_user" in v for v in inst.varningar)


def test_okand_loggniva_och_fel_typ():
    with pytest.raises(OgiltigaInstallningar):
        tolka({**GRUND, "log_level": "trace"})
    with pytest.raises(OgiltigaInstallningar):
        tolka({**GRUND, "strato_dyndns": "ja"})
    with pytest.raises(OgiltigaInstallningar):
        tolka([])


def test_las_fil(tmp_path):
    fil = tmp_path / "options.json"
    with pytest.raises(OgiltigaInstallningar, match="saknas"):
        installningar.las(fil)
    fil.write_text("{inte json", encoding="utf-8")
    with pytest.raises(OgiltigaInstallningar, match="JSON"):
        installningar.las(fil)
    fil.write_text(json.dumps(GRUND), encoding="utf-8")
    assert installningar.las(fil).domain == "drivelog.example.se"
