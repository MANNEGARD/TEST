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
