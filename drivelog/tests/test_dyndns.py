"""DynDNS-uppdateraren mot en lokal låtsasserver för STRATO och en låtsasproxy."""

import base64
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from drivelog_addon import dyndns, kontroll
from drivelog_addon.dyndns import Uppdaterare, tolka_svar

TOKEN = "kontrollnyckel-" + "x" * 20
LOSEN = "strato-losenord-hemligt"


class Server:
    def __init__(self, hanterare):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), hanterare)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.trad = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.trad.start()

    def stang(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def strato():
    """Låtsas-STRATO: svarar med nästa svar i kön och sparar anropen."""
    tillstand = {"svar": [], "anrop": []}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            tillstand["anrop"].append(
                {"path": self.path, "headers": dict(self.headers), "query": parse_qs(urlsplit(self.path).query)}
            )
            status, text = tillstand["svar"].pop(0) if tillstand["svar"] else (200, "nochg 127.0.0.1")
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    s = Server(H)
    s.tillstand = tillstand
    yield s
    s.stang()


@pytest.fixture
def proxy():
    """Låtsas-Caddy: svarar på kontrollfrågan med HMAC (som tilläggets kontrollserver gör)."""
    tillstand = {"token": TOKEN, "fragor": []}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            slump = kontroll.slumpvarde_ur(self.path)
            if slump is None:
                self.send_response(404)
                self.end_headers()
                return
            tillstand["fragor"].append(slump)
            data = kontroll.svar(tillstand["token"], slump).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    s = Server(H)
    s.tillstand = tillstand
    yield s
    s.stang()


class Klocka:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Dns:
    def __init__(self, a=None, aaaa=None):
        self.a = a or []
        self.aaaa = aaaa or []

    def __call__(self, doman, familj):
        return list(self.a if familj == socket.AF_INET else self.aaaa)


def _uppdaterare(strato, proxy, dns, klocka, **kw):
    return Uppdaterare(
        "drivelog.example.se",
        "example.se",
        LOSEN,
        TOKEN,
        version="test",
        strato_url=f"http://127.0.0.1:{strato.port}/nic/update",
        kontroll_schema="http",
        kontroll_port=proxy.port,
        sla_upp=dns,
        klocka=klocka,
        **{"godta_adress": lambda a: True, **kw},  # låtsasservrarna ligger på 127.0.0.1
    )


@pytest.mark.parametrize(
    ("text", "kod", "adress"),
    [
        ("good 1.2.3.4", "good", "1.2.3.4"),
        ("nochg 1.2.3.4\n", "nochg", "1.2.3.4"),
        ("badauth", "badauth", None),
        ("!donator", "!donator", None),
        ("GOOD 1.2.3.4", "good", "1.2.3.4"),
        ("", "", None),
        ("good 1.2.3.4\nnochg 5.6.7.8", "good", "1.2.3.4"),
    ],
)
def test_tolka_svar(text, kod, adress):
    svar = tolka_svar(text)
    assert (svar.kod, svar.adress) == (kod, adress)


def test_oforandrad_adress_skickar_ingenting(strato, proxy):
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"]), Klocka())
    assert upp.kor_ett_varv() == dyndns.KONTROLLINTERVALL
    assert upp.kor_ett_varv() == dyndns.KONTROLLINTERVALL
    assert strato.tillstand["anrop"] == []
    assert upp.kand_adress == "127.0.0.1"


def test_saknad_a_post_uppdateras_och_anropet_ar_ratt(strato, proxy, caplog):
    caplog.set_level(logging.DEBUG)
    strato.tillstand["svar"] = [(200, "good 127.0.0.1")]
    upp = _uppdaterare(strato, proxy, Dns(a=[]), Klocka())
    assert upp.kor_ett_varv() == dyndns.KONTROLLINTERVALL
    (anrop,) = strato.tillstand["anrop"]
    assert anrop["path"].startswith("/nic/update?")
    assert anrop["query"] == {"hostname": ["drivelog.example.se"]}  # ingen myip: STRATO tar avsändaren
    auth = anrop["headers"]["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == f"example.se:{LOSEN}"
    assert anrop["headers"]["User-Agent"].startswith("SMGD-DriveLog")
    assert upp.kand_adress == "127.0.0.1"
    # Nästa varv: adressen leder hit, inget nytt anrop.
    assert upp.kor_ett_varv() == dyndns.KONTROLLINTERVALL
    assert len(strato.tillstand["anrop"]) == 1
    assert LOSEN not in caplog.text
    assert base64.b64encode(f"example.se:{LOSEN}".encode()).decode() not in caplog.text


def test_bytt_adress_uppdateras(strato, proxy):
    klocka = Klocka()
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"]), klocka)
    upp.kor_ett_varv()
    assert strato.tillstand["anrop"] == []
    proxy.tillstand["token"] = "en-annan-maskin"  # adressen leder inte längre hit
    strato.tillstand["svar"] = [(200, "good 127.0.0.1")]
    klocka.t += 300
    assert upp.kor_ett_varv() == dyndns.KONTROLLINTERVALL
    assert len(strato.tillstand["anrop"]) == 1


def test_gles_sakerhetsuppdatering(strato, proxy):
    klocka = Klocka()
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"]), klocka)
    upp.kor_ett_varv()
    klocka.t += dyndns.SAKERHETSINTERVALL - 1
    upp.kor_ett_varv()
    assert strato.tillstand["anrop"] == []
    klocka.t += 2
    upp.kor_ett_varv()
    assert len(strato.tillstand["anrop"]) == 1
    klocka.t += 600
    upp.kor_ett_varv()
    assert len(strato.tillstand["anrop"]) == 1


@pytest.mark.parametrize("kod", sorted(dyndns.ATGARD))
def test_fel_som_kraver_atgard_stoppar(strato, proxy, kod, caplog):
    strato.tillstand["svar"] = [(401 if kod == "badauth" else 200, kod)]
    upp = _uppdaterare(strato, proxy, Dns(a=[]), Klocka())
    assert upp.kor_ett_varv() is None
    assert upp.stoppad
    assert upp.kor_ett_varv() is None
    assert len(strato.tillstand["anrop"]) == 1  # aldrig upprepat automatiskt
    assert "DynDNS stoppas" in caplog.text
    assert LOSEN not in caplog.text


def test_http_401_utan_svarskod_behandlas_som_badauth(strato, proxy):
    # Så svarar dyndns.strato.com utan giltig inloggning (provat 2026-11-15).
    strato.tillstand["svar"] = [(401, "<!DOCTYPE HTML><html><title>401 Authorization Required</title></html>")]
    upp = _uppdaterare(strato, proxy, Dns(a=[]), Klocka())
    assert upp.kor_ett_varv() is None
    assert upp.stoppad


@pytest.mark.parametrize("kod", ["dnserr", "911"])
def test_tillfalliga_fel_vantar_30_minuter(strato, proxy, kod):
    strato.tillstand["svar"] = [(200, kod)]
    upp = _uppdaterare(strato, proxy, Dns(a=[]), Klocka())
    assert upp.kor_ett_varv() == 30 * 60
    assert not upp.stoppad


def test_oväntat_svar_ger_vaxande_vantan(strato, proxy):
    strato.tillstand["svar"] = [(500, "<html>fel</html>")] * 8
    upp = _uppdaterare(strato, proxy, Dns(a=[]), Klocka())
    vantetider = [upp.kor_ett_varv() for _ in range(7)]
    assert vantetider == [60, 120, 240, 480, 960, 1800, 1800]
    strato.tillstand["svar"] = [(200, "good 127.0.0.1")]
    assert upp.kor_ett_varv() == dyndns.KONTROLLINTERVALL
    assert upp.antal_fel == 0


def test_strato_gar_inte_att_na(proxy, caplog):
    upp = Uppdaterare(
        "drivelog.example.se", "example.se", LOSEN, TOKEN,
        strato_url="http://127.0.0.1:9/nic/update", kontroll_schema="http", kontroll_port=proxy.port,
        sla_upp=Dns(a=[]), klocka=Klocka(), godta_adress=lambda a: True,
    )
    assert upp.kor_ett_varv() == 60
    assert "Kunde inte nå STRATO" in caplog.text
    assert LOSEN not in caplog.text


def test_utan_harnals_nat_uppdateras_glest(strato, proxy, caplog):
    # Domänen pekar på 127.0.0.2 där proxyn inte svarar (som när routern saknar hårnåls-NAT),
    # och STRATO säger att det redan är rätt adress.
    klocka = Klocka()
    strato.tillstand["svar"] = [(200, "nochg 127.0.0.2")] * 3
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.2"]), klocka)
    upp.kor_ett_varv()
    assert upp.harnal_saknas
    assert "hårnåls-NAT" in caplog.text
    klocka.t += 300
    upp.kor_ett_varv()
    assert len(strato.tillstand["anrop"]) == 1
    klocka.t += dyndns.HARNAL_INTERVALL
    upp.kor_ett_varv()
    assert len(strato.tillstand["anrop"]) == 2


def test_varnar_for_aaaa_post_som_inte_leder_hit(strato, proxy, caplog):
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"], aaaa=["::1"]), Klocka())
    upp.kor_ett_varv()
    assert "AAAA" in caplog.text and "::1" in caplog.text
    caplog.clear()
    upp.kor_ett_varv()
    assert "AAAA" not in caplog.text  # samma varning upprepas inte


def test_ingen_varning_utan_aaaa(strato, proxy, caplog):
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"]), Klocka())
    upp.kor_ett_varv()
    assert "AAAA" not in caplog.text


def test_kor_avslutas_nar_stoppad(strato, proxy):
    strato.tillstand["svar"] = [(200, "badauth")]
    upp = _uppdaterare(strato, proxy, Dns(a=[]), Klocka())
    stopp = threading.Event()
    trad = threading.Thread(target=upp.kor, args=(stopp,))
    trad.start()
    trad.join(5)
    assert not trad.is_alive()


def test_kor_stoppas_med_event(strato, proxy):
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"]), Klocka())
    stopp = threading.Event()
    trad = threading.Thread(target=upp.kor, args=(stopp,))
    trad.start()
    stopp.set()
    trad.join(5)
    assert not trad.is_alive()


# --- Granskningen av tillägget: fråga och svar, globala adresser, AAAA utan DynDNS -------------------


def test_kontrollen_ar_fraga_och_svar(strato, proxy):
    upp = _uppdaterare(strato, proxy, Dns(a=["127.0.0.1"]), Klocka())
    assert upp.leder_hit("127.0.0.1")
    assert upp.leder_hit("127.0.0.1")
    a, b = proxy.tillstand["fragor"]
    assert a != b  # nytt slumpvärde varje gång


def test_ateranvant_svar_godtas_inte(strato):
    """En server som har läst ett tidigare svar (eller den gamla statiska texten) kan inte svara rätt."""
    sett = kontroll.svar(TOKEN, "a" * 32)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = sett.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    fel_server = Server(H)
    try:
        upp = _uppdaterare(strato, fel_server, Dns(a=["127.0.0.1"]), Klocka())
        assert not upp.leder_hit("127.0.0.1")
    finally:
        fel_server.stang()


@pytest.mark.parametrize(("adress", "globalt"), [
    ("81.2.69.160", True), ("2a00:1450:4001:80b::200e", True),
    ("192.168.1.10", False), ("10.0.0.5", False), ("172.20.0.1", False), ("100.64.1.1", False),
    ("127.0.0.1", False), ("fd00::1", False), ("fe80::1%eth0", False), ("inte-en-adress", False),
])
def test_ar_global(adress, globalt):
    assert dyndns.ar_global(adress) is globalt


def test_lokal_dns_post_godtas_inte_som_bevis(strato, proxy, caplog):
    """GUI-F5: med en lokal DNS-post (192.168.x.y) uppdateras STRATO direkt i stället för efter 24 timmar."""
    caplog.set_level(logging.INFO)
    strato.tillstand["svar"] = [(200, "good 81.2.69.160")]
    upp = Uppdaterare(
        "drivelog.example.se", "example.se", LOSEN, TOKEN,
        strato_url=f"http://127.0.0.1:{strato.port}/nic/update", kontroll_schema="http", kontroll_port=proxy.port,
        sla_upp=Dns(a=["192.168.1.10"]), klocka=Klocka(),
    )
    upp.kor_ett_varv()
    assert len(strato.tillstand["anrop"]) == 1
    assert proxy.tillstand["fragor"] == []  # den lokala adressen prövades aldrig
    assert "lokal adress" in caplog.text
    assert upp.kand_adress == "81.2.69.160"


def test_lokal_adress_fran_strato_sparas_inte(strato, proxy):
    strato.tillstand["svar"] = [(200, "good 10.1.2.3")]
    upp = Uppdaterare(
        "drivelog.example.se", "example.se", LOSEN, TOKEN,
        strato_url=f"http://127.0.0.1:{strato.port}/nic/update", kontroll_schema="http", kontroll_port=proxy.port,
        sla_upp=Dns(a=[]), klocka=Klocka(),
    )
    upp.kor_ett_varv()
    assert upp.kand_adress is None


def test_lokal_aaaa_post_ger_ingen_varning(caplog):
    upp = Uppdaterare("drivelog.example.se", "", "", TOKEN, sla_upp=Dns(aaaa=["fd00::5", "fe80::1"]))
    upp.kontrollera_aaaa()
    assert "AAAA" not in caplog.text


def test_aaaa_kontroll_utan_dyndns(caplog):
    """SUP-F5: AAAA-kontrollen körs också när DynDNS är avslaget – och skickar inget till STRATO."""
    anrop = []

    def hamta(url, **kw):
        anrop.append(url)
        return 0, ""  # adressen svarar inte

    dyndns.kontrollera_aaaa_en_gang(
        "drivelog.example.se", TOKEN, threading.Event(), fordrojning=0,
        sla_upp=Dns(aaaa=["2a00:1450:4001:80b::200e"]), hamta=hamta,
    )
    assert anrop and all(dyndns.DYNDNS_KONTROLLSOKVAG in u for u in anrop)  # bara kontrollen, aldrig STRATO
    assert "AAAA" in caplog.text and "Domainverwaltung" in caplog.text
    assert "Domänverwaltung" not in caplog.text


def test_aaaa_kontroll_utan_dyndns_avbryts_vid_stopp(caplog):
    stopp = threading.Event()
    stopp.set()
    dyndns.kontrollera_aaaa_en_gang("drivelog.example.se", TOKEN, stopp, fordrojning=5,
                                    sla_upp=Dns(aaaa=["2a00:1450:4001:80b::200e"]))
    assert "AAAA" not in caplog.text


def test_kontrollservern_svarar_bara_pa_giltiga_fragor():
    server = kontroll.Kontrollserver(TOKEN, port=0).starta()
    try:
        bas = f"http://127.0.0.1:{server.port}"
        slump = kontroll.nytt_slumpvarde()
        status, text = dyndns.http_get(f"{bas}{kontroll.SOKVAG}?n={slump}")
        assert status == 200 and kontroll.ar_ratt_svar(TOKEN, slump, text)
        assert TOKEN not in text
        for fel in ("/", kontroll.SOKVAG, f"{kontroll.SOKVAG}?n=kort", f"{kontroll.SOKVAG}?n={slump}&n={slump}",
                    f"{kontroll.SOKVAG}?n=" + "%22" * 20):
            assert dyndns.http_get(bas + fel)[0] == 404, fel
    finally:
        server.stang()
