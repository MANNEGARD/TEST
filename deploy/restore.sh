#!/bin/sh
# Återställer databasen från en pg_dump-kopia skapad av backup.sh.
# Användning: deploy/restore.sh backups/drivelog-db-<tid>.dump [backups/drivelog-files-<tid>.tar.gz]
set -eu

if [ $# -lt 1 ]; then
  echo "Användning: $0 <databaskopia.dump> [filkopia.tar.gz]" >&2
  exit 1
fi
cd "$(dirname "$0")/.."
DUMP="$1"
[ -f "$DUMP" ] || { echo "Filen $DUMP finns inte." >&2; exit 1; }

printf 'Detta ERSÄTTER all data i databasen. Skriv "ATERSTALL" för att fortsätta: '
read -r ANSWER
[ "$ANSWER" = "ATERSTALL" ] || { echo "Avbrutet."; exit 1; }

docker compose stop api worker beat web
docker compose exec -T db psql -U drivelog -d postgres -c "DROP DATABASE IF EXISTS drivelog" -c "CREATE DATABASE drivelog OWNER drivelog"
docker compose exec -T db pg_restore -U drivelog -d drivelog --no-owner < "$DUMP"
if [ $# -ge 2 ] && [ -f "$2" ]; then
  docker compose run --rm -T --entrypoint sh api -c "tar -C /data -xzf -" < "$2"
fi
docker compose up -d
echo "Återställningen är klar."
