#!/usr/bin/env bash
# Installerar transkriberingen bredvid DriveLog på en egen server – allt i en rad:
#
#   curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/transcribe/installera.sh | sudo bash -s -- drivelog.smgd.se transcribe.smgd.se
#
# Raden gör samma sak som stegen i README.md: DriveLog får den extra domänen, tjänstens filer
# hämtas till /opt/transcribe, de två nycklarna frågas efter (dold inmatning – de skrivs bara i
# /opt/transcribe/.env), tjänsten startas och inbjudningslänken för administratörskontot visas.
# Det går bra att köra raden igen: en .env som redan finns skrivs inte över, och nycklar som
# redan står där frågas inte efter.
set -euo pipefail

KATALOG="${TRANSCRIBE_KATALOG:-/opt/transcribe}"
RAW="${TRANSCRIBE_REPO_RAW:-https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD}"
SERVERSKRIPT=""

fel() { echo "FEL: $*" >&2; exit 1; }

# Sätter raden NAMN=värde i .env utan att värdet syns på någon kommandorad.
satt_rad() {
  local namn="$1" varde="$2" tmp
  tmp="$(mktemp "$KATALOG/.env.XXXXXX")"
  chmod 600 "$tmp"
  NAMN="$namn" VARDE="$varde" awk '
    BEGIN { n = ENVIRON["NAMN"]; v = ENVIRON["VARDE"]; klar = 0 }
    index($0, n "=") == 1 && !klar { print n "=" v; klar = 1; next }
    { print }
    END { if (!klar) print n "=" v }
  ' "$KATALOG/.env" >"$tmp"
  mv "$tmp" "$KATALOG/.env"
}

# Frågar efter en nyckel om den saknas. Inmatningen visas inte och läses från terminalen.
fraga_nyckel() {
  local namn="$1" text="$2" nu="" varde=""
  nu="$(grep -E "^${namn}=" "$KATALOG/.env" | head -n 1 | cut -d= -f2- || true)"
  if [[ -n "$nu" ]]; then
    echo "   $namn finns redan i .env."
    return 0
  fi
  if ! { : </dev/tty; } 2>/dev/null; then
    echo "   $namn saknas och ingen terminal finns att fråga i – lägg in den i $KATALOG/.env efteråt."
    return 0
  fi
  read -rsp "   $text (visas inte; bara Enter hoppar över): " varde </dev/tty || true
  echo
  varde="${varde//[[:space:]]/}"
  if [[ -z "$varde" ]]; then
    echo "   Hoppar över. Tjänsten startar ändå, men den delen fungerar inte förrän nyckeln finns."
    return 0
  fi
  satt_rad "$namn" "$varde"
  echo "   $namn sparad."
}

main() {
  local drivelog_doman="${1:-}" doman="${2:-}"
  [[ -n "$drivelog_doman" && -n "$doman" ]] ||
    fel "ange DriveLogs domän och transkriberingens domän, t.ex.: drivelog.smgd.se transcribe.smgd.se"
  [[ "$doman" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$doman" == *.* ]] || fel "ogiltig domän: $doman"
  [[ "$drivelog_doman" != "$doman" ]] || fel "de två domänerna måste vara olika"
  [[ $EUID -eq 0 ]] || fel "kör raden med sudo"
  command -v docker >/dev/null || fel "Docker saknas – installera DriveLog först (deploy/server.sh)"

  echo "== 1/4 DriveLog tar emot $doman"
  # Hämtas till en fil och körs därifrån: det här skriptet kommer självt via ett rör, och
  # DriveLogs skript ska inte kunna läsa något från det.
  SERVERSKRIPT="$(mktemp)"
  trap 'rm -f "$SERVERSKRIPT"' EXIT
  curl -fsSL -o "$SERVERSKRIPT" "$RAW/deploy/server.sh"
  bash "$SERVERSKRIPT" "$drivelog_doman" --extra-doman "$doman=transcribe:8080" </dev/null

  echo "== 2/4 Tjänstens filer i $KATALOG"
  mkdir -p "$KATALOG"
  curl -fsSL -o "$KATALOG/docker-compose.yml" "$RAW/transcribe/docker-compose.yml"
  if [[ ! -f "$KATALOG/.env" ]]; then
    curl -fsSL -o "$KATALOG/.env" "$RAW/transcribe/.env.example"
  fi
  chmod 600 "$KATALOG/.env"
  satt_rad TRANSCRIBE_PUBLIC_URL "https://$doman"
  satt_rad TRANSCRIBE_DOMAIN "$doman"

  echo "== 3/4 Nycklar (skrivs bara i $KATALOG/.env)"
  fraga_nyckel DEEPGRAM_API_KEY "Deepgram-nyckeln, för live-texten"
  fraga_nyckel WHISPER_API_KEY "Groq-nyckeln, för slutversionen"

  echo "== 4/4 Startar tjänsten"
  docker network create smgd >/dev/null 2>&1 || true
  cd "$KATALOG"
  docker compose pull </dev/null
  docker compose up -d </dev/null

  local lank=""
  for _ in $(seq 1 30); do
    lank="$(docker compose logs transcribe 2>/dev/null | grep -o "https://[^[:space:]]*/#/inbjudan/[^[:space:]]*" | tail -n 1 || true)"
    [[ -z "$lank" ]] || break
    sleep 2
  done

  echo
  echo "Klart. Tjänsten körs och nås på https://$doman (certifikatet kan ta någon minut första gången)."
  if [[ -n "$lank" ]]; then
    echo "Skapa ditt administratörskonto med den här länken (gäller i två dygn):"
    echo "  $lank"
  else
    echo "Ingen inbjudningslänk i loggen – finns kontot redan? Annars:"
    echo "  cd $KATALOG && sudo docker compose exec transcribe node dist/cli.js inbjudan admin"
  fi
}

# Hela skriptet läses in innan något körs (det kommer via ett rör från curl).
main "$@"
