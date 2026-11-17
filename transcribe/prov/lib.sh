# shellcheck shell=bash disable=SC2001,SC2034
# Gemensamt för proven i transcribe/prov/ (källas av prov_*.sh, körs inte själv).
#
# Proven kör installera.sh och uppdatera.sh mot låtsaskommandon (latsas/: docker, systemctl, curl,
# sleep, df, cp, sync) som ligger först i PATH. Låtsas-docker håller sitt tillstånd i filer, så att varje scenario
# kan ställas in och anropen läsas efteråt. Inget riktigt Docker eller systemd behövs, och inget
# utanför provets arbetskatalog ändras.
#
# Miljövariabler:
#   TRANSCRIBE_PROV_SKRIPT_KATALOG  katalogen med installera.sh, uppdatera.sh och docker-compose.yml
#                                   som provas (standard: transcribe/ i repot)
#   TRANSCRIBE_PROV_ARBETE          arbetskatalog för provdata (standard: en ny katalog under /tmp)
#   TRANSCRIBE_PROV_ENDAST          kör bara scenarier vars namn innehåller den här texten
# Proven kör som root (som skripten på servern), t.ex.: sudo bash transcribe/prov/kor.sh

PROV_KATALOG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_TRANSCRIBE="$(cd "$PROV_KATALOG/.." && pwd)"
SKRIPT_KATALOG="${TRANSCRIBE_PROV_SKRIPT_KATALOG:-$REPO_TRANSCRIBE}"
ARBETE="${TRANSCRIBE_PROV_ARBETE:-}"
if [[ -z "$ARBETE" ]]; then ARBETE="$(mktemp -d /tmp/transcribe-prov.XXXXXX)"; fi
mkdir -p "$ARBETE"
LATSAS="$PROV_KATALOG/latsas"
IMG=ghcr.io/smgd-mp/transcribe
ANTAL_OK=0
ANTAL_FEL=0
UNDERKANDA=()

[[ $EUID -eq 0 ]] || { echo "Proven körs som root (sudo), som skripten på servern." >&2; exit 1; }
for f in installera.sh uppdatera.sh docker-compose.yml; do
  [[ -f "$SKRIPT_KATALOG/$f" ]] || { echo "Hittar inte $SKRIPT_KATALOG/$f (TRANSCRIBE_PROV_SKRIPT_KATALOG)." >&2; exit 1; }
done

# --- Uppställning ------------------------------------------------------------------------------------
# En ny, tom värld för ett scenario: tillstånd för låtsaskommandona, installationskatalog,
# systemd-katalog och låsfil. Variablerna exporteras till skripten som provas.
ny_varld() { # namn
  VARLD="$ARBETE/$1"
  rm -rf "$VARLD"
  mkdir -p "$VARLD/till/volym" "$VARLD/till/fel" "$VARLD/till/systemd" "$VARLD/till/bilder" "$VARLD/till/container" \
    "$VARLD/systemd" "$VARLD/las"
  export PROV_TILL="$VARLD/till"
  export TRANSCRIBE_KATALOG="$VARLD/opt/transcribe"
  export TRANSCRIBE_SYSTEMD_DIR="$VARLD/systemd"
  export TRANSCRIBE_LAS="$VARLD/las/transcribe-uppdatering.lock"
  export TRANSCRIBE_VANTA=10 TRANSCRIBE_STABIL=10
  unset TRANSCRIBE_IMAGE TRANSCRIBE_PULL TRANSCRIBE_KOPIOR TRANSCRIBE_CONTAINER TRANSCRIBE_VERSION
  KAT="$TRANSCRIBE_KATALOG"
  T="$PROV_TILL"
  VOL="$T/volym"
  touch "$T/anrop.log" "$T/taggar" "$T/fjarr"
}

bild_id() { printf 'sha256:%s' "$(printf '%s' "$1" | sha256sum | cut -c1-64)"; }

# En avbild. etikett "-" = ingen etikett (som 1.0.0). beteende: frisk|trasig|hanger|sjuk|utan_kontroll.
avbild() { # namn etikett [beteende] [pkg-version]
  local id d
  id="$(bild_id "$1")"
  d="$T/bilder/${id#sha256:}"
  mkdir -p "$d"
  if [[ "$2" != - ]]; then printf '%s' "$2" >"$d/etikett"; fi
  printf '%s' "${3:-frisk}" >"$d/beteende"
  printf '%s' "${4:-$2}" >"$d/pkg"
  printf '%s' "$id"
}
# Taggar: lokal (redan hämtad) eller i registret (hämtas med docker pull).
lokal() { grep -v "^$1 " "$T/taggar" >"$T/taggar.ny" || true; printf '%s %s\n' "$1" "$2" >>"$T/taggar.ny"; mv "$T/taggar.ny" "$T/taggar"; }
fjarr() { grep -v "^$1 " "$T/fjarr" >"$T/fjarr.ny" || true; printf '%s %s\n' "$1" "$2" >>"$T/fjarr.ny"; mv "$T/fjarr.ny" "$T/fjarr"; }
# Det som erbjuds som latest i registret.
erbjud() { # version [beteende]
  local id
  id="$(avbild "v$1${2:+-$2}" "$1" "${2:-frisk}")"
  fjarr "$IMG:latest" "$id"
}

# En installation som kör: compose-filen och uppdatera.sh från skriptkatalogen, en .env, en
# container igång och en datakatalog med databas och nyckel.
installation() { # version (etikett; "1.0.0-utan" = 1.0.0 utan etikett och utan kommandot upptagen)
  local id
  mkdir -p "$KAT"
  chmod 700 "$KAT"
  cp "$SKRIPT_KATALOG/docker-compose.yml" "$KAT/docker-compose.yml"
  install -m 700 "$SKRIPT_KATALOG/uppdatera.sh" "$KAT/uppdatera.sh"
  printf 'TRANSCRIBE_PUBLIC_URL=https://transcribe.example.com\nTRANSCRIBE_DOMAIN=transcribe.example.com\nDEEPGRAM_API_KEY=hemlig-provnyckel\nTRANSCRIBE_VERSION=%s\n' "${1%-utan}" >"$KAT/.env"
  chmod 600 "$KAT/.env"
  if [[ "$1" == *-utan ]]; then
    id="$(avbild "v${1}" - frisk "${1%-utan}")"
    touch "$T/bilder/${id#sha256:}/utan_upptagen"
  else
    id="$(avbild "v$1" "$1")"
  fi
  lokal "$IMG:latest" "$id"
  fjarr "$IMG:latest" "$id"
  lokal "$IMG:${1%-utan}" "$id"
  printf 'databas från %s\n' "${1%-utan}" >"$VOL/transcribe.db"
  printf 'wal\n' >"$VOL/transcribe.db-wal"
  printf 'nyckel\n' >"$VOL/secret.key"
  mkdir -p "$VOL/audio"
  echo ljud >"$VOL/audio/1.m4a"
  mkdir -p "$T/container/transcribe"
  printf '%s' "$id" >"$T/container/transcribe/bild"
  echo running >"$T/container/transcribe/status"
  echo 0 >"$T/container/transcribe/omstarter"
  echo 5 >"$T/container/transcribe/kontroller"
  echo 1 >"$T/container/transcribe/starter"
  touch "$T/natverk-smgd"
}

# --- Körning -----------------------------------------------------------------------------------------
UT=""
KOD=0
kor_med_path() { # kommando … (utskriften i $UT, koden i $KOD)
  KOD=0
  UT="$(env PATH="$LATSAS:$PATH" "$@" 2>&1 </dev/null)" || KOD=$?
  printf '%s\n' "$UT" >>"$VARLD/utskrift.log"
}
uppdatera() { kor_med_path bash "$SKRIPT_KATALOG/uppdatera.sh" "$@"; }

# Startar uppdatera.sh i bakgrunden i en egen processgrupp (som en systemd-tjänst) och väntar
# tills låtsas-docker når kroken. Sedan kan provet avbryta (KILL = strömavbrott, TERM = systemd
# stoppar tjänsten) eller släppa den.
BAKGRUND_PID=""
starta_i_bakgrunden() { # krok-n krok-mönster [argument …]
  local n="$1" m="$2"
  shift 2
  rm -f "$T/krok_nadd" "$T/krok_raknare" "$T/slapp"
  printf '%s %s\n' "$n" "$m" >"$T/krok"
  env PATH="$LATSAS:$PATH" setsid bash "$SKRIPT_KATALOG/uppdatera.sh" "$@" >"$VARLD/bakgrund.log" 2>&1 </dev/null &
  BAKGRUND_PID=$!
  local i
  for ((i = 0; i < 400; i++)); do
    [[ -e "$T/krok_nadd" ]] && return 0
    /bin/sleep 0.05
  done
  echo "    kroken '$m' nåddes aldrig" >&2
  cat "$VARLD/bakgrund.log" >&2
  return 1
}
avbryt() { # KILL|TERM
  kill -s "$1" -- "-$BAKGRUND_PID" 2>/dev/null || true
  wait "$BAKGRUND_PID" 2>/dev/null || true
  rm -f "$T/krok" "$T/krok_nadd"
}
slapp() {
  touch "$T/slapp"
  KOD=0
  wait "$BAKGRUND_PID" || KOD=$?
  UT="$(cat "$VARLD/bakgrund.log")"
  rm -f "$T/krok" "$T/krok_nadd"
}

# --- Kontroller --------------------------------------------------------------------------------------
# Varje kontroll skriver vad som väntades när den inte stämmer; scenariot avbryts då (set -e).
# OBS: "! kommando" avbryter aldrig under set -e – skriv "inte kommando".
inte() { if "$@"; then echo "    borde inte stämma: $*"; return 1; fi; }
kod_ar() { [[ "$KOD" == "$1" ]] || { echo "    väntade kod $1, fick $KOD. Utskrift:"; sed 's/^/      | /' <<<"$UT"; return 1; }; }
ut_har() { grep -qF -- "$1" <<<"$UT" || { echo "    utskriften saknar: $1"; sed 's/^/      | /' <<<"$UT"; return 1; }; }
ut_saknar() { ! grep -qF -- "$1" <<<"$UT" || { echo "    utskriften borde inte ha: $1"; return 1; }; }
logg_har() { grep -qF -- "$1" "$KAT/uppdatering.log" 2>/dev/null || { echo "    loggen saknar: $1"; return 1; }; }
lage_har() { grep -qxF -- "$1" "$KAT/lage.txt" 2>/dev/null || { echo "    lage.txt saknar raden: $1"; cat "$KAT/lage.txt" 2>/dev/null | sed 's/^/      | /'; return 1; }; }
lage_saknar() { ! grep -qE -- "^$1=" "$KAT/lage.txt" 2>/dev/null || { echo "    lage.txt borde inte ha $1"; return 1; }; }
env_har() { grep -qxF -- "$1" "$KAT/.env" || { echo "    .env saknar raden: $1"; return 1; }; }
anrop_har() { grep -qF -- "$1" "$T/anrop.log" || { echo "    inget anrop: $1"; return 1; }; }
anrop_saknar() { ! grep -qF -- "$1" "$T/anrop.log" || { echo "    oväntat anrop: $1"; grep -F -- "$1" "$T/anrop.log" | sed 's/^/      | /'; return 1; }; }
inga_okanda_anrop() { ! grep -q '^OKÄNT' "$T/anrop.log" || { echo "    låtsas-docker fick okända anrop:"; grep '^OKÄNT' "$T/anrop.log"; return 1; }; }
kor_version() { cat "$T/bilder/$(sed 's/^sha256://' "$T/container/transcribe/bild")/pkg"; }
version_ar() { [[ "$(kor_version)" == "$1" ]] || { echo "    kör $(kor_version), väntade $1"; return 1; }; }
kor_igang() { [[ "$(cat "$T/container/transcribe/status")" == running ]] || { echo "    containern kör inte"; return 1; }; }
starter() { cat "$T/container/transcribe/starter"; }
ingen_markering() { [[ ! -e "$KAT/pagaende-uppgradering" ]] || { echo "    markeringen finns kvar:"; cat "$KAT/pagaende-uppgradering"; return 1; }; }
kopior() { find "$KAT/kopior" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort; }
rattigheter() { [[ "$(stat -c '%a %U' "$1")" == "$2" ]] || { echo "    $1 har $(stat -c '%a %U' "$1"), väntade $2"; return 1; }; }

# --- Scenarier ---------------------------------------------------------------------------------------
scenario() { # namn funktion
  if [[ -n "${TRANSCRIBE_PROV_ENDAST:-}" && "$1" != *"$TRANSCRIBE_PROV_ENDAST"* ]]; then return 0; fi
  local rc
  # Inte i ett if eller ||-led: där skulle set -e inte gälla i scenariot.
  (set -e; "$2") >"$ARBETE/senaste.txt" 2>&1
  rc=$?
  if [[ "$rc" == 0 ]]; then
    ANTAL_OK=$((ANTAL_OK + 1))
    printf '  ok    %s\n' "$1"
  else
    ANTAL_FEL=$((ANTAL_FEL + 1))
    UNDERKANDA+=("$1")
    printf '  FEL   %s\n' "$1"
    sed 's/^/        /' "$ARBETE/senaste.txt"
  fi
}
summera() { # rubrik
  printf '\n%s: %d godkända, %d underkända (av %d)\n' "$1" "$ANTAL_OK" "$ANTAL_FEL" $((ANTAL_OK + ANTAL_FEL))
  [[ "$ANTAL_FEL" == 0 ]]
}
