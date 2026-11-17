#!/usr/bin/env bash
# Installerar transkriberingen bredvid DriveLog på en egen server – allt i en rad:
#
#   curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/transcribe/installera.sh | sudo bash -s -- drivelog.smgd.se transcribe.smgd.se
#
# Raden
#   1. ser till att DriveLog tar emot transkriberingens domän (hoppas över om den redan svarar),
#   2. hämtar tjänstens filer till /opt/transcribe (docker-compose.yml, uppdatera.sh och – första
#      gången – .env),
#   3. startar tjänsten; kör den redan installeras en nyare version på samma säkra sätt som den
#      automatiska uppdateringen gör det (säkerhetskopia av databasen och återgång om den nya
#      versionen inte fungerar),
#   4. slår på automatisk uppdatering: en systemd-timer kör /opt/transcribe/uppdatera.sh ungefär var
#      15:e minut, och nya versioner installeras av sig själva.
#
# Nycklarna till Deepgram och Groq läggs in i webbgränssnittet (Administration → API-nycklar).
# Det går bra att köra raden hur många gånger som helst: en .env som redan finns skrivs aldrig över.
#
# Val sist på raden:
#   --utan-automatisk-uppdatering   tar bort timern; tjänsten uppdateras då bara när raden körs
#
# Läget för den automatiska uppdateringen:  sudo /opt/transcribe/uppdatera.sh --lage
set -euo pipefail

KATALOG="${TRANSCRIBE_KATALOG:-/opt/transcribe}"
RAW="${TRANSCRIBE_REPO_RAW:-https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD}"
LAS="${TRANSCRIBE_LAS:-/run/transcribe-uppdatering.lock}"
SYSTEMD_KATALOG="${TRANSCRIBE_SYSTEMD_DIR:-/etc/systemd/system}"
PULL="${TRANSCRIBE_PULL:-1}"
VANTA="${TRANSCRIBE_VANTA:-180}"
CONTAINER=transcribe
VOLYM=transcribe_transcribe-data # volymen transcribe-data i compose-projektet transcribe
STANDARD_IMAGE=ghcr.io/smgd-mp/transcribe
ENHET=transcribe-uppdatering
# Tål en avbild utan några etiketter alls (då är .Config.Labels tom).
ETIKETT='{{with .Config.Labels}}{{index . "org.opencontainers.image.version"}}{{end}}'
TILLSTAND='{{.State.Status}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}ingen{{end}}'
TMP=""

fel() {
  echo "FEL: $1" >&2
  shift
  local rad
  for rad in "$@"; do echo "     $rad" >&2; done
  exit 1
}
ar_version() { [[ "$1" =~ ^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$ ]]; }
ar_sokvag() { [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ && "$1" != *..* && "$1" != / ]]; }

# Läser ett värde ur .env utan att källa filen (som uppdatera.sh): sista raden NAMN=…, utan CR,
# blanksteg och ett par omslutande citattecken.
las_env() { # namn
  [[ -f "$KATALOG/.env" ]] || return 0
  local v
  v="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$KATALOG/.env" 2>/dev/null | tail -n 1)" || true
  [[ -n "$v" ]] || return 0
  v="${v#*=}"
  v="${v%$'\r'}"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  if [[ "$v" =~ ^\"(.*)\"$ || "$v" =~ ^\'(.*)\'$ ]]; then v="${BASH_REMATCH[1]}"; fi
  printf '%s' "$v"
}

# Sätter NAMN=värde i .env utan att värdet syns på någon kommandorad (som uppdatera.sh): den första
# raden med namnet byts, fler likadana tas bort, övriga rader lämnas orörda.
satt_rad() { # namn värde
  local tmp
  tmp="$(mktemp "$KATALOG/.env.XXXXXX")"
  chmod 600 "$tmp"
  NAMN="$1" VARDE="$2" LC_ALL=C awk '
    BEGIN { n = ENVIRON["NAMN"]; v = ENVIRON["VARDE"]; klar = 0; m = "^[ \t]*(export[ \t]+)?" n "[ \t]*=" }
    $0 ~ m { if (!klar) { cr = (substr($0, length($0)) == "\r") ? "\r" : ""; print n "=" v cr; klar = 1 } next }
    { print }
    END { if (!klar) print n "=" v }
  ' "$KATALOG/.env" >"$tmp"
  mv -f "$tmp" "$KATALOG/.env"
}

hamta() { # sökväg i repot, mål
  curl -fsSL --max-time 60 --proto '=https,file' -o "$2" "$RAW/$1" || fel "Kunde inte hämta $RAW/$1." "Inget har ändrats i $KATALOG. Försök igen om en stund."
  [[ -s "$2" ]] || fel "$RAW/$1 var tom." "Inget har ändrats i $KATALOG."
}

# Väntar högst VANTA sekunder på att tjänsten kör och är "healthy" (samma regler som uppdatera.sh).
vanta_frisk() {
  local -i i
  local status omstarter halsa
  for ((i = 0; i < VANTA; i += 2)); do
    status="" omstarter="" halsa=""
    read -r status omstarter halsa < <(docker inspect -f "$TILLSTAND" "$CONTAINER" 2>/dev/null || true) || true
    [[ "$omstarter" =~ ^[0-9]+$ ]] || omstarter=0
    ((omstarter < 2)) || return 1
    if [[ "$status" == running ]]; then
      [[ "$halsa" != healthy && "$halsa" != ingen ]] || return 0
      [[ "$halsa" != unhealthy ]] || return 1
    fi
    sleep 2
  done
  return 1
}
# Utan styrtecken (ASCII, C1 och tecken som vänder skrivriktningen) och bara giltig UTF-8, som i
# uppdatera.sh: texten kommer från containern.
visa_loggrader() {
  echo "   De sista raderna i tjänstens logg:" >&2
  docker logs --tail 30 "$CONTAINER" 2>&1 | LC_ALL=C tr -d '\000-\010\013-\037\177' | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null \
    | LC_ALL=C sed -e 's/\xc2[\x80-\x9f]//g' -e 's/\xe2\x80[\x8e\x8f\xaa-\xae]//g' -e 's/\xe2\x81[\xa6-\xa9]//g' -e 's/^/   | /' >&2 || true
}
tjansten_kor() { [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == true ]]; }

systemd_finns() {
  command -v systemctl >/dev/null 2>&1 && [[ -d "$SYSTEMD_KATALOG" ]] && systemctl show-environment >/dev/null 2>&1
}
skriv_enheter() {
  cat >"$SYSTEMD_KATALOG/$ENHET.service" <<EOF
# Skriven av transkriberingens installationsrad (installera.sh). Slå av: kör raden med --utan-automatisk-uppdatering.
[Unit]
Description=SMGD Transkribering – automatisk uppdatering
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
Environment=TRANSCRIBE_KATALOG=$KATALOG
Environment=TRANSCRIBE_LAS=$LAS
ExecStart=/bin/bash $KATALOG/uppdatera.sh --tidsstyrd
SyslogIdentifier=$ENHET
TimeoutStartSec=20min
EOF
  cat >"$SYSTEMD_KATALOG/$ENHET.timer" <<EOF
# Skriven av transkriberingens installationsrad (installera.sh). Slå av: kör raden med --utan-automatisk-uppdatering.
[Unit]
Description=SMGD Transkribering – leta efter en ny version ungefär var 15:e minut

[Timer]
OnActiveSec=5min
OnUnitInactiveSec=15min
RandomizedDelaySec=3min

[Install]
WantedBy=timers.target
EOF
  chmod 644 "$SYSTEMD_KATALOG/$ENHET.service" "$SYSTEMD_KATALOG/$ENHET.timer"
}

main() {
  local drivelog_doman="" doman="" auto=1 arg image version id lank uppdatering_fel=0 befintlig=0 rc
  for arg in "$@"; do
    case "$arg" in
      --utan-automatisk-uppdatering) auto=0 ;;
      -*) fel "okänt val: $arg (det som finns: --utan-automatisk-uppdatering)" ;;
      *)
        if [[ -z "$drivelog_doman" ]]; then
          drivelog_doman="$arg"
        elif [[ -z "$doman" ]]; then
          doman="$arg"
        else
          fel "för många argument: ange DriveLogs domän och transkriberingens domän"
        fi
        ;;
    esac
  done
  [[ -n "$drivelog_doman" && -n "$doman" ]] ||
    fel "ange DriveLogs domän och transkriberingens domän, t.ex.: drivelog.smgd.se transcribe.smgd.se"
  local d
  for d in "$drivelog_doman" "$doman"; do
    [[ "$d" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$d" == *.* && "$d" != *..* ]] || fel "ogiltig domän: $d (skriv den utan https:// och utan snedstreck)"
  done
  [[ "$drivelog_doman" != "$doman" ]] || fel "de två domänerna måste vara olika"
  [[ $EUID -eq 0 ]] || fel "kör raden med sudo"
  command -v docker >/dev/null || fel "Docker saknas – installera DriveLog först (deploy/server.sh)"
  command -v flock >/dev/null || fel "kommandot flock saknas (paketet util-linux)"
  if ! ar_sokvag "$KATALOG" || ! ar_sokvag "$LAS"; then fel "ogiltig katalog eller låsfil: $KATALOG, $LAS"; fi
  [[ "$PULL" == 0 || "$PULL" == 1 ]] || fel "TRANSCRIBE_PULL ska vara 0 eller 1"
  [[ "$VANTA" =~ ^[0-9]{1,5}$ ]] || fel "TRANSCRIBE_VANTA ska vara ett antal sekunder"

  # Samma lås som den automatiska uppdateringen: raden och timern körs aldrig samtidigt.
  mkdir -p "$(dirname "$LAS")"
  exec 9>"$LAS"
  if ! flock -n 9; then
    echo "   En automatisk uppdatering pågår just nu – väntar tills den är klar …"
    flock -w 3600 9 || fel "den automatiska uppdateringen blev inte klar inom en timme" "Försök igen senare."
  fi
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT

  echo "== 1/4 DriveLog tar emot $doman"
  if curl -fsS --max-time 15 --proto '=https' "https://$doman/api/health" 2>/dev/null | grep -q '"ok":true'; then
    echo "   https://$doman svarar redan med giltigt certifikat – DriveLog behöver inte ändras (och startas inte om)."
  else
    # Hämtas till en fil och körs därifrån: det här skriptet kommer självt via ett rör, och
    # DriveLogs skript ska inte kunna läsa något från det.
    hamta deploy/server.sh "$TMP/server.sh"
    bash "$TMP/server.sh" "$drivelog_doman" --extra-doman "$doman=transcribe:8080" </dev/null
  fi

  echo "== 2/4 Tjänstens filer i $KATALOG"
  # Allt hämtas och kontrolleras först; inget i katalogen ändras förrän allt finns.
  hamta transcribe/docker-compose.yml "$TMP/docker-compose.yml"
  grep -qE '^[[:space:]]*image:.*\$\{TRANSCRIBE_VERSION' "$TMP/docker-compose.yml" \
    || fel "Den hämtade docker-compose.yml är inte den väntade (TRANSCRIBE_VERSION saknas)." "Inget har ändrats."
  hamta transcribe/uppdatera.sh "$TMP/uppdatera.sh"
  if [[ "$(head -n 1 "$TMP/uppdatera.sh")" != '#!/usr/bin/env bash' ]] || ! grep -q -- '--tidsstyrd' "$TMP/uppdatera.sh" \
    || ! bash -n "$TMP/uppdatera.sh" 2>/dev/null; then
    fel "Den hämtade uppdatera.sh är inte hel." "Inget har ändrats. Försök igen om en stund."
  fi
  if [[ ! -f "$KATALOG/.env" ]]; then hamta transcribe/.env.example "$TMP/.env"; fi
  mkdir -p "$KATALOG"
  chown root:root "$KATALOG"
  chmod go-w "$KATALOG"
  install -m 644 -o root -g root "$TMP/docker-compose.yml" "$KATALOG/docker-compose.yml"
  install -m 700 -o root -g root "$TMP/uppdatera.sh" "$KATALOG/uppdatera.sh.ny"
  mv -f "$KATALOG/uppdatera.sh.ny" "$KATALOG/uppdatera.sh"
  if [[ ! -f "$KATALOG/.env" ]]; then
    install -m 600 -o root -g root "$TMP/.env" "$KATALOG/.env"
    echo "   .env skapad (nycklarna läggs in i webbgränssnittet)."
  else
    echo "   .env finns redan och behålls (nycklar som står där fortsätter gälla)."
  fi
  chmod 600 "$KATALOG/.env"
  satt_rad TRANSCRIBE_PUBLIC_URL "https://$doman"
  satt_rad TRANSCRIBE_DOMAIN "$doman"
  image="${TRANSCRIBE_IMAGE:-$(las_env TRANSCRIBE_IMAGE)}"
  image="${image:-$STANDARD_IMAGE}"
  [[ "$image" =~ ^[a-z0-9][a-z0-9._:/-]{0,199}$ && "$image" != *..* && "${image##*/}" != *:* ]] \
    || fel "ogiltigt avbildsnamn i TRANSCRIBE_IMAGE"

  echo "== 3/4 Startar tjänsten"
  docker network inspect smgd >/dev/null 2>&1 || docker network create smgd >/dev/null
  if docker inspect "$CONTAINER" >/dev/null 2>&1 || [[ -e "$KATALOG/pagaende-uppgradering" ]]; then
    befintlig=1
  elif docker volume inspect "$VOLYM" >/dev/null 2>&1; then
    # Containern är borttagen (t.ex. "docker compose down"), men datat finns kvar. Versionen som
    # körde startas först, så att en nyare version installeras på samma säkra sätt som annars –
    # aldrig rakt på den gamla databasen utan säkerhetskopia.
    version="$(las_env TRANSCRIBE_VERSION)"
    if ar_version "$version"; then
      echo "   Containern finns inte, men datat finns kvar – startar först version $version, som körde senast."
    else
      # Äldre installation utan versionen i .env: den avbild som redan finns här, utan att hämta.
      version=latest
      docker image inspect "$image:latest" >/dev/null 2>&1 \
        || fel "Containern finns inte, men datat finns kvar, och det går inte att avgöra vilken version som körde." \
          "Skriv versionen i $KATALOG/.env (t.ex. TRANSCRIBE_VERSION=1.0.0) och kör raden igen." "Inget har startats."
      echo "   Containern finns inte, men datat finns kvar – startar först avbilden som redan finns här."
    fi
    (cd "$KATALOG" && TRANSCRIBE_IMAGE="$image" TRANSCRIBE_VERSION="$version" docker compose up -d </dev/null)
    vanta_frisk || { visa_loggrader; fel "Versionen som körde senast blev inte frisk." "Datat är orört av raden. Titta i loggen ovan."; }
    befintlig=1
  fi
  if [[ "$befintlig" == 1 ]]; then
    # Tjänsten finns: den startas om den är stoppad, och en nyare version installeras av
    # uppdatera.sh – med säkerhetskopia och återgång, precis som den automatiska uppdateringen.
    if [[ ! -e "$KATALOG/pagaende-uppgradering" ]] && ! tjansten_kor; then
      echo "   Tjänsten är stoppad – startar den."
      docker start "$CONTAINER" >/dev/null
      vanta_frisk || { visa_loggrader; fel "Tjänsten blev inte frisk efter starten." "Inget annat har ändrats."; }
    fi
    # Ägaren kör raden själv: en version som tidigare misslyckades får prövas igen. Går lage.txt
    # inte att skriva (full disk) lämnas den som den var.
    if [[ -f "$KATALOG/lage.txt" ]]; then
      rc=0
      grep -vE '^(misslyckad_version|avbruten_version)=' "$KATALOG/lage.txt" >"$KATALOG/lage.txt.tmp" || rc=$?
      if ((rc <= 1)) && chmod 600 "$KATALOG/lage.txt.tmp"; then
        mv -f "$KATALOG/lage.txt.tmp" "$KATALOG/lage.txt"
      else
        rm -f "$KATALOG/lage.txt.tmp"
        echo "   ! $KATALOG/lage.txt kunde inte skrivas (fullt på disken?)." >&2
      fi
    fi
    flock -u 9 # uppdatera.sh tar samma lås själv
    echo "   Letar efter en nyare version:"
    bash "$KATALOG/uppdatera.sh" </dev/null || uppdatering_fel=1
  else
    # Ny installation: den senaste versionen, låst i .env till sitt versionsnummer.
    if [[ "$PULL" == 1 ]]; then
      docker pull -q "$image:latest" >/dev/null || fel "Kunde inte hämta $image:latest." "Kontrollera nätet och kör raden igen."
    fi
    version="$(docker image inspect -f "$ETIKETT" "$image:latest" 2>/dev/null || true)"
    id="$(docker image inspect -f '{{.Id}}' "$image:latest" 2>/dev/null || true)"
    if ar_version "$version" && [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      docker tag "$id" "$image:$version"
      satt_rad TRANSCRIBE_VERSION "$version"
      echo "   Version $version."
    else
      echo "   Avbilden saknar versionsetikett – startar den som \"latest\"; den automatiska uppdateringen sätter versionen sedan."
      version="$(las_env TRANSCRIBE_VERSION)"
      ar_version "$version" || version=latest
    fi
    (cd "$KATALOG" && TRANSCRIBE_IMAGE="$image" TRANSCRIBE_VERSION="$version" docker compose up -d </dev/null)
    if ! vanta_frisk; then
      visa_loggrader
      fel "Tjänsten startade men blev inte frisk inom $VANTA sekunder." \
        "Titta i loggen ovan. Kör raden igen när felet är åtgärdat."
    fi
  fi

  echo "== 4/4 Automatisk uppdatering"
  if [[ "$auto" == 0 ]]; then
    if command -v systemctl >/dev/null 2>&1; then systemctl disable --now "$ENHET.timer" >/dev/null 2>&1 || true; fi
    rm -f "$SYSTEMD_KATALOG/$ENHET.timer" "$SYSTEMD_KATALOG/$ENHET.service"
    if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
    echo "   Avstängd. Tjänsten uppdateras när du kör raden igen."
  elif systemd_finns; then
    skriv_enheter
    systemctl daemon-reload
    systemctl enable --now "$ENHET.timer" >/dev/null 2>&1 || fel "Timern $ENHET.timer kunde inte startas." "Titta i:  systemctl status $ENHET.timer"
    echo "   Påslagen: $ENHET.timer letar efter en ny version ungefär var 15:e minut."
  else
    echo "   ! Servern saknar systemd, så automatisk uppdatering kan inte slås på. Uppdatera genom att köra raden igen." >&2
    auto=0
  fi

  # Inbjudningslänken skrivs i loggen vid första starten, när inget konto finns. Bara en länk till
  # den här domänen visas – inget annat ur containerns logg.
  lank="$(docker logs --tail 300 "$CONTAINER" 2>&1 | grep -oE "https://$doman/#/inbjudan/[A-Za-z0-9_-]+" | tail -n 1 || true)"
  echo
  if [[ "$uppdatering_fel" == 1 ]] && { [[ -e "$KATALOG/pagaende-uppgradering" ]] || ! tjansten_kor; }; then
    echo "OBS: tjänsten kör INTE just nu. Uppdateringen gick inte igenom och återgången till den förra" >&2
    echo "versionen är inte klar (se ovan). Ett nytt försök görs vid nästa kontroll, eller när du kör raden igen." >&2
    echo "Läget:  sudo $KATALOG/uppdatera.sh --lage" >&2
    exit 1
  fi
  echo "Klart. Tjänsten körs och nås på https://$doman (certifikatet kan ta någon minut första gången)."
  if [[ -n "$lank" ]]; then
    echo "Skapa ditt administratörskonto med den här länken (gäller i två dygn):"
    echo "  $lank"
  else
    echo "Behöver du en ny inbjudan till administratörskontot:"
    echo "  sudo docker exec $CONTAINER node dist/cli.js inbjudan admin"
  fi
  echo "Nycklarna till Deepgram (live-text) och Groq (slutversion) läggs in i webbgränssnittet:"
  echo "  logga in som administratör → Administration → API-nycklar."
  if [[ "$auto" == 1 ]]; then
    echo "Nya versioner installeras automatiskt, med säkerhetskopia av databasen och återgång om något går fel."
  fi
  echo "Läget för uppdateringarna:  sudo $KATALOG/uppdatera.sh --lage"
  if [[ "$uppdatering_fel" == 1 ]]; then
    echo
    echo "OBS: uppdateringen till den nya versionen gick inte igenom (se ovan). Tjänsten kör på den förra versionen." >&2
    exit 1
  fi
}

# Hela skriptet läses in innan något körs (det kommer via ett rör från curl).
main "$@"
