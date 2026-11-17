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


def _block(text, doman):
    """Webbplatsblocket för ``doman`` (från rubriken till den avslutande klammern)."""
    return re.search(rf"\n{re.escape(doman)} \{{\n(.*?)\n\}}\n", text, re.S).group(1)


def test_utan_extra_domaner_ingen_skillnad():
    assert skapa_caddyfil("drivelog.example.se") == skapa_caddyfil("drivelog.example.se", extra=())


def test_extra_domaner_far_egna_block():
    text = skapa_caddyfil(
        "drivelog.example.se",
        extra=[("transcribe.example.se", "transcribe:8080"), ("b.example.se", "annan-tjanst:9000")],
    )
    assert text.count("{") == text.count("}")
    # DriveLogs eget block är oförändrat.
    assert _block(text, "drivelog.example.se") == _block(skapa_caddyfil("drivelog.example.se"), "drivelog.example.se")
    for doman, uppstrom in (("transcribe.example.se", "transcribe:8080"), ("b.example.se", "annan-tjanst:9000")):
        block = _rader(_block(text, doman))
        assert f"reverse_proxy {uppstrom} {{" in block
        assert "header_up X-Forwarded-For {remote_host}" in block
        assert "encode zstd gzip" in block
        assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in block
        assert 'X-Content-Type-Options "nosniff"' in block
        assert 'Referrer-Policy "same-origin"' in block
        assert "-Server" in block
        assert "max_size 50MB" in block
        # Tjänsten bakom behöver mikrofon och skärmdelning och avgör själv om den får ramas in.
        assert not any(r.startswith(("Permissions-Policy", "X-Frame-Options")) for r in block)
        # Ingenting som hindrar WebSockets (Caddy skickar Upgrade vidare av sig själv).
        assert not any("Upgrade" in r or "Connection" in r for r in block)
        # Inga DriveLog-vägar i den extra domänen.
        assert "handle /api/v1/*" not in block and "127.0.0.1:8000" not in " ".join(block)
    assert text.count("header_up X-Forwarded-For {remote_host}") == 4
    # Den extra domänen kommer efter DriveLogs block.
    assert text.index("\ndrivelog.example.se {") < text.index("\ntranscribe.example.se {")


@pytest.mark.parametrize(
    ("doman", "uppstrom"),
    [
        ("transcribe.example.se", "transcribe:8080 {\n\trespond hej\n}"),
        ("transcribe.example.se", "transcribe:8080\n"),
        ("transcribe.example.se", "transcribe"),
        ("transcribe.example.se", "http://transcribe:8080"),
        ("transcribe.example.se", "transcribe/x:8080"),
        ("transcribe.example.se", 'transcribe":8080'),
        ("transcribe.example.se", "transcribe:0"),
        ("transcribe.example.se", "transcribe:99999"),
        ("transcribe.example.se", "Transcribe:8080"),
        ("transcribe.example.se {\n\trespond hej\n}\nx.se", "transcribe:8080"),
        ("transcribe.example.se\n", "transcribe:8080"),
        ("*.example.se", "transcribe:8080"),
        ("transcribe.example.se, drivelog.example.se", "transcribe:8080"),
        ("", "transcribe:8080"),
        ("a..b.se", "transcribe:8080"),
        # DriveLog självt, IP-adresser och värdmaskinen (installningar.ar_forbjuden_vard).
        ("transcribe.example.se", "127.0.0.1:8000"),
        ("transcribe.example.se", "localhost:3000"),
        ("transcribe.example.se", "0.0.0.0:8099"),
        ("transcribe.example.se", "169.254.169.254:80"),
        ("transcribe.example.se", "192.168.1.10:8123"),
        ("transcribe.example.se", "host.docker.internal:80"),
        ("transcribe.example.se", "gateway.docker.internal:80"),
        ("transcribe.example.se", "drivelog:8000"),
    ],
)
def test_ogiltig_extra_doman_avvisas(doman, uppstrom):
    with pytest.raises(ValueError):
        skapa_caddyfil("drivelog.example.se", extra=[(doman, uppstrom)])


def test_kontrolladress_med_radbrytning_avvisas():
    with pytest.raises(ValueError):
        skapa_caddyfil("drivelog.example.se", kontroll="127.0.0.1:8099\n")
