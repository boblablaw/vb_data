#!/usr/bin/env bash
#
# sync_from_prod.sh — copy the PROD Postgres database into your LOCAL one for testing.
#
# Streams a pg_dump from the OCI box (container vb_data_postgres) over SSH and restores it into
# the local vb_data_postgres container, so local mirrors prod (contests, ranking snapshots, etc.).
# The daily/weekly jobs keep prod moving, so re-run this whenever local has drifted.
#
# DESTRUCTIVE to LOCAL: --clean drops and recreates every object, and this also pulls prod's
# users/favorites/passkeys rows into local. It never writes to prod (pg_dump is read-only).
#
#   scripts/sync_from_prod.sh              # dump prod -> restore local
#   SSH_HOST=oracle scripts/sync_from_prod.sh   # override the SSH host (default: oracle)
#
set -euo pipefail

SSH_HOST="${SSH_HOST:-oracle}"
CONTAINER="${PG_CONTAINER:-vb_data_postgres}"
PGUSER="${PGUSER:-vb}"
PGDB="${PGDB:-vb}"
DUMP="${TMPDIR:-/tmp}/vb_prod_$(date +%Y%m%d_%H%M%S).dump"

echo ">> Dumping prod ($SSH_HOST:$CONTAINER/$PGDB) to $DUMP ..."
ssh "$SSH_HOST" "docker exec $CONTAINER pg_dump -U $PGUSER -d $PGDB -Fc" > "$DUMP"
if ! file "$DUMP" | grep -q "PostgreSQL custom database dump"; then
  echo "!! Dump does not look like a valid pg_dump custom archive — aborting." >&2
  head -c 300 "$DUMP" >&2; echo >&2
  exit 1
fi
echo "   dump ok ($(du -h "$DUMP" | cut -f1))"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "!! Local container '$CONTAINER' is not running — start it first (docker compose up -d)." >&2
  exit 1
fi

echo ">> Terminating other local connections to '$PGDB' ..."
docker exec "$CONTAINER" psql -U "$PGUSER" -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$PGDB' AND pid<>pg_backend_pid();" \
  >/dev/null

echo ">> Restoring into local ($CONTAINER/$PGDB) ..."
# --no-owner/--no-privileges avoids failing on prod-only roles (e.g. vb_app); data still restores.
docker exec -i "$CONTAINER" pg_restore -U "$PGUSER" -d "$PGDB" \
  --clean --if-exists --no-owner --no-privileges < "$DUMP"

echo ">> Refreshing materialized views ..."
docker exec "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" -c \
  "REFRESH MATERIALIZED VIEW player_season_stats;" >/dev/null

echo ">> Done. Local now mirrors prod:"
docker exec "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" -t -c \
  "SELECT 'size='||pg_size_pretty(pg_database_size('$PGDB'))
        ||'  alembic='||(SELECT version_num FROM alembic_version);"

echo "   (dump kept at $DUMP — delete when done.)"
