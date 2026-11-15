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

    def __init__(self, behover_konto=False, api_svarar=True, konton=None):
        self.logg = []
        # Låtsasdatabas: adress -> lösenord. behover_konto=False utan konton ger ett okänt konto.
        self.konton = dict(konton or {})
        if not behover_konto and not self.konton:
            self.konton["nagon@example.se"] = "okant-losenord-123"
        self.kommandon = []
        self.kommando_svar = None  # (kod, utskrift) för att låtsas att kommandot misslyckas
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
                return 200, {"needs_setup": not self.konton}
        if ":3000/" in url and "webbappen" in self.processer:
            return 200, {"status": "ok"}
        return 0, None

    def kor_kommando(self, kommando, *, miljo, katalog, anvandare, tidsgrans):
        """Som ``python -m app.cli create-admin <adress>``: lösenordet ur DRIVELOG_ADMIN_PASSWORD."""
        self.logg.append(("kommando", kommando[-1]))
        self.kommandon.append({"argv": kommando, "miljo": miljo, "katalog": katalog, "anvandare": anvandare})
        if self.kommando_svar:
            return self.kommando_svar
        epost = kommando[-1]
        skapad = epost not in self.konton
        self.konton[epost] = miljo["DRIVELOG_ADMIN_PASSWORD"]
        return 0, f"Kontot {epost} skapades." if skapad else f"Lösenordet för {epost} är återställt."


def _program(tmp_path, varld, **inst):
    data = {"domain": "drivelog.example.se", "strato_dyndns": False, "log_level": "info", **inst}
    sok = Sokvagar(data=tmp_path / "data", runtime=tmp_path / "run")
    sok.data.mkdir(exist_ok=True)  # en ny start med samma /data återanvänder nycklar och märke
    return Startprogram(
        installningar.tolka(data),
        hemligheter.las_eller_skapa(sok.data),
        sokvagar=sok,
        starta=varld.starta,
        hamta=varld.hamta,
        kor_kommando=varld.kor_kommando,
        byt_anvandare=False,
        intervall=0.01,
        api_tidsgrans=2,
        webb_tidsgrans=2,
        tidszon="Europe/Stockholm",
        version="test",
        kontroll_port=0,
        aaaa_fordrojning=60,
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


def test_adminuppgifter_ges_bara_till_kommandot_och_loggas_aldrig(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    varld = Varld(behover_konto=True)
    program = _program(tmp_path, varld, admin_email="agare@example.se", admin_password=ADMIN_LOSEN)
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    stopp.set()
    trad.join(5)
    # API-processen får aldrig lösenordet; kommandot får det i miljön, aldrig i argv.
    assert not any(ADMIN_LOSEN in v for v in varld.miljoer["API"].values())
    assert not any(k.startswith("DRIVELOG_FIRST_ADMIN") for k in varld.miljoer["API"])
    (kommando,) = varld.kommandon
    assert kommando["argv"][-4:] == ["-m", "app.cli", "create-admin", "agare@example.se"]
    assert all(ADMIN_LOSEN not in a for a in kommando["argv"])
    assert kommando["miljo"]["DRIVELOG_ADMIN_PASSWORD"] == ADMIN_LOSEN
    # Samma miljö (databas, nycklar) som API:t.
    for nyckel in ("DRIVELOG_DATABASE_URL", "DRIVELOG_DATA_DIR", "DRIVELOG_SECRET_KEY", "DRIVELOG_ENCRYPTION_KEYS"):
        assert kommando["miljo"][nyckel] == varld.miljoer["API"][nyckel]
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
    data = str(tmp_path / "data" / "app")
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
    assert miljo["DRIVELOG_WEB_SETUP"] == "false"  # SAK-S1: kontot skapas bara ur inställningarna
    assert miljo["HOME"] == data
    assert "DRIVELOG_FIRST_ADMIN_PASSWORD" not in miljo
    # I tillägget blir sökvägen den i kontraktet: sqlite:////data/app/drivelog.sqlite3
    standard = Startprogram(program.inst, program.nycklar).miljo_api()
    assert standard["DRIVELOG_DATABASE_URL"] == "sqlite:////data/app/drivelog.sqlite3"
    assert standard["DRIVELOG_DATA_DIR"] == "/data/app"
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


def test_dyndns_ger_kontrollfraga_i_caddyfilen(tmp_path, monkeypatch):
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
    caddyfil = (tmp_path / "run" / "Caddyfile").read_text()
    assert program.kontrollnyckel not in caddyfil  # nyckeln lämnar aldrig startprogrammet
    assert f"reverse_proxy 127.0.0.1:{program.kontroll_port}" in caddyfil
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


# --- Granskningen av tillägget -----------------------------------------------------------------------

import os  # noqa: E402
import stat  # noqa: E402

from drivelog_addon import dyndns, kontroll  # noqa: E402


def test_kontrollservern_svarar_medan_tillagget_gar(tmp_path):
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld)
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    slump = kontroll.nytt_slumpvarde()
    status, text = dyndns.http_get(f"http://127.0.0.1:{program.kontroll_port}{kontroll.SOKVAG}?n={slump}")
    assert status == 200 and kontroll.ar_ratt_svar(program.kontrollnyckel, slump, text)
    stopp.set()
    trad.join(5)
    assert program._kontrollserver is None  # stängd vid nedstängning


def _fanga_vakter(monkeypatch):
    """Adressvakternas kor() ersätts: inget nätverk, men vi ser vilka som startas och hur."""
    vakter = []
    monkeypatch.setattr(startprogram.Adressvakt, "kor", lambda self, stopp: vakter.append(self))
    return vakter


def test_adressvakten_startas_utan_dyndns(tmp_path, monkeypatch):
    """1.0.3: utan DynDNS startar adressvakten (med AAAA-kontrollen) efter samma fördröjning som förut."""
    vakter = _fanga_vakter(monkeypatch)
    uppdaterare = []
    monkeypatch.setattr(startprogram.Uppdaterare, "kor", lambda self, stopp: uppdaterare.append(self))
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld)
    program.supervisor_token = None
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang" and vakter)
    stopp.set()
    trad.join(5)
    (vakt,) = vakter
    assert vakt.doman == "drivelog.example.se"
    assert vakt.fordrojning == 60
    assert vakt.data_katalog == tmp_path / "data"
    assert uppdaterare == []


def test_ingen_adressvakt_for_lokal_doman(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    vakter = _fanga_vakter(monkeypatch)
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld, domain="localhost")
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    stopp.set()
    trad.join(5)
    assert vakter == []
    assert "Adressvakten" not in caplog.text


def test_med_dyndns_ingen_adressvakt_och_uppdateraren_som_forut(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    vakter = _fanga_vakter(monkeypatch)
    uppdaterare = []
    monkeypatch.setattr(startprogram.Uppdaterare, "kor", lambda self, stopp: uppdaterare.append(self))
    varld = Varld(behover_konto=False)
    program = _program(
        tmp_path, varld, strato_dyndns=True, strato_dyndns_user="example.se", strato_dyndns_password="p"  # noqa: S106 - låtsasvärde
    )
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang" and uppdaterare)
    stopp.set()
    trad.join(5)
    assert vakter == [] and len(uppdaterare) == 1
    assert "Adressvakten" not in caplog.text
    assert not (tmp_path / "data" / "adressvakt.json").exists()


SUPERVISOR_TOKEN = "supervisor-token-for-test-" + "q" * 16  # noqa: S105 - låtsasvärde


def test_supervisor_token_nar_aldrig_barnprocesserna_eller_loggen(tmp_path, monkeypatch, caplog):
    """Token (administratörsåtkomst till Home Assistant) stannar i startprogrammet."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("SUPERVISOR_TOKEN", SUPERVISOR_TOKEN)
    vakter = _fanga_vakter(monkeypatch)
    varld = Varld(behover_konto=True)
    program = _program(tmp_path, varld, admin_email="agare@example.se", admin_password=ADMIN_LOSEN)
    assert program.supervisor_token == SUPERVISOR_TOKEN  # läses ur startprogrammets egen miljö
    assert SUPERVISOR_TOKEN not in repr(program)
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang" and vakter)
    stopp.set()
    trad.join(5)
    assert set(varld.miljoer) == {"API", "webbappen", "Caddy"}
    (kommando,) = varld.kommandon  # create-admin
    for miljo in [*varld.miljoer.values(), kommando["miljo"]]:
        assert "SUPERVISOR_TOKEN" not in miljo
        assert not any(SUPERVISOR_TOKEN in v for v in miljo.values())
    assert all(SUPERVISOR_TOKEN not in a for a in kommando["argv"])
    assert SUPERVISOR_TOKEN not in caplog.text
    # Vakten har fått en aviserare med token – och den syns inte heller där.
    (vakt,) = vakter
    assert vakt._avisera.aktiv and SUPERVISOR_TOKEN not in repr(vakt._avisera)


def test_sparrlaget_sager_att_kontot_bara_skapas_ur_installningarna(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=True)
    program = _program(tmp_path, varld)
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "sparrad")
    stopp.set()
    trad.join(5)
    assert "Administratörens e-post" in caplog.text and "Konfiguration" in caplog.text
    assert varld.miljoer["API"]["DRIVELOG_WEB_SETUP"] == "false"


def test_med_konto_och_utan_marke_andras_ingenting_och_inget_pastas_om_vems_kontot_ar(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    varld = Varld(behover_konto=False)
    program = _program(tmp_path, varld, admin_email="agare@example.se", admin_password=ADMIN_LOSEN)
    stopp, trad, _ = _kor_i_bakgrunden(program)
    assert _vanta_tills(lambda: program.lage == "igang")
    stopp.set()
    trad.join(5)
    assert "Kontot finns" not in caplog.text
    assert varld.kommandon == []
    assert "inget konto skapades" in caplog.text and "NYTT lösenord" in caplog.text


def test_main_avbryter_nar_nyckeln_saknas_men_databasen_finns(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(startprogram.pwd, "getpwnam", lambda n: _Pw(os.geteuid()))
    data = tmp_path / "data"
    data.mkdir()
    (data / "options.json").write_text(json.dumps({"domain": "drivelog.example.se", "strato_dyndns": False}))
    sok = Sokvagar(data=data, runtime=tmp_path / "run")
    hemligheter.las_eller_skapa(data)
    sok.app_data.mkdir()
    sok.databas.write_bytes(b"SQLite format 3\x00")
    (data / hemligheter.KATALOG / hemligheter.ENCRYPTION_KEYS_FIL).unlink()
    assert startprogram.main(sok) == 1
    assert "saknas men databasen finns" in capsys.readouterr().err
    assert not (data / hemligheter.KATALOG / hemligheter.ENCRYPTION_KEYS_FIL).exists()


def test_forbered_data_utan_root_skapar_katalogerna(tmp_path):
    sok = Sokvagar(data=tmp_path / "data", runtime=tmp_path / "run")
    sok.data.mkdir()
    startprogram.forbered_data(sok, som_root=False)
    assert sok.app_data.is_dir() and sok.caddy_data.is_dir() and sok.runtime.is_dir()


def test_forbered_data_vagrar_symlank_som_appkatalog(tmp_path):
    sok = Sokvagar(data=tmp_path / "data", runtime=tmp_path / "run")
    sok.data.mkdir()
    (sok.data / "hemligheter").mkdir()
    sok.app_data.symlink_to("hemligheter")
    with pytest.raises(startprogram.OsakerLagring):
        startprogram.forbered_data(sok, som_root=False)


class _Pw:
    def __init__(self, uid):
        self.pw_uid = self.pw_gid = uid


@pytest.mark.skipif(os.geteuid() != 0, reason="kräver root (ägarbyte)")
def test_forbered_data_som_root_ger_ny_layout(tmp_path, monkeypatch):
    """SAK-S3/SUP-F1: /data root 711, /data/app API:t 700, /data/caddy Caddy 700, inga klistriga bitar."""
    monkeypatch.setattr(startprogram.pwd, "getpwnam", lambda n: _Pw({"drivelog": 10001, "caddy": 10003}[n]))
    sok = Sokvagar(data=tmp_path / "data", runtime=tmp_path / "run")
    sok.data.mkdir()
    os.chown(sok.data, 10001, 10001)  # som i 1.0.0
    os.chmod(sok.data, 0o1711)  # noqa: S103 - layouten i 1.0.0
    sok.options.write_text("{}")
    os.chmod(sok.options, 0o644)
    sok.app_data.mkdir()
    # Nycklarna skapas av en tidigare start (som i 1.0.0, innan databasen fanns).
    hemligheter.las_eller_skapa(sok.data)
    (sok.app_data / "drivelog.sqlite3").write_text("")
    # En hård länk till options.json i API:ts katalog får inte byta ägare på options.json.
    os.link(sok.options, sok.app_data / "lank")
    startprogram.forbered_data(sok, som_root=True)
    hemligheter.las_eller_skapa(sok.data, databas=sok.databas)

    def info(p):
        st = os.lstat(p)
        return st.st_uid, stat.S_IMODE(st.st_mode)

    assert info(sok.data) == (0, 0o711)
    assert info(sok.options) == (0, 0o600)
    assert info(sok.data / hemligheter.KATALOG) == (0, 0o700)
    assert info(sok.app_data) == (10001, 0o700)
    assert info(sok.databas)[0] == 10001
    assert info(sok.caddy_data) == (10003, 0o700)
