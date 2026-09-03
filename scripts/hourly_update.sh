#!/usr/bin/env bash
#
# Hourly incremental update (evenings): scrape just the last day's scoreboard, load, and refresh
# the cumulative matview so scores appear within the hour instead of once a day. This is a TRIMMED
# version of daily_update.sh — it deliberately SKIPS `enrich rpi/avca` and `snapshot-rankings`,
# which follow a daily/weekly cadence (the AVCA/RPI polls change weekly) and are owned by the
# authoritative 01:00 daily run. Safe to re-run: the scrape skips contests already in CSV+DB.
#
# Overlap guard: takes a non-blocking flock on a shared lock file, so if the daily run (or a
# previous hourly) is still going, this run is a clean no-op rather than a second browser + a
# concurrent matview refresh. Driven by vb-hourly.timer (evening game hours only). See
# daily_update.sh for the authoritative full pass.
#
set -euo pipefail

# --- shared lock with the daily job: skip cleanly if another update is already running ---
LOCK="/tmp/vb_update.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "=== vb hourly update: another update is running; skipping @ $(date -Is) ==="
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

# --- Sentry cron monitor (started only after the flock, so clean skips aren't counted as runs) ---
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/sentry_cron.sh"
CHECKIN_ID="$(sentry_checkin_start "vb-hourly-scrape" "7 0,16-23 * * *" 30 20)"
trap 'sentry_checkin_finish "vb-hourly-scrape" "$CHECKIN_ID" "$([ $? -eq 0 ] && echo ok || echo error)"' EXIT

# Season = fall year. Aug–Dec -> current year; Jan–Jul -> previous year. Override with VB_SEASON.
if [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb hourly update: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate

# Ensure Postgres is up (idempotent).
docker compose up -d db
# Wait for it to accept connections.
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

# Scrape needs a browser -> run under a virtual display (headful Chromium beats Akamai's headless
# checks on ARM). --days-back 1 keeps hourly runs light; the 01:00 daily uses a wider window
# (--days-back 3) to catch anything posted late.
xvfb-run -a vb scrape game-stats --year "$SEASON" --days-back 1

vb load-game-stats   --season "$SEASON"
vb derive-cumulative --season "$SEASON"

echo "=== vb hourly update complete @ $(date -Is) ==="
