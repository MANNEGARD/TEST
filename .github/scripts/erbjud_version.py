"""Höjer `version` i drivelog/config.yaml till versionen i drivelog/VERSION.

Home Assistant läser `version` ur drivelog/config.yaml på repots standardgren och hämtar avbilden
med just den taggen. Skulle config.yaml visa en version vars avbild inte finns än misslyckas
installationen eller uppdateringen ("manifest unknown"). Därför är ordningen:

1. Den som ger ut en ny version ändrar drivelog/VERSION (och CHANGELOG.md).
2. Arbetsflödet bygger, provstartar och publicerar avbilden med den taggen.
3. Först därefter kör arbetsflödet det här skriptet och checkar in den höjda config.yaml.

Skriptet sänker aldrig versionen och ändrar ingenting annat i filen.

    python3 .github/scripts/erbjud_version.py            # skriver "andrad" eller "oforandrad"
    python3 .github/scripts/erbjud_version.py --visa     # skriver bara versionen i drivelog/VERSION
    python3 .github/scripts/erbjud_version.py --avbild   # kod 0 bara om avbilden går att hämta
    python3 .github/scripts/erbjud_version.py --namn     # skriver "image=<avbild>" (för publiceringen)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERSIONSFIL = REPO / "drivelog" / "VERSION"
CONFIG = REPO / "drivelog" / "config.yaml"

_VERSION = re.compile(r"\d+\.\d+\.\d+")
_RAD = re.compile(r'(?m)^version: "(?P<version>[^"\n]*)"[ \t]*$')
_AVBILD = re.compile(r'(?m)^image: "(?P<avbild>ghcr\.io/[a-z0-9._/-]+)"[ \t]*$')
PLATTFORMAR = frozenset({"linux/amd64", "linux/arm64"})
_MANIFESTTYPER = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)


class Fel(Exception):
    """Något stämmer inte; ingenting har ändrats."""


def som_tal(version: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(version):
        raise Fel(f"'{version}' är ingen version på formen 1.2.3")
    return tuple(int(del_) for del_ in version.split("."))


def las_versionsfil(text: str) -> str:
    version = text.strip()
    som_tal(version)
    return version


def erbjuden_version(config_text: str) -> str:
    traffar = _RAD.findall(config_text)
    if len(traffar) != 1:
        raise Fel(f"config.yaml ska ha exakt en rad 'version: \"…\"' (hittade {len(traffar)})")
    som_tal(traffar[0])
    return traffar[0]


def satt_version(config_text: str, version: str) -> str:
    """Returnerar config.yaml med `version` höjd till `version` (oförändrad om den redan stämmer)."""
    nu = erbjuden_version(config_text)
    if som_tal(version) < som_tal(nu):
        raise Fel(f"config.yaml visar {nu}, som är nyare än {version} – en version sänks aldrig")
    return _RAD.sub(f'version: "{version}"', config_text, count=1)


def avbildens_namn(config_text: str) -> str:
    traffar = _AVBILD.findall(config_text)
    if len(traffar) != 1:
        raise Fel('config.yaml ska ha exakt en rad \'image: "ghcr.io/…"\' med gemener')
    return traffar[0]


def publiceringsnamn(config_text: str, agare: str) -> str:
    """Avbildens namn för publiceringen: under ghcr.io/<ägaren>/, med gemener, utan {arch}."""
    avbild = avbildens_namn(config_text)
    agare = agare.lower()
    if not agare or not avbild.startswith(f"ghcr.io/{agare}/"):
        raise Fel(f"image i config.yaml ({avbild}) måste ligga under ghcr.io/{agare}/ och vara gemener")
    if "{arch}" in avbild:
        raise Fel("avbilden publiceras för flera arkitekturer under ett namn")
    return avbild


def plattformar_i_manifest(manifest: dict) -> set[str]:
    """Plattformarna i en flerarkitektursavbild (tom mängd för en avbild med bara en arkitektur)."""
    funna = set()
    for post in manifest.get("manifests") or []:
        plattform = post.get("platform") or {}
        if plattform.get("os") and plattform.get("architecture") and plattform.get("os") != "unknown":
            funna.add(f"{plattform['os']}/{plattform['architecture']}")
    return funna


def hamta_manifest(avbild: str, version: str, *, oppna=urllib.request.urlopen) -> dict:
    """Hämtar avbildens manifest ANONYMT från GHCR – precis som Home Assistant gör.

    Ett privat paket eller en tagg som inte finns ger ``Fel``; då ska versionen inte erbjudas.
    """
    register, _, namn = avbild.partition("/")
    try:
        with oppna(f"https://{register}/token?scope=repository:{namn}:pull&service={register}", timeout=30) as svar:
            pollett = json.load(svar)["token"]
        fraga = urllib.request.Request(
            f"https://{register}/v2/{namn}/manifests/{version}",
            headers={"Authorization": f"Bearer {pollett}", "Accept": _MANIFESTTYPER},
        )
        with oppna(fraga, timeout=30) as svar:
            return json.load(svar)
    except urllib.error.HTTPError as fel:
        orsak = {
            401: "paketet är privat (gör det publikt under Package settings på GitHub)",
            403: "paketet är privat (gör det publikt under Package settings på GitHub)",
            404: "taggen finns inte",
        }.get(fel.code, f"HTTP {fel.code}")
        raise Fel(f"{avbild}:{version} går inte att hämta utan inloggning: {orsak}") from None
    except (OSError, ValueError, KeyError) as fel:
        raise Fel(f"{avbild}:{version} kunde inte kontrolleras ({type(fel).__name__})") from None


def kontrollera_avbild(avbild: str, version: str, *, oppna=urllib.request.urlopen) -> set[str]:
    funna = plattformar_i_manifest(hamta_manifest(avbild, version, oppna=oppna))
    saknas = PLATTFORMAR - funna
    if saknas:
        raise Fel(f"{avbild}:{version} saknar {', '.join(sorted(saknas))} (har {', '.join(sorted(funna)) or 'ingen'})")
    return funna


def main(argv: list[str]) -> int:
    try:
        version = las_versionsfil(VERSIONSFIL.read_text(encoding="utf-8"))
        if argv[1:] == ["--visa"]:
            print(version)
            return 0
        if argv[1:] == ["--namn"]:
            avbild = publiceringsnamn(CONFIG.read_text(encoding="utf-8"), os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
            print(f"image={avbild}")
            return 0
        if argv[1:] == ["--avbild"]:
            avbild = avbildens_namn(CONFIG.read_text(encoding="utf-8"))
            funna = kontrollera_avbild(avbild, version)
            print(f"{avbild}:{version} går att hämta utan inloggning ({', '.join(sorted(funna))}).")
            return 0
        if argv[1:]:
            raise Fel(f"okänt argument: {' '.join(argv[1:])}")
        fore = CONFIG.read_text(encoding="utf-8")
        efter = satt_version(fore, version)
    except Fel as fel:
        print(f"::error::{fel}", file=sys.stderr)
        return 1
    if efter == fore:
        print("oforandrad")
        return 0
    CONFIG.write_text(efter, encoding="utf-8")
    print("andrad")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
