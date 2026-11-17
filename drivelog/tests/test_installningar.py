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
    # Ogiltiga uppgifter stoppar inte starten (finns ett konto ska DriveLog starta som vanligt),
    # men de används inte och problemet följer med till loggen.
    kort = tolka({**GRUND, "admin_email": "a@example.se", "admin_password": "x" * 11})
    assert not kort.har_adminuppgifter and "minst 12" in kort.adminproblem
    assert "x" * 11 not in kort.adminproblem
    tolv = tolka({**GRUND, "admin_email": "a@example.se", "admin_password": "x" * 12})
    assert tolv.har_adminuppgifter and tolv.adminproblem is None
    fel = tolka({**GRUND, "admin_email": "inte-epost", "admin_password": "x" * 16})
    assert not fel.har_adminuppgifter and "e-postadress" in fel.adminproblem
    nul = tolka({**GRUND, "admin_email": "a@example.se", "admin_password": "x" * 12 + "\x00"})
    assert not nul.har_adminuppgifter and nul.adminproblem


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


def test_nyrad_i_domanen_avvisas():
    # "$" i ett reguljärt uttryck godtar en avslutande radbrytning – domänen hamnar i Caddyfilen.
    with pytest.raises(OgiltigaInstallningar):
        normalisera_doman("drivelog\n.example.se")


def test_extra_domaner():
    inst = tolka({**GRUND, "extra_domains": [
        "Transcribe.Example.se=Transcribe:8080", " b.example.se = 127.0.0.1:9000 ", "",
    ]})
    assert [(e.doman, e.uppstrom) for e in inst.extra_domains] == [
        ("transcribe.example.se", "transcribe:8080"), ("b.example.se", "127.0.0.1:9000"),
    ]
    assert tolka(GRUND).extra_domains == ()
    assert tolka({**GRUND, "extra_domains": []}).extra_domains == ()


@pytest.mark.parametrize(
    ("rad", "text"),
    [
        ("transcribe.example.se", "<domän>=<värd>:<port>"),
        ("transcribe.example.se=transcribe", "<domän>=<värd>:<port>"),
        ("=transcribe:8080", "<domän>=<värd>:<port>"),
        ("drivelog.example.se=transcribe:8080", "DriveLogs egen domän"),
        ("localhost=transcribe:8080", "localhost"),
        ("transcribe=transcribe:8080", "fullständigt domännamn"),
        ("transcribe.example.se:443=transcribe:8080", "extra_domains"),
        ("10.0.0.1=transcribe:8080", "IP-adress"),
        ("transcribe.example.se=localhost:8080", "127.0.0.1"),
        ("transcribe.example.se=http://transcribe:8080", "värden"),
        ("transcribe.example.se=transcribe/app:8080", "värden"),
        ("transcribe.example.se=trans cribe:8080", "värden"),
        ("transcribe.example.se=transcribe{:8080", "värden"),
        ("transcribe.example.se=transcribe\n}:8080", "värden"),
        ("transcribe.example.se=-transcribe:8080", "värden"),
        ("transcribe.example.se=" + "x" * 64 + ":8080", "värden"),
        ("transcribe.example.se=transcribe:0", "porten"),
        ("transcribe.example.se=transcribe:65536", "porten"),
        ("transcribe.example.se=transcribe:80a", "porten"),
        ("transcribe.example.se=transcribe:8080\n", None),  # yttre blanktecken tas bort
        ("transcribe.example.se=transcribe:８０", "porten"),  # bara ASCII-siffror
    ],
)
def test_ogiltiga_extra_domaner(rad, text):
    if text is None:
        assert tolka({**GRUND, "extra_domains": [rad]}).extra_domains[0].port == 8080
        return
    with pytest.raises(OgiltigaInstallningar, match="extra_domains") as fel:
        tolka({**GRUND, "extra_domains": [rad]})
    assert text in str(fel.value)


def test_extra_domaner_dubbletter_och_fel_typ():
    with pytest.raises(OgiltigaInstallningar, match="mer än en gång"):
        tolka({**GRUND, "extra_domains": ["a.example.se=x:1", "A.example.se=y:2"]})
    for fel in ("a.example.se=x:1", [1], {"a.example.se": "x:1"}):
        with pytest.raises(OgiltigaInstallningar, match="extra_domains"):
            tolka({**GRUND, "extra_domains": fel})
