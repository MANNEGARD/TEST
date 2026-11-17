"""Tilläggets YAML-filer: giltiga, och förenliga med varandra och med Home Assistants krav."""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ADDON = Path(__file__).resolve().parents[1]
REPO = ADDON.parent


def _las(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config():
    return _las(ADDON / "config.yaml")


def test_repository_yaml():
    repo = _las(REPO / "repository.yaml")
    assert repo["name"]
    assert repo["url"] == "https://github.com/SMGD-MP/TEST"
    assert repo["maintainer"]


def test_obligatoriska_nycklar(config):
    for nyckel in ("name", "version", "slug", "description", "arch"):
        assert config[nyckel], nyckel
    assert config["name"] == "SMGD DriveLog"
    assert isinstance(config["version"], str) and re.fullmatch(r"\d+\.\d+\.\d+", config["version"])
    # Katalogens namn är tilläggets slug.
    assert config["slug"] == ADDON.name
    assert sorted(config["arch"]) == ["aarch64", "amd64"]
    assert config["startup"] in {"initialize", "system", "services", "application", "once"}
    assert config["boot"] in {"auto", "manual", "manual_only"}
    assert config["backup"] in {"hot", "cold"}
    assert 10 <= config["timeout"] <= 300


def test_avbilden(config):
    bild = config["image"]
    assert bild == bild.lower()
    assert bild.startswith("ghcr.io/smgd-mp/")
    assert ":" not in bild.split("/")[-1], "taggen sätts av Supervisor utifrån version"


def test_portar_och_watchdog(config):
    assert config["ports"] == {"80/tcp": 80, "443/tcp": 443, "443/udp": 443}
    assert set(config["ports_description"]) == set(config["ports"])
    assert "8000/tcp" not in config["ports"], "API:t ska inte publiceras på värden"
    assert re.fullmatch(r"http://\[HOST\]:\[PORT:8000\]/api/v1/health/ready", config["watchdog"])
    assert not config.get("host_network", False)


def test_home_assistant_api_for_adressvaktens_avisering(config):
    # Adressvakten skickar aviseringar via Supervisorns proxy (http://supervisor/core/api).
    assert config["homeassistant_api"] is True
    # Inget annat Supervisor-API behövs.
    assert not config.get("hassio_api", False)


def test_options_och_schema(config):
    schema = config["schema"]
    assert set(schema) == {
        "domain", "admin_email", "admin_password", "strato_dyndns",
        "strato_dyndns_user", "strato_dyndns_password", "log_level", "extra_domains",
    }
    # SUP-F3/GUI-F1: alla fält står under options så att formuläret visar dem direkt.
    assert set(config["options"]) == set(schema)
    assert schema["admin_password"] == "password?"
    assert schema["strato_dyndns_password"] == "password?"
    # "email" underkänner en tom sträng i Supervisor – adressen kontrolleras av startprogrammet.
    assert schema["admin_email"] == "str?"
    # Inga hemligheter som standardvärden – de tomma fälten är tomma strängar.
    for nyckel in ("admin_password", "strato_dyndns_password", "admin_email", "strato_dyndns_user"):
        assert config["options"][nyckel] == ""
    nivaer = re.fullmatch(r"list\((.*)\)", schema["log_level"]).group(1).split("|")
    assert config["options"]["log_level"] in nivaer
    # Extra domäner: en lista med text, tom som standard (kontrolleras av startprogrammet).
    assert schema["extra_domains"] == ["str?"]
    assert config["options"]["extra_domains"] == []


def test_standardinstallningarna_godtas_av_startprogrammet(config):
    from drivelog_addon import installningar

    inst = installningar.tolka(dict(config["options"]))
    assert inst.domain == config["options"]["domain"]
    # Tomma strängar betyder "inte ifyllt".
    assert inst.admin_email is None and inst.admin_password is None and not inst.har_adminuppgifter
    assert inst.strato_dyndns_user is None and inst.strato_dyndns_password is None
    assert inst.varningar == ()
    assert inst.extra_domains == ()
    assert set(installningar.LOGGNIVAER) == set(
        re.fullmatch(r"list\((.*)\)", config["schema"]["log_level"]).group(1).split("|")
    )


_SUPERVISOR_PROV = r"""
import json, sys, yaml
from supervisor.apps.validate import SCHEMA_APP_CONFIG
from supervisor.apps.options import AppOptions
config = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
SCHEMA_APP_CONFIG(config)
class CoreSys:
    pass
validera = AppOptions(CoreSys(), config["schema"], config["name"], config["slug"], resolve_secrets=False)
ut = {"standard": validera(config["options"])}
ut["ifyllt"] = validera({**config["options"], "admin_email": "agare@example.se", "admin_password": "x" * 12,
                         "strato_dyndns": True, "strato_dyndns_user": "example.se",
                         "strato_dyndns_password": "hemligt",
                         "extra_domains": ["transcribe.smgd.se=transcribe:8080", "b.example.se=annan-tjanst:9000"]})
# Äldre options.json (från före 1.4.0) saknar extra_domains: ska fortfarande godtas.
ut["utan_extra"] = validera({k: v for k, v in config["options"].items() if k != "extra_domains"})
print(json.dumps(ut))
"""


def test_supervisors_egen_validering_godkanner_config_och_standardvarden(tmp_path):
    """SUP-F3: körs med Supervisors egen kod om den finns: antingen importerbar här, eller via
    DRIVELOG_SUPERVISOR_SRC (en klon av github.com/home-assistant/supervisor) och
    DRIVELOG_SUPERVISOR_PYTHON (en Python med Supervisors beroenden). Annars hoppas den över."""
    import json
    import os
    import subprocess
    import sys
    from importlib.util import find_spec

    prov = tmp_path / "prov.py"
    prov.write_text(_SUPERVISOR_PROV, encoding="utf-8")
    kalla = os.environ.get("DRIVELOG_SUPERVISOR_SRC")
    python = os.environ.get("DRIVELOG_SUPERVISOR_PYTHON")
    if kalla and python:
        miljo = {**os.environ, "PYTHONPATH": kalla}
        kommando = [python, str(prov), str(ADDON / "config.yaml")]
    elif find_spec("supervisor") is not None and find_spec("supervisor.apps") is not None:
        miljo = dict(os.environ)
        kommando = [sys.executable, str(prov), str(ADDON / "config.yaml")]
    else:
        pytest.skip("Supervisors källkod finns inte (sätt DRIVELOG_SUPERVISOR_SRC och DRIVELOG_SUPERVISOR_PYTHON)")
    resultat = subprocess.run(kommando, env=miljo, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603
    assert resultat.returncode == 0, resultat.stderr[-3000:]
    ut = json.loads(resultat.stdout.strip().splitlines()[-1])
    config = _las(ADDON / "config.yaml")
    assert ut["standard"] == config["options"]
    assert ut["ifyllt"]["admin_email"] == "agare@example.se"
    assert ut["ifyllt"]["extra_domains"] == ["transcribe.smgd.se=transcribe:8080", "b.example.se=annan-tjanst:9000"]
    assert "domain" in ut["utan_extra"]


@pytest.mark.parametrize("sprak", ["sv", "en"])
def test_oversattningar(config, sprak):
    t = _las(ADDON / "translations" / f"{sprak}.yaml")
    assert set(t["configuration"]) == set(config["schema"])
    for nyckel, varde in t["configuration"].items():
        assert varde["name"] and varde["description"], nyckel
    assert set(t["network"]) == set(config["ports"])


def test_filer_som_home_assistant_visar():
    for namn in ("DOCS.md", "README.md", "CHANGELOG.md", "icon.png", "logo.png", "Dockerfile"):
        assert (ADDON / namn).is_file(), namn
    assert (ADDON / "icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_changelog_namner_versionen(config):
    assert f"## {config['version']}" in (ADDON / "CHANGELOG.md").read_text(encoding="utf-8")


def test_arbetsflodet_ar_giltigt():
    flode = _las(REPO / ".github" / "workflows" / "addon.yml")
    # PyYAML tolkar nyckeln "on" som True.
    assert set(flode[True]) >= {"push", "workflow_dispatch"}
    assert flode["permissions"] == {"contents": "read"}
    jobb = flode["jobs"]
    assert set(jobb) == {"tester", "amd64", "aarch64", "publicera", "erbjud", "utgava"}
    # Bara publiceringsjobbet får skriva paket, och bara jobbet som höjer versionen i
    # drivelog/config.yaml får skriva i repot. Båda körs enbart på standardgrenen.
    for namn, j in jobb.items():
        if namn == "publicera":
            assert j["permissions"] == {"contents": "read", "packages": "write"}
            assert "default_branch" in j["if"]
        elif namn == "erbjud":
            assert j["permissions"] == {"contents": "write"}
            assert "default_branch" in j["if"] and "github.event_name == 'push'" in j["if"]
            assert j["needs"] == "publicera"
        elif namn == "utgava":
            # GitHub-utgåvan för HACS: bara efter att versionen erbjudits, bara på standardgrenen,
            # och ett fel här stoppar aldrig publiceringen eller erbjudandet.
            assert j["permissions"] == {"contents": "write"}
            assert "default_branch" in j["if"] and "github.event_name == 'push'" in j["if"]
            assert j["needs"] == "erbjud" and j["continue-on-error"] is True
            # bara när körningen publicerade en ny avbild (inte vid varje incheckning)
            assert "needs.erbjud.outputs.publicerad == 'true'" in j["if"]
            assert jobb["erbjud"]["outputs"]["publicerad"] == "${{ needs.publicera.outputs.publicerad }}"
            assert jobb["publicera"]["outputs"]["publicerad"] == "${{ steps.finns.outputs.finns == 'false' }}"
            assert any(st.get("if") == "failure()" and "::warning::" in st.get("run", "") for st in j["steps"])
        else:
            assert j.get("permissions", {}) in ({}, {"contents": "read"})
    for j in jobb.values():
        for steg in j["steps"]:
            assert "uses" in steg or "run" in steg


def test_dockerfile_etiketter():
    text = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    for etikett in ("io.hass.type", "io.hass.arch", "io.hass.version", "org.opencontainers.image.source"):
        assert etikett in text
    assert 'io.hass.arch="aarch64"' in text and 'io.hass.arch="amd64"' in text
    assert "API_INTERNAL_URL=http://127.0.0.1:8000" in text


def test_inga_hemligheter_i_tillagget():
    """Grov kontroll: inga Fernet-nycklar, privata nycklar eller lösenord i klartext i filerna."""
    fernet = re.compile(r"[A-Za-z0-9_-]{43}=")
    for path in list(ADDON.rglob("*")) + [REPO / "repository.yaml", REPO / ".github/workflows/addon.yml"]:
        if not path.is_file() or path.suffix in {".png", ".pyc"} or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not fernet.search(text), path
        assert "PRIVATE KEY" not in text, path
