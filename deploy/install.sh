#!/usr/bin/env bash
# Installerar eller uppdaterar SMGD DriveLog på en server med Docker.
#
#   deploy/install.sh drivelog.smgd.se
#
# Skriptet
#   1. skapar .env med slumpade hemligheter (befintliga värden skrivs aldrig över),
#   2. kontrollerar att domänen pekar på den här servern och att port 80/443 är lediga,
#   3. bygger och startar alla tjänster med Docker Compose,
#   4. väntar tills systemet svarar och skriver ut vad du gör härnäst.
#
# Flaggor:
#   --env-only   skapa/uppdatera bara .env och avsluta (startar ingenting)
#   --no-build   starta utan att bygga om avbilderna
#
# Det är ofarligt att köra skriptet flera gånger, t.ex. efter `git pull` vid en uppgradering.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_ONLY=0
BUILD=1
DOMAIN=""
for arg in "$@"; do
  case "$arg" in
    --env-only) ENV_ONLY=1 ;;
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Okänd flagga: $arg" >&2; exit 2 ;;
    *) DOMAIN="$arg" ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33mOBS:\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[31mFel:\033[0m %s\n' "$*" >&2; exit 1; }

# --- .env ---------------------------------------------------------------------------------

get_kv() { # get_kv NYCKEL → värdet i .env (tomt om raden saknas)
  [[ -f .env ]] || return 0
  sed -n "s/^$1=//p" .env | tail -n 1
}

set_kv() { # set_kv NYCKEL VÄRDE – ersätter raden eller lägger till den
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [[ -f .env ]] && grep -q "^$key=" .env; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > "$tmp"
  else
    { [[ -f .env ]] && cat .env; printf '%s=%s\n' "$key" "$value"; } > "$tmp"
  fi
  cat "$tmp" > .env
  rm -f "$tmp"
}

ensure_secret() { # ensure_secret NYCKEL KOMMANDO… – skapar värdet bara om det saknas
  local key="$1"; shift
  if [[ -z "$(get_kv "$key")" ]]; then
    set_kv "$key" "$("$@")"
    note "skapade $key"
  fi
}

fernet_key() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }

command -v openssl >/dev/null || die "openssl saknas (behövs för att skapa hemligheter)."

if [[ -z "$DOMAIN" ]]; then
  DOMAIN="$(get_kv DRIVELOG_DOMAIN)"
fi
[[ -n "$DOMAIN" ]] || die "Ange domänen, t.ex.: deploy/install.sh drivelog.smgd.se"
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "Ogiltigt domännamn: $DOMAIN"

say "1. Inställningar (.env)"
if [[ ! -f .env ]]; then
  ( umask 077; : > .env )
  printf '# SMGD DriveLog – skapad av deploy/install.sh. Innehåller hemligheter: dela aldrig filen.\n' > .env
  note "skapade .env"
fi
chmod 600 .env
set_kv DRIVELOG_DOMAIN "$DOMAIN"
set_kv DRIVELOG_BASE_URL "https://$DOMAIN"
# Hex-tecken: lösenordet ingår i databasadressen och får inte innehålla / + eller @.
ensure_secret POSTGRES_PASSWORD openssl rand -hex 24
ensure_secret DRIVELOG_SECRET_KEY openssl rand -hex 32
ensure_secret DRIVELOG_ENCRYPTION_KEYS fernet_key
[[ -n "$(get_kv DRIVELOG_TIMEZONE)" ]] || set_kv DRIVELOG_TIMEZONE "Europe/Stockholm"
note "domän: $DOMAIN"
note "spara en kopia av DRIVELOG_ENCRYPTION_KEYS utanför servern – utan den kan lagrade"
note "inloggningar till Volvo och Outlook inte läsas efter en återställning."

if [[ "$ENV_ONLY" == 1 ]]; then
  say "Klart: .env är förberedd (inget har startats)."
  exit 0
fi

# --- förutsättningar ------------------------------------------------------------------------

say "2. Kontroller"
command -v docker >/dev/null || die "Docker saknas. Installera Docker Engine först: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 saknas (kommandot 'docker compose')."
docker info >/dev/null 2>&1 || die "Docker svarar inte. Kör skriptet som en användare som får använda Docker (t.ex. med sudo)."
note "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?') och Compose finns"

if [[ "$DOMAIN" != "localhost" ]]; then
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | head -n 1 || true)"
  public="$(curl -fsS -m 8 https://api.ipify.org 2>/dev/null || true)"
  if [[ -z "$resolved" ]]; then
    warn "$DOMAIN har ingen DNS-post ännu. Lägg in en A-post som pekar på serverns adress${public:+ ($public)}."
    warn "Certifikatet kan hämtas först när DNS-posten finns – Caddy försöker igen av sig självt."
  elif [[ -n "$public" && "$resolved" != "$public" ]]; then
    warn "$DOMAIN pekar på $resolved, men den här servern har adressen $public."
    warn "Ändra A-posten hos din DNS-leverantör. Certifikatet kan hämtas först när den pekar hit."
  else
    note "$DOMAIN pekar på ${resolved}"
  fi
fi

if command -v ss >/dev/null; then
  for port in 80 443; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
      if ! docker compose ps --status running proxy 2>/dev/null | grep -q proxy; then
        warn "Port $port används redan av något annat på servern. Stäng av det, annars kan proxyn inte starta."
      fi
    fi
  done
fi

# --- start ----------------------------------------------------------------------------------

say "3. Bygger och startar (första gången tar det några minuter)"
if [[ "$BUILD" == 1 ]]; then
  docker compose up -d --build
else
  docker compose up -d
fi

say "4. Väntar på att systemet ska svara"
ok=0
for _ in $(seq 1 90); do
  if docker compose exec -T api curl -fsS http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1 \
     && docker compose exec -T web wget -qO- http://127.0.0.1:3000/api/healthz >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
docker compose ps
[[ "$ok" == 1 ]] || die "Systemet svarar inte. Se loggarna: docker compose logs --tail 100 api web proxy"

say "Klart!"
note "Öppna https://$DOMAIN och skapa det första kontot (det blir administratör)."
note "Kontrollera certifikatet om sidan inte svarar: docker compose logs proxy"
note "Säkerhetskopiera varje natt, t.ex. med cron:  0 3 * * *  $ROOT/deploy/backup.sh"
note "Uppgradera senare med: git pull && deploy/install.sh"
