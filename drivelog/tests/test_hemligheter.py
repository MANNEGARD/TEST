import base64
import stat

import pytest
from drivelog_addon import hemligheter


def _rattighet(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_skapas_en_gang_och_ateranvands(tmp_path):
    forsta = hemligheter.las_eller_skapa(tmp_path)
    assert set(forsta.nyskapade) == {"DRIVELOG_SECRET_KEY", "DRIVELOG_ENCRYPTION_KEYS"}
    assert len(forsta.secret_key) >= 32
    assert len(base64.urlsafe_b64decode(forsta.encryption_keys)) == 32

    andra = hemligheter.las_eller_skapa(tmp_path)
    assert andra.nyskapade == ()
    assert andra.secret_key == forsta.secret_key
    assert andra.encryption_keys == forsta.encryption_keys


def test_rattigheter(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    katalog = tmp_path / hemligheter.KATALOG
    assert _rattighet(katalog) == 0o700
    for namn in (hemligheter.SECRET_KEY_FIL, hemligheter.ENCRYPTION_KEYS_FIL):
        assert _rattighet(katalog / namn) == 0o600
    # Inga temporära filer blir kvar.
    assert sorted(p.name for p in katalog.iterdir()) == sorted(
        [hemligheter.SECRET_KEY_FIL, hemligheter.ENCRYPTION_KEYS_FIL]
    )


def test_for_vida_rattigheter_stramas_at(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    fil = tmp_path / hemligheter.KATALOG / hemligheter.SECRET_KEY_FIL
    fil.chmod(0o644)
    hemligheter.las_eller_skapa(tmp_path)
    assert _rattighet(fil) == 0o600


def test_fernet_nyckeln_godtas_av_cryptography(tmp_path):
    fernet = pytest.importorskip("cryptography.fernet")
    nycklar = hemligheter.las_eller_skapa(tmp_path).encryption_keys
    f = fernet.Fernet(nycklar.split(",")[0])
    assert f.decrypt(f.encrypt(b"test")) == b"test"


def test_flera_nycklar_efter_rotation_godtas(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    fil = tmp_path / hemligheter.KATALOG / hemligheter.ENCRYPTION_KEYS_FIL
    fil.write_text(f"{hemligheter.ny_fernet_nyckel()},{hemligheter.ny_fernet_nyckel()}\n")
    assert "," in hemligheter.las_eller_skapa(tmp_path).encryption_keys


def test_trasig_nyckel_skrivs_inte_over(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    fil = tmp_path / hemligheter.KATALOG / hemligheter.ENCRYPTION_KEYS_FIL
    fil.write_text("trasig\n")
    with pytest.raises(hemligheter.TrasigaHemligheter):
        hemligheter.las_eller_skapa(tmp_path)
    assert fil.read_text() == "trasig\n"


def test_nycklarna_syns_inte_i_repr(tmp_path):
    h = hemligheter.las_eller_skapa(tmp_path)
    assert h.secret_key not in repr(h)
    assert h.encryption_keys not in repr(h)


# --- Granskningen av tillägget (SAK-S3/SUP-F1) -------------------------------------------------------

import os  # noqa: E402

som_root = pytest.mark.skipif(os.geteuid() != 0, reason="kräver root (ägarbyte)")


def test_saknad_nyckel_skapas_inte_nar_databasen_finns(tmp_path):
    databas = tmp_path / "app" / "drivelog.sqlite3"
    hemligheter.las_eller_skapa(tmp_path, databas=databas)
    databas.parent.mkdir()
    databas.write_bytes(b"SQLite format 3\x00")
    fil = tmp_path / hemligheter.KATALOG / hemligheter.ENCRYPTION_KEYS_FIL
    fil.unlink()
    with pytest.raises(hemligheter.TrasigaHemligheter, match="saknas men databasen finns"):
        hemligheter.las_eller_skapa(tmp_path, databas=databas)
    assert not fil.exists()  # ingen ny nyckel skapades


def test_flyttad_hemlighetskatalog_ger_fel_nar_databasen_finns(tmp_path):
    databas = tmp_path / "app" / "drivelog.sqlite3"
    hemligheter.las_eller_skapa(tmp_path, databas=databas)
    databas.parent.mkdir()
    databas.write_bytes(b"x")
    (tmp_path / hemligheter.KATALOG).rename(tmp_path / "flyttad")
    with pytest.raises(hemligheter.TrasigaHemligheter, match="återställ"):
        hemligheter.las_eller_skapa(tmp_path, databas=databas)


def test_forsta_start_utan_databas_skapar_nycklar(tmp_path):
    h = hemligheter.las_eller_skapa(tmp_path, databas=tmp_path / "app" / "drivelog.sqlite3")
    assert len(h.nyskapade) == 2


def test_symlank_som_nyckelfil_avvisas(tmp_path):
    """Granskarens prov: secret_key som symlänk till options.json gav hela options.json som nyckel."""
    (tmp_path / "options.json").write_text('{"strato_dyndns_password": "' + "h" * 40 + '"}')
    katalog = tmp_path / hemligheter.KATALOG
    katalog.mkdir(mode=0o700)
    (katalog / hemligheter.SECRET_KEY_FIL).symlink_to("../options.json")
    with pytest.raises(hemligheter.TrasigaHemligheter, match="symlänk"):
        hemligheter.las_eller_skapa(tmp_path)


def test_symlank_som_katalog_avvisas(tmp_path):
    (tmp_path / "annan").mkdir(mode=0o700)
    (tmp_path / hemligheter.KATALOG).symlink_to("annan")
    with pytest.raises(hemligheter.TrasigaHemligheter, match="symlänk"):
        hemligheter.las_eller_skapa(tmp_path)


def test_hard_lank_avvisas(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    fil = tmp_path / hemligheter.KATALOG / hemligheter.SECRET_KEY_FIL
    os.link(fil, tmp_path / "kopia")
    with pytest.raises(hemligheter.TrasigaHemligheter, match="hårda länkar"):
        hemligheter.las_eller_skapa(tmp_path)


@som_root
def test_fel_agare_pa_fil_avvisas(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    fil = tmp_path / hemligheter.KATALOG / hemligheter.ENCRYPTION_KEYS_FIL
    os.chown(fil, 10001, 10001)
    with pytest.raises(hemligheter.TrasigaHemligheter, match="ägs av uid 10001"):
        hemligheter.las_eller_skapa(tmp_path)


@som_root
def test_fel_agare_pa_katalog_avvisas(tmp_path):
    hemligheter.las_eller_skapa(tmp_path)
    os.chown(tmp_path / hemligheter.KATALOG, 10001, 10001)
    with pytest.raises(hemligheter.TrasigaHemligheter, match="ägs av uid 10001"):
        hemligheter.las_eller_skapa(tmp_path)
