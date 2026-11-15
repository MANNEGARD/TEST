#!/usr/bin/env bash
# Röktest av en körande DriveLog-installation genom proxyn.
#
#   deploy/smoke-test.sh https://drivelog.smgd.se
#   deploy/smoke-test.sh https://localhost          (lokalt certifikat godtas)
#
# Kontrollerar att API:t och webbappen svarar över HTTPS, att http skickas vidare till https,
# att säkerhetshuvudena finns och att inget går att läsa utan inloggning. Mot en NY installation
# (inget konto skapat ännu) skapas dessutom ett testkonto för att pröva databas, session och
# CSRF-skydd – kör därför den delen bara i test, med flaggan --with-setup.
set -euo pipefail

BASE="${1:?Ange adressen, t.ex. https://drivelog.smgd.se}"
WITH_SETUP=0
[[ "${2:-}" == "--with-setup" ]] && WITH_SETUP=1
HOST="${BASE#https://}"
CURL=(curl -sS --max-time 20)
if [[ "$HOST" == localhost* ]]; then
  CURL+=(-k)
  [[ "${2:-}" == "" ]] && WITH_SETUP=1 # lokalt: alltid en testinstallation
fi

fail() { echo "✗ $*" >&2; exit 1; }
ok() { echo "✓ $*"; }

# Vänta tills proxyn svarar (certifikatet kan ta några sekunder).
for _ in $(seq 1 30); do
  "${CURL[@]}" -f -o /dev/null "$BASE/api/v1/health" 2>/dev/null && break
  sleep 2
done

ready="$("${CURL[@]}" -f "$BASE/api/v1/health/ready")" || fail "API:t svarar inte på /api/v1/health/ready"
echo "$ready" | grep -q '"status"' || fail "Oväntat svar från hälsokontrollen: $ready"
ok "API:t är redo: $ready"

code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/login")"
[[ "$code" == 200 ]] || fail "Webbappens inloggningssida gav $code"
ok "Webbappen svarar (/login 200)"

headers="$("${CURL[@]}" -o /dev/null -D - "$BASE/login" | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
for h in "strict-transport-security" "x-content-type-options: nosniff" "x-frame-options: deny"; do
  echo "$headers" | grep -q "$h" || fail "Säkerhetshuvud saknas: $h"
done
ok "Säkerhetshuvuden finns"

redirect="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "http://$HOST/" || true)"
[[ "$redirect" == 30* ]] || fail "http skickas inte vidare till https (svar $redirect)"
ok "http skickas vidare till https ($redirect)"

code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/api/v1/trips")"
[[ "$code" == 401 ]] || fail "API:t svarade $code utan inloggning (förväntat 401)"
ok "API:t kräver inloggning (401 utan session)"

if [[ "$WITH_SETUP" == 1 ]]; then
  status="$("${CURL[@]}" -f "$BASE/api/v1/auth/setup-status")"
  if echo "$status" | grep -q '"needs_setup": *true'; then
    jar="$(mktemp)"
    trap 'rm -f "$jar"' EXIT
    body="$("${CURL[@]}" -f -c "$jar" -H 'content-type: application/json' \
      -d '{"email":"rok@example.com","password":"ett-langt-losenord-for-roktest"}' "$BASE/api/v1/auth/setup")" \
      || fail "Det gick inte att skapa första kontot"
    csrf="$(echo "$body" | sed -n 's/.*"csrf_token": *"\([^"]*\)".*/\1/p')"
    [[ -n "$csrf" ]] || fail "Inget CSRF-värde i svaret"
    code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' "$BASE/api/v1/vehicles")"
    [[ "$code" == 200 ]] || fail "Inloggad läsning gav $code"
    code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' -X PATCH -H 'content-type: application/json' \
      -d '{"driver_name":"Rök Test"}' "$BASE/api/v1/settings/general")"
    [[ "$code" == 403 ]] || fail "Ändring utan CSRF-värde gav $code (förväntat 403)"
    code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' -X PATCH -H 'content-type: application/json' \
      -H "X-CSRF-Token: $csrf" -d '{"driver_name":"Rök Test"}' "$BASE/api/v1/settings/general")"
    [[ "$code" == 200 ]] || fail "Ändring med CSRF-värde gav $code"
    ok "Konto, session, databas och CSRF-skydd fungerar"
  else
    ok "Ett konto finns redan – hoppar över testkontot"
  fi
fi

echo "Röktestet gick igenom."
