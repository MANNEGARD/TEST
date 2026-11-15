#!/usr/bin/env bash
# Röktest av en körande DriveLog-installation genom proxyn.
#
#   deploy/smoke-test.sh https://drivelog.smgd.se
#   deploy/smoke-test.sh https://localhost          (lokalt certifikat godtas)
#
# Kontrollerar att API:t och webbappen svarar över HTTPS, att http skickas vidare till https,
# att säkerhetshuvudena finns, att API-dokumentationen är stängd och att inget går att läsa
# utan inloggning. Ingenting ändras i installationen.
#
# Anges SMOKE_EMAIL och SMOKE_PASSWORD i miljön loggar testet dessutom in och prövar session
# och CSRF-skydd (det loggar ut igen och ändrar inga uppgifter). Mot en lokal testinstallation
# utan konto (localhost) skapas i stället ett testkonto med slumpat lösenord.
set -euo pipefail

BASE="${1:?Ange adressen, t.ex. https://drivelog.smgd.se}"
HOST="${BASE#https://}"
HOST="${HOST%%/*}"
CURL=(curl -sS --max-time 20)
LOCAL=0
if [[ "$HOST" == localhost || "$HOST" == localhost:* ]]; then
  LOCAL=1
  CURL+=(-k) # lokalt certifikat
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

code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/api/v1/docs")"
[[ "$code" == 404 ]] || fail "API-dokumentationen är öppen utan inloggning (svar $code)"
ok "API-dokumentationen är stängd"

jar="$(mktemp)"
trap 'rm -f "$jar"' EXIT
csrf=""
json_field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p"; }

if [[ -n "${SMOKE_EMAIL:-}" && -n "${SMOKE_PASSWORD:-}" ]]; then
  payload="$(printf '{"email":"%s","password":"%s"}' "$SMOKE_EMAIL" "$SMOKE_PASSWORD")"
  body="$("${CURL[@]}" -f -c "$jar" -H 'content-type: application/json' -d "$payload" "$BASE/api/v1/auth/login")" \
    || fail "Inloggningen misslyckades"
  csrf="$(echo "$body" | json_field csrf_token)"
elif [[ "$LOCAL" == 1 ]] && "${CURL[@]}" -f "$BASE/api/v1/auth/setup-status" | grep -q '"needs_setup": *true'; then
  # Endast mot en lokal testinstallation: skapa ett testkonto med slumpat lösenord.
  payload="$(printf '{"email":"rok@example.com","password":"%s"}' "$(openssl rand -hex 16)")"
  body="$("${CURL[@]}" -f -c "$jar" -H 'content-type: application/json' -d "$payload" "$BASE/api/v1/auth/setup")" \
    || fail "Det gick inte att skapa testkontot"
  csrf="$(echo "$body" | json_field csrf_token)"
fi

if [[ -n "$csrf" ]]; then
  code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' "$BASE/api/v1/vehicles")"
  [[ "$code" == 200 ]] || fail "Inloggad läsning gav $code"
  # Utan CSRF-värde ska en ändring avvisas (ingenting ändras); med värdet går utloggningen igenom.
  code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/logout")"
  [[ "$code" == 403 ]] || fail "Anrop utan CSRF-värde gav $code (förväntat 403)"
  code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' -X POST -H "X-CSRF-Token: $csrf" "$BASE/api/v1/auth/logout")"
  [[ "$code" == 200 || "$code" == 204 ]] || fail "Utloggning med CSRF-värde gav $code"
  code="$("${CURL[@]}" -b "$jar" -o /dev/null -w '%{http_code}' "$BASE/api/v1/vehicles")"
  [[ "$code" == 401 ]] || fail "Sessionen gäller fortfarande efter utloggning (svar $code)"
  ok "Inloggning, session, databas och CSRF-skydd fungerar"
else
  ok "Ingen inloggning angiven – session och CSRF prövades inte"
fi

echo "Röktestet gick igenom."
