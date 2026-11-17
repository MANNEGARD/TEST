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
  # Upptäcks innan tjänsten stoppas: inget stopp och ingen omstart, varken nu eller vid nästa kontroll.
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "transcribe.db-wal i datakatalogen är inte en vanlig fil"
  ut_har "Inget har ändrats; tjänsten har inte stoppats."
  uppdatera --tidsstyrd
  kod_ar 1
  anrop_saknar "docker stop"
  [[ "$(starter)" == 1 ]]
  version_ar 1.1.0
  kor_igang
  ingen_markering
  grep -q "^senaste_fel=.*transcribe.db-wal i datakatalogen är inte en vanlig fil" "$KAT/lage.txt"
  inte grep -rq hemligt-innehall "$KAT/kopior" 2>/dev/null
  [[ "$(cat "$VARLD/utanfor")" == hemligt-innehall && -L "$VOL/transcribe.db-wal" ]]
  # Läggs länken dit först medan tjänsten stoppas, fångas den av kontrollen efter stoppet.
  ny_varld symlank-sent
  installation 1.1.0
  echo hemligt-innehall >"$VARLD/utanfor"
  erbjud 1.2.0
  starta_i_bakgrunden 1 "stop -t 30" --tidsstyrd
  rm -f "$VOL/transcribe.db-shm"
  ln -s "$VARLD/utanfor" "$VOL/transcribe.db-shm"
  slapp
  kod_ar 1
  ut_har "transcribe.db-shm i datakatalogen är inte en vanlig fil"
  ut_har "Inget har ändrats; 1.1.0 startades igen."
  version_ar 1.1.0
  kor_igang
  ingen_markering
  inte grep -rq hemligt-innehall "$KAT/kopior" 2>/dev/null
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

s_compose_fungerar_inte() {
  # docker compose saknas: upptäcks innan något stoppas, och tjänsten kör vidare.
  ny_varld compose-saknas
  installation 1.1.0
  erbjud 1.2.0
  touch "$T/fel/compose"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Ingen uppgradering till 1.2.0: kommandot \"docker compose\" fungerar inte"
  anrop_saknar "docker stop"
  anrop_saknar "docker rm"
  version_ar 1.1.0
  kor_igang
  ingen_markering
  lage_saknar misslyckad_version
  # Nätet smgd saknas: samma sak.
  rm -f "$T/fel/compose" "$T/natverk-smgd"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Docker-nätet smgd finns inte"
  anrop_saknar "docker stop"
  # När det är lagat görs uppgraderingen.
  touch "$T/natverk-smgd"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
}

s_atergang_vantar_pa_compose() {
  # Avbrott under återgången, och sedan fungerar inte docker compose: inget tas bort förrän den
  # förra versionen går att starta. Markeringen ligger kvar tills det går.
  ny_varld atergang-compose
  installation 1.1.0
  erbjud 1.2.0 trasig
  starta_i_bakgrunden 2 "compose up" --tidsstyrd
  avbryt KILL
  grep -qx "fas=aterstaller" "$KAT/pagaende-uppgradering"
  touch "$T/fel/compose"
  : >"$T/anrop.log"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "återgången kan inte göras än: kommandot \"docker compose\" fungerar inte"
  anrop_saknar "docker rm"
  anrop_saknar "sync -- $VOL"
  grep -qx "fas=aterstaller" "$KAT/pagaende-uppgradering"
  rm -f "$T/fel/compose"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.1.0
  kor_igang
  ingen_markering
}

s_tillfalligt_fel_svartlistar_inte() {
  # "docker compose up" misslyckas en gång innan den nya versionen alls har startats: det är inte
  # versionens fel. Återgång nu, ett nytt försök vid nästa kontroll – och då går det.
  ny_varld tillfalligt-fel
  installation 1.1.0
  erbjud 1.2.0
  touch "$T/fel/compose_en_gang"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "Uppgraderingen till 1.2.0 gick inte (docker compose kunde inte starta 1.2.0"
  ut_har "Ett nytt försök görs vid nästa kontroll."
  version_ar 1.1.0
  kor_igang
  lage_saknar misslyckad_version
  lage_har "avbruten_version=1.2.0"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
  lage_saknar avbruten_version
  # Händer det två gånger i rad räknas versionen som misslyckad.
  ny_varld tillfalligt-fel-tva
  installation 1.1.0
  erbjud 1.2.0
  touch "$T/fel/compose_en_gang"
  uppdatera --tidsstyrd
  touch "$T/fel/compose_en_gang"
  uppdatera --tidsstyrd
  kod_ar 1
  lage_har "misslyckad_version=1.2.0"
  ut_har "1.2.0 prövas inte igen"
  version_ar 1.1.0
}

s_kraschar_strax_efter_frisk() {
  # Frisk vid första hälsokontrollen men kraschar strax efter: räknas inte som lyckad.
  ny_varld kraschar-sen
  installation 1.1.0
  erbjud 1.2.0 kraschar_sen
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "den slutade fungera strax efter starten"
  ut_saknar "Klart: SMGD Transkribering 1.2.0"
  version_ar 1.1.0
  kor_igang
  lage_har "misslyckad_version=1.2.0"
  [[ "$(cat "$VOL/transcribe.db")" == $'databas från 1.1.0\nstart 1.1.0' ]]
}

s_skapad_men_inte_startad_efter_avbrott() {
  # Avbrott när compose hunnit skapa den nya containern men inte starta den ("created", som Docker
  # inte startar själv efter en omstart): nästa körning startar den och gör uppgraderingen klar.
  ny_varld skapad
  installation 1.1.0
  erbjud 1.2.0
  starta_i_bakgrunden 1 "compose up" --tidsstyrd
  avbryt KILL
  printf '%s' "$(bild_id v1.2.0)" >"$T/container/transcribe/bild"
  echo created >"$T/container/transcribe/status"
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Den nya versionen 1.2.0 blev frisk – uppgraderingen gjordes klar."
  version_ar 1.2.0
  kor_igang
  lage_saknar misslyckad_version
  ingen_markering
}

s_wal_och_shm_fran_ny_version_tas_bort() {
  # Den nya versionen skriver egna -wal och -shm innan den kraschar. Efter återgången får inget av
  # det finnas kvar: -wal är den från kopian och -shm (som inte fanns före) är borta.
  ny_varld wal-shm
  installation 1.1.0
  erbjud 1.2.0 trasig
  touch "$T/bilder/$(bild_id v1.2.0-trasig | sed 's/^sha256://')/skriver_wal"
  uppdatera --tidsstyrd
  kod_ar 1
  version_ar 1.1.0
  [[ "$(cat "$VOL/transcribe.db-wal")" == wal ]] || { echo "    -wal är inte den från kopian"; return 1; }
  [[ ! -e "$VOL/transcribe.db-shm" ]] || { echo "    -shm från den nya versionen finns kvar"; return 1; }
}

s_versioner_jamfors_som_tal() {
  # 1.10.0 är nyare än 1.9.0 (inte som text).
  ny_varld talvis
  installation 1.9.0
  erbjud 1.10.0
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.10.0
  # Erbjuds en lägre version (en annan avbild) installeras den inte.
  erbjud 1.9.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Ingen ny version (kör 1.10.0, erbjuds 1.9.0)."
  version_ar 1.10.0
  ny_varld lagre
  installation 1.2.0
  erbjud 1.1.5
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Ingen ny version (kör 1.2.0, erbjuds 1.1.5)."
  anrop_saknar "docker stop"
  version_ar 1.2.0
}

s_fel_version_efter_start() {
  # Avbilden utgav sig för att vara 1.2.0, men containern visar en annan version: återgång.
  ny_varld fel-version
  installation 1.1.0
  erbjud 1.2.0
  printf '1.1.9' >"$T/bilder/$(bild_id v1.2.0 | sed 's/^sha256://')/etikett_i_container"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "containern kör version 1.1.9 i stället för 1.2.0"
  version_ar 1.1.0
  lage_har "misslyckad_version=1.2.0"
}

s_disken_racker_inte() {
  # För lite ledigt för kopian: ingen uppgradering, den förra versionen startas igen.
  ny_varld disk-full
  installation 1.1.0
  erbjud 1.2.0
  echo 10 >"$T/df_ledigt"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "För lite ledigt diskutrymme för säkerhetskopian"
  ut_har "Inget har ändrats; 1.1.0 startades igen."
  version_ar 1.1.0
  kor_igang
  ingen_markering
  [[ -z "$(kopior)" ]]
  anrop_saknar "compose up"
  # Kopian blir kort (disken tar slut mitt i): ingen uppgradering, ingen halv kopia kvar.
  rm -f "$T/df_ledigt"
  touch "$T/kort_cp"
  uppdatera --tidsstyrd
  kod_ar 1
  ut_har "transcribe.db kunde inte kopieras"
  version_ar 1.1.0
  kor_igang
  [[ -z "$(kopior)" ]]
  [[ -z "$(find "$KAT/kopior" -mindepth 1 2>/dev/null)" ]]
  anrop_saknar "compose up"
}

s_atergang_oppnar_bara_aterlagda_filer() {
  # Containern lägger en länk i datakatalogen. Vid återgången öppnar root bara de filer som läggs
  # tillbaka (sync), aldrig något annat i katalogen.
  ny_varld sync-lank
  installation 1.1.0
  ln -s /dev/zero "$VOL/zz-lank"
  erbjud 1.2.0 trasig
  uppdatera --tidsstyrd
  kod_ar 1
  version_ar 1.1.0
  anrop_har "sync -- $VOL $VOL/transcribe.db"
  anrop_saknar "zz-lank"
  anrop_saknar "audio"
}

s_styrtecken_fran_containern() {
  # Text från containern når varken terminalen eller loggen med styrtecken: inga ESC/BEL, inga
  # C1-tecken (U+009B) och inga tecken som vänder skrivriktningen (U+202E). Svenska bokstäver blir kvar.
  ny_varld styrtecken
  installation 1.1.0
  printf '10 upptagen\033]0;titel\007 \xc2\x9b31mrött \xe2\x80\xaeomvänd\n' >"$T/upptagen"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "rött"
  ut_har "omvänd"
  local f
  for f in "$VARLD/utskrift.log" "$KAT/uppdatering.log" "$KAT/lage.txt"; do
    if LC_ALL=C grep -q $'\x1b\\|\x07\\|\xc2\x9b\\|\xe2\x80\xae' "$f"; then
      echo "    styrtecken från containern i $f"
      return 1
    fi
  done
}

s_v100_upptagen_fragar_databasen() {
  # 1.0.0 saknar kommandot "upptagen": databasen frågas direkt, och pågår något väntar uppdateringen.
  ny_varld v100-upptagen
  installation 1.0.0-utan
  erbjud 1.1.0
  echo 1 >"$T/upptagen_db"
  uppdatera --tidsstyrd
  kod_ar 0
  ut_har "Ny version 1.1.0 finns, men tjänsten är upptagen (upptagen: 1 inspelning(ar) eller bearbetning(ar) pågår)"
  anrop_saknar "docker stop"
  version_ar 1.0.0
  # Ett konstigt svar räknas som ledigt (som kontraktet säger för 1.0.0).
  echo "1; rm -rf /" >"$T/upptagen_db"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.1.0
}

s_lang_vantan_syns() {
  # Har en ny version väntat i mer än ett dygn på att tjänsten blir ledig syns det som ett fel.
  ny_varld lang-vantan
  installation 1.1.0
  echo "10 upptagen: 1 väntar på slutversion" >"$T/upptagen"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  grep -q '^vantar_sedan=[0-9]\+$' "$KAT/lage.txt"
  lage_saknar senaste_fel
  uppdatera --lage
  ut_har "Väntar sedan:"
  # Ett dygn senare.
  sed -i "s/^vantar_sedan=.*/vantar_sedan=$(($(date +%s) - 90000))/" "$KAT/lage.txt"
  uppdatera --tidsstyrd
  kod_ar 0
  grep -q "^senaste_fel=.*1.2.0 har väntat sedan .* på att tjänsten ska bli ledig" "$KAT/lage.txt"
  version_ar 1.1.0
  # När den blir ledig uppgraderas den och väntan glöms.
  echo "0 ledig" >"$T/upptagen"
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
  lage_saknar vantar_sedan
  lage_saknar senaste_fel
}

s_lage_vid_full_disk() {
  # Går lage.txt inte att skriva (full disk) får den inte tömmas eller bytas ut.
  ny_varld lage-full
  installation 1.1.0
  printf 'senaste_kontroll=förut\nmisslyckad_version=1.0.5\n' >"$KAT/lage.txt"
  ln -s /dev/full "$KAT/lage.txt.tmp"
  erbjud 1.2.0
  uppdatera --tidsstyrd
  kod_ar 0
  version_ar 1.2.0
  [[ -f "$KAT/lage.txt" && ! -L "$KAT/lage.txt" ]] || { echo "    lage.txt är inte längre en vanlig fil"; return 1; }
  grep -q "^senaste_uppgradering=.* – 1.1.0 → 1.2.0$" "$KAT/lage.txt"
  [[ ! -e "$KAT/lage.txt.tmp" ]]
}

s_laset_ligger_dar_bara_root_skriver() {
  # Standardlåset ligger i /run (bara root kan skriva där), inte i /run/lock som alla kan skriva i.
  grep -qF 'LAS="${TRANSCRIBE_LAS:-/run/transcribe-uppdatering.lock}"' "$SKRIPT_KATALOG/uppdatera.sh"
  grep -qF 'LAS="${TRANSCRIBE_LAS:-/run/transcribe-uppdatering.lock}"' "$SKRIPT_KATALOG/installera.sh"
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
scenario "docker compose eller nätet saknas → inget stoppas" s_compose_fungerar_inte
scenario "återgången väntar tills docker compose fungerar" s_atergang_vantar_pa_compose
scenario "tillfälligt fel före start svartlistar inte versionen" s_tillfalligt_fel_svartlistar_inte
scenario "kraschar strax efter frisk → återgång" s_kraschar_strax_efter_frisk
scenario "avbrott: ny container skapad men inte startad" s_skapad_men_inte_startad_efter_avbrott
scenario "-wal/-shm från nya versionen tas bort vid återgång" s_wal_och_shm_fran_ny_version_tas_bort
scenario "versioner jämförs som tal, aldrig nedåt" s_versioner_jamfors_som_tal
scenario "fel version efter start → återgång" s_fel_version_efter_start
scenario "för lite disk / kort kopia → ingen uppgradering" s_disken_racker_inte
scenario "återgången öppnar bara återlagda filer" s_atergang_oppnar_bara_aterlagda_filer
scenario "styrtecken från containern tvättas bort" s_styrtecken_fran_containern
scenario "1.0.0: databasen frågas om något pågår" s_v100_upptagen_fragar_databasen
scenario "lång väntan på 'upptagen' syns i läget" s_lang_vantan_syns
scenario "lage.txt vid full disk" s_lage_vid_full_disk
scenario "låset ligger där bara root kan skriva" s_laset_ligger_dar_bara_root_skriver
summera "uppdatera.sh"
