#!/usr/bin/env bash
# DriveLog på en egen server (t.ex. en hyrd VPS) – allt i EN container, med automatisk HTTPS.
#
# Klistra in den här raden i serverns terminal (byt domänen mot din egen):
#
#   curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/deploy/server.sh | sudo bash -s -- drivelog.smgd.se
#
# Skriptet
#   1. installerar Docker om det saknas,
#   2. kontrollerar att domänen pekar på den här servern och att port 80/443 är lediga,
#   3. frågar (första gången) efter e-post och lösenord för ditt konto,
#   4. hämtar den färdiga avbilden – samma som Home Assistant-appen, provstartad innan den
#      publicerades – och startar den,
#   5. väntar tills https://<domän> svarar med giltigt certifikat,
#   6. tar bort lösenordet ur inställningsfilen igen (kontot finns då i databasen).
#
# Kör samma rad igen för att uppdatera till senaste versionen; allt ditt ligger kvar i
# /opt/drivelog/data. Glömt lösenordet: lägg till --nytt-losenord sist på raden.
#
# Inställningar via miljövariabler (behövs normalt inte):
#   DRIVELOG_ADMIN_EMAIL, DRIVELOG_ADMIN_PASSWORD  konto utan frågor (automatiserad installation)
#   DRIVELOG_VERSION     en viss version i stället för den senaste
#   DRIVELOG_DATA        datakatalog (standard /opt/drivelog/data)
#   DRIVELOG_TZ          tidszon (standard Europe/Stockholm)
#   DRIVELOG_SKIP_DNS_CHECK=1  hoppa över kontrollen att domänen pekar hit
set -euo pipefail

REPO_RAW="${DRIVELOG_REPO_RAW:-https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD}"
IMAGE="${DRIVELOG_IMAGE:-ghcr.io/smgd-mp/drivelog}"
VERSION="${DRIVELOG_VERSION:-}"
DATA="${DRIVELOG_DATA:-/opt/drivelog/data}"
NAME="${DRIVELOG_CONTAINER:-drivelog}"
PULL="${DRIVELOG_PULL:-1}"
TZ_NAMN="${DRIVELOG_TZ:-Europe/Stockholm}"
VANTA_SEKUNDER="${DRIVELOG_WAIT:-300}"

rubrik() { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
varna() { printf '   ! %s\n' "$*" >&2; }
die() {
  printf '\n✗ %s\n' "$1" >&2
  shift || true
  for rad in "$@"; do printf '  %s\n' "$rad" >&2; done
  exit 1
}

DOMAN=""
NYTT_LOSENORD=0
for arg in "$@"; do
  case "$arg" in
    --nytt-losenord) NYTT_LOSENORD=1 ;;
    -*) die "Okänt val: $arg" ;;
    *) [[ -z "$DOMAN" ]] || die "Ange bara en domän."; DOMAN="$arg" ;;
  esac
done
[[ -n "$DOMAN" ]] || die "Ange domänen sist på raden, t.ex. drivelog.smgd.se."
DOMAN="${DOMAN,,}"
[[ "$DOMAN" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$ ]] \
  || die "'$DOMAN' ser inte ut som ett domännamn (skriv det utan https:// och utan snedstreck)."
[[ "$(id -u)" == 0 ]] || die "Kör raden med sudo (skriptet behöver vara root för att installera och starta Docker)."
LOKAL=0
[[ "$DOMAN" == localhost ]] && LOKAL=1

# Frågor ställs i terminalen även när skriptet kommer genom ett rör (curl … | bash).
fraga() { # variabelnamn, ledtext, [dold]
  { : </dev/tty; } 2>/dev/null || die "Ingen terminal att fråga i." \
    "Ange DRIVELOG_ADMIN_EMAIL och DRIVELOG_ADMIN_PASSWORD i miljön, eller kör raden i en vanlig terminal."
  local svar
  if [[ "${3:-}" == dold ]]; then
    IFS= read -r -s -p "   $2" svar </dev/tty
    printf '\n' >/dev/tty
  else
    IFS= read -r -p "   $2" svar </dev/tty
  fi
  printf -v "$1" '%s' "$svar"
}

# --- 1. Docker -------------------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  rubrik "Installerar Docker (tar någon minut)"
  if ! command -v curl >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq curl ca-certificates
  fi
  curl -fsSL https://get.docker.com | sh
fi
if ! docker info >/dev/null 2>&1; then
  systemctl enable --now docker >/dev/null 2>&1 || true
  sleep 3
  docker info >/dev/null 2>&1 || die "Docker är installerat men svarar inte." "Starta om servern och kör raden igen."
fi

# --- 2. Version ------------------------------------------------------------------------------------
if [[ -z "$VERSION" ]]; then
  # Den version som erbjuds i repot är alltid färdigbyggd och publicerad (se .github/workflows/addon.yml).
  VERSION="$(curl -fsSL --max-time 20 "$REPO_RAW/drivelog/config.yaml" 2>/dev/null \
    | sed -n 's/^version: "\([0-9][0-9.]*\)"[[:space:]]*$/\1/p' | head -n 1 || true)"
  if [[ -z "$VERSION" ]]; then
    varna "Kunde inte läsa aktuell version – använder den senast publicerade."
    VERSION=latest
  fi
fi
BILD="$IMAGE:$VERSION"

# --- 3. Pekar domänen hit? Är portarna lediga? ---------------------------------------------------------
ar_privat() {
  case "$1" in
    10.* | 192.168.* | 127.* | 169.254.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].*) return 0 ;;
    100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].*) return 0 ;;
    *) return 1 ;;
  esac
}
if [[ "$LOKAL" == 0 && "${DRIVELOG_SKIP_DNS_CHECK:-0}" != 1 ]]; then
  rubrik "Kontrollerar att $DOMAN pekar på den här servern"
  mina4="$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]}' || true)"
  mina6="$(ip -6 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]}' || true)"
  publika4=""
  for a in $mina4; do ar_privat "$a" || publika4+="${publika4:+ }$a"; done
  dns4="$(getent ahostsv4 "$DOMAN" 2>/dev/null | awk '{print $1}' | sort -u || true)"
  # getent ahostsv6 svarar med ::ffff:a.b.c.d när domänen bara har en A-post – det är ingen AAAA-post.
  dns6="$(getent ahostsv6 "$DOMAN" 2>/dev/null | awk '{print $1}' | grep -vi '^::ffff:' | sort -u || true)"

  [[ -n "$dns4" ]] || die "$DOMAN har ingen A-post (IPv4-adress)." \
    "Lägg in serverns adress${publika4:+ ($publika4)} som A-post hos den som sköter domänens DNS (för smgd.se: STRATO)," \
    "vänta några minuter och kör raden igen."
  if [[ -n "$publika4" ]]; then
    traff=0
    for a in $dns4; do [[ " $publika4 " == *" $a "* ]] && traff=1; done
    [[ "$traff" == 1 ]] || die "$DOMAN pekar på $(echo $dns4), inte på den här servern ($publika4)." \
      "Ändra A-posten hos den som sköter domänens DNS (för smgd.se: STRATO) till serverns adress," \
      "vänta några minuter och kör raden igen. Utan det kan certifikatet inte hämtas."
    info "A-posten stämmer: $(echo $dns4)"
  else
    varna "Servern har ingen publik adress på sitt eget nätverkskort, så kontrollen kan inte göras här."
    varna "$DOMAN pekar på $(echo $dns4) – det ska vara serverns publika adress."
  fi
  if [[ -n "$dns6" ]]; then
    traff=0
    for a in $dns6; do [[ $'\n'"$mina6"$'\n' == *$'\n'"$a"$'\n'* ]] && traff=1; done
    [[ "$traff" == 1 ]] || die "$DOMAN har en AAAA-post (IPv6) som inte leder hit: $(echo $dns6)." \
      "Certifikatutfärdaren kontrollerar helst över IPv6 och hamnar då på fel server." \
      "Ta bort AAAA-posten (eller peka den på serverns IPv6-adress) och kör raden igen."
  fi
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  upptagna="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E '[:.](80|443)$' | sort -u | tr '\n' ' ' || true)"
  [[ -z "$upptagna" ]] || die "Port 80 eller 443 används redan av något annat på servern ($upptagna)." \
    "Stäng av den webbservern (t.ex. apache2 eller nginx) och kör raden igen."
fi

# --- 4. Hämta avbilden -----------------------------------------------------------------------------
rubrik "Hämtar DriveLog $VERSION"
if [[ "$PULL" == 1 ]]; then
  docker pull -q "$BILD" >/dev/null || die "Kunde inte hämta $BILD." "Kontrollera att servern når internet och kör raden igen."
else
  docker image inspect "$BILD" >/dev/null 2>&1 || die "Avbilden $BILD finns inte lokalt."
fi

# --- 5. Konto och inställningar ----------------------------------------------------------------------
mkdir -p "$DATA"
chmod 711 "$DATA"
# Startprogrammet sparar ett märke när kontots uppgifter har tillämpats. Saknas det finns inget
# konto än – även om en tidigare, misslyckad start redan har skapat databasfilen.
NY_INSTALLATION=0
[[ -e "$DATA/hemligheter/admin_losenord_marke" ]] || NY_INSTALLATION=1

EPOST="${DRIVELOG_ADMIN_EMAIL:-}"
LOSENORD="${DRIVELOG_ADMIN_PASSWORD:-}"
if [[ "$NY_INSTALLATION" == 1 || "$NYTT_LOSENORD" == 1 ]]; then
  if [[ "$NY_INSTALLATION" == 1 ]]; then
    rubrik "Ditt konto i DriveLog"
  else
    rubrik "Nytt lösenord"
    info "Skriv e-postadressen du loggar in med och ett nytt lösenord."
  fi
  while [[ ! "$EPOST" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; do
    [[ -z "$EPOST" ]] || varna "Det ser inte ut som en e-postadress."
    fraga EPOST "E-postadress: "
  done
  while [[ "${#LOSENORD}" -lt 12 ]]; do
    [[ -z "$LOSENORD" ]] || varna "Lösenordet måste vara minst 12 tecken."
    fraga LOSENORD "Lösenord (minst 12 tecken, syns inte när du skriver): " dold
    fraga LOSENORD2 "Samma lösenord en gång till: " dold
    if [[ "$LOSENORD" != "$LOSENORD2" ]]; then
      varna "Lösenorden var inte lika – försök igen."
      LOSENORD=""
    fi
  done
else
  EPOST=""
  LOSENORD=""
fi

# Inställningsfilen skrivs av Python i avbilden (riktig JSON även för konstiga tecken). Värdena
# går in via standard in – aldrig på en kommandorad eller i en miljövariabel.
skriv_installningar() { # e-post ("" = behåll), lösenord ("" = inget)
  printf '%s\n%s\n%s\n' "$DOMAN" "$1" "$2" | docker run --rm -i --entrypoint python3 -v "$DATA:/data" "$BILD" -I -c '
import json, os, sys
doman, epost, losenord = (rad.rstrip("\n") for rad in sys.stdin.readlines()[:3])
sokvag = "/data/options.json"
standard = {"domain": "", "admin_email": "", "admin_password": "", "strato_dyndns": False,
            "strato_dyndns_user": "", "strato_dyndns_password": "", "log_level": "info"}
try:
    with open(sokvag, encoding="utf-8") as fil:
        gammal = json.load(fil)
    if not isinstance(gammal, dict):
        gammal = {}
except (OSError, ValueError):
    gammal = {}
data = {nyckel: gammal.get(nyckel, varde) for nyckel, varde in standard.items()}
data["domain"] = doman
if epost:
    data["admin_email"] = epost
data["admin_password"] = losenord
tmp = sokvag + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fil:
    json.dump(data, fil)
    fil.flush()
    os.fsync(fil.fileno())
os.replace(tmp, sokvag)
'
}
LOSENORD_I_FILEN=0
stada() {
  # Lösenordet ska inte ligga kvar i klartext: efter en lyckad start finns kontot i databasen, och
  # efter en misslyckad frågar skriptet igen nästa gång. Ett tomt fält ändrar ingenting vid start.
  if [[ "$LOSENORD_I_FILEN" == 1 ]]; then
    LOSENORD_I_FILEN=0
    skriv_installningar "" "" || varna "Lösenordet kunde inte tas bort ur $DATA/options.json – kör raden igen."
  fi
}
trap stada EXIT
[[ -z "$LOSENORD" ]] || LOSENORD_I_FILEN=1
skriv_installningar "$EPOST" "$LOSENORD"

# --- 6. Starta -------------------------------------------------------------------------------------
rubrik "Startar DriveLog"
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker stop -t 30 "$NAME" >/dev/null 2>&1 || true
  docker rm "$NAME" >/dev/null
fi
docker run -d --name "$NAME" --restart unless-stopped --init -e TZ="$TZ_NAMN" \
  -v "$DATA:/data" -p 80:80 -p 443:443 -p 443:443/udp "$BILD" >/dev/null

visa_logg() { docker logs --tail "${1:-40}" "$NAME" 2>&1 | sed 's/^/   | /' >&2 || true; }
halsa() { # extra curl-val
  curl -fsS --max-time 8 "$@" --resolve "$DOMAN:443:127.0.0.1" "https://$DOMAN/api/v1/health/ready" >/dev/null 2>&1
}
KURL_VAL=()
[[ "$LOKAL" == 1 ]] && KURL_VAL=(-k) # lokalt certifikat

if [[ "$LOKAL" == 1 ]]; then
  info "Väntar på att DriveLog ska svara …"
else
  info "Väntar på att DriveLog ska svara och att certifikatet ska hämtas (kan ta ett par minuter) …"
fi
klar=0
slut=$((SECONDS + VANTA_SEKUNDER))
while ((SECONDS < slut)); do
  if halsa "${KURL_VAL[@]}"; then
    klar=1
    break
  fi
  logg="$(docker logs --tail 300 "$NAME" 2>&1 || true)"
  if grep -qF -e "Det finns inget konto" -e "Kontot kunde inte skapas" <<<"$logg"; then
    visa_logg 25
    die "Kontot kunde inte skapas – se raderna ovan." "Rätta det som står där och kör raden igen."
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]]; then
    sleep 5 # Docker startar om containern av sig själv; ge den en chans
    if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]]; then
      visa_logg 40
      die "DriveLog startade inte – se raderna ovan."
    fi
  fi
  sleep 3
done

stada

if [[ "$klar" != 1 ]]; then
  visa_logg 40
  if halsa -k; then
    die "DriveLog är igång, men certifikatet för $DOMAN har inte kunnat hämtas än." \
      "Vanliga orsaker: A-posten pekar inte hit, det finns en AAAA-post (IPv6) som pekar fel," \
      "eller så släpper leverantörens brandvägg inte in port 80 och 443 till servern." \
      "DriveLog försöker igen av sig själv. Titta om en stund:  docker logs --tail 50 $NAME"
  fi
  die "DriveLog svarade inte inom $VANTA_SEKUNDER sekunder – se raderna ovan." \
    "Titta i loggen:  docker logs --tail 100 $NAME"
fi

rubrik "Klart"
info "DriveLog $VERSION är igång: https://$DOMAN"
if [[ "$NY_INSTALLATION" == 1 ]]; then
  info "Logga in med $EPOST och lösenordet du valde."
elif [[ "$NYTT_LOSENORD" == 1 ]]; then
  info "Lösenordet för $EPOST är bytt."
fi
info "Uppdatera senare: kör samma rad igen."
info "Allt ditt ligger i $DATA – ta med den katalogen i serverns säkerhetskopia."
info "Loggen: docker logs --tail 50 $NAME"
