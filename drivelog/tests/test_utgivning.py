"""Utgivningen: Home Assistant får aldrig erbjudas en version vars avbild inte finns än.

drivelog/VERSION är versionen som byggs och publiceras. `version` i drivelog/config.yaml (den
Home Assistant läser) höjs av arbetsflödet först när avbilden är publicerad.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ADDON = REPO / "drivelog"
SKRIPT = REPO / ".github" / "scripts" / "erbjud_version.py"


def _ladda():
    spec = importlib.util.spec_from_file_location("erbjud_version", SKRIPT)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture(scope="module")
def skript():
    return _ladda()


CONFIG = '# kommentar\nname: "SMGD DriveLog"\nversion: "1.0.1"\nslug: "drivelog"\nurl: "x"\n'


def test_versionsfilen_ar_en_version(skript):
    text = (ADDON / "VERSION").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", text), "drivelog/VERSION ska innehålla bara versionen"
    assert skript.las_versionsfil(text) == text.strip()


def test_config_visar_aldrig_en_nyare_version_an_den_som_byggs(skript):
    """config.yaml får ligga efter VERSION (avbilden byggs) men aldrig före."""
    bygg = skript.las_versionsfil((ADDON / "VERSION").read_text(encoding="utf-8"))
    erbjuden = skript.erbjuden_version((ADDON / "config.yaml").read_text(encoding="utf-8"))
    assert skript.som_tal(erbjuden) <= skript.som_tal(bygg)
    # Skriptets tolkning av filen stämmer med en riktig YAML-läsare.
    assert yaml.safe_load((ADDON / "config.yaml").read_text(encoding="utf-8"))["version"] == erbjuden


def test_changelog_namner_versionen_som_byggs(skript):
    bygg = skript.las_versionsfil((ADDON / "VERSION").read_text(encoding="utf-8"))
    assert f"## {bygg}\n" in (ADDON / "CHANGELOG.md").read_text(encoding="utf-8")


def test_satt_version_hojer_bara_versionsraden(skript):
    ny = skript.satt_version(CONFIG, "1.0.2")
    assert ny == CONFIG.replace('version: "1.0.1"', 'version: "1.0.2"')
    assert yaml.safe_load(ny)["version"] == "1.0.2"


def test_satt_version_ar_oforandrad_nar_den_redan_stammer(skript):
    assert skript.satt_version(CONFIG, "1.0.1") == CONFIG


def test_satt_version_sanker_aldrig(skript):
    with pytest.raises(skript.Fel, match="sänks aldrig"):
        skript.satt_version(CONFIG, "1.0.0")


def test_satt_version_jamfor_som_tal_inte_som_text(skript):
    assert 'version: "1.0.10"' in skript.satt_version(CONFIG.replace("1.0.1", "1.0.9"), "1.0.10")
    with pytest.raises(skript.Fel):
        skript.satt_version(CONFIG.replace("1.0.1", "1.0.10"), "1.0.9")


@pytest.mark.parametrize("text", ["", "1.0", "v1.0.2", "1.0.2-beta", "1.0.2 1.0.3", "latest"])
def test_ogiltig_versionsfil_avvisas(skript, text):
    with pytest.raises(skript.Fel):
        skript.las_versionsfil(text)


@pytest.mark.parametrize(
    "config",
    [
        'name: "x"\n',  # ingen versionsrad
        'version: "1.0.1"\nversion: "1.0.2"\n',  # två
        "version: 1.0.1\n",  # utan citattecken – skriptet gissar inte
        'version: "senaste"\n',
    ],
)
def test_otydlig_config_avvisas(skript, config):
    with pytest.raises(skript.Fel):
        skript.satt_version(config, "1.0.2")


def test_main_skriver_filen_och_ar_idempotent(skript, tmp_path, monkeypatch, capsys):
    (tmp_path / "VERSION").write_text("1.0.2\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(skript, "VERSIONSFIL", tmp_path / "VERSION")
    monkeypatch.setattr(skript, "CONFIG", tmp_path / "config.yaml")

    assert skript.main(["x", "--visa"]) == 0
    assert capsys.readouterr().out.strip() == "1.0.2"
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == CONFIG  # --visa ändrar inget

    assert skript.main(["x"]) == 0
    assert capsys.readouterr().out.strip() == "andrad"
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["version"] == "1.0.2"

    assert skript.main(["x"]) == 0
    assert capsys.readouterr().out.strip() == "oforandrad"


def test_main_andrar_inget_vid_fel(skript, tmp_path, monkeypatch, capsys):
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(skript, "VERSIONSFIL", tmp_path / "VERSION")
    monkeypatch.setattr(skript, "CONFIG", tmp_path / "config.yaml")
    assert skript.main(["x"]) == 1
    assert "sänks aldrig" in capsys.readouterr().err
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == CONFIG


# Avbilden måste gå att hämta anonymt innan versionen erbjuds -------------------------------------

INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:a", "platform": {"architecture": "amd64", "os": "linux"}},
        {"digest": "sha256:b", "platform": {"architecture": "arm64", "os": "linux"}},
        # Intyg (attestation) som buildx kan lägga till räknas inte som en plattform.
        {"digest": "sha256:c", "platform": {"architecture": "unknown", "os": "unknown"}},
    ],
}


class _Svar(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _register(manifest=None, *, pollettkod=None, manifestkod=None):
    """Ett låtsasregister: först frågan om pollett, sedan manifestet. Sparar vad som frågades."""
    fragor = []

    def oppna(fraga, timeout=None):
        url = fraga if isinstance(fraga, str) else fraga.full_url
        rubriker = {} if isinstance(fraga, str) else dict(fraga.header_items())
        fragor.append((url, rubriker))
        if "/token?" in url:
            if pollettkod:
                raise urllib.error.HTTPError(url, pollettkod, "nej", {}, None)
            return _Svar(json.dumps({"token": "anonym-pollett"}).encode())
        if manifestkod:
            raise urllib.error.HTTPError(url, manifestkod, "nej", {}, None)
        return _Svar(json.dumps(manifest).encode())

    return oppna, fragor


def test_avbild_med_bada_arkitekturerna_godtas(skript):
    oppna, fragor = _register(INDEX)
    assert skript.kontrollera_avbild("ghcr.io/agare/drivelog", "1.0.2", oppna=oppna) == {"linux/amd64", "linux/arm64"}
    (pollett_url, _), (manifest_url, rubriker) = fragor
    # Anonymt, som Home Assistant: en pull-pollett utan inloggning, sedan manifestet för taggen.
    assert pollett_url == "https://ghcr.io/token?scope=repository:agare/drivelog:pull&service=ghcr.io"
    assert manifest_url == "https://ghcr.io/v2/agare/drivelog/manifests/1.0.2"
    assert rubriker["Authorization"] == "Bearer anonym-pollett"
    assert "application/vnd.oci.image.index.v1+json" in rubriker["Accept"]


def test_avbild_som_saknar_en_arkitektur_avvisas(skript):
    bara_amd64 = {"manifests": [INDEX["manifests"][0]]}
    oppna, _ = _register(bara_amd64)
    with pytest.raises(skript.Fel, match="saknar linux/arm64"):
        skript.kontrollera_avbild("ghcr.io/agare/drivelog", "1.0.2", oppna=oppna)


def test_avbild_med_en_enda_arkitektur_avvisas(skript):
    oppna, _ = _register({"schemaVersion": 2, "config": {}, "layers": []})
    with pytest.raises(skript.Fel, match="saknar linux/amd64, linux/arm64"):
        skript.kontrollera_avbild("ghcr.io/agare/drivelog", "1.0.2", oppna=oppna)


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"manifestkod": 404}, "taggen finns inte"),
        ({"pollettkod": 401}, "paketet är privat"),
        ({"manifestkod": 401}, "paketet är privat"),
        ({"pollettkod": 403}, "paketet är privat"),
        ({"manifestkod": 500}, "HTTP 500"),
    ],
)
def test_avbild_som_inte_gar_att_hamta_avvisas(skript, kwargs, text):
    oppna, _ = _register(INDEX, **kwargs)
    with pytest.raises(skript.Fel, match=text):
        skript.kontrollera_avbild("ghcr.io/agare/drivelog", "1.0.2", oppna=oppna)


def test_natverksfel_ger_inte_godkant(skript):
    def oppna(fraga, timeout=None):
        raise OSError("nätet är nere")

    with pytest.raises(skript.Fel, match="kunde inte kontrolleras"):
        skript.kontrollera_avbild("ghcr.io/agare/drivelog", "1.0.2", oppna=oppna)


def test_avbildens_namn_lases_ur_config(skript):
    assert skript.avbildens_namn((ADDON / "config.yaml").read_text(encoding="utf-8")) == (
        yaml.safe_load((ADDON / "config.yaml").read_text(encoding="utf-8"))["image"]
    )
    with pytest.raises(skript.Fel):
        skript.avbildens_namn('image: "docker.io/annan/avbild"\n')
    with pytest.raises(skript.Fel):
        skript.avbildens_namn('name: "x"\n')


# Arbetsflödet ---------------------------------------------------------------------------------------


def _flode():
    return yaml.safe_load((REPO / ".github" / "workflows" / "addon.yml").read_text(encoding="utf-8"))


def test_byggena_laser_versionen_ur_versionsfilen():
    """Inget jobb bygger eller publicerar utifrån `version` i config.yaml (den höjs först efteråt)."""
    for namn, jobb in _flode()["jobs"].items():
        for steg in jobb["steps"]:
            kod = steg.get("run", "")
            assert "c['version']" not in kod and 'c["version"]' not in kod, (namn, steg.get("name"))
    for namn in ("amd64", "aarch64", "publicera", "erbjud"):
        meta = next(s for s in _flode()["jobs"][namn]["steps"] if s.get("id") == "meta")
        assert "erbjud_version.py --visa" in meta["run"], namn


def test_versionen_erbjuds_forst_nar_avbilden_gar_att_hamta():
    steg = _flode()["jobs"]["erbjud"]["steps"]
    kor = [s.get("run", "") for s in steg]
    kontroll = next(i for i, kod in enumerate(kor) if "erbjud_version.py --avbild" in kod)
    hojning = next(i for i, kod in enumerate(kor) if "git push" in kod)
    assert kontroll < hojning
    # Kontrollen görs utan inloggning mot GHCR: inget steg i jobbet loggar in.
    assert not any("login" in s.get("uses", "") for s in steg)
    # Bara config.yaml checkas in, och aldrig med tvång.
    assert "drivelog/config.yaml" in kor[hojning] and "--force" not in kor[hojning] and " -f " not in kor[hojning]


def test_arbetsflodet_startar_nar_utgivningsfilerna_andras():
    sokvagar = _flode()[True]["push"]["paths"]  # PyYAML tolkar nyckeln "on" som True
    assert "drivelog/**" in sokvagar and ".github/scripts/**" in sokvagar
