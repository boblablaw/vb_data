#!/usr/bin/env bash
#
# Daily scouting-report rebuild. Runs the deterministic scout builder (`vb build-scouting`) once a
# day — morning — to refresh every team's precomputed scouting report from the data available to
# that point. No browser, no scrape, no matview refresh: it only reads the existing box-score /
# play-by-play tables and upserts the `scouting_reports` rows, so it is cheap (a single DB pass).
# Driven by vb-scouting.timer. (Filename kept as scouting_weekly.sh for deploy stability.)
#
# Overlap guard: shares the scrape jobs' lock. The builder only writes scouting_reports (never the
# browser or the cumulative matview), so it is safe alongside a scrape — but we still wait briefly
# for an in-flight run rather than piling a concurrent write on top of it, then proceed anyway.
#
set -euo pipefail

# --- shared lock with the scrape jobs: wait a little, then proceed regardless ---
LOCK="/tmp/vb_update.lock"
exec 9>"$LOCK"
flock -w 120 9 || echo "warning: update lock wait timed out; proceeding anyway"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

# --- Sentry cron monitor: alert if this weekly run goes missing or fails (no-op without a DSN) ---
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/sentry_cron.sh"
CHECKIN_ID="$(sentry_checkin_start "vb-scouting" "30 7 * * *" 30 90)"
trap 'sentry_checkin_finish "vb-scouting" "$CHECKIN_ID" "$([ $? -eq 0 ] && echo ok || echo error)"' EXIT

# Season = fall year. Aug–Dec -> current year; Jan–Jul -> previous year. Override with VB_SEASON.
if [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb scouting build: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate

# Ensure Postgres is up (idempotent).
docker compose up -d db
# Wait for it to accept connections.
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

vb build-scouting --season "$SEASON"

echo "=== vb scouting build complete @ $(date -Is) ==="
