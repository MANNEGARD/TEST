"""Säkerhet 1.3.1, byggflöden och behållare: det som inte får glida tillbaka.

- Varje åtgärd i arbetsflödena är fäst vid en commit-SHA, inte en flyttbar tagg.
- Publiceringsjobbet (packages: write) lämnar inte sin token i .git/config och installerar
  inga paket från PyPI; avbildens namn läses och kontrolleras av erbjud_version.py.
- Avbildens byggkontext skickar aldrig .env-filer, oavsett katalog.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
SKRIPT = REPO / ".github" / "scripts" / "erbjud_version.py"
_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _ladda():
    spec = importlib.util.spec_from_file_location("erbjud_version_drift", SKRIPT)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _flode(namn: str) -> dict:
    return yaml.safe_load((WORKFLOWS / namn).read_text(encoding="utf-8"))


def _alla_uses():
    for fil in sorted(WORKFLOWS.glob("*.yml")):
        for jobb_namn, jobb in (yaml.safe_load(fil.read_text(encoding="utf-8"))["jobs"] or {}).items():
            for steg in jobb.get("steps") or []:
                if "uses" in steg:
                    yield fil.name, jobb_namn, steg["uses"]


def test_varje_atgard_ar_fast_vid_en_commit_sha():
    losa = [f"{fil}/{jobb}: {uses}" for fil, jobb, uses in _alla_uses() if not _SHA.match(uses)]
    assert not losa, "åtgärder på flyttbara taggar: " + ", ".join(losa)


def test_samma_atgard_har_samma_sha_i_alla_arbetsfloden():
    """ci.yml och addon.yml ska köra exakt samma version av en och samma åtgärd."""
    per_atgard: dict[str, set[str]] = {}
    for _fil, _jobb, uses in _alla_uses():
        namn, _, ref = uses.partition("@")
        per_atgard.setdefault(namn, set()).add(ref)
    assert {k: v for k, v in per_atgard.items() if len(v) > 1} == {}


def test_publicera_lamnar_ingen_token_i_git_och_installerar_inget_fran_pypi():
    steg = _flode("addon.yml")["jobs"]["publicera"]["steps"]
    for s in steg:
        if s.get("uses", "").startswith("actions/checkout@"):
            assert (s.get("with") or {}).get("persist-credentials") is False
    kor = "\n".join(s.get("run", "") for s in steg)
    assert "pip install" not in kor
    assert "import yaml" not in kor
    # Avbildens namn läses (och kontrolleras) av skriptet.
    assert "erbjud_version.py --namn" in kor


@pytest.fixture(scope="module")
def skript():
    return _ladda()


CONFIG = 'name: "x"\nversion: "1.0.0"\nimage: "ghcr.io/agare/drivelog"\n'


def test_namn_godtar_agarens_avbild(skript, tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(skript, "CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(skript, "VERSIONSFIL", tmp_path / "VERSION")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "Agare")
    assert skript.main(["x", "--namn"]) == 0
    assert capsys.readouterr().out == "image=ghcr.io/agare/drivelog\n"


@pytest.mark.parametrize(
    ("image", "agare"),
    [
        ("ghcr.io/annan/drivelog", "agare"),  # annan ägare
        ("ghcr.io/Agare/drivelog", "agare"),  # versaler
        ("ghcr.io/{arch}-agare/drivelog", "agare"),  # en avbild per arkitektur
        ("ghcr.io/agarexyz/drivelog", "agare"),  # prefix men inte samma ägare
    ],
)
def test_namn_avvisar_fel_avbild(skript, tmp_path, monkeypatch, capsys, image, agare):
    (tmp_path / "config.yaml").write_text(CONFIG.replace("ghcr.io/agare/drivelog", image), encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(skript, "CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(skript, "VERSIONSFIL", tmp_path / "VERSION")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", agare)
    assert skript.main(["x", "--namn"]) == 1
    assert "image=" not in capsys.readouterr().out


def test_namn_stammer_med_yaml_for_den_riktiga_configfilen(skript, monkeypatch, capsys):
    """Samma svar som den tidigare YAML-läsningen gav för den incheckade config.yaml."""
    config = yaml.safe_load((REPO / "drivelog" / "config.yaml").read_text(encoding="utf-8"))
    agare = config["image"].split("/")[1]
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", agare)
    assert skript.main(["x", "--namn"]) == 0
    assert capsys.readouterr().out == f"image={config['image']}\n"


def _ignoreras(monster: list[str], sokvag: str) -> bool:
    """Förenklad .dockerignore-tolkning (räcker för mönstren i filen): senaste träff avgör."""
    ute = False
    for m in monster:
        neg = m.startswith("!")
        m = m[1:] if neg else m
        if fnmatch.fnmatchcase(sokvag, m) or (m.startswith("**/") and fnmatch.fnmatchcase(sokvag, m[3:])):
            ute = not neg
    return ute


@pytest.mark.parametrize("sokvag", [".env", ".env.local", "frontend/.env", "frontend/.env.production", "backend/.env.local"])
def test_byggkontexten_skickar_inga_env_filer(sokvag):
    rader = (REPO / "drivelog" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    monster = [r.strip() for r in rader if r.strip() and not r.startswith("#")]
    assert _ignoreras(monster, sokvag)
