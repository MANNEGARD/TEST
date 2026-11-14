#!/usr/bin/env bash
# Kör frontendens röktest mot en helt ny databas.
#   1. bygger frontend (om .next saknas eller E2E_BUILD=1)
#   2. startar backend (:8000, SQLite i temporär katalog) och `next start` (:3000)
#   3. skriver exempelexporter och kör frontend/e2e/smoke.mjs
# Användning: scripts/e2e.sh [skärmbildskatalog]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
SHOTS="${1:-$WORK/shots}"
API_PORT="${E2E_API_PORT:-8000}"
WEB_PORT="${E2E_WEB_PORT:-3000}"

cleanup() {
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for port in "$API_PORT" "$WEB_PORT"; do
  if curl -s -o /dev/null "http://127.0.0.1:$port/"; then
    echo "Port $port används redan – stoppa den servern först (röktestet kräver en ren databas)." >&2
    exit 2
  fi
done

cd "$ROOT/frontend"
if [[ ! -d .next || "${E2E_BUILD:-0}" == "1" ]]; then
  API_INTERNAL_URL="http://127.0.0.1:$API_PORT" npm run build
fi

cd "$ROOT/backend"
DRIVELOG_ENVIRONMENT=development DRIVELOG_JOBS_EAGER=true DRIVELOG_BASE_URL="http://localhost:$WEB_PORT" \
DRIVELOG_DATA_DIR="$WORK/data" DRIVELOG_DATABASE_URL="sqlite:///$WORK/data.sqlite3" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >"$WORK/api.log" 2>&1 &
API_PID=$!
.venv/bin/python "$ROOT/scripts/make_sample_exports.py" "$WORK/samples" >/dev/null

cd "$ROOT/frontend"
API_INTERNAL_URL="http://127.0.0.1:$API_PORT" node_modules/.bin/next start --port "$WEB_PORT" >"$WORK/web.log" 2>&1 &
WEB_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$API_PORT/api/v1/auth/setup-status" >/dev/null 2>&1 \
     && curl -fsS "http://127.0.0.1:$WEB_PORT/api/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done

BASE_URL="http://localhost:$WEB_PORT" SAMPLES_DIR="$WORK/samples" SHOTS_DIR="$SHOTS" node e2e/smoke.mjs
