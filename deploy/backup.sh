#!/bin/sh
# Säkerhetskopierar databasen (pg_dump) och uppladdade filer till ./backups.
# Kör från katalogen med docker-compose.yml, t.ex. via cron:  0 3 * * *  /srv/drivelog/deploy/backup.sh
set -eu

cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
mkdir -p "$DEST"
umask 077

docker compose exec -T db pg_dump -U drivelog -d drivelog --format=custom --no-owner > "$DEST/drivelog-db-$STAMP.dump"
docker compose exec -T api tar -C /data -czf - imports exports 2>/dev/null > "$DEST/drivelog-files-$STAMP.tar.gz" || true

# Kontrollera att databaskopian går att läsa innan gamla kopior gallras.
docker compose exec -T db pg_restore --list < "$DEST/drivelog-db-$STAMP.dump" > /dev/null

find "$DEST" -name 'drivelog-*' -type f -mtime "+$KEEP_DAYS" -delete
echo "Säkerhetskopia klar: $DEST/drivelog-db-$STAMP.dump"
echo "OBS: förvara även .env (DRIVELOG_ENCRYPTION_KEYS) säkert – utan nyckeln kan lagrade tokens inte läsas."
