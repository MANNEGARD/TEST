#!/usr/bin/env bash
# SMGD Transkribering – automatisk uppdatering på en egen server.
#
# Installeras av installationsraden (installera.sh) som /opt/transcribe/uppdatera.sh och körs av
# systemd-timern transcribe-uppdatering.timer ungefär var 15:e minut. Användning (som root):
#
#   uppdatera.sh               kontrollera nu och uppdatera om en nyare version finns (för hand)
#   uppdatera.sh --tidsstyrd   samma, körs av timern: inga frågor, allt loggas till fil
#   uppdatera.sh --lage        visa läget i klartext (ändrar ingenting)
#
# Så går en uppdatering till:
#   1. Avbilden ghcr.io/smgd-mp/transcribe:latest hämtas. Dess etikett
#      org.opencontainers.image.version säger vilken version den är; är den högre än den som kör
#      och ingen inspelning eller bearbetning pågår, uppgraderas tjänsten.
#   2. Båda versionerna märks lokalt (<avbild>:<version>), så att återgången fungerar utan nät.
#   3. En markering skrivs på disken, tjänsten stoppas och databasen (transcribe.db med -wal/-shm
#      och krypteringsnyckeln secret.key) kopieras till /opt/transcribe/kopior/. Ljudet kopieras inte.
#   4. TRANSCRIBE_VERSION i .env sätts till den nya versionen och tjänsten startas med docker compose.
#   5. Blir den nya versionen inte frisk inom TRANSCRIBE_VANTA sekunder, eller håller den sig inte
#      frisk den första minuten (TRANSCRIBE_STABIL): databasen läggs tillbaka
#      från kopian och den förra versionen startas igen. Versionen som misslyckades prövas inte igen
#      förrän en ännu nyare erbjuds eller ägaren kör installationsraden.
#   Avbryts allt mitt i (omstart, strömavbrott) gör nästa körning klart det utifrån markeringen.
#
# Läge: /opt/transcribe/lage.txt   Logg: /opt/transcribe/uppdatering.log (roteras vid 1 MB)
#
# Miljövariabler (behövs normalt inte; används av proven):
#   TRANSCRIBE_KATALOG    installationens katalog (/opt/transcribe)
#   TRANSCRIBE_CONTAINER  containerns namn (transcribe)
#   TRANSCRIBE_IMAGE      avbilden utan tagg (annars ur .env, annars ghcr.io/smgd-mp/transcribe)
#   TRANSCRIBE_PULL       1 = hämta latest, 0 = använd bara lokala avbilder (1)
#   TRANSCRIBE_VANTA      sekunder att vänta på att en version blir frisk (180)
#   TRANSCRIBE_STABIL     sekunder som en version sedan ska hålla sig frisk utan omstart (60)
#   TRANSCRIBE_KOPIOR     antal säkerhetskopior som sparas (3)
#   TRANSCRIBE_LAS        låsfil, delas med installationsraden (/run/transcribe-uppdatering.lock; bara
#                         root kan skriva i /run, till skillnad från /run/lock)
#   TRANSCRIBE_SYSTEMD_DIR  katalogen med systemd-enheterna (/etc/systemd/system)
set -euo pipefail

KATALOG="${TRANSCRIBE_KATALOG:-/opt/transcribe}"
CONTAINER="${TRANSCRIBE_CONTAINER:-transcribe}"
IMAGE="${TRANSCRIBE_IMAGE:-}"
PULL="${TRANSCRIBE_PULL:-1}"
VANTA="${TRANSCRIBE_VANTA:-180}"
STABIL="${TRANSCRIBE_STABIL:-60}"
ANTAL_KOPIOR="${TRANSCRIBE_KOPIOR:-3}"
LAS="${TRANSCRIBE_LAS:-/run/transcribe-uppdatering.lock}"
SYSTEMD_KATALOG="${TRANSCRIBE_SYSTEMD_DIR:-/etc/systemd/system}"
STANDARD_IMAGE=ghcr.io/smgd-mp/transcribe
ENHET=transcribe-uppdatering
ENV_FIL="$KATALOG/.env"
COMPOSE_FIL="$KATALOG/docker-compose.yml"
LAGEFIL="$KATALOG/lage.txt"
KOPIEKATALOG="$KATALOG/kopior"
# Finns medan en uppgradering pågår. Blir den kvar (avbrott, omstart) gör nästa körning klart
# uppgraderingen innan något annat händer.
PAGAENDE="$KATALOG/pagaende-uppgradering"
# Det som säkerhetskopieras ur datakatalogen (volymen transcribe-data, /data i containern).
DATAFILER=(transcribe.db transcribe.db-wal transcribe.db-shm secret.key)
# Tål en avbild utan några etiketter alls (då är .Config.Labels tom).
ETIKETT='{{with .Config.Labels}}{{index . "org.opencontainers.image.version"}}{{end}}'
TILLSTAND='{{.State.Status}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}ingen{{end}}'
MONTERING='{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'

LOGGFIL=""
FEL=""
# Uppgifterna om en pågående uppgradering (sparas i PAGAENDE).
P_FAS="" P_GAMMAL="" P_NY="" P_KOPIA="" P_DATA=""

# --- Utskrift och logg -----------------------------------------------------------------------------
tid() { date '+%Y-%m-%d %H:%M:%S'; }
till_loggfil() {
  [[ -n "$LOGGFIL" ]] || return 0
  printf '%s %s\n' "$(tid)" "$*" >>"$LOGGFIL" 2>/dev/null || true
}
info() { printf '   %s\n' "$*"; till_loggfil "$*"; }
varna() { printf '   ! %s\n' "$*" >&2; till_loggfil "VARNING: $*"; }
die() {
  printf '\n✗ %s\n' "$1" >&2
  till_loggfil "FEL: $1"
  satt_lage senaste_fel "$(tid) – $1"
  shift || true
  local rad
  for rad in "$@"; do
    printf '  %s\n' "$rad" >&2
    till_loggfil "  $rad"
  done
  exit 1
}
# Text från containern eller avbilden visas aldrig rakt av: en rad, kort, bara giltig UTF-8 och utan
# styrtecken – varken ASCII, C1 (U+0080–U+009F) eller tecken som vänder skrivriktningen.
tvatta() {
  local v
  v="$(head -n 1 <<<"$1" | LC_ALL=C tr -d '\000-\037\177' | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null \
    | LC_ALL=C sed -e 's/\xc2[\x80-\x9f]//g' -e 's/\xe2\x80[\x8e\x8f\xaa-\xae]//g' -e 's/\xe2\x81[\xa6-\xa9]//g')" || true
  printf '%s' "${v:0:200}"
}

borja_logga() { # roteras vid 1 MB
  LOGGFIL="$KATALOG/uppdatering.log"
  if [[ -f "$LOGGFIL" ]] && (($(stat -c %s "$LOGGFIL" 2>/dev/null || echo 0) > 1048576)); then
    mv -f "$LOGGFIL" "$LOGGFIL.1" || true
  fi
  touch "$LOGGFIL" 2>/dev/null && chmod 600 "$LOGGFIL" || LOGGFIL=""
}

# --- Versioner ---------------------------------------------------------------------------------------
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
ar_avbilds_id() { [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]; }
sakert_varde() { [[ "$1" =~ ^[A-Za-z0-9._:/@+=-]*$ && "${#1}" -le 300 ]]; }
ar_sokvag() { [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ && "$1" != *..* && "$1" != / ]]; }
ar_avbildsnamn() { # utan tagg, t.ex. ghcr.io/smgd-mp/transcribe eller localhost:5000/transcribe
  [[ "$1" =~ ^[a-z0-9][a-z0-9._:/-]{0,199}$ && "$1" != *..* && "$1" != *//* && "$1" != */ && "${1##*/}" != *:* ]]
}

# Etiketten org.opencontainers.image.version på en container eller avbild. Tomt om den saknas.
las_etikett() { # container|image, namn
  local v
  # Exakt det Docker skriver, minus radbrytningen efter mallen: en etikett som själv slutar med
  # en radbrytning är alltså inte X.Y.Z.
  v="$(docker "$1" inspect -f "$ETIKETT" "$2" 2>/dev/null && echo x)" || return 1
  v="${v%x}"
  v="${v%$'\n'}"
  [[ "$v" != "<no value>" ]] || v=""
  # Den som frågar får värdet genom $( … ), som tar bort avslutande radbrytningar – så en etikett
  # med radbrytning i görs ogiltig här.
  [[ "$v" != *$'\n'* ]] || v="ogiltig"
  printf '%s' "$v"
}

# Versionen som kör: etiketten, och för äldre avbilder utan etikett (1.0.0) package.json i
# containern. Skriver versionen, eller inget om den inte går att avgöra säkert.
korande_version() {
  local v
  v="$(las_etikett container "$CONTAINER")" || return 1
  if [[ -z "$v" ]]; then
    v="$(timeout 30 docker exec "$CONTAINER" node -p "require('/app/package.json').version" 2>/dev/null)" || return 1
  fi
  ar_version "$v" || return 1
  printf '%s' "$v"
}

# --- Läge ----------------------------------------------------------------------------------------------
las_lage() { # nyckel → värdet (en rad, utan styrtecken; versioner kontrolleras av den som frågar)
  [[ -f "$LAGEFIL" && ! -L "$LAGEFIL" ]] || return 0
  local v
  v="$(grep -E "^$1=" "$LAGEFIL" 2>/dev/null | tail -n 1 | LC_ALL=C tr -d '\000-\037\177')" || true
  v="${v#*=}"
  printf '%s' "${v:0:500}"
}
satt_lage() { # nyckel värde ("" tar bort nyckeln)
  [[ -d "$KATALOG" ]] || return 0
  local varde tmp="$LAGEFIL.tmp"
  varde="$(printf '%s' "$2" | LC_ALL=C tr '\000-\037\177' ' ')"
  varde="${varde:0:500}"
  # Går något att skriva fel (t.ex. full disk) lämnas lage.txt som den var: grep ger 1 när inga
  # rader blir kvar, men 2 vid fel – och då får den halvskrivna filen inte ersätta den gamla.
  if ! (
    if [[ -f "$LAGEFIL" ]]; then
      grep -v "^$1=" "$LAGEFIL"
      (($? <= 1)) || exit 1
    fi
    if [[ -n "$varde" ]]; then printf '%s=%s\n' "$1" "$varde" || exit 1; fi
  ) >"$tmp" 2>/dev/null || ! chmod 600 "$tmp" || ! sync -- "$tmp" 2>/dev/null || ! mv -f -- "$tmp" "$LAGEFIL"; then
    rm -f -- "$tmp"
  fi
  return 0
}
misslyckad_version() {
  local v
  v="$(las_lage misslyckad_version)"
  if ar_version "$v"; then printf '%s' "$v"; fi
}

# --- .env ------------------------------------------------------------------------------------------------
# Läser ett värde ur .env utan att källa filen: sista raden NAMN=…, utan CR, blanksteg och ett par
# omslutande citattecken. Värdet kontrolleras alltid av den som frågar.
las_env() { # namn
  [[ -f "$ENV_FIL" ]] || return 0
  local v
  v="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$ENV_FIL" 2>/dev/null | tail -n 1)" || true
  [[ -n "$v" ]] || return 0
  v="${v#*=}"
  v="${v%$'\r'}"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  if [[ "$v" =~ ^\"(.*)\"$ || "$v" =~ ^\'(.*)\'$ ]]; then v="${BASH_REMATCH[1]}"; fi
  printf '%s' "$v"
}

# Sätter NAMN=värde i .env. Hela filen skrivs om via en tillfällig fil (600) i samma katalog: den
# första raden med namnet byts, fler likadana tas bort, övriga rader lämnas byte för byte orörda
# (även CRLF). Innehållet skrivs aldrig ut.
satt_env() { # namn värde
  local tmp
  tmp="$(mktemp "$KATALOG/.env.XXXXXX")" || return 1
  chmod 600 "$tmp" || { rm -f -- "$tmp"; return 1; }
  if ! NAMN="$1" VARDE="$2" LC_ALL=C awk '
    BEGIN { n = ENVIRON["NAMN"]; v = ENVIRON["VARDE"]; klar = 0; m = "^[ \t]*(export[ \t]+)?" n "[ \t]*=" }
    $0 ~ m { if (!klar) { cr = (substr($0, length($0)) == "\r") ? "\r" : ""; print n "=" v cr; klar = 1 } next }
    { print }
    END { if (!klar) print n "=" v }
  ' "$ENV_FIL" >"$tmp" || ! sync -- "$tmp" || ! mv -f -- "$tmp" "$ENV_FIL"; then
    rm -f -- "$tmp"
    return 1
  fi
  sync -- "$KATALOG" 2>/dev/null || true
}

# --- Containern --------------------------------------------------------------------------------------
kor_containern() { [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == true ]]; }

# Startar tjänsten med en viss version. Variablerna skickas med uttryckligen så att resultatet inte
# beror på något annat i miljön; .env har redan samma värden för den som kör docker compose för hand.
compose_upp() { # version
  if ! (cd "$KATALOG" && TRANSCRIBE_IMAGE="$IMAGE" TRANSCRIBE_VERSION="$1" docker compose up -d </dev/null) \
    >>"${LOGGFIL:-/dev/null}" 2>&1; then
    FEL="docker compose kunde inte starta $1 (se $KATALOG/uppdatering.log)"
    return 1
  fi
}

# Det som behövs för att alls kunna starta tjänsten igen: docker compose fungerar, filen går att
# tolka och nätet smgd finns. Kontrolleras innan något stoppas eller tas bort. Sätter FEL.
compose_fungerar() {
  if ! docker compose version >/dev/null 2>&1; then
    FEL="kommandot \"docker compose\" fungerar inte (paketet docker-compose-plugin saknas?)"
  elif ! (cd "$KATALOG" && TRANSCRIBE_IMAGE="$IMAGE" TRANSCRIBE_VERSION=0.0.0 docker compose config -q </dev/null) >/dev/null 2>&1; then
    FEL="docker compose kan inte läsa $COMPOSE_FIL"
  elif ! docker network inspect smgd >/dev/null 2>&1; then
    FEL="Docker-nätet smgd finns inte (det skapas av installationsraden)"
  else
    return 0
  fi
  return 1
}

# Väntar högst VANTA sekunder på att containern kör, är "healthy" och är rätt version, och sedan
# STABIL sekunder till: under den tiden ska den hålla sig frisk utan att startas om (en version som
# kraschar strax efter starten räknas alltså inte som lyckad). Startas den om gång på gång, blir den
# "unhealthy" eller stannar den, räknas det som fel direkt. Sätter FEL.
vanta_frisk() { # version
  local -i i stannad=0 utan_kontroll=0 frisk_sedan=-1 omstarter_da=0
  local status omstarter halsa v
  for ((i = 0; frisk_sedan >= 0 || i < VANTA; i += 2)); do
    status="" omstarter="" halsa=""
    read -r status omstarter halsa < <(docker inspect -f "$TILLSTAND" "$CONTAINER" 2>/dev/null || true) || true
    [[ "$omstarter" =~ ^[0-9]+$ ]] || omstarter=0
    if ((frisk_sedan >= 0)); then
      if [[ "$status" != running || "$halsa" == unhealthy || "$halsa" == starting ]] || ((omstarter != omstarter_da)); then
        FEL="den slutade fungera strax efter starten (${status:-finns inte}, ${halsa:-okänt}, omstarter: $omstarter)"
        return 1
      fi
      if ((i - frisk_sedan >= STABIL)); then return 0; fi
      sleep 2
      continue
    fi
    if ((omstarter >= 2)); then
      FEL="containern startades om gång på gång"
      return 1
    fi
    if [[ "$status" == running ]]; then
      stannad=0
      if [[ "$halsa" == unhealthy ]]; then
        FEL="hälsokontrollen säger att den inte fungerar (unhealthy)"
        return 1
      fi
      # En avbild utan hälsokontroll räknas som frisk när den har kört en stund.
      if [[ "$halsa" == ingen ]]; then utan_kontroll+=1; fi
      if [[ "$halsa" == healthy ]] || ((utan_kontroll >= 3)); then
        v="$(korande_version || true)"
        if [[ "$v" != "$1" ]]; then
          FEL="containern kör version ${v:-okänd} i stället för $1"
          return 1
        fi
        frisk_sedan=i
        omstarter_da=omstarter
        if ((STABIL == 0)); then return 0; fi
      fi
    elif [[ "$status" != restarting ]]; then
      # Stoppad, borta eller bara skapad ("created" startas inte av sig själv).
      stannad+=1
      if ((stannad >= 2)); then
        FEL="containern kör inte (${status:-finns inte})"
        return 1
      fi
    fi
    sleep 2
  done
  FEL="den blev inte frisk inom $VANTA sekunder"
  return 1
}

visa_containerlogg() {
  local rad
  while IFS= read -r rad; do
    till_loggfil "   | $(tvatta "$rad")"
  done < <(docker logs --tail 20 "$CONTAINER" 2>&1 || true)
}

# Datakatalogen på värden: monteringen med Destination /data (läses av Docker, inte av containern).
datakatalog() {
  local d
  d="$(docker inspect -f "$MONTERING" "$CONTAINER" 2>/dev/null)" || return 1
  ar_sokvag "$d" && [[ -d "$d" && ! -L "$d" ]] || return 1
  printf '%s' "$d"
}

# --- Säkerhetskopia ------------------------------------------------------------------------------------
# Bara vanliga filer kopieras; är någon av dem något annat (t.ex. en symbolisk länk som containern
# har lagt dit) görs ingen uppgradering alls. Prövas innan tjänsten stoppas, och igen när den är
# stoppad. Sätter FEL.
datafiler_vanliga() { # datakatalog
  local f
  for f in "${DATAFILER[@]}"; do
    if [[ -L "$1/$f" ]] || { [[ -e "$1/$f" ]] && [[ ! -f "$1/$f" ]]; }; then
      FEL="$f i datakatalogen är inte en vanlig fil (en symbolisk länk eller en katalog) – den följs inte."
      return 1
    fi
  done
}

# Containern är stoppad, så inget i den kan ändra filerna under tiden.
sakerhetskopiera() { # datakatalog, kopians katalog
  local data="$1" mal="$2" tmp="$2.tmp" f behov=0 ledigt
  local -a kopierade=()
  datafiler_vanliga "$data" || return 1
  for f in "${DATAFILER[@]}"; do
    if [[ -f "$data/$f" ]]; then behov=$((behov + $(stat -c %s "$data/$f"))); fi
  done
  if [[ ! -f "$data/transcribe.db" ]]; then
    FEL="databasen $data/transcribe.db finns inte."
    return 1
  fi
  ledigt="$(df -Pk "$KOPIEKATALOG" 2>/dev/null | awk 'NR == 2 {print $4}')"
  if [[ ! "$ledigt" =~ ^[0-9]+$ ]] || ((ledigt * 1024 < behov + 104857600)); then
    FEL="För lite ledigt diskutrymme för säkerhetskopian (databasen är $((behov / 1048576)) MB)."
    return 1
  fi
  rm -rf -- "$tmp"
  if ! (umask 077 && mkdir -- "$tmp"); then
    FEL="$tmp kunde inte skapas."
    return 1
  fi
  for f in "${DATAFILER[@]}"; do
    [[ -f "$data/$f" ]] || continue
    if ! cp -p --no-dereference -- "$data/$f" "$tmp/$f" \
      || [[ "$(stat -c %s "$data/$f")" != "$(stat -c %s "$tmp/$f")" ]]; then
      rm -rf -- "$tmp"
      FEL="$f kunde inte kopieras (fullt på disken?)."
      return 1
    fi
    kopierade+=("$tmp/$f")
  done
  sync -- "$tmp" "${kopierade[@]}" 2>/dev/null || true
  mv -T -- "$tmp" "$mal" || { rm -rf -- "$tmp"; FEL="$mal kunde inte skapas."; return 1; }
  sync -- "$KOPIEKATALOG" 2>/dev/null || true
}

# Lägger tillbaka databasfilerna från kopian. Containern är borttagen. Varje mål tas bort först
# (är det en länk följs den inte), och filer som inte fanns när kopian togs tas bort. Bara de filer
# som läggs tillbaka öppnas (för sync) – inget annat i datakatalogen, som containern bestämmer över.
aterstall_data() { # datakatalog, kopians katalog
  local data="$1" kopia="$2" f
  local -a aterlagda=()
  if [[ ! -d "$kopia" || -L "$kopia" || ! -f "$kopia/transcribe.db" || -L "$kopia/transcribe.db" ]]; then
    FEL="säkerhetskopian $kopia finns inte eller är ofullständig"
    return 1
  fi
  if [[ ! -d "$data" || -L "$data" ]]; then
    FEL="datakatalogen $data finns inte"
    return 1
  fi
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    FEL="containern $CONTAINER kunde inte tas bort"
    return 1
  fi
  for f in "${DATAFILER[@]}"; do
    rm -rf -- "${data:?}/$f" || { FEL="$data/$f kunde inte tas bort"; return 1; }
    if [[ -f "$kopia/$f" && ! -L "$kopia/$f" ]]; then
      cp -p --no-dereference -- "$kopia/$f" "$data/$f" || { FEL="$f kunde inte läggas tillbaka (fullt på disken?)"; return 1; }
      aterlagda+=("$data/$f")
    fi
  done
  sync -- "$data" "${aterlagda[@]}" 2>/dev/null || true
}

kopior_nyast_forst() {
  [[ -d "$KOPIEKATALOG" ]] || return 0
  find "$KOPIEKATALOG" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended \
    -regex '.*/[0-9]{8}T[0-9]{6}(_[0-9]{1,3})?-[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | cut -d' ' -f2- || true
}
stada_kopior() { # behåll alltid: den nya kopian
  local gammal
  local -i sparade=1
  find "$KOPIEKATALOG" -mindepth 1 -maxdepth 1 -type d -name '*.tmp' -exec rm -rf -- {} + 2>/dev/null || true
  while IFS= read -r gammal; do
    [[ -n "$gammal" && "$gammal" != "$1" ]] || continue
    if ((sparade < ANTAL_KOPIOR)); then
      sparade+=1
      continue
    fi
    rm -rf -- "$gammal"
    info "Äldre säkerhetskopia borttagen: ${gammal##*/}"
  done < <(kopior_nyast_forst)
  return 0
}

stada_avbilder() { # behåll: versioner som ska sparas
  [[ "$PULL" == 1 ]] || return 0 # lokala avbilder (prov) rörs inte
  local ref tagg
  while IFS= read -r ref; do
    [[ "$ref" == "$IMAGE:"* ]] || continue
    tagg="${ref#"$IMAGE:"}"
    [[ "$tagg" =~ ^[A-Za-z0-9._-]{1,128}$ && "$tagg" != latest ]] || continue
    [[ " $* " == *" $tagg "* ]] && continue
    if docker rmi "$ref" >/dev/null 2>&1; then info "Gammal avbild borttagen: $ref"; fi
  done < <(docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE" 2>/dev/null || true)
  return 0
}

# --- Markeringen för en pågående uppgradering ------------------------------------------------------
# Skrivs (och synkas till disken) innan något ändras. fas: stoppad = den förra versionen stoppas
# och kopian tas, datat är orört; provar = kopian är klar och den nya versionen får röra datat;
# aterstaller = återgången har börjat.
skriv_pagaende() { # fas
  local tmp="$PAGAENDE.tmp"
  P_FAS="$1"
  {
    printf '# SMGD Transkribering – en uppgradering pågår eller avbröts. Tas bort när den är klar.\n'
    printf 'fas=%s\ngammal=%s\nny=%s\nkopia=%s\ndata=%s\ncontainer=%s\n' \
      "$P_FAS" "$P_GAMMAL" "$P_NY" "$P_KOPIA" "$P_DATA" "$CONTAINER"
  } >"$tmp" || return 1
  chmod 600 "$tmp" && sync -- "$tmp" && mv -f -- "$tmp" "$PAGAENDE" && sync -- "$KATALOG"
}
las_pagaende() { # strikt: kända nycklar och säkra värden, källas aldrig
  local rad n v namn=""
  P_FAS="" P_GAMMAL="" P_NY="" P_KOPIA="" P_DATA=""
  [[ -f "$PAGAENDE" && ! -L "$PAGAENDE" ]] || return 1
  while IFS= read -r rad || [[ -n "$rad" ]]; do
    [[ -z "$rad" || "$rad" == \#* ]] && continue
    [[ "$rad" == *=* ]] || return 1
    n="${rad%%=*}"
    v="${rad#*=}"
    sakert_varde "$v" || return 1
    case "$n" in
      fas) P_FAS="$v" ;;
      gammal) P_GAMMAL="$v" ;;
      ny) P_NY="$v" ;;
      kopia) P_KOPIA="$v" ;;
      data) P_DATA="$v" ;;
      container) namn="$v" ;;
      *) return 1 ;;
    esac
  done <"$PAGAENDE"
  [[ "$P_FAS" =~ ^(stoppad|provar|aterstaller)$ && "$namn" == "$CONTAINER" ]] || return 1
  ar_version "$P_GAMMAL" && ar_version "$P_NY" && ar_sokvag "$P_DATA" || return 1
  [[ "$P_KOPIA" =~ ^"$KOPIEKATALOG"/[0-9]{8}T[0-9]{6}(_[0-9]{1,3})?-([0-9.]+)$ && "${BASH_REMATCH[2]}" == "$P_GAMMAL" ]]
}

# --- Uppgraderingen ----------------------------------------------------------------------------------
lyckad() {
  rm -f -- "$PAGAENDE"
  P_FAS=""
  satt_lage senaste_uppgradering "$(tid) – $P_GAMMAL → $P_NY"
  satt_lage misslyckad_version ""
  satt_lage avbruten_version ""
  satt_lage vantar_sedan ""
  satt_lage senaste_fel ""
  info "Klart: SMGD Transkribering $P_NY är igång och frisk. Säkerhetskopia: $P_KOPIA"
  stada_kopior "$P_KOPIA"
  stada_avbilder "$P_NY" "$P_GAMMAL"
}

# En version som aldrig fick köras färdigt (avbrott, eller ett fel här innan den startade) får ett
# nytt försök; händer det igen räknas den som misslyckad och prövas inte mer.
inte_provad() { # version
  if [[ "$(las_lage avbruten_version)" == "$1" ]]; then
    satt_lage misslyckad_version "$1"
  else
    satt_lage avbruten_version "$1"
  fi
}

# Går tillbaka till den förra versionen med databasen från kopian. Slutar alltid med fel (exit 1):
# antingen kör den förra versionen igen, eller så ligger markeringen kvar och nästa körning försöker.
aterga() { # orsak
  local orsak="$1"
  skriv_pagaende aterstaller || true
  info "Går tillbaka till $P_GAMMAL …"
  # Inget tas bort förrän den förra versionen går att starta igen.
  compose_fungerar \
    || die "Uppgraderingen till $P_NY gick inte ($orsak), och återgången kan inte göras än: $FEL." \
      "Ett nytt försök att gå tillbaka görs vid nästa kontroll. Säkerhetskopian ligger i $P_KOPIA."
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  aterstall_data "$P_DATA" "$P_KOPIA" \
    || die "Uppgraderingen till $P_NY gick inte ($orsak), och databasen kunde inte läggas tillbaka: $FEL." \
      "Ett nytt försök att gå tillbaka görs vid nästa kontroll. Står felet kvar: be om hjälp." \
      "Säkerhetskopian ligger i $P_KOPIA."
  satt_env TRANSCRIBE_VERSION "$P_GAMMAL" \
    || die "Uppgraderingen till $P_NY gick inte ($orsak), och .env kunde inte ändras tillbaka till $P_GAMMAL." \
      "Ett nytt försök att gå tillbaka görs vid nästa kontroll."
  if ! compose_upp "$P_GAMMAL" || ! vanta_frisk "$P_GAMMAL"; then
    visa_containerlogg
    die "Uppgraderingen till $P_NY gick inte ($orsak), och återgången till $P_GAMMAL blev inte heller frisk ($FEL)." \
      "Databasen är återställd från $P_KOPIA. Ett nytt försök görs vid nästa kontroll." \
      "Loggen: sudo docker logs --tail 50 $CONTAINER"
  fi
  rm -f -- "$PAGAENDE"
  P_FAS=""
  info "Återgick till $P_GAMMAL med databasen från före försöket."
}

uppgradera() { # gammal version, ny version, den nya avbildens id, den gamlas id
  P_GAMMAL="$1" P_NY="$2" P_KOPIA="" P_DATA=""
  local ny_id="$3" gammal_id="$4" orsak
  local -i n=1
  local stampel
  info "Uppgraderar $P_GAMMAL → $P_NY …"
  P_DATA="$(datakatalog)" || die "Datakatalogen (volymen på /data) gick inte att hitta – inget har ändrats."
  datafiler_vanliga "$P_DATA" || die "Ingen uppgradering: $FEL" "Inget har ändrats; tjänsten har inte stoppats."
  # Båda versionerna finns sedan lokalt: inget hämtas två gånger och återgången kräver inget nät.
  if ! { docker tag "$ny_id" "$IMAGE:$P_NY" && docker tag "$gammal_id" "$IMAGE:$P_GAMMAL"; }; then
    die "Avbilderna kunde inte märkas med sina versioner – inget har ändrats."
  fi
  stampel="$(date +%Y%m%dT%H%M%S)"
  P_KOPIA="$KOPIEKATALOG/$stampel-$P_GAMMAL"
  while [[ -e "$P_KOPIA" || -e "$P_KOPIA.tmp" ]]; do # två försök inom samma sekund
    n+=1
    P_KOPIA="$KOPIEKATALOG/${stampel}_$n-$P_GAMMAL"
  done
  if ! { (umask 077 && mkdir -p "$KOPIEKATALOG") && chmod 700 "$KOPIEKATALOG"; }; then
    die "$KOPIEKATALOG kunde inte skapas – inget har ändrats."
  fi
  if ! skriv_pagaende stoppad; then
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "Markeringen $PAGAENDE kunde inte skrivas – inget har ändrats."
  fi

  info "Stoppar $P_GAMMAL för säkerhetskopian …"
  if ! docker stop -t 30 "$CONTAINER" >/dev/null 2>&1 || kor_containern; then
    docker start "$CONTAINER" >/dev/null 2>&1 || true
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "Tjänsten kunde inte stoppas – inget har ändrats."
  fi
  if ! sakerhetskopiera "$P_DATA" "$P_KOPIA" || ! skriv_pagaende provar; then
    orsak="${FEL:-markeringen kunde inte skrivas}"
    rm -rf -- "$P_KOPIA.tmp"
    docker start "$CONTAINER" >/dev/null 2>&1 || true
    rm -f -- "$PAGAENDE"
    P_FAS=""
    die "Ingen uppgradering: $orsak" "Inget har ändrats; $P_GAMMAL startades igen."
  fi
  info "Säkerhetskopia: $P_KOPIA"

  info "Startar $P_NY …"
  # Ett fel innan den nya versionen alls har startats (.env, docker compose) ligger inte i
  # versionen: den får ett nytt försök. Startade den men blev inte frisk prövas den inte igen.
  FEL=""
  if ! satt_env TRANSCRIBE_VERSION "$P_NY"; then
    orsak="$ENV_FIL kunde inte skrivas"
    inte_provad "$P_NY"
  elif ! compose_upp "$P_NY"; then
    orsak="$FEL"
    inte_provad "$P_NY"
  elif vanta_frisk "$P_NY"; then
    lyckad
    return 0
  else
    orsak="$FEL"
    satt_lage misslyckad_version "$P_NY"
  fi
  varna "$P_NY fungerade inte: $orsak."
  visa_containerlogg
  aterga "$orsak"
  if [[ "$(misslyckad_version)" == "$P_NY" ]]; then
    die "Version $P_NY fungerade inte ($orsak). $P_GAMMAL kör igen, med databasen från före försöket." \
      "$P_NY prövas inte igen förrän en nyare version erbjuds eller du kör installationsraden."
  fi
  die "Uppgraderingen till $P_NY gick inte ($orsak). $P_GAMMAL kör igen, med databasen från före försöket." \
    "Ett nytt försök görs vid nästa kontroll."
}

# Gör klart en uppgradering som avbröts. Körs med låset taget, först i varje körning.
slutfor_avbruten() {
  las_pagaende || die "Markeringen $PAGAENDE om en pågående uppgradering går inte att läsa." \
    "Säkerhetskopiorna ligger i $KOPIEKATALOG. Be om hjälp innan du kör något annat."
  info "Gör klart en avbruten uppgradering ($P_GAMMAL → $P_NY, fas $P_FAS) …"
  case "$P_FAS" in
    stoppad)
      # Bara stoppet eller säkerhetskopian hann påbörjas; datat och .env är orörda.
      rm -rf -- "$P_KOPIA.tmp" "$P_KOPIA"
      satt_env TRANSCRIBE_VERSION "$P_GAMMAL" || die ".env kunde inte skrivas – försöker igen vid nästa kontroll."
      if ! compose_upp "$P_GAMMAL" || ! vanta_frisk "$P_GAMMAL"; then
        visa_containerlogg
        die "$P_GAMMAL kunde inte startas igen efter avbrottet ($FEL)." \
          "Datat är orört. Ett nytt försök görs vid nästa kontroll."
      fi
      rm -f -- "$PAGAENDE"
      P_FAS=""
      info "Uppgraderingen till $P_NY avbröts innan något ändrades; $P_GAMMAL kör igen. Ett nytt försök görs vid nästa kontroll."
      satt_lage senaste_fel "$(tid) – uppgraderingen till $P_NY avbröts innan något ändrades; $P_GAMMAL kör igen"
      ;;
    provar)
      # Finns den nya versionens container redan får den chansen att bli frisk; annars återgång.
      # Hann den skapas men inte startas (avbrott mitt i "docker compose up", eller en omstart av
      # servern: en container som bara är skapad startar Docker inte själv) startas den först.
      if [[ "$(las_etikett container "$CONTAINER" || true)" == "$P_NY" ]]; then
        if ! kor_containern && compose_fungerar; then compose_upp "$P_NY" || true; fi
        if vanta_frisk "$P_NY"; then
          info "Den nya versionen $P_NY blev frisk – uppgraderingen gjordes klar."
          lyckad
          return 0
        fi
        satt_lage misslyckad_version "$P_NY"
      else
        inte_provad "$P_NY"
      fi
      aterga "uppgraderingen avbröts innan den nya versionen var kontrollerad"
      satt_lage senaste_fel "$(tid) – uppgraderingen till $P_NY avbröts och gjordes klar genom återgång till $P_GAMMAL"
      ;;
    aterstaller)
      aterga "återgången avbröts"
      satt_lage senaste_fel "$(tid) – återgången från $P_NY avbröts och är nu klar; $P_GAMMAL kör"
      ;;
  esac
  return 0
}

# Avbrott (systemd stoppar tjänsten, servern stängs av): markeringen ligger kvar så att nästa
# körning gör klart. Var bara den förra versionen stoppad startas den direkt igen.
avbrott() { # signal
  trap - TERM HUP INT
  till_loggfil "AVBRUTEN ($1)${P_FAS:+ under fasen $P_FAS}."
  if [[ "$P_FAS" == stoppad ]]; then
    rm -rf -- "$P_KOPIA.tmp"
    if timeout 20 docker start "$CONTAINER" >/dev/null 2>&1; then
      rm -rf -- "$P_KOPIA"
      rm -f -- "$PAGAENDE"
      satt_lage senaste_fel "$(tid) – uppgraderingen till $P_NY avbröts ($1) innan något ändrades; $P_GAMMAL kör igen"
      exit 143
    fi
  fi
  [[ -z "$P_FAS" ]] || satt_lage senaste_fel "$(tid) – uppgraderingen till $P_NY avbröts ($1); den görs klar vid nästa kontroll"
  exit 143
}

# --- Läget i klartext ----------------------------------------------------------------------------------
visa_lage() {
  local v kor status halsa auto kopior antal
  printf 'SMGD Transkribering – läge\n\n'
  if [[ -f "$SYSTEMD_KATALOG/$ENHET.timer" ]]; then
    if command -v systemctl >/dev/null 2>&1 && [[ "$(systemctl is-active "$ENHET.timer" 2>/dev/null)" == active ]]; then
      auto="på (letar efter en ny version ungefär var 15:e minut)"
    else
      auto="på, men timern $ENHET.timer är inte aktiv – kör installationsraden igen"
    fi
  else
    auto="av (slå på genom att köra installationsraden utan --utan-automatisk-uppdatering)"
  fi
  printf '  Automatisk uppdatering:  %s\n' "$auto"
  if command -v docker >/dev/null 2>&1 && docker inspect "$CONTAINER" >/dev/null 2>&1; then
    kor="$(korande_version || true)"
    read -r status _ halsa < <(docker inspect -f "$TILLSTAND" "$CONTAINER" 2>/dev/null || true) || true
    if [[ "$status" == running && "$halsa" == healthy ]]; then
      status="igång och frisk"
    elif [[ "$status" == running ]]; then
      status="igång, men inte frisk (${halsa:-okänt})"
    else
      status="STOPPAD"
    fi
    printf '  Kör version:             %s (%s)\n' "${kor:-okänd}" "$status"
  else
    printf '  Kör version:             ingen container som heter %s\n' "$CONTAINER"
  fi
  v="$(las_lage senaste_kontroll)"
  printf '  Senaste kontroll:        %s\n' "${v:-ingen än}"
  v="$(las_lage senaste_uppgradering)"
  printf '  Senaste uppgradering:    %s\n' "${v:-ingen än}"
  v="$(las_lage vantar_sedan)"
  if [[ "$v" =~ ^[0-9]{1,12}$ ]]; then
    printf '  Väntar sedan:            %s på att tjänsten ska bli ledig (en inspelning eller bearbetning pågår)\n' "$(date -d "@$v" '+%Y-%m-%d %H:%M')"
  fi
  v="$(misslyckad_version)"
  if [[ -n "$v" ]]; then
    printf '  Hoppar över version:     %s (fungerade inte; prövas igen när en nyare erbjuds eller när du kör installationsraden)\n' "$v"
  fi
  v="$(las_lage senaste_fel)"
  printf '  Senaste fel:             %s\n' "${v:-inget}"
  if las_pagaende; then
    printf '  Avbruten uppgradering:   %s → %s (görs klar vid nästa kontroll)\n' "$P_GAMMAL" "$P_NY"
  elif [[ -e "$PAGAENDE" ]]; then
    printf '  Avbruten uppgradering:   markeringen %s går inte att läsa – be om hjälp\n' "$PAGAENDE"
  fi
  kopior="$(kopior_nyast_forst)"
  antal="$(grep -c . <<<"$kopior" || true)"
  if ((antal > 0)); then
    printf '  Säkerhetskopior:         %s st i %s\n' "$antal" "$KOPIEKATALOG"
    while IFS= read -r v; do printf '                           %s\n' "${v##*/}"; done <<<"$kopior"
  else
    printf '  Säkerhetskopior:         inga än\n'
  fi
  printf '\n  Loggen:  sudo tail -n 50 %s   eller   journalctl -u %s\n' "$KATALOG/uppdatering.log" "$ENHET.service"
}

# --- Upptagen? -------------------------------------------------------------------------------------------
# 1.0.0 saknar kommandot "upptagen": där frågas databasen direkt, med samma regel som kommandot har
# i 1.1.0 (en inspelning med aktivitet de senaste 2 minuterna, eller en slutversion som bearbetas).
FRAGA_100='const D = require("better-sqlite3"); const d = new D("/data/transcribe.db", { readonly: true, fileMustExist: true }); console.log(d.prepare("SELECT COUNT(*) AS n FROM transcripts WHERE status = ? OR (status = ? AND last_activity >= ?)").get("processing", "recording", new Date(Date.now() - 120000).toISOString()).n)'
UPPTAGEN=""
# Sant om en inspelning eller bearbetning pågår; vad som pågår står då i UPPTAGEN. Allt annat än ett
# tydligt "upptagen" räknas som ledigt.
ar_upptagen() {
  local ut kod=0
  UPPTAGEN=""
  ut="$(timeout 60 docker exec "$CONTAINER" node dist/cli.js upptagen 2>/dev/null)" || kod=$?
  if [[ "$kod" == 10 ]]; then
    UPPTAGEN="$(tvatta "$ut")"
    return 0
  fi
  [[ "$kod" != 0 ]] || return 1
  ut="$(timeout 60 docker exec "$CONTAINER" node -e "$FRAGA_100" 2>/dev/null)" || return 1
  [[ "$ut" =~ ^[0-9]{1,6}$ ]] && ((10#$ut > 0)) || return 1
  UPPTAGEN="upptagen: $((10#$ut)) inspelning(ar) eller bearbetning(ar) pågår"
}

# Hur länge en ny version har fått vänta på att tjänsten blir ledig. Efter ett dygn syns det som ett
# fel i läget (något kan ha fastnat i "bearbetas"); uppgraderingen görs ändå inte mitt i något.
VANTA_LARM=86400
notera_vantan() { # erbjuden version
  local sedan nu
  nu="$(date +%s)"
  sedan="$(las_lage vantar_sedan)"
  if [[ ! "$sedan" =~ ^[0-9]{1,12}$ ]]; then
    sedan="$nu"
    satt_lage vantar_sedan "$sedan"
  fi
  if ((nu - sedan >= VANTA_LARM)); then
    satt_lage senaste_fel "$(tid) – $1 har väntat sedan $(date -d "@$sedan" '+%Y-%m-%d %H:%M') på att tjänsten ska bli ledig ($UPPTAGEN). Fastnar något i \"bearbetas\"?"
  fi
}

# --- En kontroll ---------------------------------------------------------------------------------------
kontrollera() { # tidsstyrd (1/0)
  local kor gammal_id erbjuden ny_id env_version miss
  borja_logga
  till_loggfil "Kontroll startad ($([[ "$1" == 1 ]] && echo tidsstyrd || echo för hand))."
  command -v flock >/dev/null 2>&1 || die "Kommandot flock saknas (paketet util-linux)."
  mkdir -p "$(dirname "$LAS")"
  exec 9>"$LAS"
  if ! flock -n 9; then
    if [[ "$1" == 1 ]]; then
      info "En annan körning (installationsraden eller en kontroll) pågår redan – den här kontrollen hoppas över."
      exit 0
    fi
    info "En annan körning pågår – väntar tills den är klar …"
    flock -w 3600 9 || die "Den andra körningen blev inte klar inom en timme." "Försök igen senare."
  fi
  trap 'avbrott TERM' TERM
  trap 'avbrott HUP' HUP
  trap 'avbrott INT' INT
  docker info >/dev/null 2>&1 || die "Docker svarar inte – ingen uppdatering nu."

  if [[ -e "$PAGAENDE" || -L "$PAGAENDE" ]]; then
    slutfor_avbruten
    satt_lage senaste_kontroll "$(tid) – en avbruten uppgradering gjordes klar"
    exit 0
  fi
  find "$KOPIEKATALOG" -mindepth 1 -maxdepth 1 -type d -name '*.tmp' -exec rm -rf -- {} + 2>/dev/null || true

  [[ -f "$ENV_FIL" ]] || die "$ENV_FIL saknas – kör installationsraden."
  if [[ ! -f "$COMPOSE_FIL" ]] || ! grep -qE '^[[:space:]]*image:.*\$\{TRANSCRIBE_VERSION' "$COMPOSE_FIL"; then
    die "$COMPOSE_FIL är av en äldre sort (utan TRANSCRIBE_VERSION) – ingen automatisk uppdatering." \
      "Kör installationsraden en gång till, så hämtas den nya filen."
  fi
  if ! kor_containern; then
    satt_lage senaste_kontroll "$(tid) – tjänsten ($CONTAINER) kör inte, ingen uppdatering"
    info "Tjänsten ($CONTAINER) kör inte – ingen uppdatering (den startas inte av den här kontrollen)."
    exit 0
  fi
  kor="$(korande_version)" || die "Kan inte avgöra vilken version som kör (etiketten är inte X.Y.Z) – inget har ändrats."
  gammal_id="$(docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null)" || true
  ar_avbilds_id "$gammal_id" || die "Avbilden som kör går inte att avgöra – inget har ändrats."

  # Äldre installation utan versionen i .env: lås den till versionen som kör, så att en senare
  # "docker compose up" inte byter version av misstag.
  env_version="$(las_env TRANSCRIBE_VERSION)"
  if [[ "$env_version" != "$kor" ]]; then
    docker tag "$gammal_id" "$IMAGE:$kor" || die "Avbilden som kör kunde inte märkas med $kor – inget har ändrats."
    satt_env TRANSCRIBE_VERSION "$kor" || die "$ENV_FIL kunde inte skrivas – inget har ändrats."
    info "TRANSCRIBE_VERSION i .env är nu $kor (versionen som kör)."
  fi

  if [[ "$PULL" == 1 ]]; then
    docker pull -q "$IMAGE:latest" >/dev/null 2>&1 \
      || die "Kunde inte hämta $IMAGE:latest – inget har ändrats. Försöker igen vid nästa kontroll."
  fi
  ny_id="$(docker image inspect -f '{{.Id}}' "$IMAGE:latest" 2>/dev/null)" || true
  ar_avbilds_id "$ny_id" || die "Avbilden $IMAGE:latest finns inte eller går inte att läsa – inget har ändrats."
  # Samma avbild som kör (t.ex. 1.0.0 utan etikett, innan en nyare har getts ut): inget nytt.
  if [[ "$ny_id" == "$gammal_id" ]]; then
    satt_lage senaste_kontroll "$(tid) – ingen ny version (kör $kor, erbjuds $kor)"
    info "Ingen ny version (kör $kor, erbjuds $kor)."
    exit 0
  fi
  erbjuden="$(las_etikett image "$IMAGE:latest")" || die "Avbilden $IMAGE:latest går inte att läsa – inget har ändrats."
  ar_version "$erbjuden" || die "Avbilden $IMAGE:latest har ingen giltig versionsetikett (X.Y.Z) – inget har ändrats."

  if ! ar_nyare "$erbjuden" "$kor"; then
    satt_lage vantar_sedan ""
    satt_lage senaste_kontroll "$(tid) – ingen ny version (kör $kor, erbjuds $erbjuden)"
    info "Ingen ny version (kör $kor, erbjuds $erbjuden)."
    exit 0
  fi
  miss="$(misslyckad_version)"
  if [[ "$erbjuden" == "$miss" ]]; then
    satt_lage senaste_kontroll "$(tid) – $erbjuden hoppas över (fungerade inte förra gången), kör $kor"
    info "Version $erbjuden fungerade inte förra gången – försöker inte igen (kör $kor)."
    exit 0
  fi
  if ar_upptagen; then
    satt_lage senaste_kontroll "$(tid) – $erbjuden väntar: tjänsten är upptagen (kör $kor)"
    notera_vantan "$erbjuden"
    info "Ny version $erbjuden finns, men tjänsten är upptagen ($UPPTAGEN) – väntar till nästa kontroll."
    exit 0
  fi
  satt_lage vantar_sedan ""
  compose_fungerar || die "Ingen uppgradering till $erbjuden: $FEL – inget har ändrats."
  satt_lage senaste_kontroll "$(tid) – ny version $erbjuden (kör $kor)"
  info "Ny version erbjuds: $erbjuden (kör $kor)."
  uppgradera "$kor" "$erbjuden" "$ny_id" "$gammal_id"
}

# --- Start ---------------------------------------------------------------------------------------------
TIDSSTYRD=0
case "$#:${1:-}" in
  0:) ;;
  1:--tidsstyrd) TIDSSTYRD=1 ;;
  1:--lage) ;;
  *)
    printf 'Användning (som root):\n  %s               kontrollera nu och uppdatera om en nyare version finns\n  %s --tidsstyrd   samma, körs av timern\n  %s --lage        visa läget\n' "$0" "$0" "$0" >&2
    exit 2
    ;;
esac
[[ $EUID -eq 0 ]] || { echo "Kör som root: sudo $0 $*" >&2; exit 1; }
ar_sokvag "$KATALOG" && [[ -d "$KATALOG" ]] || { echo "Installationen ($KATALOG) finns inte – kör installationsraden." >&2; exit 1; }
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || { echo "Ogiltigt containernamn." >&2; exit 1; }
[[ "$PULL" == 0 || "$PULL" == 1 ]] || { echo "TRANSCRIBE_PULL ska vara 0 eller 1." >&2; exit 1; }
[[ "$VANTA" =~ ^[0-9]{1,5}$ && "$STABIL" =~ ^[0-9]{1,4}$ && "$ANTAL_KOPIOR" =~ ^[1-9][0-9]?$ ]] \
  || { echo "Ogiltigt TRANSCRIBE_VANTA, TRANSCRIBE_STABIL eller TRANSCRIBE_KOPIOR." >&2; exit 1; }
[[ -n "$IMAGE" ]] || IMAGE="$(las_env TRANSCRIBE_IMAGE)"
[[ -n "$IMAGE" ]] || IMAGE="$STANDARD_IMAGE"
ar_avbildsnamn "$IMAGE" || { echo "Ogiltigt avbildsnamn i TRANSCRIBE_IMAGE." >&2; exit 1; }

if [[ "${1:-}" == --lage ]]; then
  visa_lage
  exit 0
fi
kontrollera "$TIDSSTYRD"
