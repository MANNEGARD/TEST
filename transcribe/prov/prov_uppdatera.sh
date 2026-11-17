#!/usr/bin/env bash
# Prov av uppdatera.sh (automatisk uppdatering) mot låtsaskommandon. Se lib.sh.
#   sudo bash transcribe/prov/prov_uppdatera.sh
# Avsiktligt bokstavliga $( … ) i provdatan; KOD m.fl. läses av lib.sh.
# shellcheck source-path=SCRIPTDIR disable=SC2016,SC2034
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

HEMLIG=hemlig-provnyckel

ingen_hemlighet_visad() {
  if grep -qF "$HEMLIG" "$VARLD/utskrift.log" "$KAT/uppdatering.log" "$KAT/lage.txt" "$T/anrop.log" 2>/dev/null; then
    echo "    nyckeln från .env syns i en utskrift, loggen, läget eller ett kommando"
    return 1
  fi
}

s_ingen_ny_version() {
  ny_varld ingen-ny
  installation 1.1.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Ingen ny version (kör 1.1.0, erbjuds 1.1.0)."
  [[ "$(starter)" == 1 ]]
  anrop_saknar "docker stop"
  anrop_saknar "compose up"
  grep -q "^senaste_kontroll=.*ingen ny version (kör 1.1.0, erbjuds 1.1.0)$" "$KAT/lage.txt"
  logg_har "Kontroll startad (tidsstyrd)."
  [[ -z "$(kopior)" ]]
  ingen_markering
  inga_okanda_anrop
}

s_ny_version_lyckas() {
  ny_varld lyckas
  installation 1.1.0
  # Fyra äldre kopior (äldre ändringstid) och två äldre avbilder som ska städas bort.
  for d in 1 2 3 4; do
    mkdir -p "$KAT/kopior/2026010${d}T000000-1.0.${d}"
    touch -d "2026-01-0$d" "$KAT/kopior/2026010${d}T000000-1.0.${d}"
  done
  lokal "$IMG:0.9.0" "$(avbild v0.9.0 0.9.0)"
  lokal "$IMG:1.0.0" "$(avbild v1.0.0 1.0.0)"
  cp "$KAT/.env" "$VARLD/env.fore"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Klart: SMGD Transkribering 1.2.0 är igång och frisk."
  version_ar 1.2.0
  kor_igang
  anrop_har "docker stop -t 30 transcribe"
  anrop_har "compose-ref $IMG:1.2.0"
  # Båda versionerna märktes lokalt.
  grep -q "^$IMG:1.2.0 $(bild_id v1.2.0)$" "$T/taggar"
  grep -q "^$IMG:1.1.0 $(bild_id v1.1.0)$" "$T/taggar"
  # Säkerhetskopian: databasfilerna, inte ljudet, 700 root.
  local k
  k="$(ls -d "$KAT"/kopior/*-1.1.0)"
  [[ "$(cat "$k/transcribe.db")" == "databas från 1.1.0" ]]
  [[ -f "$k/transcribe.db-wal" && -f "$k/secret.key" && ! -e "$k/audio" && ! -e "$k/transcribe.db-shm" ]]
  rattigheter "$k" "700 root"
  rattigheter "$KAT/kopior" "700 root"
  # .env: nya versionen, övriga rader orörda, 600.
  env_har "TRANSCRIBE_VERSION=1.2.0"
  diff <(grep -v '^TRANSCRIBE_VERSION=' "$VARLD/env.fore") <(grep -v '^TRANSCRIBE_VERSION=' "$KAT/.env")
  [[ "$(grep -c '^TRANSCRIBE_VERSION=' "$KAT/.env")" == 1 ]]
  rattigheter "$KAT/.env" "600 root"
  grep -q "^senaste_uppgradering=.* – 1.1.0 → 1.2.0$" "$KAT/lage.txt"
  # Tre kopior sparas (den nya och de två nyaste äldre), äldre avbilder städas bort.
  [[ "$(kopior | wc -l)" == 3 ]]
  [[ -d "$KAT/kopior/20260104T000000-1.0.4" && -d "$KAT/kopior/20260103T000000-1.0.3" ]]
  [[ ! -e "$KAT/kopior/20260101T000000-1.0.1" ]]
  inte grep -q "^$IMG:0.9.0 " "$T/taggar"
  inte grep -q "^$IMG:1.0.0 " "$T/taggar"
  grep -q "^$IMG:latest " "$T/taggar"
  ingen_markering
  ingen_hemlighet_visad
  inga_okanda_anrop
}

s_trasig_version_atergang() {
  ny_varld trasig
  installation 1.1.0
  erbjud 1.2.0 trasig
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Version 1.2.0 fungerade inte (containern startades om gång på gång). 1.1.0 kör igen"
  ut_har "1.2.0 prövas inte igen förrän en nyare version erbjuds eller du kör installationsraden."
  version_ar 1.1.0
  kor_igang
  # Databasen är den från före försöket (den trasiga versionens "migrering" är borta).
  [[ "$(cat "$VOL/transcribe.db")" == $'databas från 1.1.0\nstart 1.1.0' ]]
  [[ "$(cat "$VOL/secret.key")" == nyckel && -f "$VOL/transcribe.db-wal" && -f "$VOL/audio/1.m4a" ]]
  env_har "TRANSCRIBE_VERSION=1.1.0"
  lage_har "misslyckad_version=1.2.0"
  grep -q "^senaste_fel=.*Version 1.2.0 fungerade inte" "$KAT/lage.txt"
  logg_har "Återgick till 1.1.0 med databasen från före försöket."
  ingen_markering
  # Nästa kontroll: samma version prövas inte igen.
  local fore
  fore="$(starter)"
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Version 1.2.0 fungerade inte förra gången – försöker inte igen (kör 1.1.0)."
  [[ "$(starter)" == "$fore" ]]
  # En ännu nyare version prövas.
  erbjud 1.3.0
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.3.0
  lage_saknar misslyckad_version
  ingen_hemlighet_visad
  inga_okanda_anrop
}

s_hanger_och_sjuk_atergang() {
  ny_varld hanger
  installation 1.1.0
  erbjud 1.2.0 hanger
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "den blev inte frisk inom 10 sekunder"
  version_ar 1.1.0
  kor_igang
  [[ "$(cat "$VOL/transcribe.db")" == $'databas från 1.1.0\nstart 1.1.0' ]]
  lage_har "misslyckad_version=1.2.0"
  erbjud 1.2.1 sjuk
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "unhealthy"
  version_ar 1.1.0
  lage_har "misslyckad_version=1.2.1"
  [[ "$(cat "$VOL/transcribe.db")" == $'databas från 1.1.0\nstart 1.1.0\nstart 1.1.0' ]]
  inga_okanda_anrop
}

s_upptagen_vantar() {
  ny_varld upptagen
  installation 1.1.0
  echo "10 upptagen: 1 inspelning pågår" >"$T/upptagen"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Ny version 1.2.0 finns, men tjänsten är upptagen (upptagen: 1 inspelning pågår) – väntar till nästa kontroll."
  version_ar 1.1.0
  anrop_saknar "docker stop"
  grep -q "^senaste_kontroll=.*1.2.0 väntar: tjänsten är upptagen" "$KAT/lage.txt"
  # När den blir ledig uppgraderas den.
  echo "0 ledig" >"$T/upptagen"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
}

s_v100_utan_etikett_och_utan_upptagen() {
  ny_varld v100
  installation 1.0.0-utan
  # Äldre installation: ingen TRANSCRIBE_VERSION i .env.
  sed -i '/^TRANSCRIBE_VERSION=/d' "$KAT/.env"
  erbjud 1.1.0
  uppdatera
  kod_ar 0
  ut_har "TRANSCRIBE_VERSION i .env är nu 1.0.0 (versionen som kör)."
  anrop_har "docker exec transcribe node -p require('/app/package.json').version"
  anrop_har "docker exec transcribe node dist/cli.js upptagen"
  anrop_har "docker tag $(bild_id v1.0.0-utan) $IMG:1.0.0"
  version_ar 1.1.0
  env_har "TRANSCRIBE_VERSION=1.1.0"
  ls -d "$KAT"/kopior/*-1.0.0 >/dev/null
  logg_har "Kontroll startad (för hand)."
  inga_okanda_anrop
}

s_atergang_till_v100_utan_etikett() {
  ny_varld v100-atergang
  installation 1.0.0-utan
  erbjud 1.1.0 trasig
  uppdatera --tidsstyrd
  kod_ar 1
  version_ar 1.0.0
  kor_igang
  env_har "TRANSCRIBE_VERSION=1.0.0"
  anrop_har "compose-ref $IMG:1.0.0"
  logg_har "Återgick till 1.0.0"
  ingen_markering
}

s_v100_ingen_ny_utan_etikett() {
  # Innan 1.1.0 har getts ut är latest samma avbild som 1.0.0 (utan etikett): inget nytt, inget fel.
  ny_varld v100-samma
  installation 1.0.0-utan
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Ingen ny version (kör 1.0.0, erbjuds 1.0.0)."
  anrop_saknar "docker stop"
  lage_saknar senaste_fel
}

s_container_stoppad() {
  ny_varld stoppad
  installation 1.1.0
  echo exited >"$T/container/transcribe/status"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Tjänsten (transcribe) kör inte – ingen uppdatering"
  anrop_saknar "docker pull"
  anrop_saknar "docker start"
  anrop_saknar "compose up"
  [[ "$(cat "$T/container/transcribe/status")" == exited ]]
}

s_latest_utan_etikett() {
  ny_varld utan-etikett
  installation 1.1.0
  fjarr "$IMG:latest" "$(avbild latest-utan - frisk 1.2.0)"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "har ingen giltig versionsetikett (X.Y.Z) – inget har ändrats."
  anrop_saknar "docker stop"
  version_ar 1.1.0
  grep -q "^senaste_fel=.*ingen giltig versionsetikett" "$KAT/lage.txt"
}

s_konstiga_etiketter() {
  ny_varld konstiga
  installation 1.1.0
  local e i=0 offer="$VARLD/offer-fil"
  echo orörd >"$offer"
  for e in "1.2.0 " $'1.2.0\n' $'1.2.0\n9.9.9' "../1.2.0" "1.2.0; rm -rf $offer" "1.2.0/../../x" "\$(rm $offer)" "v1.2.0" "1.2" "1.2.0.1" "1234567.0.0" "-1.2.0"; do
    i=$((i + 1))
    fjarr "$IMG:latest" "$(avbild "konstig$i" "$e" frisk 1.2.0)"
    uppdatera --tidsstyrd
    kod_ar 1
    ut_har "har ingen giltig versionsetikett"
  done
  anrop_saknar "docker stop"
  anrop_saknar "docker tag"
  [[ "$(cat "$offer")" == orörd ]]
  version_ar 1.1.0
  [[ ! -e "$KAT/kopior" || -z "$(kopior)" ]]
  # Konstig etikett på den som kör: inget ändras.
  ny_varld konstig-korande
  installation 1.1.0
  printf '%s' "1.1.0; rm -rf /" >"$T/bilder/$(bild_id v1.1.0 | sed 's/^sha256://')/etikett"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Kan inte avgöra vilken version som kör"
  anrop_saknar "docker stop"
  inga_okanda_anrop
}

s_hamtning_misslyckas() {
  ny_varld hamtning
  installation 1.1.0
  erbjud 1.2.0
  touch "$T/fel/pull"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Kunde inte hämta $IMG:latest – inget har ändrats."
  anrop_saknar "docker stop"
  version_ar 1.1.0
}

s_avbrott_vid_stopp_strom() {
  ny_varld avbrott-stopp
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "stop -t 30" --tidsstyrd
  avbryt KILL
  grep -qx "fas=stoppad" "$KAT/pagaende-uppgradering"
  # En halvfärdig kopia (som om kopieringen hann börja).
  local kopia
  kopia="$(sed -n 's/^kopia=//p' "$KAT/pagaende-uppgradering")"
  mkdir -p "$kopia.tmp" && echo halv >"$kopia.tmp/transcribe.db"
  echo exited >"$T/container/transcribe/status" # som efter en omstart av servern
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Uppgraderingen till 1.2.0 avbröts innan något ändrades; 1.1.0 kör igen."
  version_ar 1.1.0
  kor_igang
  ingen_markering
  [[ ! -e "$kopia.tmp" && ! -e "$kopia" ]]
  # Nästa kontroll gör uppgraderingen.
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
}

s_avbrott_vid_stopp_term() {
  ny_varld avbrott-stopp-term
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "stop -t 30" --tidsstyrd
  avbryt TERM
  # systemd stoppade tjänsten innan något ändrats: den förra versionen startas direkt igen.
  ingen_markering
  version_ar 1.1.0
  kor_igang
  grep -q "^senaste_fel=.*avbröts (TERM) innan något ändrades; 1.1.0 kör igen" "$KAT/lage.txt"
  logg_har "AVBRUTEN (TERM) under fasen stoppad."
}

s_avbrott_fore_start_av_ny() {
  ny_varld avbrott-provar
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "compose up" --tidsstyrd
  avbryt KILL
  grep -qx "fas=provar" "$KAT/pagaende-uppgradering"
  env_har "TRANSCRIBE_VERSION=1.2.0"
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Återgick till 1.1.0"
  version_ar 1.1.0
  kor_igang
  env_har "TRANSCRIBE_VERSION=1.1.0"
  ingen_markering
  lage_har "avbruten_version=1.2.0"
  lage_saknar misslyckad_version
  # Ett nytt försök görs (avbrottet var inte versionens fel).
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
  lage_saknar avbruten_version
}

s_avbrott_under_vantan_ny_blir_frisk() {
  ny_varld avbrott-vantan
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "inspect -f {{.State.Status}}" --tidsstyrd
  avbryt KILL
  grep -qx "fas=provar" "$KAT/pagaende-uppgradering"
  version_ar 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Den nya versionen 1.2.0 blev frisk – uppgraderingen gjordes klar."
  version_ar 1.2.0
  grep -q "^senaste_uppgradering=.* – 1.1.0 → 1.2.0$" "$KAT/lage.txt"
  ingen_markering
  ls -d "$KAT"/kopior/*-1.1.0 >/dev/null
}

s_avbrott_under_vantan_ny_hanger() {
  ny_varld avbrott-hanger
  installation 1.1.0
  erbjud 1.2.0 hanger
  starta_i_bakgrunden 1 "inspect -f {{.State.Status}}" --tidsstyrd
  avbryt KILL
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.1.0
  [[ "$(cat "$VOL/transcribe.db")" == $'databas från 1.1.0\nstart 1.1.0' ]]
  lage_har "misslyckad_version=1.2.0"
  ingen_markering
}

s_avbrott_under_vantan_term() {
  ny_varld avbrott-vantan-term
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "inspect -f {{.State.Status}}" --tidsstyrd
  avbryt TERM
  grep -qx "fas=provar" "$KAT/pagaende-uppgradering"
  grep -q "^senaste_fel=.*avbröts (TERM); den görs klar vid nästa kontroll" "$KAT/lage.txt"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
  ingen_markering
}

s_avbrott_under_atergang() {
  ny_varld avbrott-atergang
  installation 1.1.0
  erbjud 1.2.0 trasig
  starta_i_bakgrunden 2 "compose up" --tidsstyrd
  avbryt KILL
  grep -qx "fas=aterstaller" "$KAT/pagaende-uppgradering"
  lage_har "misslyckad_version=1.2.0"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.1.0
  kor_igang
  [[ "$(cat "$VOL/transcribe.db")" == $'databas från 1.1.0\nstart 1.1.0' ]]
  ingen_markering
  grep -q "^senaste_fel=.*återgången från 1.2.0 avbröts och är nu klar" "$KAT/lage.txt"
  # Nästa kontroll hoppar över den trasiga versionen.
  uppdatera --tidsstyrd
  ut_har "fungerade inte förra gången"
}

s_atergang_som_misslyckas_forsoks_igen() {
  ny_varld atergang-fel
  installation 1.1.0
  erbjud 1.2.0 trasig
  # Den förra versionen startar inte heller (t.ex. Docker krånglar): markeringen ligger kvar.
  echo trasig >"$T/bilder/$(bild_id v1.1.0 | sed 's/^sha256://')/beteende"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "återgången till 1.1.0 blev inte heller frisk"
  grep -qx "fas=aterstaller" "$KAT/pagaende-uppgradering"
  # Nästa körning försöker igen – nu går det.
  echo frisk >"$T/bilder/$(bild_id v1.1.0 | sed 's/^sha256://')/beteende"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.1.0
  ingen_markering
}

s_tva_korningar_samtidigt() {
  ny_varld samtidigt
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "docker pull" --tidsstyrd
  # Timern hoppar över medan en körning pågår.
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "pågår redan – den här kontrollen hoppas över"
  # En körning för hand väntar.
  env PATH="$LATSAS:$PATH" bash "$SKRIPT_KATALOG/uppdatera.sh" >"$VARLD/hand.log" 2>&1 </dev/null &
  local hand=$! i
  for ((i = 0; i < 100; i++)); do grep -q "väntar tills den är klar" "$VARLD/hand.log" && break; /bin/sleep 0.05; done
  grep -q "En annan körning pågår – väntar tills den är klar" "$VARLD/hand.log"
  kill -0 "$hand"
  slapp
  kod_ar 0
  version_ar 1.2.0
  wait "$hand"
  grep -q "Ingen ny version (kör 1.2.0, erbjuds 1.2.0)." "$VARLD/hand.log"
}

s_symlank_i_datakatalogen() {
  ny_varld symlank
  installation 1.1.0
  echo hemligt-innehall >"$VARLD/utanfor"
  rm -f "$VOL/transcribe.db-wal"
  ln -s "$VARLD/utanfor" "$VOL/transcribe.db-wal"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "transcribe.db-wal i datakatalogen är inte en vanlig fil"
  ut_har "Inget har ändrats; 1.1.0 startades igen."
  version_ar 1.1.0
  kor_igang
  ingen_markering
  inte grep -rq hemligt-innehall "$KAT/kopior" 2>/dev/null
  [[ "$(cat "$VARLD/utanfor")" == hemligt-innehall && -L "$VOL/transcribe.db-wal" ]]
}

s_planterad_lank_foljs_inte_vid_atergang() {
  ny_varld planterad
  installation 1.1.0
  echo offrets-innehall >"$T/offer"
  erbjud 1.2.0 trasig
  touch "$T/bilder/$(bild_id v1.2.0-trasig | sed 's/^sha256://')/planterar"
  uppdatera --tidsstyrd
  kod_ar 1
  version_ar 1.1.0
  # Länken som den trasiga versionen lade dit är borttagen; filen den pekade på är orörd.
  [[ -f "$VOL/secret.key" && ! -L "$VOL/secret.key" && "$(cat "$VOL/secret.key")" == nyckel ]]
  [[ "$(cat "$T/offer")" == offrets-innehall ]]
}

s_env_med_konstiga_rader() {
  ny_varld env-konstig
  installation 1.1.0
  {
    printf '# kommentar med = och "citat"\n'
    printf 'TRANSCRIBE_PUBLIC_URL=https://transcribe.example.com\n'
    printf 'LIKA=a=b=c\n'
    printf 'CITAT="värde med blanksteg och # tecken"\n'
    printf "ENKEL='\$HOME/inte/utbytt'\n"
    printf 'DOLLAR=$(rm -rf /)$PATH\n'
    printf 'CRLF=rad med vagnretur\r\n'
    printf 'export TRANSCRIBE_VERSION=1.1.0\n'
    printf '\n'
    printf 'DEEPGRAM_API_KEY=%s\n' "$HEMLIG"
    printf 'TRANSCRIBE_VERSION=1.1.0\n'
    printf 'SIST_UTAN_RADSLUT=x'
  } >"$KAT/.env"
  chmod 600 "$KAT/.env"
  cp "$KAT/.env" "$VARLD/env.fore"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
  [[ "$(grep -cE '^(export )?TRANSCRIBE_VERSION=' "$KAT/.env")" == 1 ]]
  env_har "TRANSCRIBE_VERSION=1.2.0"
  # Alla andra rader är byte för byte desamma (sista raden får ett radslut).
  diff <(grep -avE '^(export )?TRANSCRIBE_VERSION=' "$VARLD/env.fore") <(grep -avE '^(export )?TRANSCRIBE_VERSION=' "$KAT/.env")
  grep -aq $'^CRLF=rad med vagnretur\r$' "$KAT/.env"
  rattigheter "$KAT/.env" "600 root"
  ingen_hemlighet_visad
  [[ -z "$(find "$KAT" -maxdepth 1 -name '.env.*')" ]]
}

s_env_crlf_pa_versionsraden() {
  ny_varld env-crlf
  installation 1.1.0
  sed -i 's/^TRANSCRIBE_VERSION=1.1.0$/TRANSCRIBE_VERSION=1.1.0\r/' "$KAT/.env"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_saknar "TRANSCRIBE_VERSION i .env är nu"
  grep -aq $'^TRANSCRIBE_VERSION=1.2.0\r$' "$KAT/.env"
  version_ar 1.2.0
}

s_gammal_compose_fil() {
  ny_varld gammal-compose
  installation 1.1.0
  # Som filen som installerades med 1.0.0: fast "latest", ingen variabel.
  sed -i -e '/^[[:space:]]*#/d' -e 's/^\(    image:\).*/\1 ghcr.io\/smgd-mp\/transcribe:latest/' "$KAT/docker-compose.yml"
  inte grep -q TRANSCRIBE_VERSION "$KAT/docker-compose.yml"
  cp "$KAT/.env" "$VARLD/env.fore"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Kör installationsraden en gång till"
  anrop_saknar "docker pull"
  anrop_saknar "docker stop"
  cmp "$KAT/.env" "$VARLD/env.fore"
}

s_trasig_markering() {
  ny_varld trasig-markering
  installation 1.1.0
  printf 'fas=provar\ngammal=1.1.0\nny=1.2.0\nkopia=/etc\ndata=%s\ncontainer=transcribe\n' "$VOL" >"$KAT/pagaende-uppgradering"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "går inte att läsa"
  anrop_saknar "docker stop"
  anrop_saknar "docker rm"
  printf 'fas=provar\ngammal=1.1.0\nny=1.2.0\nkopia=%s/kopior/20260101T000000-1.1.0\ndata=$(rm -rf /)\ncontainer=transcribe\n' "$KAT" >"$KAT/pagaende-uppgradering"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "går inte att läsa"
  version_ar 1.1.0
}

s_lage_i_klartext() {
  ny_varld lage
  installation 1.1.0
  uppdatera --lage
  kod_ar 0
  ut_har "Automatisk uppdatering:  av"
  ut_har "Kör version:             1.1.0 (igång och frisk)"
  ut_har "Senaste kontroll:        ingen än"
  ut_har "Säkerhetskopior:         inga än"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  erbjud 1.3.0 trasig
  uppdatera --tidsstyrd
  touch "$TRANSCRIBE_SYSTEMD_DIR/transcribe-uppdatering.timer" "$T/systemd/active-transcribe-uppdatering.timer"
  : >"$T/anrop.log"
  local fore
  fore="$(find "$KAT" -type f -exec md5sum {} + | sort)"
  uppdatera --lage
  kod_ar 0
  ut_har "Automatisk uppdatering:  på (letar efter en ny version ungefär var 15:e minut)"
  ut_har "Kör version:             1.2.0 (igång och frisk)"
  ut_har "Senaste uppgradering:"
  ut_har "1.1.0 → 1.2.0"
  ut_har "Hoppar över version:     1.3.0"
  ut_har "Senaste fel:"
  ut_har "Säkerhetskopior:         2 st i $KAT/kopior"
  ut_har "-1.1.0"
  ut_har "-1.2.0"
  ut_har "sudo tail -n 50 $KAT/uppdatering.log"
  # --lage ändrar ingenting.
  [[ "$(find "$KAT" -type f -exec md5sum {} + | sort)" == "$fore" ]]
  anrop_saknar "docker stop"
  anrop_saknar "docker pull"
  anrop_saknar "docker tag"
  ingen_hemlighet_visad
}

s_loggen_roteras() {
  ny_varld logg
  installation 1.1.0
  head -c 1100000 /dev/zero | tr '\0' 'x' >"$KAT/uppdatering.log"
  uppdatera --tidsstyrd
  kod_ar 0
  [[ -f "$KAT/uppdatering.log.1" && "$(stat -c %s "$KAT/uppdatering.log")" -lt 10000 ]]
  rattigheter "$KAT/uppdatering.log" "600 root"
  grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} ' "$KAT/uppdatering.log"
}

s_fel_argument() {
  ny_varld argument
  installation 1.1.0
  uppdatera --okant
  kod_ar 2
  ut_har "Användning"
  uppdatera --tidsstyrd --lage
  kod_ar 2
}

echo "uppdatera.sh ($SKRIPT_KATALOG/uppdatera.sh)"
scenario "ingen ny version" s_ingen_ny_version
scenario "ny version lyckas (kopia, .env, läge, städning)" s_ny_version_lyckas
scenario "trasig version → återgång, hoppas över sedan" s_trasig_version_atergang
scenario "hänger / unhealthy → återgång" s_hanger_och_sjuk_atergang
scenario "upptagen → väntar" s_upptagen_vantar
scenario "1.0.0 utan etikett och utan 'upptagen'" s_v100_utan_etikett_och_utan_upptagen
scenario "återgång till 1.0.0 utan etikett" s_atergang_till_v100_utan_etikett
scenario "1.0.0 som latest (utan etikett) är ingen ny version" s_v100_ingen_ny_utan_etikett
scenario "containern stoppad" s_container_stoppad
scenario "latest utan giltig etikett" s_latest_utan_etikett
scenario "konstiga etiketter ändrar ingenting" s_konstiga_etiketter
scenario "hämtningen misslyckas" s_hamtning_misslyckas
scenario "avbrott (strömavbrott) vid stoppet" s_avbrott_vid_stopp_strom
scenario "avbrott (TERM) vid stoppet" s_avbrott_vid_stopp_term
scenario "avbrott innan nya versionen startats" s_avbrott_fore_start_av_ny
scenario "avbrott under väntan, nya blir frisk" s_avbrott_under_vantan_ny_blir_frisk
scenario "avbrott under väntan, nya hänger" s_avbrott_under_vantan_ny_hanger
scenario "avbrott (TERM) under väntan" s_avbrott_under_vantan_term
scenario "avbrott under återgången" s_avbrott_under_atergang
scenario "återgång som misslyckas försöks igen" s_atergang_som_misslyckas_forsoks_igen
scenario "två körningar samtidigt" s_tva_korningar_samtidigt
scenario "symlänk i datakatalogen följs inte" s_symlank_i_datakatalogen
scenario "planterad länk följs inte vid återgång" s_planterad_lank_foljs_inte_vid_atergang
scenario ".env med konstiga rader bevaras" s_env_med_konstiga_rader
scenario ".env med CRLF på versionsraden" s_env_crlf_pa_versionsraden
scenario "gammal docker-compose.yml" s_gammal_compose_fil
scenario "trasig markering ändrar ingenting" s_trasig_markering
scenario "--lage i klartext, ändrar ingenting" s_lage_i_klartext
scenario "loggen roteras vid 1 MB" s_loggen_roteras
scenario "felaktiga argument" s_fel_argument
summera "uppdatera.sh"
