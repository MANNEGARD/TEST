"""Skapar GitHub-utgåvan "vX.Y.Z" för versionen i drivelog/VERSION (för HACS).

HACS visar taggnamnet på senaste utgåvan som integrationens version och erbjuder en
uppdatering först när en ny utgåva finns (hacs.json: ``hide_default_branch``). Utan utgåvor
visar HACS commit-hashar och erbjuder en "uppdatering" vid varje incheckning.

Arbetsflödet (.github/workflows/addon.yml, jobbet ``utgava``) kör skriptet efter att avbilden är
publicerad och versionen erbjuden i Home Assistant:

    python3 .github/scripts/utgava.py --anteckningar > notes.md   # utgåvans text, utan nätverk
    python3 .github/scripts/utgava.py --skapa notes.md            # skapar utgåvan med gh

Texten är versionens avsnitt ur drivelog/CHANGELOG.md. Har custom_components/ inte ändrats sedan
förra utgåvans tagg står det rakt ut att uppdateringen i Home Assistant kan vänta (den kräver en
omstart av Home Assistant). En befintlig tagg eller utgåva skrivs aldrig över.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERSIONSFIL = REPO / "drivelog" / "VERSION"
CHANGELOG = REPO / "drivelog" / "CHANGELOG.md"
INTEGRATION = "custom_components/"
OFORANDRAD = "Ingen ändring i Home Assistant-integrationen – uppdateringen kan vänta."

_VERSION = re.compile(r"\d+\.\d+\.\d+")
_TAGG = re.compile(r"v(\d+\.\d+\.\d+)")


class Fel(Exception):
    """Något stämmer inte; ingen utgåva har skapats."""


def version() -> str:
    text = VERSIONSFIL.read_text(encoding="utf-8").strip()
    if not _VERSION.fullmatch(text):
        raise Fel(f"drivelog/VERSION innehåller '{text}', ingen version på formen 1.2.3")
    return text


def som_tal(v: str) -> tuple[int, ...]:
    return tuple(int(d) for d in v.split("."))


def avsnitt(v: str, text: str | None = None) -> str:
    """Versionens avsnitt ur CHANGELOG.md (utan rubriken), eller tom text."""
    if text is None:
        text = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
    rader: list[str] = []
    inne = False
    for rad in text.splitlines():
        if rad.startswith("## "):
            if inne:
                break
            inne = rad[3:].strip() == v
            continue
        if inne:
            rader.append(rad)
    return "\n".join(rader).strip()


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def forra_taggen(v: str) -> str | None:
    """Den högsta taggen vX.Y.Z som är lägre än ``v``, eller None."""
    resultat = _git("tag", "--list", "v*")
    if resultat.returncode != 0:
        return None
    lagre = [
        (som_tal(m.group(1)), tagg)
        for tagg in resultat.stdout.split()
        if (m := _TAGG.fullmatch(tagg)) and som_tal(m.group(1)) < som_tal(v)
    ]
    return max(lagre)[1] if lagre else None


def integrationen_oforandrad(v: str) -> bool:
    """Sant bara när förra utgåvans tagg finns och custom_components/ är oförändrad sedan dess."""
    tagg = forra_taggen(v)
    if tagg is None:
        return False
    return _git("diff", "--quiet", tagg, "HEAD", "--", INTEGRATION).returncode == 0


def anteckningar(v: str) -> str:
    delar = [avsnitt(v) or f"DriveLog {v}."]
    if integrationen_oforandrad(v):
        delar.append(OFORANDRAD)
    return "\n\n".join(delar) + "\n"


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], cwd=REPO, capture_output=True, text=True, check=False)


def finns(v: str) -> bool:
    """Finns taggen eller utgåvan redan? Ett fel som inte är "finns inte" avbryter."""
    tagg = f"v{v}"
    if _git("rev-parse", "--quiet", "--verify", f"refs/tags/{tagg}").returncode == 0:
        return True
    if _git("ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tagg}").returncode == 0:
        return True
    resultat = _gh("release", "view", tagg, "--json", "tagName")
    if resultat.returncode == 0:
        return True
    if "not found" in (resultat.stderr + resultat.stdout).lower():
        return False
    raise Fel(f"Kunde inte avgöra om utgåvan {tagg} finns: {resultat.stderr.strip() or resultat.returncode}")


def skapa(v: str, anteckningsfil: str) -> str:
    if finns(v):
        return f"Utgåvan v{v} finns redan – ingenting ändras."
    mal = os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD").stdout.strip()
    if not mal:
        raise Fel("Hittar inte vilken commit utgåvan ska peka på.")
    resultat = _gh(
        "release", "create", f"v{v}", "--target", mal, "--title", f"DriveLog {v}", "--notes-file", anteckningsfil
    )
    if resultat.returncode != 0:
        raise Fel(f"gh release create misslyckades: {resultat.stderr.strip() or resultat.returncode}")
    return f"Utgåvan v{v} är skapad."


def main(argv: list[str]) -> int:
    try:
        v = version()
        if argv == ["--anteckningar"]:
            sys.stdout.write(anteckningar(v))
            return 0
        if len(argv) == 2 and argv[0] == "--skapa":
            print(skapa(v, argv[1]))
            return 0
        print(__doc__, file=sys.stderr)
        return 2
    except Fel as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
