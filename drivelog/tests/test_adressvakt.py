"""Adressvakten (när STRATO DynDNS är avslaget) och aviseringen i Home Assistant – varv för varv,
med injicerad klocka, uppslagning, hämtning och aviseringsfunktion. Inget nätverk används."""

import json
import logging
import os
import socket
import stat
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from drivelog_addon import adressvakt, avisering, kontroll
from drivelog_addon.adressvakt import Adressvakt, Tillstand

TOKEN = "kontrollnyckel-" + "y" * 20
DOMAN = "drivelog.smgd.se"
HEMMA = "203.0.113.10"  # adressen där "vår" proxy svarar
PARKERING = "198.51.100.20"  # t.ex. STRATOS parkeringssida eller en gammal hemadress
LOKAL = "192.168.1.10"
AAAA_FEL = "2001:db8::5"  # dokumentationsadress – godkänns som "global" av låtsasregeln nedan


class Klocka:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


class Natet:
    """Låtsas-DNS och låtsasproxy: ``svarar`` är adresserna där den egna proxyn svarar rätt."""

    def __init__(self, a=(), aaaa=(), svarar=(HEMMA,)):
        self.a, self.aaaa, self.svarar = list(a), list(aaaa), set(svarar)
        self.hamtningar = []

    def sla_upp(self, doman, familj):
        assert doman == DOMAN
        return list(self.a if familj == socket.AF_INET else self.aaaa)

    def hamta(self, url, *, headers, timeout, ansluta_till, verifiera):
        self.hamtningar.append((url, ansluta_till))
        assert kontroll.SOKVAG in url  # bara kontrollfrågan – ingen annan tjänst tillfrågas
        if ansluta_till not in self.svarar:
            raise OSError("ingen svarar")
        slump = parse_qs(urlsplit(url).query)["n"][0]
        return 200, kontroll.svar(TOKEN, slump)


class Aviseringar:
    def __init__(self, lyckas=True):
        self.anrop = []
        self.lyckas = lyckas

    def __call__(self, atgard, titel, text):
        self.anrop.append((atgard, titel, text))
        return self.lyckas

    def atgarder(self):
        return [a for a, _, _ in self.anrop]


def _godta(adress):
    return not adress.startswith(("192.168.", "10.", "127."))


def _vakt(tmp_path, natet, aviseringar=None, klocka=None, doman=DOMAN):
    return Adressvakt(
        doman, TOKEN, data_katalog=tmp_path, avisera=aviseringar, fordrojning=0,
        hamta=natet.hamta, sla_upp=natet.sla_upp, klocka=klocka or Klocka(), godta_adress=_godta,
    )


def _varv(vakt, klocka, n):
    for _ in range(n):
        assert vakt.kor_ett_varv() == adressvakt.INTERVALL
        klocka.t += adressvakt.INTERVALL


# --- Utfallen -------------------------------------------------------------------------------------


def test_leder_hit_loggas_forsta_gangen_och_sparas(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    natet, klocka = Natet(a=[HEMMA]), Klocka()
    vakt = _vakt(tmp_path, natet, Aviseringar(), klocka)
    _varv(vakt, klocka, 1)
    assert f"{DOMAN} leder hit (A-posten {HEMMA})." in caplog.text
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN, lyckats=True)
    caplog.clear()
    _varv(vakt, klocka, 3)
    assert caplog.text == ""  # upprepas inte i varje varv


def test_nagon_global_adress_som_svarar_racker(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    natet, klocka = Natet(a=[LOKAL, PARKERING, HEMMA]), Klocka()
    vakt = _vakt(tmp_path, natet, klocka=klocka)
    _varv(vakt, klocka, 1)
    assert f"leder hit (A-posten {HEMMA})" in caplog.text
    assert LOKAL not in [a for _, a in natet.hamtningar]  # lokala adresser prövas aldrig


def test_leder_inte_hit_och_aldrig_lyckats_namner_harnals_nat(tmp_path, caplog):
    natet, klocka = Natet(a=[PARKERING]), Klocka()
    aviseringar = Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, 1)
    (rad,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    text = rad.getMessage()
    assert f"{DOMAN} leder inte hit: A-posten pekar på {PARKERING}" in text
    assert "Ändra A-posten hos STRATO till ditt hems publika IPv4-adress" in text
    assert "WAN- eller internetadress" in text and "port 80 och 443" in text
    assert "hårnåls-NAT" in text and "mobilnätet" in text
    # Har aldrig lyckats → aldrig någon avisering, hur länge det än pågår.
    _varv(vakt, klocka, 20)
    assert aviseringar.anrop == []


def test_loggraden_upprepas_hogst_en_gang_per_dygn(tmp_path, caplog):
    natet, klocka = Natet(a=[PARKERING]), Klocka()
    vakt = _vakt(tmp_path, natet, klocka=klocka)
    varv_per_dygn = adressvakt.PAMINNELSE // adressvakt.INTERVALL
    _varv(vakt, klocka, varv_per_dygn)
    assert sum("leder inte hit" in r.getMessage() for r in caplog.records) == 1
    _varv(vakt, klocka, 1)
    assert sum("leder inte hit" in r.getMessage() for r in caplog.records) == 2


def test_nytt_lage_loggas_direkt(tmp_path, caplog):
    natet, klocka = Natet(a=[PARKERING]), Klocka()
    vakt = _vakt(tmp_path, natet, klocka=klocka)
    _varv(vakt, klocka, 2)
    natet.a = []
    _varv(vakt, klocka, 2)
    meddelanden = [r.getMessage() for r in caplog.records]
    assert sum("leder inte hit" in m for m in meddelanden) == 1
    assert sum("har ingen A-post" in m for m in meddelanden) == 1


def test_ingen_a_post(tmp_path, caplog):
    natet, klocka = Natet(a=[]), Klocka()
    vakt = _vakt(tmp_path, natet, klocka=klocka)
    _varv(vakt, klocka, 1)
    assert f"{DOMAN} har ingen A-post (IPv4-adress)" in caplog.text
    assert "Ändra A-posten hos STRATO" in caplog.text
    assert natet.hamtningar == []


def test_bara_lokala_adresser_sags_tydligt_och_larmar_aldrig(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    natet, klocka = Natet(a=[LOKAL]), Klocka()
    aviseringar = Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, 10)
    assert f"ger bara lokala adresser här ({LOKAL})" in caplog.text
    assert sum("lokala adresser" in r.getMessage() for r in caplog.records) == 1
    assert aviseringar.anrop == [] and natet.hamtningar == []


# --- Aviseringen ----------------------------------------------------------------------------------


def test_avisering_efter_tidigare_lyckad_kontroll_och_n_misslyckade_varv(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    natet, klocka, aviseringar = Natet(a=[HEMMA]), Klocka(), Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, 2)
    natet.a = [PARKERING]  # hemmets adress har bytts; A-posten pekar på den gamla
    _varv(vakt, klocka, adressvakt.LARM_EFTER - 1)
    assert aviseringar.anrop == []  # en kort störning (omstart av routern) larmar inte
    # Har lyckats tidigare → loggraden nämner inte hårnåls-NAT.
    assert "hårnåls-NAT" not in caplog.text
    _varv(vakt, klocka, 1)
    ((atgard, titel, text),) = aviseringar.anrop
    assert atgard == "skapa"
    assert titel == f"DriveLog: {DOMAN} leder inte längre hem"
    assert PARKERING in text and "Ändra A-posten hos STRATO" in text and "port 80 och 443" in text
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN, lyckats=True, aviserad=True)
    _varv(vakt, klocka, 10)
    assert len(aviseringar.anrop) == 1  # skickas inte om i varje varv
    # Återhämtning: aviseringen tas bort och det loggas.
    natet.a = [HEMMA]
    caplog.clear()
    _varv(vakt, klocka, 1)
    assert aviseringar.atgarder() == ["skapa", "ta_bort"]
    assert f"{DOMAN} leder hit igen (A-posten {HEMMA})." in caplog.text
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN, lyckats=True, aviserad=False)
    _varv(vakt, klocka, 2)
    assert aviseringar.atgarder() == ["skapa", "ta_bort"]


def test_avisering_nar_a_posten_har_forsvunnit(tmp_path):
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    natet, klocka, aviseringar = Natet(a=[]), Klocka(), Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, adressvakt.LARM_EFTER)
    ((_, _, text),) = aviseringar.anrop
    assert "inte längre någon A-post" in text


def test_aviseringen_tas_bort_efter_omstart(tmp_path, caplog):
    """Tillståndet överlever omstart: en avisering från förra starten tas bort när det fungerar igen."""
    caplog.set_level(logging.INFO)
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True, aviserad=True))
    natet, klocka, aviseringar = Natet(a=[HEMMA]), Klocka(), Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, 1)
    assert aviseringar.atgarder() == ["ta_bort"]
    assert "leder hit igen" in caplog.text


def test_misslyckad_borttagning_provas_igen(tmp_path):
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True, aviserad=True))
    natet, klocka, aviseringar = Natet(a=[HEMMA]), Klocka(), Aviseringar(lyckas=False)
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, 2)
    assert aviseringar.atgarder() == ["ta_bort", "ta_bort"]
    assert adressvakt.las_tillstand(tmp_path, DOMAN).aviserad
    aviseringar.lyckas = True
    _varv(vakt, klocka, 1)
    assert not adressvakt.las_tillstand(tmp_path, DOMAN).aviserad


def test_utan_aviseringsfunktion_bara_logg(tmp_path, caplog):
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    natet, klocka = Natet(a=[PARKERING]), Klocka()
    vakt = _vakt(tmp_path, natet, None, klocka)
    _varv(vakt, klocka, adressvakt.LARM_EFTER + 2)
    assert "leder inte hit" in caplog.text
    assert not adressvakt.las_tillstand(tmp_path, DOMAN).aviserad


# --- AAAA-kontrollen i samma vakt -----------------------------------------------------------------


def test_aaaa_varnas_en_gang_och_bara_kontrollfragan_anvands(tmp_path, caplog):
    natet, klocka = Natet(a=[HEMMA], aaaa=[AAAA_FEL]), Klocka()
    vakt = _vakt(tmp_path, natet, klocka=klocka)
    _varv(vakt, klocka, 5)
    assert sum("AAAA" in r.getMessage() for r in caplog.records) == 1
    assert "Domainverwaltung" in caplog.text
    assert all(kontroll.SOKVAG in url for url, _ in natet.hamtningar)


# --- Tillståndsfilen ------------------------------------------------------------------------------


def test_tillstandsfilen_ar_600_och_skrivs_atomart(tmp_path):
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    fil = tmp_path / adressvakt.TILLSTANDSFIL
    assert stat.S_IMODE(os.lstat(fil).st_mode) == 0o600
    assert json.loads(fil.read_text()) == {"doman": DOMAN, "lyckats": True, "aviserad": False}
    assert [p.name for p in tmp_path.iterdir()] == [adressvakt.TILLSTANDSFIL]  # ingen kvarlämnad tmp-fil


def test_tillstandsfilen_ersatts_via_rename(tmp_path, monkeypatch):
    """Atomärt: den nya filen skrivs bredvid och byter plats med os.replace – aldrig halvskriven."""
    anrop = []
    original = os.replace
    monkeypatch.setattr(adressvakt.os, "replace", lambda a, b: (anrop.append((str(a), str(b))), original(a, b)))
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    ((fran, till),) = anrop
    assert fran.startswith(str(tmp_path / f".{adressvakt.TILLSTANDSFIL}.")) and till == str(tmp_path / adressvakt.TILLSTANDSFIL)


@pytest.mark.parametrize("innehall", [b"", b"{", b"[]", b"\xff\xfe", b'{"doman": 5}', b'{"lyckats": "ja"}'])
def test_trasig_tillstandsfil_betyder_aldrig_lyckats(tmp_path, innehall):
    (tmp_path / adressvakt.TILLSTANDSFIL).write_bytes(innehall)
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN)


def test_saknad_tillstandsfil(tmp_path):
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN)


def test_symlank_foljs_aldrig(tmp_path):
    annan = tmp_path / "annan"
    annan.mkdir()
    mal = annan / "mal.json"
    mal.write_text(json.dumps({"doman": DOMAN, "lyckats": True, "aviserad": True}))
    lank = tmp_path / adressvakt.TILLSTANDSFIL
    lank.symlink_to(mal)
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN)  # läses inte genom länken
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    assert not lank.is_symlink()  # länken ersattes ...
    assert json.loads(mal.read_text())["aviserad"] is True  # ... och målet skrevs aldrig


def test_fil_med_flera_harda_lankar_godtas_inte(tmp_path):
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    os.link(tmp_path / adressvakt.TILLSTANDSFIL, tmp_path / "lank")
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN)


def test_bytt_doman_borjar_om(tmp_path, caplog):
    adressvakt.spara_tillstand(tmp_path, Tillstand("gammal.smgd.se", lyckats=True, aviserad=False))
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN)
    # Med den nya domänen räknas kontrollen som aldrig lyckad: ingen avisering, hårnåls-NAT nämns.
    natet, klocka, aviseringar = Natet(a=[PARKERING]), Klocka(), Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, adressvakt.LARM_EFTER + 1)
    assert aviseringar.anrop == [] and "hårnåls-NAT" in caplog.text


def test_bytt_doman_tar_bort_gammal_avisering_vid_lyckad_kontroll(tmp_path):
    adressvakt.spara_tillstand(tmp_path, Tillstand("gammal.smgd.se", lyckats=True, aviserad=True))
    natet, klocka, aviseringar = Natet(a=[HEMMA]), Klocka(), Aviseringar()
    vakt = _vakt(tmp_path, natet, aviseringar, klocka)
    _varv(vakt, klocka, 1)
    assert aviseringar.atgarder() == ["ta_bort"]
    assert adressvakt.las_tillstand(tmp_path, DOMAN) == Tillstand(DOMAN, lyckats=True)


def test_tillstandet_som_inte_gar_att_spara_faller_inget(tmp_path, caplog):
    saknas = tmp_path / "finns-inte"
    natet, klocka = Natet(a=[HEMMA]), Klocka()
    vakt = _vakt(saknas, natet, klocka=klocka)
    _varv(vakt, klocka, 3)
    assert sum("kunde inte sparas" in r.getMessage() for r in caplog.records) == 1


# --- Tråden ---------------------------------------------------------------------------------------


def test_ovantat_fel_faller_inte_vakten(tmp_path, caplog):
    natet = Natet(a=[HEMMA])
    vakt = _vakt(tmp_path, natet)
    varv = []
    stopp = threading.Event()

    def trasig():
        varv.append(1)
        stopp.set()  # så att testet inte väntar ett helt intervall
        raise RuntimeError("oväntat")

    vakt.kor_ett_varv = trasig
    trad = threading.Thread(target=vakt.kor, args=(stopp,))
    trad.start()
    trad.join(5)
    assert not trad.is_alive() and varv == [1]
    assert "Oväntat fel i adressvakten" in caplog.text


def test_stopp_under_fordrojningen_kor_inget(tmp_path):
    natet = Natet(a=[HEMMA])
    vakt = _vakt(tmp_path, natet)
    vakt.fordrojning = 60
    stopp = threading.Event()
    trad = threading.Thread(target=vakt.kor, args=(stopp,))
    trad.start()
    stopp.set()
    trad.join(5)
    assert not trad.is_alive() and natet.hamtningar == []


# --- Aviseraren (Supervisorns proxy) --------------------------------------------------------------


SUPERVISOR_TOKEN = "supervisor-token-for-test-" + "z" * 16  # noqa: S105 - låtsasvärde


class Post:
    def __init__(self, svar=200):
        self.anrop = []
        self.svar = svar

    def __call__(self, url, data, rubriker, timeout):
        self.anrop.append((url, json.loads(data), rubriker))
        if isinstance(self.svar, Exception):
            raise self.svar
        return self.svar


def test_aviseraren_anropar_fast_adress_med_fast_id():
    post = Post()
    aviserare = avisering.Aviserare(SUPERVISOR_TOKEN, post=post)
    assert aviserare("skapa", "Titel", "Text")
    assert aviserare("ta_bort", None, None)
    (url1, data1, rub1), (url2, data2, rub2) = post.anrop
    assert url1 == "http://supervisor/core/api/services/persistent_notification/create"
    assert url2 == "http://supervisor/core/api/services/persistent_notification/dismiss"
    assert data1 == {"notification_id": avisering.NOTIS_ID, "title": "Titel", "message": "Text"}
    assert data2 == {"notification_id": avisering.NOTIS_ID}
    assert rub1["Authorization"] == f"Bearer {SUPERVISOR_TOKEN}" == rub2["Authorization"]
    assert SUPERVISOR_TOKEN not in repr(aviserare)


def test_aviseraren_utan_token_gor_ingenting(caplog):
    post = Post()
    for token in (None, ""):
        assert not avisering.Aviserare(token, post=post)("skapa", "T", "M")
    assert post.anrop == [] and caplog.text == ""


@pytest.mark.parametrize("svar", [401, 500, OSError("nere"), RuntimeError("oväntat")])
def test_fel_fran_supervisorn_faller_inget_och_loggas_en_gang(caplog, svar):
    aviserare = avisering.Aviserare(SUPERVISOR_TOKEN, post=Post(svar))
    for _ in range(3):
        assert aviserare("skapa", "T", "M") is False
        assert aviserare("ta_bort", None, None) is False
    varningar = [r for r in caplog.records if "Aviseringen i Home Assistant" in r.getMessage()]
    assert len(varningar) == 1
    assert SUPERVISOR_TOKEN not in caplog.text


def test_riktig_post_mot_otillganglig_adress_faller_inget(monkeypatch, caplog):
    """Den riktiga POST-funktionen: ett anslutningsfel blir en varning, aldrig ett undantag."""
    monkeypatch.setattr(avisering, "BAS", "http://127.0.0.1:9/core/api/services/persistent_notification/")
    aviserare = avisering.Aviserare(SUPERVISOR_TOKEN)
    assert aviserare("skapa", "T", "M") is False
    assert "Aviseringen i Home Assistant kunde inte skickas" in caplog.text
    assert SUPERVISOR_TOKEN not in caplog.text


def test_vakt_med_aviserare_som_felar_fortsatter(tmp_path, caplog):
    adressvakt.spara_tillstand(tmp_path, Tillstand(DOMAN, lyckats=True))
    natet, klocka = Natet(a=[PARKERING]), Klocka()
    aviserare = avisering.Aviserare(SUPERVISOR_TOKEN, post=Post(OSError("nere")))
    vakt = _vakt(tmp_path, natet, aviserare, klocka)
    _varv(vakt, klocka, adressvakt.LARM_EFTER + 3)
    assert not adressvakt.las_tillstand(tmp_path, DOMAN).aviserad
    assert sum("Aviseringen i Home Assistant" in r.getMessage() for r in caplog.records) == 1
    assert SUPERVISOR_TOKEN not in caplog.text
