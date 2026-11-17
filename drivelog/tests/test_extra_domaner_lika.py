"""deploy/server.sh och startprogrammet ska säga samma sak om samma extra domän (proxy-2).

server.sh skriver --extra-doman till inställningsfilen och startar sedan DriveLog. Godtar skriptet
ett värde som startprogrammet (installningar.tolka) inte använder, startar DriveLog utan den
domänen – eller inte alls, och då står servern nere. Här matas samma lista av bra och dåliga värden
till skriptets egna funktioner (utklippta ur deploy/server.sh och körda med bash) och till tolka.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from drivelog_addon.installningar import OgiltigaInstallningar, tolka

SERVERSKRIPT = Path(__file__).resolve().parents[2] / "deploy" / "server.sh"
HUVUDDOMAN = "drivelog.example.se"

BRA = [
    "transcribe.example.se=transcribe:8080",
    "T.Example.SE=Transcribe:080",
    "a1.b2c=x:1",
    "x-y.example.se=app_2:65535",
    "t.example.se=tjanst.lokal:1",
    "t.example.se=x0x:80",
    "t.example.se=drivelog2:80",
    "t.example.se=x.:1",
    "a.b-1=x:1",
    "xn--bcher-kva.example=t:1",
    "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 58 + ".se=x:1",  # 253 tecken
]
DALIGA = [
    # Domänen (startprogrammet: normalisera_doman)
    "1.2.3.4=x:1",
    "10.0.0.1=x:1",
    "a.123=x:1",
    "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 59 + ".se=x:1",  # 254 tecken
    "localhost=x:1",
    "transcribe=x:1",
    "-a.example.se=x:1",
    "a..example.se=x:1",
    "t.example.se",
    "t.example.se=x",
    "=x:1",
    # Värden: DriveLog självt, IP-adresser och värdmaskinen (proxy-1)
    "t.example.se=127.0.0.1:8000",
    "t.example.se=127.1:8000",
    "t.example.se=localhost:3000",
    "t.example.se=0.0.0.0:8099",
    "t.example.se=169.254.169.254:80",
    "t.example.se=192.168.1.10:8123",
    "t.example.se=2130706433:80",
    "t.example.se=0x7f000001:80",
    "t.example.se=0x7f.1:80",
    "t.example.se=host.docker.internal:2375",
    "t.example.se=gateway.docker.internal:80",
    "t.example.se=ip6-localhost:80",
    "t.example.se=a.localhost:80",
    "t.example.se=drivelog:8000",
    "t.example.se=localhost.:80",
    "t.example.se=drivelog.:8000",
    "t.example.se=host.docker.internal.:80",
    "t.example.se=x.localhost.:1",
    "t.example.se=local-drivelog:8000",
    "t.example.se=local-drivelog.:8000",
    # Värden och porten i övrigt
    "t.example.se=http://x:1",
    "t.example.se=-x:1",
    "t.example.se=" + "x" * 64 + ":1",
    "t.example.se=x/y:1",
    "t.example.se=x:0",
    "t.example.se=x:65536",
    "t.example.se=x:99999",
]


def _skriptets_funktioner() -> str:
    text = SERVERSKRIPT.read_text(encoding="utf-8")
    delar = []
    for namn in ("ar_doman", "ar_extra_doman", "ar_forbjuden_vard"):
        traff = re.search(rf"^{namn}\(\) \{{.*?(?:\}}$|^\}}$)", text, re.MULTILINE | re.DOTALL)
        assert traff, f"{namn} saknas i deploy/server.sh"
        delar.append(traff.group(0))
    return "\n".join(delar)


def _skriptet_godtar(varden: list[str]) -> list[bool]:
    """Samma steg som när server.sh tolkar --extra-doman: gemener, formen, sedan värden."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash saknas")
    program = _skriptets_funktioner() + r"""
while IFS= read -r del; do
  del="${del,,}"
  vard="${del#*=}"
  vard="${vard%:*}"
  if ar_extra_doman "$del" && ! ar_forbjuden_vard "$vard"; then echo ja; else echo nej; fi
done
"""
    ut = subprocess.run(
        [bash, "-c", program], input="\n".join(varden) + "\n", capture_output=True, text=True, check=True,
        timeout=60,
    )
    svar = ut.stdout.split()
    assert len(svar) == len(varden), ut.stderr
    return [s == "ja" for s in svar]


def _startprogrammet_godtar(varde: str) -> bool:
    try:
        inst = tolka({"domain": HUVUDDOMAN, "strato_dyndns": False, "extra_domains": [varde]})
    except OgiltigaInstallningar:
        return False
    return len(inst.extra_domains) == 1


def test_samma_svar_for_samma_varde():
    varden = BRA + DALIGA
    skriptet = _skriptet_godtar(varden)
    olika = [
        (v, s, _startprogrammet_godtar(v))
        for v, s in zip(varden, skriptet, strict=True)
        if s != _startprogrammet_godtar(v)
    ]
    assert olika == [], "server.sh och startprogrammet säger olika (värde, skriptet, startprogrammet)"


def test_listorna_ar_ratt_sorterade():
    assert all(_skriptet_godtar(BRA))
    assert not any(_skriptet_godtar(DALIGA))
    assert all(_startprogrammet_godtar(v) for v in BRA)
    assert not any(_startprogrammet_godtar(v) for v in DALIGA)


def test_forbjudna_namn_ar_samma_lista():
    """Listan över förbjudna namn står på två ställen; de ska vara lika."""
    from drivelog_addon.installningar import FORBJUDNA_VARDAR

    text = SERVERSKRIPT.read_text(encoding="utf-8")
    rad = re.search(r"^ar_forbjuden_vard\(\) \{\n.*?^  case \"\$v\" in\n    (.*?)\) return 0 ;;", text, re.MULTILINE | re.DOTALL)
    assert rad
    assert {namn.strip() for namn in rad.group(1).split("|")} == FORBJUDNA_VARDAR
