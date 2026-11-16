"""GitHub-utgåvan för HACS (.github/scripts/utgava.py): text ur CHANGELOG, raden om oförändrad
integration och att en befintlig tagg aldrig skrivs över.

Skriptet prövas i en tillfällig git-katalog; ``gh`` ersätts av ett skript i PATH som loggar
anropen. Själva skapandet mot GitHub kan bara prövas på standardgrenen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SKRIPT = REPO / ".github" / "scripts" / "utgava.py"

CHANGELOG = """# Ändringar

## 1.3.0

- **Nyhet ett.** Text.
- **Nyhet två.** Text.

## 1.2.3

- **Gammal nyhet.** Text.
"""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


def _skriv(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Ett litet repo med skriptet, VERSION 1.2.3 taggad som v1.2.3, och sedan VERSION 1.3.0."""
    if shutil.which("git") is None:
        pytest.skip("git saknas")
    root = tmp_path / "repo"
    _skriv(root / ".github" / "scripts" / "utgava.py", SKRIPT.read_text(encoding="utf-8"))
    _skriv(root / "drivelog" / "CHANGELOG.md", CHANGELOG)
    _skriv(root / "drivelog" / "VERSION", "1.2.3\n")
    _skriv(root / "custom_components" / "smgd_drivelog" / "const.py", 'INTEGRATION_VERSION = "1.2.3"\n')
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "1.2.3")
    _git(root, "tag", "v1.2.3")
    _skriv(root / "drivelog" / "VERSION", "1.3.0\n")
    _skriv(root / "backend.txt", "ändring utanför integrationen\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "1.3.0")
    return root


@pytest.fixture()
def fake_gh(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """``gh`` som loggar sina argument; ``release view`` svarar "release not found"."""
    bin_dir, log = tmp_path / "bin", tmp_path / "gh.log"
    gh = bin_dir / "gh"
    _skriv(gh, (
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "$1 $2" = "release view" ]; then echo "release not found" >&2; exit 1; fi\n'
        "exit 0\n"
    ))
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GITHUB_SHA": "abc123"}
    return env, log


def _kor(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / ".github" / "scripts" / "utgava.py"), *args],
        cwd=root, env=env, capture_output=True, text=True, check=False,
    )


def test_anteckningarna_ar_versionens_avsnitt_och_sager_nar_integrationen_ar_oforandrad(repo):
    utfall = _kor(repo, "--anteckningar")
    assert utfall.returncode == 0, utfall.stderr
    assert "**Nyhet ett.**" in utfall.stdout and "**Nyhet två.**" in utfall.stdout
    assert "Gammal nyhet" not in utfall.stdout and "## 1.3.0" not in utfall.stdout
    assert "Ingen ändring i Home Assistant-integrationen – uppdateringen kan vänta." in utfall.stdout


def test_raden_utelamnas_nar_integrationen_har_andrats(repo):
    _skriv(repo / "custom_components" / "smgd_drivelog" / "const.py", 'INTEGRATION_VERSION = "1.3.0"\n')
    _git(repo, "commit", "-q", "-am", "integrationen ändrad")
    utfall = _kor(repo, "--anteckningar")
    assert utfall.returncode == 0 and "**Nyhet ett.**" in utfall.stdout
    assert "Ingen ändring i Home Assistant-integrationen" not in utfall.stdout


def test_raden_utelamnas_utan_tidigare_tagg(repo):
    _git(repo, "tag", "-d", "v1.2.3")
    utfall = _kor(repo, "--anteckningar")
    assert utfall.returncode == 0 and "Ingen ändring i Home Assistant-integrationen" not in utfall.stdout


def test_skapa_gor_utgavan_mot_den_byggda_committen(repo, fake_gh, tmp_path):
    env, log = fake_gh
    notes = tmp_path / "notes.md"
    notes.write_text("text", encoding="utf-8")
    utfall = _kor(repo, "--skapa", str(notes), env=env)
    assert utfall.returncode == 0, utfall.stderr
    anrop = log.read_text(encoding="utf-8").splitlines()
    skapa = [a for a in anrop if a.startswith("release create")]
    assert skapa == [f"release create v1.3.0 --target abc123 --title DriveLog 1.3.0 --notes-file {notes}"]
    assert "--prerelease" not in skapa[0] and "--latest" not in skapa[0]


def test_skapa_avstar_nar_taggen_redan_finns(repo, fake_gh, tmp_path):
    env, log = fake_gh
    _git(repo, "tag", "v1.3.0")
    notes = tmp_path / "notes.md"
    notes.write_text("text", encoding="utf-8")
    utfall = _kor(repo, "--skapa", str(notes), env=env)
    assert utfall.returncode == 0 and "finns redan" in utfall.stdout
    assert not log.exists() or "release create" not in log.read_text(encoding="utf-8")


def test_skapa_avstar_nar_utgavan_redan_finns(repo, tmp_path):
    bin_dir = tmp_path / "bin2"
    gh = bin_dir / "gh"
    _skriv(gh, f'#!/bin/sh\necho "$@" >> "{tmp_path / "gh2.log"}"\nexit 0\n')  # release view lyckas
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GITHUB_SHA": "abc123"}
    utfall = _kor(repo, "--skapa", str(tmp_path / "notes.md"), env=env)
    assert utfall.returncode == 0 and "finns redan" in utfall.stdout
    assert "release create" not in (tmp_path / "gh2.log").read_text(encoding="utf-8")


def test_skapa_avbryter_nar_det_inte_gar_att_avgora(repo, tmp_path):
    bin_dir = tmp_path / "bin3"
    gh = bin_dir / "gh"
    _skriv(gh, f'#!/bin/sh\necho "$@" >> "{tmp_path / "gh3.log"}"\necho "HTTP 502" >&2\nexit 1\n')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GITHUB_SHA": "abc123"}
    utfall = _kor(repo, "--skapa", str(tmp_path / "notes.md"), env=env)
    assert utfall.returncode == 1 and "Kunde inte avgöra" in utfall.stderr
    assert "release create" not in (tmp_path / "gh3.log").read_text(encoding="utf-8")


def test_anteckningarna_for_repots_egen_version():
    """Samma kontroll som steget i testjobbet: avsnittet för drivelog/VERSION finns."""
    utfall = subprocess.run([sys.executable, str(SKRIPT), "--anteckningar"], cwd=REPO, capture_output=True,
                            text=True, check=False)
    assert utfall.returncode == 0, utfall.stderr
    version = (REPO / "drivelog" / "VERSION").read_text(encoding="utf-8").strip()
    assert utfall.stdout.strip() and utfall.stdout.strip() != f"DriveLog {version}."


def test_jobbet_kor_skriptet_efter_erbjudandet_utan_andra_hemligheter():
    flode = yaml.safe_load((REPO / ".github" / "workflows" / "addon.yml").read_text(encoding="utf-8"))
    jobb = flode["jobs"]["utgava"]
    kod = "\n".join(s.get("run", "") for s in jobb["steps"])
    assert "utgava.py --anteckningar" in kod and "utgava.py --skapa" in kod
    assert jobb["steps"][0]["with"]["fetch-depth"] == 0
    env = {k: v for s in jobb["steps"] for k, v in (s.get("env") or {}).items()}
    assert env == {"GH_TOKEN": "${{ github.token }}"}
    assert "secrets." not in yaml.safe_dump(jobb)
    sokvagar = flode[True]["push"]["paths"]
    assert "custom_components/**" in sokvagar and "hacs.json" in sokvagar
    tester = "\n".join(s.get("run", "") for s in flode["jobs"]["tester"]["steps"])
    assert "utgava.py --anteckningar" in tester
