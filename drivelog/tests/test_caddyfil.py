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


def test_dyndns_kontroll_bara_nar_paslagen():
    assert DYNDNS_KONTROLLSOKVAG not in skapa_caddyfil("drivelog.example.se")
    token = "A" * 32
    text = skapa_caddyfil("drivelog.example.se", dyndns_token=token)
    assert f"handle {DYNDNS_KONTROLLSOKVAG}" in text
    assert f'respond "{token}" 200' in text
    # Kontrollen ligger före proxyreglerna.
    assert text.index(DYNDNS_KONTROLLSOKVAG) < text.index("handle /api/v1/*")


@pytest.mark.parametrize("token", ["kort", 'a"b' * 10, "a b" * 10])
def test_ogiltigt_token_avvisas(token):
    with pytest.raises(ValueError):
        skapa_caddyfil("drivelog.example.se", dyndns_token=token)


def test_localhost_for_provkorning():
    text = skapa_caddyfil("localhost", lagring="/tmp/caddy")
    assert "\nlocalhost {\n" in text
    assert "skip_install_trust" in text
    assert "root /tmp/caddy" in text
