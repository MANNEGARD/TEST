#!/usr/bin/env bash
# Installerar eller uppdaterar SMGD DriveLog på en server med Docker.
#
#   deploy/install.sh drivelog.smgd.se
#
# Skriptet
#   1. skapar .env med slumpade hemligheter (befintliga värden skrivs aldrig över),
#   2. kontrollerar minne, DNS (IPv4 och IPv6) och att port 80/443 är lediga,
#   3. bygger avbilderna och startar först bara databasen och API:t,
#   4. skapar administratörskontot INNAN något nås från internet,
#   5. startar webbappen och proxyn och kontrollerar att HTTPS verkligen svarar.
#
# Flaggor:
#   --env-only   skapa/uppdatera bara .env och avsluta (startar ingenting)
#   --no-build   starta utan att bygga om avbilderna
#
# Automatiserad installation (utan terminal): sätt DRIVELOG_ADMIN_EMAIL och
# DRIVELOG_ADMIN_PASSWORD i miljön så skapas kontot utan frågor.
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
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Okänd flagga: $arg" >&2; exit 2 ;;
    *) DOMAIN="$arg" ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33mOBS:\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[31mFel:\033[0m %s\n' "$*" >&2; exit 1; }

LOG_HINT="Se loggarna: docker compose logs --tail 100 api web proxy"

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

if [[ "$BUILD" == 1 && -r /proc/meminfo ]]; then
  mem_mb="$(awk '/^(MemTotal|SwapTotal):/ {s+=$2} END {print int(s/1024)}' /proc/meminfo)"
  if [[ "$mem_mb" -lt 3000 ]]; then
    warn "Servern har bara ${mem_mb} MB minne inklusive växlingsutrymme. Bygget av webbappen kan avbrytas"
    warn "(\"Killed\"). Lägg till växlingsutrymme först och kör skriptet igen:"
    warn "  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
  fi
fi

if [[ "$DOMAIN" != "localhost" ]]; then
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | head -n 1 || true)"
  public="$(curl -4 -fsS -m 8 https://api.ipify.org 2>/dev/null || true)"
  if [[ -z "$resolved" ]]; then
    warn "$DOMAIN har ingen A-post ännu. Lägg in en A-post som pekar på serverns adress${public:+ ($public)}."
    warn "Certifikatet kan hämtas först när DNS-posten finns – Caddy försöker igen av sig självt."
  elif [[ -n "$public" && "$resolved" != "$public" ]]; then
    warn "$DOMAIN pekar på $resolved, men den här servern har adressen $public."
    warn "Ändra A-posten hos din DNS-leverantör. Certifikatet kan hämtas först när den pekar hit."
  else
    note "$DOMAIN pekar på ${resolved} (IPv4)"
  fi
  # En AAAA-post (IPv6) som pekar någon annanstans – t.ex. på DNS-leverantörens parkeringssida –
  # stoppar certifikatet: Let's Encrypt kontrollerar helst över IPv6.
  resolved6="$(getent ahostsv6 "$DOMAIN" 2>/dev/null | awk '{print $1}' | grep -v '^::ffff:' | sort -u | head -n 1 || true)"
  if [[ -n "$resolved6" ]]; then
    public6="$(curl -6 -fsS -m 8 https://api64.ipify.org 2>/dev/null || true)"
    if [[ "$resolved6" != "$public6" ]]; then
      warn "$DOMAIN har även en AAAA-post (IPv6) som pekar på $resolved6 – inte på den här servern."
      warn "Ta bort AAAA-posten hos din DNS-leverantör (eller peka den på serverns IPv6-adress),"
      warn "annars kan certifikatet inte utfärdas."
    fi
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

# --- bygg och starta databasen och API:t ------------------------------------------------------

api_get() { docker compose exec -T api curl -fsS "http://127.0.0.1:8000/api/v1/$1" 2>/dev/null; }

say "3. Bygger och startar databasen och API:t (första gången tar det några minuter)"
docker compose pull --quiet db redis proxy || warn "Kunde inte hämta de senaste bas-avbilderna – fortsätter med de som finns."
if [[ "$BUILD" == 1 ]]; then
  # En i taget: två samtidiga byggen kräver mer minne än en liten server har.
  docker compose build --pull api || die "Bygget av backend misslyckades."
  docker compose build --pull web || die "Bygget av webbappen misslyckades (minnesbrist? se kontrollen ovan)."
fi
# Proxyn startas inte ännu: ingenting ska gå att nå från internet förrän det finns ett konto.
docker compose up -d db redis api || die "Databasen eller API:t startade inte. $LOG_HINT"
ok=0
for _ in $(seq 1 90); do
  if api_get health/ready >/dev/null; then ok=1; break; fi
  sleep 2
done
[[ "$ok" == 1 ]] || die "API:t svarar inte. $LOG_HINT"
note "databasen och API:t är igång"

# --- administratörskonto ----------------------------------------------------------------------

say "4. Administratörskonto"
if api_get auth/setup-status | grep -q '"needs_setup": *true'; then
  if [[ -n "${DRIVELOG_ADMIN_EMAIL:-}" && -n "${DRIVELOG_ADMIN_PASSWORD:-}" ]]; then
    docker compose exec -T -e "DRIVELOG_ADMIN_PASSWORD=$DRIVELOG_ADMIN_PASSWORD" api \
      python -m app.cli create-admin "$DRIVELOG_ADMIN_EMAIL" || die "Kontot kunde inte skapas."
  elif [[ -t 0 ]]; then
    note "Inget konto finns ännu. Kontot skapas nu, innan systemet öppnas mot internet."
    created=0
    for _ in 1 2 3; do
      read -rp "  E-postadress för administratören: " admin_email
      [[ -n "$admin_email" ]] || continue
      note "Välj ett lösenord på minst 10 tecken (det syns inte när du skriver)."
      if docker compose exec api python -m app.cli create-admin "$admin_email"; then
        created=1
        break
      fi
    done
    [[ "$created" == 1 ]] || die "Kontot skapades inte. Kör skriptet igen."
  elif [[ "$DOMAIN" == "localhost" ]]; then
    note "inget konto skapat (lokal testinstallation) – skapa det i webbläsaren"
  else
    die "Inget administratörskonto finns, och skriptet körs utan terminal. Kör det i en terminal, eller sätt DRIVELOG_ADMIN_EMAIL och DRIVELOG_ADMIN_PASSWORD. Proxyn har inte startats, så ingenting är öppet mot internet."
  fi
else
  note "ett konto finns redan"
fi

# --- webbapp och proxy ------------------------------------------------------------------------

say "5. Startar webbappen och proxyn"
docker compose up -d || die "Alla tjänster startade inte. $LOG_HINT"
ok=0
for _ in $(seq 1 60); do
  if docker compose exec -T web wget -qO- http://127.0.0.1:3000/api/healthz >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
docker compose ps
[[ "$ok" == 1 ]] || die "Webbappen svarar inte. $LOG_HINT"

# Hela vägen genom proxyn: certifikatet ska vara utfärdat och giltigt för domänen.
https_ok=0
if [[ "$DOMAIN" == "localhost" ]]; then
  check=(curl -fsS -k -m 10 "https://localhost/api/v1/health")
else
  check=(curl -fsS -m 10 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/v1/health")
fi
for _ in $(seq 1 45); do
  if "${check[@]}" >/dev/null 2>&1; then https_ok=1; break; fi
  sleep 2
done

if [[ "$https_ok" != 1 ]]; then
  say "Tjänsterna är igång, men HTTPS svarar inte ännu."
  note "Vanligaste orsaken: DNS-posterna för $DOMAIN pekar inte på den här servern (se varningarna"
  note "under punkt 2), eller så släpper en brandvägg inte in port 80 och 443."
  note "Caddy försöker hämta certifikatet igen av sig självt när DNS stämmer. Senaste försöken:"
  docker compose logs --tail 40 proxy 2>/dev/null | grep -iE 'error|challenge|obtain|certificate' | tail -n 8 || true
  exit 3
fi

say "Klart!"
note "Öppna https://$DOMAIN och logga in."
note "Säkerhetskopiera varje natt, t.ex. med cron:  0 3 * * *  $ROOT/deploy/backup.sh"
note "Uppgradera senare med: git pull && deploy/install.sh"
