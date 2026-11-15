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


def test_options_och_schema(config):
    schema = config["schema"]
    assert set(schema) == {
        "domain", "admin_email", "admin_password", "strato_dyndns",
        "strato_dyndns_user", "strato_dyndns_password", "log_level",
    }
    # Obligatoriska inställningar har ett standardvärde; valfria (?) kan saknas.
    for nyckel, typ in schema.items():
        if not str(typ).endswith("?"):
            assert nyckel in config["options"], nyckel
    assert set(config["options"]) <= set(schema)
    assert schema["admin_password"] == "password?"
    assert schema["strato_dyndns_password"] == "password?"
    # Inga hemligheter som standardvärden.
    for nyckel in ("admin_password", "strato_dyndns_password", "admin_email", "strato_dyndns_user"):
        assert nyckel not in config["options"]
    nivaer = re.fullmatch(r"list\((.*)\)", schema["log_level"]).group(1).split("|")
    assert config["options"]["log_level"] in nivaer


def test_standardinstallningarna_godtas_av_startprogrammet(config):
    from drivelog_addon import installningar

    inst = installningar.tolka(dict(config["options"]))
    assert inst.domain == config["options"]["domain"]
    assert set(installningar.LOGGNIVAER) == set(
        re.fullmatch(r"list\((.*)\)", config["schema"]["log_level"]).group(1).split("|")
    )


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
    assert set(jobb) == {"tester", "amd64", "aarch64", "publicera"}
    # Bara publiceringsjobbet får skriva paket.
    for namn, j in jobb.items():
        if namn == "publicera":
            assert j["permissions"] == {"contents": "read", "packages": "write"}
            assert "default_branch" in j["if"]
        else:
            assert "packages" not in j.get("permissions", {})
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
