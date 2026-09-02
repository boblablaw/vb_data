#!/usr/bin/env bash
#
# Daily incremental update: scrape only the last few days' contests via the daily scoreboard
# (one fetch per date, not a page per team), load, derive cumulative, refresh RPI, and dump
# CSV snapshots. Safe to re-run — the scrape skips contests already in the CSV *and* the DB, so
# a second run adds nothing. The weekly job (weekly_rosters.sh) does a full team-sweep reconcile
# that catches any late-posted contest the scoreboard missed. Driven by vb-daily.timer.
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
# headless checks on hosts without real Google Chrome, e.g. ARM). --days-back covers the last
# few scoreboards so a missed run (or a contest posted a day late) is still picked up.
xvfb-run -a vb scrape game-stats --year "$SEASON" --days-back 3

vb load-game-stats   --season "$SEASON"
vb derive-cumulative --season "$SEASON"
vb enrich rpi
vb enrich avca

echo "=== vb daily update complete @ $(date -Is) ==="
