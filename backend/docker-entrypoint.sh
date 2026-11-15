#!/bin/sh
# Startar API, worker eller schemaläggare. API-rollen kör databasmigrationerna.
# Enprocessläget (DRIVELOG_JOBS_EAGER=true, t.ex. Home Assistant-tillägget) använder bara
# rollen "api": jobben och schemat körs då i API-processen – starta aldrig fler än en.
set -eu

case "${1:-api}" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}"
    ;;
  worker)
    exec celery -A app.worker.celery_app:celery worker --loglevel="${DRIVELOG_LOG_LEVEL:-INFO}" --concurrency="${WORKER_CONCURRENCY:-2}" -Q drivelog
    ;;
  beat)
    exec celery -A app.worker.celery_app:celery beat --loglevel="${DRIVELOG_LOG_LEVEL:-INFO}" --schedule=/tmp/celerybeat-schedule
    ;;
  *)
    exec "$@"
    ;;
esac
