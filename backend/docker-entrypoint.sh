#!/bin/sh
# Startar API, worker eller schemaläggare. API-rollen kör databasmigrationerna.
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
