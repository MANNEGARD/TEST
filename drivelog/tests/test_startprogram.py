"""Startordningen och kontospärren, med låtsasprocesser i stället för API, webbapp och Caddy."""

import json
import logging
import subprocess
import sys
import threading

import pytest
from drivelog_addon import hemligheter, installningar, startprogram
from drivelog_addon.startprogram import Sokvagar, Startprogram

ADMIN_LOSEN = "ett-langt-losenord-for-test"


class LatsasProcess:
    def __init__(self, namn, logg):
        self.namn = namn
        self.pid = 1000 + len(logg)
        self.kod = None
        self.logg = logg

    def poll(self):
        return self.kod

    def terminate(self):
        self.logg.append(("stoppa", self.namn))
        self.kod = -15

    def kill(self):
        self.kod = -9

    def wait(self, timeout=None):
        return self.kod


class Varld:
    """Låtsasvärld: håller reda på startade processer och svarar på HTTP-anrop."""

    def __init__(self, behover_konto=False, api_svarar=True):
        self.logg = []
        self.processer = {}
        self.behover_konto = behover_konto
        self.api_svarar = api_svarar
        self.miljoer = {}
        self.anvandare = {}
        self.lag_portar = {}

    def starta(self, namn, kommando, *, miljo, katalog, anvandare, lag_portar=False):
        self.logg.append(("starta", namn))
        self.miljoer[namn] = miljo
        self.anvandare[namn] = anvandare
        self.lag_portar[namn] = lag_portar
        proc = LatsasProcess(namn, self.logg)
        self.processer[namn] = proc
        return proc

    def hamta(self, url, timeout):
        if ":8000/" in url:
            if "API" not in self.processer or not self.api_svarar:
                return 0, None
            if url.endswith("/health/ready"):
                return 200, {"status": "ok"}
            if url.endswith("/auth/setup-status"):
                return 200, {"needs_setup": self.behover_konto}
        if ":3000/" in url and "webbappen" in self.processer:
            return 200, {"status": "ok"}
        return 0, None


def _program(tmp_path, varld, **inst):
    data = {"domain": "drivelog.example.se", "strato_dyndns": False, "log_level": "info", **inst}
    sok = Sokvagar(data=tmp_path / "data", runtime=tmp_path / "run")
    sok.data.mkdir()
    return Startprogram(
        installningar.tolka(data),
        hemligheter.las_eller_skapa(sok.data),
        sokvagar=sok,
        starta=varld.starta,
        hamta=varld.hamta,
        byt_anvandare=False,
        intervall=0.01,
        api_tidsgrans=2,
        webb_tidsgrans=2,
        tidszon="Europe/Stockholm",
        version="test",
    )


def _kor_i_bakgrunden(program):
    stopp = threading.Event()
    resultat = {}
    trad = threading.Thread(target=lambda: resultat.setdefault("kod", program.kor(stopp)))
    trad.start()
    return stopp, trad, resultat


def _vanta_tills(villkor, tid=3.0):
    import time

    slut = time.monotonic() + tid
    while time.monotonic() < slut:
        if villkor():
            return True
        time.sleep(0.01)
    return False


def test_full_start_ordning_och_ren_nedstangning(tmp_path):
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld)
    stopp, trad, resultat = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    stopp.set()
    trad.join(5)
    assert resultat["kod"] == 0
    startade = [n for h, n in varld.logg if h == "starta"]
    assert startade == ["API", "webbappen", "Caddy"]
    stoppade = [n for h, n in varld.logg if h == "stoppa"]
    assert stoppade == ["Caddy", "webbappen", "API"]
    assert varld.lag_portar == {"API": False, "webbappen": False, "Caddy": True}
    caddyfile = (tmp_path / "run" / "Caddyfile").read_text()
    assert "drivelog.example.se {" in caddyfile


def test_utan_konto_och_utan_uppgifter_startas_inte_webb_eller_proxy(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=True)
    program = _program(tmp_path, varld)
    stopp, trad, resultat = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "sparrad")
    # Tillägget fortsätter att gå (ingen omstartsslinga) men öppnar ingenting.
    assert trad.is_alive()
    stopp.set()
    trad.join(5)
    assert resultat["kod"] == 0
    assert [n for h, n in varld.logg if h == "starta"] == ["API"]
    assert not (tmp_path / "run" / "Caddyfile").exists()
    assert "admin_email" in caplog.text and "admin_password" in caplog.text


def test_adminuppgifter_skickas_till_api_men_loggas_aldrig(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld, admin_email="agare@example.se", admin_password=ADMIN_LOSEN)
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    stopp.set()
    trad.join(5)
    miljo = varld.miljoer["API"]
    assert miljo["DRIVELOG_FIRST_ADMIN_EMAIL"] == "agare@example.se"
    assert miljo["DRIVELOG_FIRST_ADMIN_PASSWORD"] == ADMIN_LOSEN
    assert ADMIN_LOSEN not in caplog.text
    assert program.nycklar.secret_key not in caplog.text
    assert program.nycklar.encryption_keys not in caplog.text
    # Webbappen och Caddy får inga hemligheter.
    for namn in ("webbappen", "Caddy"):
        varden = " ".join(varld.miljoer[namn].values())
        assert ADMIN_LOSEN not in varden
        assert program.nycklar.secret_key not in varden
        assert program.nycklar.encryption_keys not in varden


def test_kontraktets_miljovariabler(tmp_path):
    varld = Varld()
    program = _program(tmp_path, varld)
    miljo = program.miljo_api()
    data = str(tmp_path / "data")
    assert miljo["DRIVELOG_ENVIRONMENT"] == "production"
    assert miljo["DRIVELOG_DATABASE_URL"] == f"sqlite:///{data}/drivelog.sqlite3"
    assert miljo["DRIVELOG_DATA_DIR"] == data
    assert miljo["DRIVELOG_JOBS_EAGER"] == "true"
    assert miljo["DRIVELOG_REDIS_URL"] == ""
    assert miljo["DRIVELOG_BASE_URL"] == "https://drivelog.example.se"
    assert miljo["DRIVELOG_TIMEZONE"] == "Europe/Stockholm"
    assert miljo["FORWARDED_ALLOW_IPS"] == "127.0.0.1"
    assert miljo["DRIVELOG_SECRET_KEY"] == program.nycklar.secret_key
    assert miljo["DRIVELOG_ENCRYPTION_KEYS"] == program.nycklar.encryption_keys
    assert "DRIVELOG_FIRST_ADMIN_PASSWORD" not in miljo
    # I tillägget blir sökvägen den i kontraktet: sqlite:////data/drivelog.sqlite3
    standard = Startprogram(program.inst, program.nycklar).miljo_api()
    assert standard["DRIVELOG_DATABASE_URL"] == "sqlite:////data/drivelog.sqlite3"
    assert standard["DRIVELOG_DATA_DIR"] == "/data"
    # Supervisors token och liknande ärvs inte.
    assert "SUPERVISOR_TOKEN" not in miljo
    assert program.miljo_webb()["HOSTNAME"] == "127.0.0.1"


def test_process_som_dor_avslutar_allt_med_felkod(tmp_path):
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld)
    stopp, trad, resultat = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    varld.processer["webbappen"].kod = 1
    trad.join(5)
    assert not trad.is_alive()
    assert resultat["kod"] == 1
    stoppade = [n for h, n in varld.logg if h == "stoppa"]
    assert stoppade == ["Caddy", "API"]


def test_api_som_aldrig_blir_redo_ger_felkod(tmp_path):
    varld = Varld(api_svarar=False)
    program = _program(tmp_path, varld)
    program.api_tidsgrans = 0.2
    assert program.kor(threading.Event()) == 1
    assert [n for h, n in varld.logg if h == "starta"] == ["API"]
    assert [n for h, n in varld.logg if h == "stoppa"] == ["API"]


def test_api_som_dor_under_start(tmp_path):
    varld = Varld(api_svarar=False)
    program = _program(tmp_path, varld)
    orig = varld.starta

    def starta(*a, **k):
        proc = orig(*a, **k)
        proc.kod = 3
        return proc

    program.starta = starta
    assert program.kor(threading.Event()) == 1


def test_dyndns_ger_kontrollvarde_i_caddyfilen(tmp_path, monkeypatch):
    startade = []
    monkeypatch.setattr(startprogram.Uppdaterare, "kor", lambda self, stopp: startade.append(self.doman))
    varld = Varld(behover_konto=False)
    program = _program(
        tmp_path, varld, strato_dyndns=True, strato_dyndns_user="example.se", strato_dyndns_password="p"
    )
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang" and startade)
    stopp.set()
    trad.join(5)
    assert program.dyndns_token
    assert program.dyndns_token in (tmp_path / "run" / "Caddyfile").read_text()
    assert startade == ["drivelog.example.se"]


def test_main_med_ogiltiga_installningar(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "options.json").write_text(json.dumps({"domain": "inte giltig", "strato_dyndns": False}))
    assert startprogram.main(Sokvagar(data=data, runtime=tmp_path / "run")) == 1
    assert "domännamn" in capsys.readouterr().err


def test_riktig_processtart_utan_anvandarbyte(tmp_path):
    """starta_process med en riktig (kort) process – utan setpriv eftersom ingen användare anges."""
    proc = startprogram.starta_process(
        "test", [sys.executable, "-c", "print('hej')"], miljo={"PATH": "/usr/bin:/bin"}, katalog=tmp_path,
        anvandare=None,
    )
    assert proc.wait(timeout=10) == 0


@pytest.mark.skipif(not hasattr(subprocess, "Popen"), reason="kräver subprocess")
def test_hamta_json_utan_server():
    assert startprogram.hamta_json("http://127.0.0.1:9/finns-inte", 0.5) == (0, None)


def test_cap_net_bind_service_lases_ur_capbnd(tmp_path):
    fil = tmp_path / "status"
    fil.write_text("Name:\tx\nCapBnd:\t00000000a80425fb\n")  # Dockers standardmängd
    assert startprogram.har_cap_net_bind_service(fil)
    fil.write_text("CapBnd:\t00000000a80421fb\n")  # utan bit 10
    assert not startprogram.har_cap_net_bind_service(fil)
    assert not startprogram.har_cap_net_bind_service(tmp_path / "saknas")


def test_lagsta_oprivilegierade_port(tmp_path):
    fil = tmp_path / "ip_unprivileged_port_start"
    fil.write_text("0\n")
    assert startprogram.lagsta_oprivilegierade_port(fil) == 0
    assert startprogram.lagsta_oprivilegierade_port(tmp_path / "saknas") == 1024
