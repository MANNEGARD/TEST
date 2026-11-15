import re
from pathlib import Path

import pytest
from drivelog_addon.caddyfil import DYNDNS_KONTROLLSOKVAG, skapa_caddyfil

REPO = Path(__file__).resolve().parents[2]


def _rader(text):
    return {r.strip() for r in text.splitlines() if r.strip() and not r.strip().startswith("#")}


def test_grundstruktur():
    text = skapa_caddyfil("drivelog.example.se")
    assert "\ndrivelog.example.se {\n" in text
    assert "admin off" in text
    assert "root /data/caddy" in text
    assert "reverse_proxy 127.0.0.1:8000" in text
    assert "reverse_proxy 127.0.0.1:3000" in text
    assert "handle /api/v1/*" in text
    # Klamrarna är balanserade.
    assert text.count("{") == text.count("}")


def test_samma_sakerhetshuvuden_och_granser_som_serverdriften():
    deploy = (REPO / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    text = skapa_caddyfil("drivelog.example.se")
    header = re.search(r"header \{(.*?)\n\t\}", deploy, re.S).group(1)
    for rad in _rader(header):
        assert rad in _rader(text), rad
    for rad in ("encode zstd gzip", "max_size 60MB"):
        assert rad in _rader(deploy) and rad in _rader(text)


def test_x_forwarded_for_ersatts():
    text = skapa_caddyfil("drivelog.example.se")
    assert text.count("header_up X-Forwarded-For {remote_host}") == 2


def test_kontrollen_skickas_till_startprogrammet():
    assert DYNDNS_KONTROLLSOKVAG not in skapa_caddyfil("drivelog.example.se")
    text = skapa_caddyfil("drivelog.example.se", kontroll="127.0.0.1:8099")
    assert f"handle {DYNDNS_KONTROLLSOKVAG}" in text
    assert "reverse_proxy 127.0.0.1:8099" in text
    assert "respond" not in text  # inget statiskt kontrollvärde som kan läsas och återanvändas
    # Kontrollen ligger före proxyreglerna.
    assert text.index(DYNDNS_KONTROLLSOKVAG) < text.index("handle /api/v1/*")
    assert text.count("{") == text.count("}")


@pytest.mark.parametrize("adress", ["10.0.0.1:8099", "127.0.0.1", 'a"b', "127.0.0.1:80 {"])
def test_ogiltig_kontrolladress_avvisas(adress):
    with pytest.raises(ValueError):
        skapa_caddyfil("drivelog.example.se", kontroll=adress)


def test_localhost_for_provkorning():
    text = skapa_caddyfil("localhost", lagring="/tmp/caddy")
    assert "\nlocalhost {\n" in text
    assert "skip_install_trust" in text
    assert "root /tmp/caddy" in text
