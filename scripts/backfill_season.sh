#!/usr/bin/env bash
#
# One-time, resumable backfill of a WHOLE (usually historical) season: rosters, coaches, schedule,
# per-contest box scores, and play-by-play, then the derived season/setter stats. Intended for
# adding a past season (e.g. 2025) with full parity to the live season, minus the current-season-only
# enrichments (see below).
#
# Designed to run in a nightly off-peak window under a systemd timer (vb-backfill.timer) with a hard
# RuntimeMaxSec: every scraper is resumable (it skips contests already in the CSV *and* the DB), so a
# SIGTERM mid-run is safe and the next night picks up where it left off. Over a few nights it
# converges on the full season.
#
# Season: first positional arg (e.g. `backfill_season.sh 2025`), else $VB_SEASON, else inferred from
# the date (Aug–Dec -> this year).
#
# NOTE: deliberately does NOT run `enrich rpi` / `enrich avca` / `snapshot-rankings`. Those write the
# GLOBAL teams.rpi_rank/avca_rank from the *live current* NCAA/AVCA tables — running them for a past
# season is meaningless and would clobber the current season's ranks.
#
set -euo pipefail

# --- shared lock with the live hourly/daily/weekly jobs ---
# Never drive two Chrome/Xvfb sweeps at NCAA at once. Non-blocking: if a live scrape holds the lock,
# skip this run entirely and let the timer resume tomorrow night (the backfill is fully resumable).
LOCK="/tmp/vb_update.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "vb backfill: update lock held by a live scrape; skipping this run (will resume next window)"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

# --- Sentry cron monitor: alert if a scheduled backfill run fails (no-op without a DSN) ---
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/sentry_cron.sh"
CHECKIN_ID="$(sentry_checkin_start "vb-backfill" "12 2 * * *" 360 30)"
trap 'sentry_checkin_finish "vb-backfill" "$CHECKIN_ID" "$([ $? -eq 0 ] && echo ok || echo error)"' EXIT

if [ -n "${1:-}" ]; then
  SEASON="$1"
elif [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb season backfill: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate

# Ensure Postgres is up and accepting connections (idempotent).
docker compose up -d db
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

# Teams/conferences are global; this just (re)asserts the season's team_season_ids mapping.
vb load-teams --season "$SEASON"

# Rosters + coaches (players must exist before game-stats can attribute lines to them).
echo "--- rosters @ $(date -Is) ---"
xvfb-run -a vb scrape rosters --year "$SEASON"
vb load-rosters --season "$SEASON"
vb load-coaches --season "$SEASON"

# Team schedules (played + any upcoming; for a past season these are all final).
echo "--- schedule @ $(date -Is) ---"
xvfb-run -a vb scrape schedule --year "$SEASON"
vb load-schedule --season "$SEASON"

# Per-contest box scores — full team sweep (a page per team, then a page per contest). Resumable.
echo "--- game-stats @ $(date -Is) ---"
xvfb-run -a vb scrape game-stats --year "$SEASON"
vb load-game-stats --season "$SEASON"

# Play-by-play — one fetch per contest without pbp_events yet (the heavy part). Resumable.
echo "--- play-by-play @ $(date -Is) ---"
xvfb-run -a vb scrape pbp --year "$SEASON"
vb load-pbp --season "$SEASON"

# Derived stats: cumulative matview (global; picks up the new season) + season setter stats.
echo "--- derive @ $(date -Is) ---"
vb derive-cumulative --season "$SEASON"
vb derive-pbp --season "$SEASON"

# ncaa.com game-id mapping so played games link out (plain HTTP, one call per date; idempotent).
vb map-ncaa-games --season "$SEASON"

echo "=== vb season backfill complete: season $SEASON @ $(date -Is) ==="
