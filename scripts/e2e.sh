#!/usr/bin/env bash
# Kör frontendens helhetstest mot en helt ny databas.
#   1. bygger frontend (om bygget saknas, är byggt mot en annan API-port eller E2E_BUILD=1)
#   2. startar backend (SQLite i en temporär katalog) och `next start` på egna portar
#   3. skriver exempelexporter och kör frontend/e2e/smoke.mjs
#
# Användning: [E2E_API_PORT=8100] [E2E_WEB_PORT=3100] [E2E_BUILD=1] scripts/e2e.sh [skärmbildskatalog]
#
# Portarna får inte vara upptagna (testet kräver en ren databas). Standard är 8100/3100 så att
# testet kan köras medan utvecklingsmiljön (:8000/:3000) är igång. Bygget skriver till .next/
# men rör inte .next/dev/, så en körande `next dev` påverkas inte. Proxyns mål (API_INTERNAL_URL)
# bakas in i bygget – kör `npm run build` igen efteråt om du vill använda bygget mot :8000.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
SHOTS="${1:-$WORK/shots}"
API_PORT="${E2E_API_PORT:-8100}"
WEB_PORT="${E2E_WEB_PORT:-3100}"
STATUS=1

cleanup() {
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  if [[ "$STATUS" != "0" ]]; then
    echo "--- api.log (sista raderna): $WORK/api.log" >&2
    tail -n 30 "$WORK/api.log" >&2 2>/dev/null || true
    echo "--- web.log (sista raderna): $WORK/web.log" >&2
    tail -n 30 "$WORK/web.log" >&2 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ "$API_PORT" == "$WEB_PORT" ]]; then
  echo "E2E_API_PORT och E2E_WEB_PORT måste vara olika." >&2
  exit 2
fi
for port in "$API_PORT" "$WEB_PORT"; do
  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/"; then
    echo "Port $port används redan – välj en annan med E2E_API_PORT/E2E_WEB_PORT (testet kräver en ren databas)." >&2
    exit 2
  fi
done

cd "$ROOT/frontend"
API_URL="http://127.0.0.1:$API_PORT"
if [[ "${E2E_BUILD:-0}" == "1" || ! -f .next/BUILD_ID ]] || ! grep -qF "$API_URL/api/v1" .next/routes-manifest.json 2>/dev/null; then
  echo "Bygger frontend mot $API_URL …"
  API_INTERNAL_URL="$API_URL" npm run build
fi

cd "$ROOT/backend"
DRIVELOG_ENVIRONMENT=development DRIVELOG_JOBS_EAGER=true DRIVELOG_BASE_URL="http://localhost:$WEB_PORT" \
DRIVELOG_DATA_DIR="$WORK/data" DRIVELOG_DATABASE_URL="sqlite:///$WORK/data.sqlite3" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >"$WORK/api.log" 2>&1 &
API_PID=$!
.venv/bin/python "$ROOT/scripts/make_sample_exports.py" "$WORK/samples" >/dev/null

cd "$ROOT/frontend"
API_INTERNAL_URL="$API_URL" node_modules/.bin/next start --port "$WEB_PORT" >"$WORK/web.log" 2>&1 &
WEB_PID=$!

ready=0
for _ in $(seq 1 90); do
  if curl -fsS "$API_URL/api/v1/auth/setup-status" >/dev/null 2>&1 \
     && curl -fsS "http://127.0.0.1:$WEB_PORT/api/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "Servrarna startade inte inom 90 s." >&2
  exit 1
fi

set +e
BASE_URL="http://localhost:$WEB_PORT" SAMPLES_DIR="$WORK/samples" SHOTS_DIR="$SHOTS" \
PYTHON="$ROOT/backend/.venv/bin/python" WORK_DIR="$WORK" node e2e/smoke.mjs
STATUS=$?
set -e
exit "$STATUS"
