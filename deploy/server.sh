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
# Automatisk uppdatering (val sist på raden):
#   --automatisk-uppdatering        installerar som vanligt och slår sedan på automatiken: en
#                                   systemd-timer kör ungefär var 15:e minut en lokal kopia av
#                                   skriptet (/opt/drivelog/server.sh) som installerar en nyare
#                                   version när den erbjuds – med säkerhetskopia och återgång.
#   --utan-automatisk-uppdatering   slår av den (domänen behövs inte).
#   --lage                          visar läget i klartext (domänen behövs inte).
#
# Extra domäner – fler tjänster på samma server (val sist på raden):
#   --extra-doman <domän>=<värd>:<port>
#                                   DriveLogs Caddy hämtar certifikat även för <domän> och skickar
#                                   trafiken vidare till <värd>:<port>, t.ex.
#                                     --extra-doman transcribe.smgd.se=transcribe:8080
#                                   Valet kan upprepas, eller flera anges med komma emellan. Det
#                                   sparas i inställningsfilen och gäller tills det ändras – även
#                                   vid automatisk uppdatering. DriveLog ansluts då till Docker-nätet
#                                   "smgd" (skapas om det saknas); den andra tjänstens container
#                                   måste också vara med i det nätet för att nås på sitt namn.
#   --utan-extra-doman              tar bort alla extra domäner.
#
# Inställningar via miljövariabler (behövs normalt inte):
#   DRIVELOG_ADMIN_EMAIL, DRIVELOG_ADMIN_PASSWORD  konto utan frågor (automatiserad installation)
#   DRIVELOG_VERSION     en viss version i stället för den senaste
#   DRIVELOG_DATA        datakatalog (standard /opt/drivelog/data)
#   DRIVELOG_TZ          tidszon (standard Europe/Stockholm)
#   DRIVELOG_SKIP_DNS_CHECK=1  hoppa över kontrollen att domänen pekar hit
#   DRIVELOG_HEM         katalog för automatikens kopia, läge, logg och säkerhetskopior (/opt/drivelog)
#   DRIVELOG_KOPIOR      antal säkerhetskopior som sparas vid automatisk uppdatering (standard 3)
set -euo pipefail

REPO_RAW="${DRIVELOG_REPO_RAW:-https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD}"
IMAGE="${DRIVELOG_IMAGE:-ghcr.io/smgd-mp/drivelog}"
VERSION="${DRIVELOG_VERSION:-}"
DATA="${DRIVELOG_DATA:-/opt/drivelog/data}"
NAME="${DRIVELOG_CONTAINER:-drivelog}"
PULL="${DRIVELOG_PULL:-1}"
TZ_NAMN="${DRIVELOG_TZ:-Europe/Stockholm}"
VANTA_SEKUNDER="${DRIVELOG_WAIT:-300}"
HEM="${DRIVELOG_HEM:-/opt/drivelog}"
ANTAL_KOPIOR="${DRIVELOG_KOPIOR:-3}"
LAS="${DRIVELOG_LAS:-/run/lock/drivelog-server.lock}"
SYSTEMD_KATALOG="${DRIVELOG_SYSTEMD_DIR:-/etc/systemd/system}"
ENHET=drivelog-uppdatering
KONF="$HEM/automatik.conf"
LAGEFIL="$HEM/lage.txt"
KOPIEKATALOG="$HEM/kopior"
# Docker-nätet som DriveLog ansluts till när det finns extra domäner (--extra-doman): där når
# Caddy de andra tjänsternas containrar på deras namn.
NATVERK=smgd
# Finns medan en automatisk uppgradering pågår. Blir den kvar (avbrott, omstart) gör nästa körning
# – tidsstyrd eller för hand – klart återgången innan något annat händer.
PAGAENDE="$HEM/pagaende-uppgradering"
SKRIPT="${BASH_SOURCE[0]:-}"
# Det här som automatiken sparar och läser tillbaka (inga hemligheter).
KONF_NYCKLAR=(DOMAN IMAGE DATA NAME PULL TZ_NAMN VANTA_SEKUNDER REPO_RAW ANTAL_KOPIOR)

TIDSSTYRD=0 # 1 = körs av systemd-timern: inga frågor, allt loggas, återgång i stället för halvfärdigt
LOGGFIL=""  # sätts när automatiken är på: allt som skrivs ut hamnar också där, med tidsstämpel

tid() { date '+%Y-%m-%d %H:%M:%S'; }
till_loggfil() {
  [[ -n "$LOGGFIL" ]] || return 0
  printf '%s %s\n' "$(tid)" "$*" >>"$LOGGFIL" 2>/dev/null || true
  # Under systemd går utskriften redan till journalen; körs raden för hand skickas den dit här.
  if [[ "$TIDSSTYRD" != 1 ]] && command -v logger >/dev/null 2>&1; then
    logger -t "$ENHET" -- "$*" 2>/dev/null || true
  fi
}
rubrik() { printf '\n== %s\n' "$*"; till_loggfil "== $*"; }
info() { printf '   %s\n' "$*"; till_loggfil "$*"; }
varna() { printf '   ! %s\n' "$*" >&2; till_loggfil "VARNING: $*"; }
die() {
  printf '\n✗ %s\n' "$1" >&2
  till_loggfil "FEL: $1"
  [[ "$TIDSSTYRD" != 1 ]] || satt_lage senaste_fel "$(tid) – $1"
  shift || true
  for rad in "$@"; do
    printf '  %s\n' "$rad" >&2
    till_loggfil "  $rad"
  done
  exit 1
}

# --- Versioner, läge och inställningar för automatiken -------------------------------------------
ar_version() { [[ "$1" =~ ^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$ ]]; }
ar_nyare() { # a b → sant om a är en högre version än b (talvis: 1.10.0 > 1.9.0)
  local -a a b
  local i
  IFS=. read -r -a a <<<"$1"
  IFS=. read -r -a b <<<"$2"
  for i in 0 1 2; do
    ((10#${a[i]} > 10#${b[i]})) && return 0
    ((10#${a[i]} < 10#${b[i]})) && return 1
  done
  return 1
}

# Versionen som erbjuds (drivelog/config.yaml på standardgrenen). Strikt: exakt en rad
# `version: "X.Y.Z"`, och bara den läses – inget annat från nätet tolkas.
las_erbjuden_version() {
  local text rad
  text="$(curl -fsSL --max-time 20 --max-filesize 65536 --proto '=https,file' \
    "$REPO_RAW/drivelog/config.yaml" 2>/dev/null)" || return 1
  rad="$(grep -E '^version:' <<<"$text" || true)"
  [[ -n "$rad" && "$rad" != *$'\n'* ]] || return 1
  [[ "$rad" =~ ^version:\ \"([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})\"[[:space:]]*$ ]] || return 1
  printf '%s' "${BASH_REMATCH[1]}"
}

# Versionen i den container som kör: avbildens etikett io.hass.version (alltid satt vid bygget),
# annars taggen. Tomt om den inte går att avgöra.
korande_version() {
  local v
  v="$(docker inspect -f '{{index .Config.Labels "io.hass.version"}}' "$NAME" 2>/dev/null || true)"
  if ! ar_version "$v"; then
    v="$(docker inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || true)"
    v="${v##*:}"
    ar_version "$v" || v=""
  fi
  printf '%s' "$v"
}

auto_ar_pa() { [[ -f "$SYSTEMD_KATALOG/$ENHET.timer" && -f "$KONF" ]]; }

las_lage() { # nyckel
  [[ -r "$LAGEFIL" ]] || return 0
  sed -n "s/^$1=//p" "$LAGEFIL" | tail -n 1
}
satt_lage() { # nyckel värde ("" tar bort nyckeln)
  [[ -d "$HEM" ]] || return 0
  local varde="${2//$'\n'/ }" tmp="$LAGEFIL.tmp"
  {
    if [[ -r "$LAGEFIL" ]]; then grep -v "^$1=" "$LAGEFIL" || true; fi
    if [[ -n "$varde" ]]; then printf '%s=%s\n' "$1" "$varde"; fi
  } >"$tmp" 2>/dev/null || return 0
  chmod 600 "$tmp" && mv -f "$tmp" "$LAGEFIL" || true
}

borja_logga() { # loggfilen i HEM; roteras vid 1 MB
  [[ -d "$HEM" ]] || return 0
  LOGGFIL="$HEM/uppdatering.log"
  if [[ -f "$LOGGFIL" ]] && (($(stat -c %s "$LOGGFIL" 2>/dev/null || echo 0) > 1048576)); then
    mv -f "$LOGGFIL" "$LOGGFIL.1" || true
  fi
  touch "$LOGGFIL" && chmod 600 "$LOGGFIL" || LOGGFIL=""
}

# <domän>=<värd>:<port> för --extra-doman. Samma regler som startprogrammet (installningar.py):
# värden skrivs in i Caddyfilen, så bara a–z, 0–9, punkt, bindestreck och understreck.
ar_extra_doman() {
  [[ "$1" =~ ^([^=]+)=([a-z0-9][a-z0-9_.-]{0,62}):([0-9]{1,5})$ ]] || return 1
  local doman="${BASH_REMATCH[1]}" vard="${BASH_REMATCH[2]}" port="${BASH_REMATCH[3]}"
  ar_doman "$doman" && [[ "$doman" == *.* && "$doman" != localhost && "$vard" != localhost ]] \
    && ((10#$port >= 1 && 10#$port <= 65535))
}

sakert_varde() { [[ "$1" =~ ^[A-Za-z0-9._:/@+=-]*$ && "${#1}" -le 300 ]]; }
ar_doman() { [[ "$1" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$ ]]; }

skriv_konf() {
  local n tmp="$KONF.tmp"
  for n in "${KONF_NYCKLAR[@]}"; do
    sakert_varde "${!n}" || die "Inställningen $n kan inte sparas för automatiken (otillåtna tecken)."
  done
  {
    printf '# DriveLog – automatisk uppdatering. Skrivs av server.sh när raden körs; inga hemligheter.\n'
    for n in "${KONF_NYCKLAR[@]}"; do printf '%s=%s\n' "$n" "${!n}"; done
  } >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$KONF"
}
las_konf() { # läser bara kända nycklar med säkra värden; källas aldrig som skript
  [[ -r "$KONF" ]] || return 1
  local rad n v
  while IFS= read -r rad || [[ -n "$rad" ]]; do
    [[ -z "$rad" || "$rad" == \#* ]] && continue
    [[ "$rad" == *=* ]] || return 1
    n="${rad%%=*}"
    v="${rad#*=}"
    [[ " ${KONF_NYCKLAR[*]} " == *" $n "* ]] || return 1
    sakert_varde "$v" || return 1
    printf -v "$n" '%s' "$v"
  done <"$KONF"
}
kontrollera_konf() {
  ar_doman "$DOMAN" || return 1
  [[ "$DATA" == /* && "$DATA" != / && "$DATA" != *..* ]] || return 1
  [[ "$PULL" == 0 || "$PULL" == 1 ]] || return 1
  [[ "$VANTA_SEKUNDER" =~ ^[0-9]{1,5}$ && "$ANTAL_KOPIOR" =~ ^[1-9][0-9]?$ ]] || return 1
  [[ -n "$IMAGE" && -n "$NAME" && -n "$REPO_RAW" ]]
}

ta_las() { # vänta (1/0): två körningar (för hand eller automatiskt) får aldrig gå samtidigt
  command -v flock >/dev/null 2>&1 || return 0
  mkdir -p "$(dirname "$LAS")"
  exec 9>"$LAS"
  flock -n 9 && return 0
  [[ "$1" == 1 ]] || return 1
  info "En annan körning (t.ex. den automatiska uppdateringen) pågår – väntar tills den är klar …"
  flock -w 3600 9 || die "Den andra körningen blev inte klar inom en timme." "Försök igen senare."
}

# Den lokala kopia som timern kör. Kommer skriptet genom ett rör (curl … | bash) finns ingen fil
# att kopiera; då hämtas samma fil ur repot. Kopian kontrolleras innan den ersätter den gamla.
spara_kopia() {
  local tmp="$HEM/server.sh.ny"
  rm -f "$tmp"
  if [[ -n "$SKRIPT" && -f "$SKRIPT" && -r "$SKRIPT" ]]; then
    cp -- "$SKRIPT" "$tmp" || return 1
  else
    curl -fsSL --max-time 30 --max-filesize 1048576 --proto '=https,file' \
      "$REPO_RAW/deploy/server.sh" -o "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  fi
  if [[ "$(head -n 1 "$tmp")" != '#!/usr/bin/env bash' ]] || ! grep -q -- '--tidsstyrd' "$tmp" \
    || ! bash -n "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    return 1
  fi
  chmod 700 "$tmp" && mv -f "$tmp" "$HEM/server.sh"
}

skriv_enheter() {
  [[ "$HEM" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "Katalogen $HEM kan inte användas för automatiken."
  cat >"$SYSTEMD_KATALOG/$ENHET.service" <<EOF
# Skriven av DriveLogs installationsrad (server.sh). Slå av: kör raden med --utan-automatisk-uppdatering.
[Unit]
Description=DriveLog – automatisk uppdatering
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
Environment=DRIVELOG_HEM=$HEM
ExecStart=/bin/bash $HEM/server.sh --tidsstyrd
SyslogIdentifier=$ENHET
TimeoutStartSec=45min
EOF
  cat >"$SYSTEMD_KATALOG/$ENHET.timer" <<EOF
# Skriven av DriveLogs installationsrad (server.sh). Slå av: kör raden med --utan-automatisk-uppdatering.
[Unit]
Description=DriveLog – leta efter en ny version ungefär var 15:e minut

[Timer]
OnActiveSec=15min
OnUnitInactiveSec=15min
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF
  chmod 644 "$SYSTEMD_KATALOG/$ENHET.service" "$SYSTEMD_KATALOG/$ENHET.timer"
}

sla_pa_automatik() {
  command -v systemctl >/dev/null 2>&1 && [[ -d "$SYSTEMD_KATALOG" ]] \
    || die "Servern saknar systemd, så automatisk uppdatering kan inte slås på." "Uppdatera genom att köra raden igen."
  rubrik "Slår på automatisk uppdatering"
  (umask 077 && mkdir -p "$HEM" "$KOPIEKATALOG")
  chmod 700 "$HEM" "$KOPIEKATALOG"
  borja_logga
  spara_kopia || die "Den lokala kopian av skriptet kunde inte sparas i $HEM." "Kör raden igen om en stund."
  skriv_konf
  skriv_enheter
  systemctl daemon-reload
  systemctl enable --now "$ENHET.timer" >/dev/null 2>&1 \
    || die "Timern $ENHET.timer kunde inte startas." "Titta i:  systemctl status $ENHET.timer"
  satt_lage misslyckad_version ""
  satt_lage avbruten_version ""
  info "Automatisk uppdatering är påslagen (kopia: $HEM/server.sh, timer: $ENHET.timer)."
}

fornya_automatik() { # raden kördes för hand med automatiken på: ny kopia och aktuella inställningar
  if spara_kopia; then
    skriv_konf
    skriv_enheter
    systemctl daemon-reload || true
    info "Automatikens kopia av skriptet och dess inställningar är förnyade."
  else
    varna "Automatikens kopia av skriptet kunde inte förnyas – den gamla används tills vidare."
  fi
}

sla_av_automatik() {
  rubrik "Slår av automatisk uppdatering"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$ENHET.timer" >/dev/null 2>&1 || true
  fi
  rm -f "$SYSTEMD_KATALOG/$ENHET.timer" "$SYSTEMD_KATALOG/$ENHET.service"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl reset-failed "$ENHET.service" >/dev/null 2>&1 || true
  fi
  [[ -d "$HEM" ]] && borja_logga
  rm -f "$KONF" "$HEM/server.sh"
  info "Automatisk uppdatering är avslagen. DriveLog kör vidare som förut; uppdatera genom att köra raden igen."
  [[ ! -d "$KOPIEKATALOG" ]] || info "Säkerhetskopiorna från tidigare uppdateringar ligger kvar i $KOPIEKATALOG."
  if [[ -e "$PAGAENDE" ]]; then
    varna "En avbruten uppgradering är inte färdigställd. Kör installationsraden (med din domän) så görs det."
  fi
}

# Nyaste säkerhetskopian efter ändringstid (namnets tidsstämpel kan vara fel om klockan gick fel).
kopior_nyast_forst() {
  [[ -d "$KOPIEKATALOG" ]] || return 0
  find "$KOPIEKATALOG" -maxdepth 1 -type f -name 'drivelog-data-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | cut -d' ' -f2- || true
}

visa_lage() {
  local erb kor status kopior senaste v
  if [[ -f "$KONF" ]]; then las_konf || true; fi
  printf 'DriveLog – läge\n\n'
  if auto_ar_pa; then
    status="på"
    if command -v systemctl >/dev/null 2>&1 && [[ "$(systemctl is-active "$ENHET.timer" 2>/dev/null)" == active ]]; then
      status+=" (letar efter en ny version ungefär var 15:e minut)"
    else
      status+=", men timern $ENHET.timer är inte aktiv – kör installationsraden med --automatisk-uppdatering igen"
    fi
  else
    status="av (uppdatera genom att köra installationsraden igen)"
  fi
  printf '  Automatisk uppdatering:  %s\n' "$status"
  if command -v docker >/dev/null 2>&1 && docker inspect "$NAME" >/dev/null 2>&1; then
    kor="$(korande_version)"
    if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" == true ]]; then
      printf '  Kör version:             %s (igång)\n' "${kor:-okänd}"
    else
      printf '  Kör version:             %s (STOPPAD)\n' "${kor:-okänd}"
    fi
  else
    printf '  Kör version:             ingen DriveLog-container (%s) hittades\n' "$NAME"
  fi
  erb="$(las_erbjuden_version || true)"
  printf '  Erbjuden version:        %s\n' "${erb:-kunde inte läsas just nu}"
  v="$(las_lage senaste_kontroll)"
  printf '  Senaste kontroll:        %s\n' "${v:-ingen än}"
  v="$(las_lage senaste_uppgradering)"
  printf '  Senaste uppgradering:    %s\n' "${v:-ingen än}"
  v="$(las_lage senaste_fel)"
  printf '  Senaste fel:             %s\n' "${v:-inget}"
  if [[ -n "$(las_lage misslyckad_version)" ]]; then
    printf '  Hoppar över version:     %s (startade inte; prövas igen när en nyare erbjuds eller när du kör raden själv)\n' \
      "$(las_lage misslyckad_version)"
  fi
  if [[ -e "$PAGAENDE" ]]; then
    printf '  Avbruten uppgradering:   %s (görs klar vid nästa kontroll eller när du kör raden själv)\n' \
      "$(sed -n 's/^gammal=//p' "$PAGAENDE") → $(sed -n 's/^ny=//p' "$PAGAENDE")"
  fi
  if [[ -d "$KOPIEKATALOG" ]]; then
    kopior="$(kopior_nyast_forst)"
    senaste="$(head -n 1 <<<"$kopior")"
    printf '  Säkerhetskopior:         %s st i %s%s\n' "$(grep -c . <<<"$kopior" || true)" "$KOPIEKATALOG" \
      "${senaste:+ (senaste: ${senaste##*/})}"
  fi
  printf '\n  Hela loggen:  sudo tail -n 50 %s   eller   journalctl -t %s\n' "$HEM/uppdatering.log" "$ENHET"
}

# --- Argument --------------------------------------------------------------------------------------
DOMAN=""
NYTT_LOSENORD=0
AUTO_PA=0
AUTO_AV=0
VISA_LAGE=0
EXTRA=()     # --extra-doman, som de skrevs
UTAN_EXTRA=0 # --utan-extra-doman
vantar_extra=0
for arg in "$@"; do
  if [[ "$vantar_extra" == 1 ]]; then
    vantar_extra=0
    EXTRA+=("$arg")
    continue
  fi
  case "$arg" in
    --nytt-losenord) NYTT_LOSENORD=1 ;;
    --extra-doman) vantar_extra=1 ;;
    --extra-doman=*) EXTRA+=("${arg#*=}") ;;
    --utan-extra-doman) UTAN_EXTRA=1 ;;
    --automatisk-uppdatering) AUTO_PA=1 ;;
    --utan-automatisk-uppdatering) AUTO_AV=1 ;;
    --lage) VISA_LAGE=1 ;;
    --tidsstyrd) TIDSSTYRD=1 ;;
    -*) die "Okänt val: $arg" ;;
    *) [[ -z "$DOMAN" ]] || die "Ange bara en domän."; DOMAN="$arg" ;;
  esac
done
[[ "$AUTO_PA$AUTO_AV" != 11 ]] || die "Välj antingen --automatisk-uppdatering eller --utan-automatisk-uppdatering."
[[ "$vantar_extra" == 0 ]] || die "--extra-doman behöver ett värde, t.ex. --extra-doman transcribe.smgd.se=transcribe:8080."
[[ "$UTAN_EXTRA" == 0 || ${#EXTRA[@]} -eq 0 ]] || die "Välj antingen --extra-doman eller --utan-extra-doman."
if [[ "$TIDSSTYRD" == 1 && $# -ne 1 ]]; then die "--tidsstyrd används bara av timern och tar inga andra val."; fi
if [[ "$VISA_LAGE" == 1 || "$TIDSSTYRD" == 1 || ("$AUTO_AV" == 1 && -z "$DOMAN") ]]; then
  [[ "$(id -u)" == 0 ]] || die "Kör raden med sudo."
fi
if [[ "$VISA_LAGE" == 1 ]]; then
  visa_lage
  exit 0
fi
if [[ "$AUTO_AV" == 1 && -z "$DOMAN" ]]; then
  ta_las 1
  sla_av_automatik
  exit 0
fi
if [[ "$TIDSSTYRD" == 1 ]]; then
  # Inställningarna är de som gällde när ägaren senast körde raden (automatik.conf).
  if ! las_konf || ! kontrollera_konf; then
    borja_logga
    die "Automatikens inställningar ($KONF) saknas eller är trasiga." \
      "Kör installationsraden med --automatisk-uppdatering igen."
  fi
fi
[[ -n "$DOMAN" ]] || die "Ange domänen sist på raden, t.ex. drivelog.smgd.se."
DOMAN="${DOMAN,,}"
ar_doman "$DOMAN" \
  || die "'$DOMAN' ser inte ut som ett domännamn (skriv det utan https:// och utan snedstreck)."
[[ "$(id -u)" == 0 ]] || die "Kör raden med sudo (skriptet behöver vara root för att installera och starta Docker)."
LOKAL=0
[[ "$DOMAN" == localhost ]] && LOKAL=1

# Extra domäner: "behall" = som i inställningsfilen, "satt:<a>,<b>" = ersätt (tomt = ta bort alla).
EXTRA_RAD=behall
EXTRA_DOMANER=() # kontrollerade, <domän>=<värd>:<port>
if [[ ${#EXTRA[@]} -gt 0 || "$UTAN_EXTRA" == 1 ]]; then
  for rad in "${EXTRA[@]}"; do
    IFS=, read -r -a delar <<<"$rad"
    for del in "${delar[@]}"; do
      del="${del,,}"
      [[ -n "$del" ]] || continue
      ar_extra_doman "$del" || die "--extra-doman '$del' ska skrivas <domän>=<värd>:<port>, t.ex. transcribe.smgd.se=transcribe:8080." \
        "Värden är namnet på den andra tjänstens container (eller 127.0.0.1), utan http:// och utan sökväg."
      [[ "${del%%=*}" != "$DOMAN" ]] || die "--extra-doman: ${del%%=*} är redan DriveLogs egen domän."
      for annan in "${EXTRA_DOMANER[@]}"; do
        [[ "${annan%%=*}" != "${del%%=*}" ]] || die "--extra-doman: ${del%%=*} står med mer än en gång."
      done
      EXTRA_DOMANER+=("$del")
    done
  done
  [[ ${#EXTRA_DOMANER[@]} -gt 0 || "$UTAN_EXTRA" == 1 ]] || die "--extra-doman behöver ett värde, t.ex. --extra-doman transcribe.smgd.se=transcribe:8080."
  EXTRA_RAD="satt:$(IFS=,; printf '%s' "${EXTRA_DOMANER[*]}")"
fi

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

# --- Gemensamt för start och uppgradering --------------------------------------------------------
# Inställningsfilen skrivs av Python i avbilden (riktig JSON även för konstiga tecken). Värdena
# går in via standard in – aldrig på en kommandorad eller i en miljövariabel. De extra domänerna
# (EXTRA_RAD) ändras bara när --extra-doman eller --utan-extra-doman angavs; annars behålls de.
skriv_installningar() { # e-post ("" = behåll), lösenord ("" = inget)
  printf '%s\n%s\n%s\n%s\n' "$DOMAN" "$EXTRA_RAD" "$1" "$2" | docker run --rm -i --entrypoint python3 -v "$DATA:/data" "$BILD" -I -c '
import json, os, sys
doman, extra, epost, losenord = (rad.rstrip("\n") for rad in sys.stdin.readlines()[:4])
sokvag = "/data/options.json"
standard = {"domain": "", "admin_email": "", "admin_password": "", "strato_dyndns": False,
            "strato_dyndns_user": "", "strato_dyndns_password": "", "log_level": "info",
            "extra_domains": []}
try:
    with open(sokvag, encoding="utf-8") as fil:
        gammal = json.load(fil)
    if not isinstance(gammal, dict):
        gammal = {}
except (OSError, ValueError):
    gammal = {}
data = {nyckel: gammal.get(nyckel, varde) for nyckel, varde in standard.items()}
data["domain"] = doman
if extra.startswith("satt:"):
    data["extra_domains"] = [rad for rad in extra[len("satt:"):].split(",") if rad]
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

# Finns det extra domäner i inställningsfilen? Läses med Python i avbilden (som när filen skrivs),
# varje gång containern skapas – även vid automatisk uppdatering och återgång.
har_extra_domaner() { # avbild
  docker run --rm --entrypoint python3 -v "$DATA:/data:ro" "$1" -I -c '
import json, sys
try:
    with open("/data/options.json", encoding="utf-8") as fil:
        data = json.load(fil)
except (OSError, ValueError):
    sys.exit(1)
lista = data.get("extra_domains") if isinstance(data, dict) else None
sys.exit(0 if isinstance(lista, list) and any(isinstance(rad, str) and rad.strip() for rad in lista) else 1)
' >/dev/null 2>&1
}

sakerstall_natverk() {
  docker network inspect "$NATVERK" >/dev/null 2>&1 && return 0
  info "Skapar Docker-nätet $NATVERK (för de extra domänerna)."
  docker network create "$NATVERK" >/dev/null 2>&1 || docker network inspect "$NATVERK" >/dev/null 2>&1
}

starta_container() { # avbild (namn eller id)
  local natverk=()
  # Med extra domäner ansluts DriveLog till nätet där de andra tjänsterna nås på sina namn.
  if har_extra_domaner "$1"; then
    if sakerstall_natverk; then
      natverk=(--network "$NATVERK")
    else
      varna "Docker-nätet $NATVERK kunde inte skapas – DriveLog startas utan det, och de extra domänerna når inte sina tjänster."
    fi
  fi
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker stop -t 30 "$NAME" >/dev/null 2>&1 || true
    docker rm "$NAME" >/dev/null
  fi
  docker run -d --name "$NAME" --restart unless-stopped --init -e TZ="$TZ_NAMN" "${natverk[@]}" \
    -v "$DATA:/data" -p 80:80 -p 443:443 -p 443:443/udp "$1" >/dev/null
}

visa_logg() {
  if [[ -n "$LOGGFIL" ]]; then
    docker logs --tail "${1:-40}" "$NAME" 2>&1 | sed 's/^/   | /' | tee -a "$LOGGFIL" >&2 || true
  else
    docker logs --tail "${1:-40}" "$NAME" 2>&1 | sed 's/^/   | /' >&2 || true
  fi
}
halsa() { # extra curl-val
  curl -fsS --max-time 8 "$@" --resolve "$DOMAN:443:127.0.0.1" "https://$DOMAN/api/v1/health/ready" >/dev/null 2>&1
}
KURL_VAL=()
[[ "$LOKAL" == 1 ]] && KURL_VAL=(-k) # lokalt certifikat

# Väntar på hälsokontrollen. 0 = svarar, 2 = kontot kunde inte skapas, 3 = containern startade
# inte, 4 = inget svar inom tidsgränsen.
vanta_pa_svar() {
  local slut=$((SECONDS + VANTA_SEKUNDER)) logg
  while ((SECONDS < slut)); do
    if halsa "${KURL_VAL[@]}"; then
      return 0
    fi
    logg="$(docker logs --tail 300 "$NAME" 2>&1 || true)"
    if grep -qF -e "Det finns inget konto" -e "Kontot kunde inte skapas" <<<"$logg"; then
      return 2
    fi
    # Utan tillsyn: en container som startas om gång på gång har inte startat.
    if [[ "$TIDSSTYRD" == 1 ]] && (($(docker inspect -f '{{.RestartCount}}' "$NAME" 2>/dev/null || echo 0) >= 3)); then
      return 3
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]]; then
      sleep 5 # Docker startar om containern av sig själv; ge den en chans
      if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]]; then
        return 3
      fi
    fi
    sleep 3
  done
  return 4
}

# --- Automatisk uppdatering (körs av timern) -------------------------------------------------------
FEL=""
KOPIA=""
ATGARDAT=0 # 1 = en avbruten uppgradering gjordes klar i den här körningen
# Uppgifterna om en pågående uppgradering (sparas i PAGAENDE).
P_FAS="" P_GAMMAL="" P_BILD="" P_NY="" P_KOPIA="" P_DATA="" P_NAMN=""

hamta_avbild() { # BILD
  if [[ "$PULL" == 1 ]]; then
    docker pull -q "$BILD" >/dev/null 2>&1
  else
    docker image inspect "$BILD" >/dev/null 2>&1
  fi
}

# Markeringen skrivs (och synkas till disken) innan något ändras, så att den finns kvar även efter
# ett strömavbrott. fas: stoppad = den förra versionen är stoppad, datat är orört; provar = kopian
# är klar och den nya versionen får röra datat; aterstaller = återgången har börjat.
skriv_pagaende() { # fas
  local tmp="$PAGAENDE.tmp"
  P_FAS="$1"
  {
    printf '# DriveLog – en automatisk uppgradering pågår eller avbröts. Tas bort när den är klar.\n'
    printf 'fas=%s\ngammal=%s\ngammal_bild=%s\nny=%s\nkopia=%s\ndata=%s\nnamn=%s\n' \
      "$P_FAS" "$P_GAMMAL" "$P_BILD" "$P_NY" "$P_KOPIA" "$DATA" "$NAME"
  } >"$tmp" || return 1
  chmod 600 "$tmp" && sync -- "$tmp" && mv -f "$tmp" "$PAGAENDE" && sync -- "$HEM"
}
las_pagaende() { # strikt, som las_konf: kända nycklar och säkra värden, källas aldrig
  local rad n v
  P_FAS="" P_GAMMAL="" P_BILD="" P_NY="" P_KOPIA="" P_DATA="" P_NAMN=""
  [[ -r "$PAGAENDE" ]] || return 1
  while IFS= read -r rad || [[ -n "$rad" ]]; do
    [[ -z "$rad" || "$rad" == \#* ]] && continue
    n="${rad%%=*}"
    v="${rad#*=}"
    sakert_varde "$v" || return 1
    case "$n" in
      fas) P_FAS="$v" ;;
      gammal) P_GAMMAL="$v" ;;
      gammal_bild) P_BILD="$v" ;;
      ny) P_NY="$v" ;;
      kopia) P_KOPIA="$v" ;;
      data) P_DATA="$v" ;;
      namn) P_NAMN="$v" ;;
      *) return 1 ;;
    esac
  done <"$PAGAENDE"
  [[ "$P_FAS" =~ ^(stoppad|provar|aterstaller)$ ]] && ar_version "$P_GAMMAL" && ar_version "$P_NY" \
    && [[ -n "$P_BILD" && -n "$P_DATA" && -n "$P_NAMN" ]] || return 1
  [[ "$P_FAS" == stoppad || ("$P_KOPIA" == "$KOPIEKATALOG"/drivelog-data-*.tar.gz && "$P_KOPIA" != *..*) ]]
}

stada_halvfardiga_kopior() { # rester efter en avbruten säkerhetskopiering (körs med låset taget)
  [[ -d "$KOPIEKATALOG" ]] || return 0
  find "$KOPIEKATALOG" -maxdepth 1 -type f -name 'drivelog-data-*.tar.gz.tmp' -delete 2>/dev/null || true
}

# Säkerhetskopia av hela datakatalogen (databas, nycklar, certifikat, inställningar). Containern
# är stoppad, så SQLite skrivs inte under tiden. De ANTAL_KOPIOR senaste sparas – den nya alltid.
sakerhetskopiera() { # versionen som kopian hör till
  local behov ledigt namn gammal
  local -i sparade=1
  (umask 077 && mkdir -p "$KOPIEKATALOG")
  stada_halvfardiga_kopior
  behov="$(du -sk "$DATA" 2>/dev/null | cut -f1)"
  ledigt="$(df -Pk "$KOPIEKATALOG" 2>/dev/null | awk 'NR == 2 {print $4}')"
  if [[ ! "$behov" =~ ^[0-9]+$ || ! "$ledigt" =~ ^[0-9]+$ ]] || ((ledigt < behov + 102400)); then
    FEL="För lite ledigt diskutrymme för säkerhetskopian (datat är ungefär $((${behov:-0} / 1024)) MB, ledigt $((${ledigt:-0} / 1024)) MB)."
    return 1
  fi
  namn="$KOPIEKATALOG/drivelog-data-$(date +%Y%m%dT%H%M%S)-$1.tar.gz"
  if ! tar --numeric-owner -C "$DATA" -czf "$namn.tmp" . 2>/dev/null || ! tar -tzf "$namn.tmp" >/dev/null 2>&1 \
    || ! sync -- "$namn.tmp"; then
    rm -f "$namn.tmp"
    FEL="Säkerhetskopian av $DATA kunde inte skapas."
    return 1
  fi
  chmod 600 "$namn.tmp"
  mv -f "$namn.tmp" "$namn"
  sync -- "$KOPIEKATALOG" || true
  KOPIA="$namn"
  while IFS= read -r gammal; do
    [[ -n "$gammal" && "$gammal" != "$KOPIA" ]] || continue
    if ((sparade < ANTAL_KOPIOR)); then
      sparade+=1
      continue
    fi
    rm -f -- "$gammal"
    info "Äldre säkerhetskopia borttagen: ${gammal##*/}"
  done < <(kopior_nyast_forst)
}

# Lägger tillbaka datakatalogen från en kopia. Kopian kontrolleras och packas upp bredvid först;
# den nuvarande datakatalogen byts ut först när uppackningen är hel. Går det inte är den orörd.
aterstall_data() { # kopia
  local kopia="$1" ny="$DATA.aterstalls" gammal="$DATA.fore-aterstallning"
  if [[ ! -f "$kopia" || ! -r "$kopia" ]]; then
    FEL="säkerhetskopian $kopia finns inte"
    return 1
  fi
  if ! tar -tzf "$kopia" >/dev/null 2>&1; then
    FEL="säkerhetskopian $kopia går inte att läsa"
    return 1
  fi
  rm -rf -- "$ny"
  if ! (umask 077 && mkdir -p "$ny") || ! tar --numeric-owner -p -C "$ny" -xzf "$kopia" 2>/dev/null; then
    rm -rf -- "$ny"
    FEL="säkerhetskopian kunde inte packas upp (fullt på disken?)"
    return 1
  fi
  rm -rf -- "$gammal"
  if [[ ! -e "$DATA" ]] || mv -T -- "$DATA" "$gammal" 2>/dev/null; then
    mv -T -- "$ny" "$DATA" || { FEL="$ny kunde inte flyttas till $DATA"; return 1; }
    rm -rf -- "$gammal"
  else
    # Datakatalogen går inte att flytta (t.ex. en egen disk monterad där): töm och kopiera in.
    if ! { find "$DATA" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && cp -a -- "$ny/." "$DATA/"; }; then
      FEL="datat kunde inte kopieras in i $DATA (kopian finns i $ny)"
      return 1
    fi
    rm -rf -- "$ny"
  fi
  sync -- "$DATA" || true
}

# Startar en avbild och kontrollerar att den svarar och är rätt version. Sätter FEL om inte.
starta_och_kontrollera() { # avbild, version, skriv inställningar (1/0)
  local kod=0 kor
  BILD="$1"
  if [[ "$3" == 1 ]] && ! skriv_installningar "" ""; then
    FEL="inställningsfilen kunde inte skrivas med avbilden $1"
    return 1
  fi
  if ! starta_container "$1"; then
    FEL="containern kunde inte skapas"
    return 1
  fi
  vanta_pa_svar || kod=$?
  case "$kod" in
    0)
      kor="$(korande_version)"
      [[ "$kor" == "$2" ]] && return 0
      FEL="den svarar, men som version ${kor:-okänd} i stället för $2"
      ;;
    2) FEL="kontot kunde inte läsas eller skapas" ;;
    3) FEL="containern startade inte (stannade eller startades om gång på gång)" ;;
    *) FEL="den svarade inte inom $VANTA_SEKUNDER sekunder" ;;
  esac
  visa_logg 40
  return 1
}

stada_avbilder() { # behåll: avbilder som ska sparas
  [[ "$PULL" == 1 ]] || return 0 # lokala avbilder (prov) rörs inte
  local ref
  while IFS= read -r ref; do
    [[ -n "$ref" && "$ref" != *"<none>"* ]] || continue
    [[ " $* " == *" $ref "* ]] && continue
    docker rmi "$ref" >/dev/null 2>&1 && info "Gammal avbild borttagen: $ref"
  done < <(docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE" 2>/dev/null || true)
}

# Gör klart en uppgradering som avbröts (omstart, strömavbrott, tjänsten stoppad). Körs med låset
# taget, först i varje körning. Den förra versionen startas igen med datat från före försöket;
# den nya versionen litar skriptet inte på, eftersom den aldrig blev kontrollerad.
slutfor_avbruten() {
  [[ -e "$PAGAENDE" ]] || return 0
  las_pagaende || die "Markeringen $PAGAENDE om en pågående uppgradering går inte att läsa." \
    "Säkerhetskopiorna ligger i $KOPIEKATALOG. Be om hjälp innan du kör något annat."
  [[ "$P_DATA" == "$DATA" && "$P_NAMN" == "$NAME" ]] \
    || die "Den avbrutna uppgraderingen gällde $P_DATA (container $P_NAMN), inte $DATA ($NAME)." \
      "Kör raden med samma inställningar som när automatiken slogs på."
  rubrik "Gör klart en avbruten uppgradering ($P_GAMMAL → $P_NY)"
  stada_halvfardiga_kopior
  local efter="" fas="$P_FAS"
  if [[ "$fas" == stoppad ]]; then
    # Bara säkerhetskopieringen avbröts; datat är orört. Den förra versionen startas igen.
    info "Säkerhetskopieringen avbröts – DriveLog $P_GAMMAL startas igen med datat som det är."
    starta_och_kontrollera "$P_BILD" "$P_GAMMAL" 0 \
      || die "DriveLog $P_GAMMAL kunde inte startas igen efter avbrottet ($FEL)." \
        "Datat är orört i $DATA. Ett nytt försök görs vid nästa kontroll, eller kör installationsraden."
    rm -f -- "$PAGAENDE"
    efter="Uppgraderingen till $P_NY avbröts under säkerhetskopieringen; DriveLog $P_GAMMAL kör igen, datat är orört."
  else
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    if [[ "$fas" == provar ]]; then
      # Avbröts den nya versionen mitt i provet: ett nytt försök, men inte fler.
      if [[ "$(las_lage avbruten_version)" == "$P_NY" ]]; then
        satt_lage misslyckad_version "$P_NY"
      else
        satt_lage avbruten_version "$P_NY"
      fi
      skriv_pagaende aterstaller || true
    fi
    info "Återställer datat från $P_KOPIA …"
    aterstall_data "$P_KOPIA" \
      || die "Den avbrutna uppgraderingen till $P_NY kunde inte göras klar: $FEL." \
        "Datakatalogen $DATA har inte ändrats. Ett nytt försök görs vid nästa kontroll; be om hjälp om felet står kvar."
    starta_och_kontrollera "$P_BILD" "$P_GAMMAL" 0 \
      || die "Datat är återställt från $P_KOPIA, men DriveLog $P_GAMMAL startade inte ($FEL)." \
        "Ett nytt försök görs vid nästa kontroll, eller kör installationsraden."
    rm -f -- "$PAGAENDE"
    if [[ "$fas" == provar ]]; then
      efter="Uppgraderingen till $P_NY avbröts innan den var kontrollerad"
    else
      efter="Återgången från $P_NY avbröts och är nu klar"
    fi
    efter+="; DriveLog $P_GAMMAL kör igen med datat från före försöket."
    if [[ "$(las_lage misslyckad_version)" == "$P_NY" ]]; then
      efter+=" $P_NY prövas inte igen förrän en nyare version erbjuds eller du kör raden själv."
    else
      efter+=" Ett nytt försök görs vid nästa kontroll."
    fi
  fi
  info "$efter"
  satt_lage senaste_fel "$(tid) – $efter"
  ATGARDAT=1
}

# Avbrott (systemd stoppar tjänsten, servern stängs av): markeringen ligger kvar så att nästa
# körning gör klart. Var bara den förra versionen stoppad startas den direkt igen – en container
# som stoppats med docker stop startar annars inte av sig själv efter en omstart av servern.
avbrott() { # signal
  trap - TERM HUP INT
  till_loggfil "AVBRUTEN ($1)${P_FAS:+ under fasen $P_FAS}."
  if [[ "$P_FAS" == stoppad ]]; then
    stada_halvfardiga_kopior
    if timeout 20 docker start "$NAME" >/dev/null 2>&1; then
      rm -f -- "$PAGAENDE"
      till_loggfil "DriveLog $P_GAMMAL startades igen; inget har ändrats."
      satt_lage senaste_fel "$(tid) – uppgraderingen till $P_NY avbröts ($1) innan något ändrades; DriveLog $P_GAMMAL kör igen."
      exit 143
    fi
  fi
  [[ -z "$P_FAS" ]] || satt_lage senaste_fel "$(tid) – uppgraderingen till $P_NY avbröts ($1); den görs klar vid nästa kontroll."
  exit 143
}

uppgradera() { # körande version, ny version
  local gammal="$1" ny="$2" gammal_id gammal_bild orsak
  BILD="$IMAGE:$ny"
  rubrik "Uppgraderar DriveLog $gammal → $ny"
  hamta_avbild || die "Kunde inte hämta $BILD – inget har ändrats. Försöker igen vid nästa kontroll."
  gammal_id="$(docker inspect -f '{{.Image}}' "$NAME")"
  gammal_bild="$(docker inspect -f '{{.Config.Image}}' "$NAME")"
  # Återgången startar exakt samma avbild – även om taggen skulle ha flyttats.
  [[ "$(docker image inspect -f '{{.Id}}' "$gammal_bild" 2>/dev/null)" == "$gammal_id" ]] || gammal_bild="$gammal_id"
  P_GAMMAL="$gammal" P_BILD="$gammal_bild" P_NY="$ny" P_KOPIA=""
  if ! { sakert_varde "$gammal_bild" && skriv_pagaende stoppad; }; then
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "Kunde inte skriva $PAGAENDE – inget har ändrats."
  fi

  info "Stoppar DriveLog $gammal för säkerhetskopian …"
  if ! docker stop -t 30 "$NAME" >/dev/null; then
    docker start "$NAME" >/dev/null 2>&1 || true
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "DriveLog $gammal kunde inte stoppas – inget har ändrats."
  fi
  if ! sakerhetskopiera "$gammal"; then
    docker start "$NAME" >/dev/null 2>&1 || true
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "$FEL Inget har ändrats; DriveLog $gammal startades igen."
  fi
  P_KOPIA="$KOPIA"
  if ! skriv_pagaende provar; then
    docker start "$NAME" >/dev/null 2>&1 || true
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "Kunde inte skriva $PAGAENDE – inget har ändrats; DriveLog $gammal startades igen."
  fi
  info "Säkerhetskopia: $KOPIA"

  info "Startar DriveLog $ny …"
  if starta_och_kontrollera "$BILD" "$ny" 1; then
    rm -f -- "$PAGAENDE"
    P_FAS=""
    satt_lage misslyckad_version ""
    satt_lage avbruten_version ""
    satt_lage senaste_uppgradering "$(tid) – $gammal → $ny"
    info "Klart: DriveLog $ny är igång och svarar."
    stada_avbilder "$BILD" "$gammal_bild"
    return 0
  fi

  orsak="$FEL"
  varna "DriveLog $ny fungerade inte: $orsak. Går tillbaka till $gammal."
  satt_lage misslyckad_version "$ny"
  skriv_pagaende aterstaller || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  info "Återställer datat från $KOPIA …"
  aterstall_data "$KOPIA" || die "Version $ny startade inte ($orsak), och datat kunde inte återställas: $FEL." \
    "Datakatalogen $DATA har inte ändrats sedan försöket. Ett nytt försök att gå tillbaka görs vid nästa kontroll." \
    "Står felet kvar: be om hjälp."
  if starta_och_kontrollera "$gammal_bild" "$gammal" 0; then
    rm -f -- "$PAGAENDE"
    P_FAS=""
    info "Återgick till DriveLog $gammal med datat från före försöket."
    die "Version $ny startade inte ($orsak). DriveLog $gammal kör igen, med datat från före försöket." \
      "$ny prövas inte igen förrän en nyare version erbjuds eller du kör installationsraden själv."
  fi
  die "Version $ny startade inte ($orsak), och återgången till $gammal misslyckades också ($FEL)." \
    "Datat är återställt från $KOPIA. Ett nytt försök görs vid nästa kontroll, eller kör installationsraden."
}

tidsstyrd_korning() {
  local erbjuden kor miss
  borja_logga
  if ! ta_las 0; then
    info "En annan körning pågår redan – den här kontrollen hoppas över."
    exit 0
  fi
  trap 'avbrott TERM' TERM
  trap 'avbrott HUP' HUP
  trap 'avbrott INT' INT
  docker info >/dev/null 2>&1 || die "Docker svarar inte – ingen uppdatering nu."
  stada_halvfardiga_kopior
  slutfor_avbruten
  if [[ "$ATGARDAT" == 1 ]]; then
    satt_lage senaste_kontroll "$(tid) – en avbruten uppgradering gjordes klar (kör $(korande_version))"
    exit 0
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]]; then
    satt_lage senaste_kontroll "$(tid) – DriveLog ($NAME) kör inte, ingen uppdatering"
    die "DriveLog-containern $NAME kör inte – ingen uppdatering." "Starta DriveLog genom att köra installationsraden."
  fi
  erbjuden="$(las_erbjuden_version)" \
    || die "Kunde inte läsa vilken version som erbjuds – inget har ändrats. Försöker igen vid nästa kontroll."
  kor="$(korande_version)"
  [[ -n "$kor" ]] || die "Kan inte avgöra vilken version som kör – ingen automatisk uppdatering." \
    "Kör installationsraden för hand."
  if ! ar_nyare "$erbjuden" "$kor"; then
    satt_lage senaste_kontroll "$(tid) – ingen ny version (kör $kor, erbjuds $erbjuden)"
    info "Ingen ny version (kör $kor, erbjuds $erbjuden)."
    exit 0
  fi
  miss="$(las_lage misslyckad_version)"
  if [[ "$erbjuden" == "$miss" ]]; then
    satt_lage senaste_kontroll "$(tid) – $erbjuden hoppas över (startade inte förra gången), kör $kor"
    info "Version $erbjuden startade inte förra gången – försöker inte igen (kör $kor)."
    exit 0
  fi
  satt_lage senaste_kontroll "$(tid) – ny version $erbjuden erbjuds (kör $kor)"
  info "Ny version erbjuds: $erbjuden (kör $kor)."
  uppgradera "$kor" "$erbjuden"
}

if [[ "$TIDSSTYRD" == 1 ]]; then
  tidsstyrd_korning
  exit 0
fi

# --- 0. Lås och automatik ----------------------------------------------------------------------------
ta_las 1
if auto_ar_pa; then
  borja_logga
  till_loggfil "Installationsraden körs för hand${VERSION:+ (version $VERSION)}."
  # Ägaren kör raden själv: en version som tidigare misslyckades får prövas igen.
  satt_lage misslyckad_version ""
  satt_lage avbruten_version ""
fi
[[ "$AUTO_AV" == 0 ]] || sla_av_automatik

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

# En automatisk uppgradering som avbröts görs klar först (den förra versionen och datat tillbaka).
slutfor_avbruten
if [[ "$ATGARDAT" == 1 ]]; then
  satt_lage misslyckad_version "" # ägaren kör raden själv: allt får prövas igen
  satt_lage avbruten_version ""
fi
# Saknas kontot men det finns säkerhetskopior från automatiken har datat försvunnit – då skapas
# inget nytt, tomt konto ovanpå; ägaren får veta var datat finns.
if [[ ! -e "$DATA/hemligheter/admin_losenord_marke" ]]; then
  SENASTE_KOPIA="$(kopior_nyast_forst | head -n 1 || true)"
  [[ -z "$SENASTE_KOPIA" ]] || die "Ditt DriveLog-data saknas i $DATA, men det finns säkerhetskopior i $KOPIEKATALOG." \
    "Inget nytt konto skapas, så att ingenting skrivs över. Lägg tillbaka den senaste kopian och kör raden igen:" \
    "  sudo mkdir -p $DATA && sudo tar --numeric-owner -xzpf $SENASTE_KOPIA -C $DATA" \
    "Vill du verkligen börja om med ett tomt DriveLog: flytta först bort $KOPIEKATALOG."
fi

# --- 2. Version ------------------------------------------------------------------------------------
if [[ -z "$VERSION" ]]; then
  # Den version som erbjuds i repot är alltid färdigbyggd och publicerad (se .github/workflows/addon.yml).
  VERSION="$(las_erbjuden_version || true)"
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
    # shellcheck disable=SC2086,SC2116 # echo slår ihop raderna till en
    [[ "$traff" == 1 ]] || die "$DOMAN pekar på $(echo $dns4), inte på den här servern ($publika4)." \
      "Ändra A-posten hos den som sköter domänens DNS (för smgd.se: STRATO) till serverns adress," \
      "vänta några minuter och kör raden igen. Utan det kan certifikatet inte hämtas."
    # shellcheck disable=SC2086,SC2116
    info "A-posten stämmer: $(echo $dns4)"
  else
    varna "Servern har ingen publik adress på sitt eget nätverkskort, så kontrollen kan inte göras här."
    # shellcheck disable=SC2086,SC2116
    varna "$DOMAN pekar på $(echo $dns4) – det ska vara serverns publika adress."
  fi
  if [[ -n "$dns6" ]]; then
    traff=0
    for a in $dns6; do [[ $'\n'"$mina6"$'\n' == *$'\n'"$a"$'\n'* ]] && traff=1; done
    # shellcheck disable=SC2086,SC2116
    [[ "$traff" == 1 ]] || die "$DOMAN har en AAAA-post (IPv6) som inte leder hit: $(echo $dns6)." \
      "Certifikatutfärdaren kontrollerar helst över IPv6 och hamnar då på fel server." \
      "Ta bort AAAA-posten (eller peka den på serverns IPv6-adress) och kör raden igen."
  fi
fi

if [[ "${DRIVELOG_SKIP_DNS_CHECK:-0}" != 1 ]]; then
  for rad in "${EXTRA_DOMANER[@]}"; do
    [[ -n "$(getent ahosts "${rad%%=*}" 2>/dev/null || true)" ]] \
      || varna "${rad%%=*} hittas inte i DNS än – peka den på den här servern, annars kan certifikatet inte hämtas."
  done
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
    LOSENORD2=""
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
FORE=""
if [[ -n "$LOGGFIL" ]] && docker inspect "$NAME" >/dev/null 2>&1; then FORE="$(korande_version)"; fi
starta_container "$BILD"

if [[ "$LOKAL" == 1 ]]; then
  info "Väntar på att DriveLog ska svara …"
else
  info "Väntar på att DriveLog ska svara och att certifikatet ska hämtas (kan ta ett par minuter) …"
fi
svar=0
vanta_pa_svar || svar=$?
if [[ "$svar" == 2 ]]; then
  visa_logg 25
  die "Kontot kunde inte skapas – se raderna ovan." "Rätta det som står där och kör raden igen."
fi
if [[ "$svar" == 3 ]]; then
  visa_logg 40
  die "DriveLog startade inte – se raderna ovan."
fi

stada

if [[ "$svar" != 0 ]]; then
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

# --- 7. Automatisk uppdatering ------------------------------------------------------------------------
if [[ -n "$LOGGFIL" ]]; then
  EFTER="$(korande_version)"
  if [[ -n "$FORE" && -n "$EFTER" && "$FORE" != "$EFTER" ]]; then
    satt_lage senaste_uppgradering "$(tid) – $FORE → $EFTER (installationsraden för hand)"
  fi
  till_loggfil "Installationsraden klar: DriveLog ${EFTER:-$VERSION} är igång."
fi
if [[ "$AUTO_PA" == 1 ]]; then
  sla_pa_automatik
elif auto_ar_pa; then
  fornya_automatik
fi

rubrik "Klart"
info "DriveLog $VERSION är igång: https://$DOMAN"
for rad in "${EXTRA_DOMANER[@]}"; do
  info "Extra domän: https://${rad%%=*} skickas vidare till ${rad#*=} (Docker-nätet $NATVERK)."
done
[[ "$UTAN_EXTRA" == 0 ]] || info "Inga extra domäner längre."
if [[ "$NY_INSTALLATION" == 1 ]]; then
  info "Logga in med $EPOST och lösenordet du valde."
elif [[ "$NYTT_LOSENORD" == 1 ]]; then
  info "Lösenordet för $EPOST är bytt."
fi
if auto_ar_pa; then
  info "Nya versioner installeras automatiskt. Läget:  sudo $HEM/server.sh --lage"
else
  info "Uppdatera senare: kör samma rad igen."
fi
info "Allt ditt ligger i $DATA – ta med den katalogen i serverns säkerhetskopia."
info "Loggen: docker logs --tail 50 $NAME"
