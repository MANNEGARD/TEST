"""Återställning av administratörens lösenord från appens inställningar (version 1.0.2).

Regeln: uppgifterna i "Administratörens e-post"/"Administratörens lösenord" tillämpas när de har
ÄNDRATS sedan de senast tillämpades. Märket (en nyckelad hash) ligger i /data/hemligheter.
"""

import logging
import os
import stat
import sys

import pytest
from drivelog_addon import hemligheter, startprogram
from test_startprogram import Varld, _kor_i_bakgrunden, _program, _vanta_tills

LOSEN_1 = "forsta-losenordet-123"
LOSEN_2 = "andra-losenordet-456"
EPOST = "agare@example.se"


def _kor(tmp_path, varld, **inst):
    """En start till dess att tillägget är igång eller spärrat, sedan nedstängning."""
    prog = _program(tmp_path, varld, **inst)
    stopp, trad, resultat = _kor_i_bakgrunden(prog)
    assert _vanta_tills(lambda: prog.lage in ("igang", "sparrad"))
    stopp.set()
    trad.join(5)
    assert resultat["kod"] == 0
    return prog


def _marke(tmp_path):
    return hemligheter.las_admin_marke(tmp_path / "data")


def _startade(varld):
    return [(h, n) for h, n in varld.logg if h in ("starta", "kommando")]


# --- Märket ---------------------------------------------------------------------------------------


def test_marke_ar_nyckelad_hash_utan_losenordet():
    a = hemligheter.admin_marke("nyckel-" * 6, EPOST, LOSEN_1)
    assert a.startswith("v1:") and len(a) == 67
    assert LOSEN_1 not in a and EPOST not in a
    # Utan nyckeln går märket inte att återskapa – en annan nyckel ger ett annat märke.
    assert hemligheter.admin_marke("annan-" * 6, EPOST, LOSEN_1) != a
    import hashlib

    assert hashlib.sha256(LOSEN_1.encode()).hexdigest() not in a
    assert hemligheter.admin_marke("nyckel-" * 6, EPOST, LOSEN_2) != a
    assert hemligheter.admin_marke("nyckel-" * 6, "annan@example.se", LOSEN_1) != a
    assert hemligheter.admin_marke("nyckel-" * 6, " AGARE@example.se ", LOSEN_1) == a
    assert hemligheter.stammer_admin_marke(a, "nyckel-" * 6, EPOST, LOSEN_1)
    assert not hemligheter.stammer_admin_marke(a, "nyckel-" * 6, EPOST, LOSEN_2)
    assert not hemligheter.stammer_admin_marke(hemligheter.SAKNAS, "nyckel-" * 6, EPOST, LOSEN_1)


def test_marke_sparas_600_och_ersatts(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    assert hemligheter.las_admin_marke(data) == hemligheter.SAKNAS
    m1 = hemligheter.admin_marke("k" * 40, EPOST, LOSEN_1)
    m2 = hemligheter.admin_marke("k" * 40, EPOST, LOSEN_2)
    hemligheter.spara_admin_marke(data, m1)
    fil = data / hemligheter.KATALOG / hemligheter.ADMIN_MARKE_FIL
    assert stat.S_IMODE(os.lstat(fil).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(fil.parent).st_mode) == 0o700
    assert hemligheter.las_admin_marke(data) == m1
    hemligheter.spara_admin_marke(data, m2)
    assert hemligheter.las_admin_marke(data) == m2
    assert sorted(p.name for p in fil.parent.iterdir()) == [hemligheter.ADMIN_MARKE_FIL]  # inga tmp-filer
    with pytest.raises(ValueError):
        hemligheter.spara_admin_marke(data, LOSEN_1)


def test_trasigt_marke_och_symlank(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    katalog = data / hemligheter.KATALOG
    katalog.mkdir(mode=0o700)
    fil = katalog / hemligheter.ADMIN_MARKE_FIL
    fil.write_text("skräp\n")
    assert hemligheter.las_admin_marke(data) == hemligheter.TRASIGT
    fil.unlink()
    mal = tmp_path / "annan-fil"
    mal.write_text(hemligheter.admin_marke("k" * 40, EPOST, LOSEN_1) + "\n")
    fil.symlink_to(mal)
    assert hemligheter.las_admin_marke(data) == hemligheter.TRASIGT  # följer aldrig symlänkar
    hemligheter.spara_admin_marke(data, hemligheter.admin_marke("k" * 40, EPOST, LOSEN_2))
    assert not fil.is_symlink()
    assert hemligheter.admin_marke("k" * 40, EPOST, LOSEN_1) in mal.read_text()  # målet orört


# --- Flödet vid start -----------------------------------------------------------------------------


def test_forsta_start_skapar_kontot_fore_webbapp_och_proxy(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    varld = Varld(behover_konto=True)
    prog = _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    assert prog.lage == "igang"
    assert _startade(varld) == [("starta", "API"), ("kommando", EPOST), ("starta", "webbappen"), ("starta", "Caddy")]
    assert varld.konton == {EPOST: LOSEN_1}
    assert hemligheter.stammer_admin_marke(_marke(tmp_path), prog.nycklar.secret_key, EPOST, LOSEN_1)
    assert f"Lösenordet för {EPOST} sattes från appens inställningar." in caplog.text
    assert "Administratörskontot" in caplog.text
    assert LOSEN_1 not in caplog.text


def test_hela_forloppet_andrat_oforandrat_och_bytt_i_drivelog(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    varld = Varld(behover_konto=True)
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    assert len(varld.kommandon) == 1

    # Omstart med oförändrat fält: ingenting görs.
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    assert len(varld.kommandon) == 1
    assert "oförändrat" in caplog.text

    # Ägaren byter lösenord inne i DriveLog; fältet lämnas kvar – det bytta lösenordet gäller.
    varld.konton[EPOST] = "bytt-i-drivelog-789"
    prog = _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    assert prog.lage == "igang"
    assert len(varld.kommandon) == 1
    assert varld.konton[EPOST] == "bytt-i-drivelog-789"

    # Glömt lösenordet: ett nytt i fältet tillämpas, sessionerna avslutas.
    caplog.clear()
    prog = _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_2)
    assert len(varld.kommandon) == 2
    assert varld.konton[EPOST] == LOSEN_2
    assert hemligheter.stammer_admin_marke(_marke(tmp_path), prog.nycklar.secret_key, EPOST, LOSEN_2)
    assert f"Lösenordet för {EPOST} sattes från appens inställningar." in caplog.text
    assert "sessioner" in caplog.text
    assert _startade(varld)[-3:] == [("kommando", EPOST), ("starta", "webbappen"), ("starta", "Caddy")]

    # Fältet tömt: ingenting görs och märket lämnas.
    marke = _marke(tmp_path)
    _kor(tmp_path, varld, admin_email="", admin_password="")
    assert len(varld.kommandon) == 2
    assert _marke(tmp_path) == marke

    # Samma lösenord skrivs in igen efter att fältet varit tomt: märket stämmer, ingenting görs.
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_2)
    assert len(varld.kommandon) == 2

    for losen in (LOSEN_1, LOSEN_2, "bytt-i-drivelog-789"):
        assert losen not in caplog.text


def test_tomt_falt_gor_ingenting(tmp_path):
    varld = Varld(behover_konto=False)
    prog = _kor(tmp_path, varld)
    assert prog.lage == "igang" and varld.kommandon == []
    assert _marke(tmp_path) == hemligheter.SAKNAS


@pytest.mark.parametrize("trasig", [False, True])
def test_saknat_eller_trasigt_marke_med_konto_skriver_inte_over(tmp_path, caplog, trasig):
    """T.ex. uppdatering från 1.0.1 med ett kvarglömt lösenord: ingenting ändras, märket sparas."""
    caplog.set_level(logging.INFO)
    varld = Varld(konton={EPOST: "bytt-i-drivelog-789"})
    prog = _program(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    if trasig:
        (tmp_path / "data" / hemligheter.KATALOG / hemligheter.ADMIN_MARKE_FIL).write_text("trasig\n")
    stopp, trad, _ = _kor_i_bakgrunden(prog)
    assert _vanta_tills(lambda: prog.lage == "igang")
    stopp.set()
    trad.join(5)
    assert varld.kommandon == []
    assert varld.konton[EPOST] == "bytt-i-drivelog-789"
    assert hemligheter.stammer_admin_marke(_marke(tmp_path), prog.nycklar.secret_key, EPOST, LOSEN_1)
    assert "inget lösenord" in caplog.text and "NYTT lösenord" in caplog.text
    assert ("märkesfilen" in caplog.text) == trasig
    # Nästa start med samma fält: fortfarande ingenting. Med ett nytt lösenord: det tillämpas.
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    assert varld.kommandon == []
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_2)
    assert varld.konton[EPOST] == LOSEN_2


def test_trasigt_marke_utan_konto_skapar_kontot(tmp_path):
    varld = Varld(behover_konto=True)
    prog = _program(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    (tmp_path / "data" / hemligheter.KATALOG / hemligheter.ADMIN_MARKE_FIL).write_text("trasig\n")
    stopp, trad, _ = _kor_i_bakgrunden(prog)
    assert _vanta_tills(lambda: prog.lage == "igang")
    stopp.set()
    trad.join(5)
    assert varld.konton == {EPOST: LOSEN_1}


@pytest.mark.parametrize(
    ("epost", "losen", "text"),
    [(EPOST, "kort-11-tkn", "minst 12"), ("inte-en-adress", LOSEN_1, "e-postadress")],
)
def test_ogiltiga_uppgifter_utan_konto_sparrar(tmp_path, caplog, epost, losen, text):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=True)
    prog = _kor(tmp_path, varld, admin_email=epost, admin_password=losen)
    assert prog.lage == "sparrad"
    assert varld.kommandon == []
    assert [n for h, n in varld.logg if h == "starta"] == ["API"]
    assert text in caplog.text and "Kontot kan inte skapas" in caplog.text
    assert losen not in caplog.text


@pytest.mark.parametrize(
    ("epost", "losen", "text"),
    [(EPOST, "kort-11-tkn", "minst 12"), ("inte-en-adress", LOSEN_1, "e-postadress")],
)
def test_ogiltiga_uppgifter_med_konto_startar_som_vanligt(tmp_path, caplog, epost, losen, text):
    caplog.set_level(logging.INFO)
    varld = Varld(konton={EPOST: LOSEN_1})
    prog = _kor(tmp_path, varld, admin_email=epost, admin_password=losen)
    assert prog.lage == "igang"
    assert varld.kommandon == [] and varld.konton == {EPOST: LOSEN_1}
    assert text in caplog.text and "Lösenordet ändrades inte" in caplog.text
    assert _marke(tmp_path) == hemligheter.SAKNAS
    assert losen not in caplog.text


def test_misslyckat_kommando_utan_konto_sparrar_och_sparar_inget_marke(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=True)
    varld.kommando_svar = (1, f"något gick fel {LOSEN_1}")  # även ett eko av lösenordet döljs
    prog = _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    assert prog.lage == "sparrad"
    assert [n for h, n in varld.logg if h == "starta"] == ["API"]
    assert _marke(tmp_path) == hemligheter.SAKNAS
    assert "kunde inte sättas" in caplog.text and "Kontot kunde inte skapas" in caplog.text
    assert LOSEN_1 not in caplog.text


def test_misslyckat_kommando_med_konto_startar_med_tidigare_losenord(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=True)
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    marke = _marke(tmp_path)
    varld.kommando_svar = (-1, "kommandot blev inte klart inom 300 sekunder")
    prog = _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_2)
    assert prog.lage == "igang"
    assert _marke(tmp_path) == marke  # försöks igen vid nästa start
    assert "tidigare lösenordet" in caplog.text
    varld.kommando_svar = None
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_2)
    assert varld.konton[EPOST] == LOSEN_2


def test_ny_adress_med_andra_konton_skapar_administrator_och_varnar(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=True)
    _kor(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    _kor(tmp_path, varld, admin_email="ny@example.se", admin_password=LOSEN_1)
    assert varld.konton == {EPOST: LOSEN_1, "ny@example.se": LOSEN_1}
    assert "Det fanns inget konto med adressen ny@example.se" in caplog.text


def test_kommandot_kors_som_api_anvandaren(tmp_path):
    varld = Varld(behover_konto=True)
    prog = _program(tmp_path, varld, admin_email=EPOST, admin_password=LOSEN_1)
    prog.byt_anvandare = True
    prog.tillampa_adminuppgifter(True)
    (k,) = varld.kommandon
    assert k["anvandare"] == startprogram.ANVANDARE_API
    assert k["katalog"] == prog.sokvagar.app
    assert k["argv"][0] == prog.sokvagar.python


def test_riktigt_kommando_far_losenordet_i_miljon_inte_i_argv(tmp_path):
    kod, ut = startprogram.kor_kommando(
        [sys.executable, "-c", "import os, sys; print(sys.argv[1:]); print(os.environ['DRIVELOG_ADMIN_PASSWORD'] == 'x' * 12)",
         EPOST],
        miljo={"PATH": "/usr/bin:/bin", "DRIVELOG_ADMIN_PASSWORD": "x" * 12},
        katalog=tmp_path,
        anvandare=None,
        tidsgrans=30,
    )
    assert kod == 0
    assert ut.splitlines() == [f"['{EPOST}']", "True"]


def test_riktigt_kommando_med_tidsgrans(tmp_path):
    kod, ut = startprogram.kor_kommando(
        [sys.executable, "-c", "import time; time.sleep(5)"], miljo={}, katalog=tmp_path, anvandare=None,
        tidsgrans=0.3,
    )
    assert kod == -1 and "inom" in ut
    kod, ut = startprogram.kor_kommando(
        [str(tmp_path / "finns-inte")], miljo={}, katalog=tmp_path, anvandare=None, tidsgrans=5
    )
    assert kod == -1


def test_som_anvandare_lagger_setpriv_forst_som_root(monkeypatch):
    monkeypatch.setattr(startprogram.os, "geteuid", lambda: 0)
    kommando = startprogram._som_anvandare(["/opt/venv/bin/python", "-m", "app.cli", "create-admin", EPOST], "drivelog")
    assert kommando[:2] == ["setpriv", "--reuid=drivelog"]
    assert "--no-new-privs" in kommando
    assert not any("caps" in a for a in kommando)
    assert kommando[-5:] == ["/opt/venv/bin/python", "-m", "app.cli", "create-admin", EPOST]
