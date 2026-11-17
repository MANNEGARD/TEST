#!/usr/bin/env bash
# Prov av installera.sh (installationsraden) mot låtsaskommandon. Se lib.sh.
#   sudo bash transcribe/prov/prov_installera.sh
# Raden körs som ägaren kör den: skriptet kommer på standard in (curl … | sudo bash -s -- …), och
# filerna hämtas med file:// ur en låtsad kopia av repot (TRANSCRIBE_REPO_RAW).
# Avsiktligt bokstavliga $( … ) i provdatan; KOD m.fl. läses av lib.sh.
# shellcheck source-path=SCRIPTDIR disable=SC2016,SC2034
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

HEMLIG=hemlig-provnyckel
DL=drivelog.example.com
TR=transcribe.example.com

# Den låtsade kopian av repot: DriveLogs skript skriver bara ned hur det anropades.
kalla() {
  KALLA="$VARLD/kalla"
  mkdir -p "$KALLA/deploy" "$KALLA/transcribe"
  cat >"$KALLA/deploy/server.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$PROV_TILL/drivelog.log"
echo "   (DriveLogs skript – låtsas)"
[[ ! -e "$PROV_TILL/drivelog_fel" ]]
EOF
  cp "$SKRIPT_KATALOG/installera.sh" "$SKRIPT_KATALOG/docker-compose.yml" "$SKRIPT_KATALOG/uppdatera.sh" "$KALLA/transcribe/"
  cp "$REPO_TRANSCRIBE/.env.example" "$KALLA/transcribe/.env.example"
  export TRANSCRIBE_REPO_RAW="file://$KALLA"
}
installera() { # argument …
  KOD=0
  UT="$(cat "$SKRIPT_KATALOG/installera.sh" | env PATH="$LATSAS:$PATH" bash -s -- "$@" 2>&1)" || KOD=$?
  printf '%s\n' "$UT" >>"$VARLD/utskrift.log"
}
gammal_compose() { # som filen som installerades med 1.0.0
  sed -e '/^[[:space:]]*#/d' -e 's/^\(    image:\).*/\1 ghcr.io\/smgd-mp\/transcribe:latest/' \
    "$SKRIPT_KATALOG/docker-compose.yml" >"$KAT/docker-compose.yml"
}
timern_pa() {
  [[ -f "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.timer" && -f "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.service" ]] \
    || { echo "    enheterna saknas"; return 1; }
  [[ -e "$T/systemd/active-transcribe-uppdatering.timer" && -e "$T/systemd/enabled-transcribe-uppdatering.timer" ]] \
    || { echo "    timern är inte påslagen"; return 1; }
}
ingen_hemlighet_visad() {
  if grep -qF "$HEMLIG" "$VARLD/utskrift.log" "$T/anrop.log" 2>/dev/null; then
    echo "    nyckeln från .env syns i en utskrift eller ett kommando"
    return 1
  fi
}

s_ny_server() {
  ny_varld inst-ny
  kalla
  erbjud 1.1.0
  echo "https://$TR/#/inbjudan/abcDEF123_-x" >"$T/inbjudan"
  installera "$DL" "$TR"
  kod_ar 0
  [[ "$(cat "$T/drivelog.log")" == "$DL --extra-doman $TR=transcribe:8080" ]]
  cmp "$KAT/docker-compose.yml" "$SKRIPT_KATALOG/docker-compose.yml"
  cmp "$KAT/uppdatera.sh" "$SKRIPT_KATALOG/uppdatera.sh"
  rattigheter "$KAT/uppdatera.sh" "700 root"
  rattigheter "$KAT/.env" "600 root"
  env_har "TRANSCRIBE_PUBLIC_URL=https://$TR"
  env_har "TRANSCRIBE_DOMAIN=$TR"
  env_har "TRANSCRIBE_VERSION=1.1.0"
  [[ "$(grep -c '^TRANSCRIBE_PUBLIC_URL=' "$KAT/.env")" == 1 ]]
  anrop_har "docker network create smgd"
  anrop_har "docker tag $(bild_id v1.1.0) $IMG:1.1.0"
  anrop_har "compose-ref $IMG:1.1.0"
  version_ar 1.1.0
  kor_igang
  timern_pa
  grep -qxF "ExecStart=/bin/bash $KAT/uppdatera.sh --tidsstyrd" "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.service"
  grep -qxF "Environment=TRANSCRIBE_KATALOG=$KAT" "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.service"
  grep -qxF "Type=oneshot" "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.service"
  grep -qxF "OnUnitInactiveSec=15min" "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.timer"
  if command -v systemd-analyze >/dev/null 2>&1; then
    (cd "$TRANSCRIBE_SYSTEMD_DIR" && systemd-analyze verify ./transcribe-uppdatering.service ./transcribe-uppdatering.timer)
  fi
  ut_har "https://$TR/#/inbjudan/abcDEF123_-x"
  ut_har "Administration → API-nycklar"
  ut_har "Nya versioner installeras automatiskt"
  ut_har "sudo $KAT/uppdatera.sh --lage"
  ut_saknar "Nyckel"
  inga_okanda_anrop
}

s_dagens_server() {
  # Som servern ser ut idag: 1.0.0 utan etikett igång, gammal docker-compose.yml, .env med
  # nyckel men utan TRANSCRIBE_VERSION, ingen timer, DriveLog tar redan emot domänen.
  ny_varld inst-idag
  kalla
  installation 1.0.0-utan
  gammal_compose
  sed -i '/^TRANSCRIBE_VERSION=/d' "$KAT/.env"
  rm -f "$KAT/uppdatera.sh"
  cp "$KAT/.env" "$VARLD/env.fore"
  echo ok >"$T/https"
  erbjud 1.1.0
  installera "$DL" "$TR"
  kod_ar 0
  ut_har "https://$TR svarar redan med giltigt certifikat – DriveLog behöver inte ändras"
  [[ ! -e "$T/drivelog.log" ]]
  ut_har ".env finns redan och behålls"
  env_har "DEEPGRAM_API_KEY=$HEMLIG"
  env_har "TRANSCRIBE_VERSION=1.1.0"
  diff <(grep -v '^TRANSCRIBE_VERSION=' "$VARLD/env.fore") <(grep -v '^TRANSCRIBE_VERSION=' "$KAT/.env")
  version_ar 1.1.0
  ls -d "$KAT"/kopior/*-1.0.0 >/dev/null
  [[ "$(cat "$KAT"/kopior/*-1.0.0/transcribe.db)" == "databas från 1.0.0" ]]
  timern_pa
  ut_har "sudo docker exec transcribe node dist/cli.js inbjudan admin"
  ingen_hemlighet_visad
  inga_okanda_anrop
}

s_andra_gangen() {
  ny_varld inst-igen
  kalla
  erbjud 1.1.0
  installera "$DL" "$TR"
  kod_ar 0
  echo ok >"$T/https"
  cp "$KAT/.env" "$VARLD/env.fore"
  local fore
  fore="$(starter)"
  installera "$DL" "$TR"
  kod_ar 0
  ut_har "Ingen ny version (kör 1.1.0, erbjuds 1.1.0)."
  [[ "$(starter)" == "$fore" ]]
  cmp "$KAT/.env" "$VARLD/env.fore"
  timern_pa
  [[ "$(grep -c . "$T/drivelog.log")" == 1 ]]
}

s_utan_automatisk_uppdatering() {
  ny_varld inst-utan
  kalla
  erbjud 1.1.0
  installera "$DL" "$TR"
  timern_pa
  installera "$DL" "$TR" --utan-automatisk-uppdatering
  kod_ar 0
  ut_har "Avstängd. Tjänsten uppdateras när du kör raden igen."
  [[ ! -e "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.timer" && ! -e "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.service" ]]
  [[ ! -e "$T/systemd/active-transcribe-uppdatering.timer" && ! -e "$T/systemd/enabled-transcribe-uppdatering.timer" ]]
  kor_igang
  [[ -x "$KAT/uppdatera.sh" ]]
  ut_saknar "Nya versioner installeras automatiskt"
  # … och på igen.
  installera "$DL" "$TR"
  timern_pa
}

s_misslyckad_provas_igen() {
  ny_varld inst-igen-misslyckad
  kalla
  installation 1.1.0
  printf 'misslyckad_version=1.2.0\nsenaste_kontroll=x\n' >"$KAT/lage.txt"
  erbjud 1.2.0
  installera "$DL" "$TR"
  kod_ar 0
  version_ar 1.2.0
  lage_saknar misslyckad_version
}

s_uppdateringen_misslyckas() {
  ny_varld inst-uppd-fel
  kalla
  installation 1.1.0
  erbjud 1.2.0 trasig
  installera "$DL" "$TR"
  kod_ar 1
  ut_har "OBS: uppdateringen till den nya versionen gick inte igenom"
  version_ar 1.1.0
  kor_igang
  timern_pa
}

s_stoppad_tjanst_startas() {
  ny_varld inst-stoppad
  kalla
  installation 1.1.0
  echo exited >"$T/container/transcribe/status"
  erbjud 1.1.0
  installera "$DL" "$TR"
  kod_ar 0
  ut_har "Tjänsten är stoppad – startar den."
  anrop_har "docker start transcribe"
  kor_igang
}

s_ny_server_blir_inte_frisk() {
  ny_varld inst-trasig
  kalla
  erbjud 1.1.0 trasig
  mkdir -p "$KAT"
  printf 'DEEPGRAM_API_KEY=%s\n' "$HEMLIG" >"$KAT/.env"
  installera "$DL" "$TR"
  kod_ar 1
  ut_har "Tjänsten startade men blev inte frisk"
  ut_har "De sista raderna i tjänstens logg:"
  ingen_hemlighet_visad
  [[ ! -e "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.timer" ]]
}

s_trasig_hamtning_andrar_inget() {
  ny_varld inst-trasig-hamtning
  kalla
  installation 1.1.0
  cp "$KAT/docker-compose.yml" "$VARLD/compose.fore"
  cp "$KAT/uppdatera.sh" "$VARLD/uppdatera.fore"
  cp "$KAT/.env" "$VARLD/env.fore"
  printf '#!/usr/bin/env bash\n# --tidsstyrd\nif then fi (\n' >"$KALLA/transcribe/uppdatera.sh"
  installera "$DL" "$TR"
  kod_ar 1
  ut_har "Den hämtade uppdatera.sh är inte hel."
  cmp "$KAT/docker-compose.yml" "$VARLD/compose.fore"
  cmp "$KAT/uppdatera.sh" "$VARLD/uppdatera.fore"
  cmp "$KAT/.env" "$VARLD/env.fore"
  # En compose-fil utan TRANSCRIBE_VERSION tas inte heller emot.
  cp "$SKRIPT_KATALOG/uppdatera.sh" "$KALLA/transcribe/uppdatera.sh"
  printf 'services: {}\n' >"$KALLA/transcribe/docker-compose.yml"
  installera "$DL" "$TR"
  kod_ar 1
  cmp "$KAT/docker-compose.yml" "$VARLD/compose.fore"
  # Saknas filen helt.
  rm "$KALLA/transcribe/docker-compose.yml"
  installera "$DL" "$TR"
  kod_ar 1
  ut_har "Kunde inte hämta"
  cmp "$KAT/docker-compose.yml" "$VARLD/compose.fore"
}

s_utan_systemd() {
  ny_varld inst-utan-systemd
  kalla
  touch "$T/utan_systemd"
  erbjud 1.1.0
  installera "$DL" "$TR"
  kod_ar 0
  ut_har "Servern saknar systemd"
  [[ ! -e "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.timer" ]]
  kor_igang
}

s_vantar_pa_laset() {
  ny_varld inst-las
  kalla
  erbjud 1.1.0
  flock "$TRANSCRIBE_LAS" /bin/sleep 1 &
  local p=$!
  /bin/sleep 0.2
  installera "$DL" "$TR"
  wait "$p"
  kod_ar 0
  ut_har "En automatisk uppdatering pågår just nu – väntar tills den är klar"
  kor_igang
}

s_env_med_konstiga_rader() {
  ny_varld inst-env
  kalla
  installation 1.1.0
  {
    printf 'LIKA=a=b=c\n'
    printf 'CITAT="med blanksteg"\n'
    printf 'DOLLAR=$(rm -rf /)$PATH\n'
    printf 'CRLF=vagnretur\r\n'
    printf 'TRANSCRIBE_PUBLIC_URL=https://gammal.example.com\n'
    printf 'TRANSCRIBE_PUBLIC_URL=https://dubblett.example.com\n'
    printf 'DEEPGRAM_API_KEY=%s\n' "$HEMLIG"
    printf 'TRANSCRIBE_VERSION=1.1.0\n'
  } >"$KAT/.env"
  chmod 600 "$KAT/.env"
  cp "$KAT/.env" "$VARLD/env.fore"
  erbjud 1.1.0
  installera "$DL" "$TR"
  kod_ar 0
  [[ "$(grep -c '^TRANSCRIBE_PUBLIC_URL=' "$KAT/.env")" == 1 ]]
  env_har "TRANSCRIBE_PUBLIC_URL=https://$TR"
  diff <(grep -avE '^(TRANSCRIBE_PUBLIC_URL|TRANSCRIBE_DOMAIN)=' "$VARLD/env.fore") \
    <(grep -avE '^(TRANSCRIBE_PUBLIC_URL|TRANSCRIBE_DOMAIN)=' "$KAT/.env")
  ingen_hemlighet_visad
}

s_felaktiga_argument() {
  ny_varld inst-arg
  kalla
  installera "$DL"
  kod_ar 1
  ut_har "ange DriveLogs domän och transkriberingens domän"
  installera "$TR" "$TR"
  kod_ar 1
  installera "$DL" "https://$TR/"
  kod_ar 1
  ut_har "ogiltig domän"
  installera "$DL" "$TR" --okant
  kod_ar 1
  ut_har "okänt val"
  installera "$DL" "$TR" "tredje.example.com"
  kod_ar 1
  [[ ! -e "$KAT" ]]
  [[ ! -s "$T/anrop.log" ]] || inte grep -q "^docker" "$T/anrop.log"
}

echo "installera.sh ($SKRIPT_KATALOG/installera.sh)"
scenario "ny server" s_ny_server
scenario "servern som den ser ut idag (1.0.0, gammal compose)" s_dagens_server
scenario "en andra gång" s_andra_gangen
scenario "--utan-automatisk-uppdatering" s_utan_automatisk_uppdatering
scenario "misslyckad version prövas igen när raden körs" s_misslyckad_provas_igen
scenario "uppdateringen misslyckas → förra versionen kör" s_uppdateringen_misslyckas
scenario "stoppad tjänst startas" s_stoppad_tjanst_startas
scenario "ny server, tjänsten blir inte frisk" s_ny_server_blir_inte_frisk
scenario "trasig hämtning ändrar ingenting" s_trasig_hamtning_andrar_inget
scenario "server utan systemd" s_utan_systemd
scenario "väntar på låset" s_vantar_pa_laset
scenario ".env med konstiga rader" s_env_med_konstiga_rader
scenario "felaktiga argument" s_felaktiga_argument
summera "installera.sh"
