#!/usr/bin/env bash
#
# Daily incremental update: scrape only NEW contests, load, derive cumulative, refresh RPI,
# and dump CSV snapshots. Safe to re-run — the game-stats scrape skips contests already in the
# CSV *and* the DB, so a second run adds nothing. Intended to be driven by vb-daily.timer.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

# Season = fall year. Aug–Dec -> current year; Jan–Jul -> previous year. Override with VB_SEASON.
if [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb daily update: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate

# Ensure Postgres is up (idempotent).
docker compose up -d db
# Wait for it to accept connections.
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

# Scrape needs a browser -> run under a virtual display (headful Chromium beats Akamai's
# headless checks on hosts without real Google Chrome, e.g. ARM).
xvfb-run -a vb scrape game-stats --year "$SEASON"

vb load-game-stats   --season "$SEASON"
vb derive-cumulative --season "$SEASON"
vb enrich rpi

# CSV snapshots (optional but cheap; overwrite each run).
vb export merged     --season "$SEASON"
vb export game_stats --season "$SEASON"

echo "=== vb daily update complete @ $(date -Is) ==="
