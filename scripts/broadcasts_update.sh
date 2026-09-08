#!/usr/bin/env bash
#
# Hourly TV/streaming broadcast refresh. Attaches network tags to game cards from the public
# conference ICS calendars (primary) + the personal TPS IPTV feeds (fallback; skipped when the
# TPS_* creds are absent from the env). Plain HTTP — no browser, no matview refresh — so it is
# cheap enough to run every hour, unlike the heavy scrape jobs (daily_update.sh / hourly_update.sh),
# which only fire during game hours. Networks get announced/changed at all times of day, so this
# runs 24/7 to keep the "on TV" tags fresh within the hour. Driven by vb-broadcasts.timer.
#
# Overlap guard: shares the same lock file as the scrape jobs. This job touches only game
# broadcast/network columns (never the browser or the cumulative matview), so it is harmless to
# run alongside them — but we still WAIT briefly for an in-flight scrape rather than piling a
# concurrent DB write on top of it, then proceed anyway (a missed broadcast refresh is trivial).
#
set -euo pipefail

# --- shared lock with the scrape jobs: wait a little, then proceed regardless ---
LOCK="/tmp/vb_update.lock"
exec 9>"$LOCK"
flock -w 120 9 || echo "warning: update lock wait timed out; proceeding anyway"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

# --- Sentry cron monitor: alert if this hourly run goes missing or fails (no-op without a DSN) ---
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/sentry_cron.sh"
CHECKIN_ID="$(sentry_checkin_start "vb-broadcasts" "37 * * * *" 15 20)"
trap 'sentry_checkin_finish "vb-broadcasts" "$CHECKIN_ID" "$([ $? -eq 0 ] && echo ok || echo error)"' EXIT

# Season = fall year. Aug–Dec -> current year; Jan–Jul -> previous year. Override with VB_SEASON.
if [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb broadcasts update: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate

# Ensure Postgres is up (idempotent).
docker compose up -d db
# Wait for it to accept connections.
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

# Refresh recent + near-term games so both day-to-day TV changes and freshly-announced networks
# propagate within the hour. Same window the daily pass used before this moved to its own timer.
vb ingest-broadcasts --season "$SEASON" --days-back 3 --days-ahead 10

echo "=== vb broadcasts update complete @ $(date -Is) ==="
