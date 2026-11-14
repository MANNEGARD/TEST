#!/usr/bin/env bash
# Startar SMGD DriveLog lokalt för utveckling:
#   backend  – uvicorn på :8000 med SQLite i en datakatalog (jobb körs direkt, utan Redis)
#   frontend – next dev på :3000 (proxar /api/v1 till backend)
#
# Användning:  scripts/dev.sh [datakatalog]      (standard: .devdata i repots rot)
#   DRIVELOG_FRESH=1 scripts/dev.sh               – börja med en tom databas
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$(mkdir -p "${1:-$ROOT/.devdata}" && cd "${1:-$ROOT/.devdata}" && pwd)"

if [[ "${DRIVELOG_FRESH:-0}" == "1" ]]; then
  rm -f "$DATA_DIR"/drivelog.sqlite3*
fi

export DRIVELOG_ENVIRONMENT=development
export DRIVELOG_JOBS_EAGER=true
export DRIVELOG_BASE_URL=http://localhost:3000
export DRIVELOG_DATA_DIR="$DATA_DIR"
export DRIVELOG_DATABASE_URL="sqlite:///$DATA_DIR/drivelog.sqlite3"

cleanup() {
  trap - EXIT INT TERM
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$ROOT/backend"
# Utvecklingsläget skapar tabellerna själv vid start (init_schema i app.main).
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
API_PID=$!

cd "$ROOT/frontend"
[[ -d node_modules ]] || npm install
API_INTERNAL_URL=http://127.0.0.1:8000 node_modules/.bin/next dev --port 3000 &
WEB_PID=$!

echo "Backend:  http://127.0.0.1:8000/api/v1   (data: $DATA_DIR)"
echo "Frontend: http://localhost:3000"
wait -n "$API_PID" "$WEB_PID"
