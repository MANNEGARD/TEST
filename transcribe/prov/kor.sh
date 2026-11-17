#!/usr/bin/env bash
# Kör alla prov av installationsraden och den automatiska uppdateringen (mot låtsaskommandon):
#
#   sudo bash transcribe/prov/kor.sh
#
# Skripten som provas: TRANSCRIBE_PROV_SKRIPT_KATALOG (standard transcribe/ i repot). Provdata
# hamnar i TRANSCRIBE_PROV_ARBETE (standard en ny katalog under /tmp) och tas bort när allt gick bra.
set -uo pipefail
katalog="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skript="${TRANSCRIBE_PROV_SKRIPT_KATALOG:-$(cd "$katalog/.." && pwd)}"
arbete="${TRANSCRIBE_PROV_ARBETE:-$(mktemp -d /tmp/transcribe-prov.XXXXXX)}"
export TRANSCRIBE_PROV_SKRIPT_KATALOG="$skript"
fel=0

echo "== Syntax"
for f in installera.sh uppdatera.sh; do
  if bash -n "$skript/$f"; then echo "  ok    bash -n $f"; else echo "  FEL   bash -n $f"; fel=1; fi
done
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck "$skript/installera.sh" "$skript/uppdatera.sh"; then echo "  ok    shellcheck"; else echo "  FEL   shellcheck"; fel=1; fi
else
  echo "  (shellcheck finns inte här – hoppas över)"
fi

for prov in prov_uppdatera.sh prov_installera.sh; do
  echo
  echo "== $prov"
  TRANSCRIBE_PROV_ARBETE="$arbete/${prov%.sh}" bash "$katalog/$prov" || fel=1
done

echo
if [[ "$fel" == 0 ]]; then
  echo "Alla prov godkända."
  [[ -n "${TRANSCRIBE_PROV_ARBETE:-}" ]] || rm -rf "$arbete"
else
  echo "Något prov är underkänt (provdata: $arbete)." >&2
fi
exit "$fel"
