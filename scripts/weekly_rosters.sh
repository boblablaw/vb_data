#!/usr/bin/env bash
#
# Weekly maintenance: (1) roster refresh, then (2) a full team-sweep game-stats reconcile.
#
# (1) New mid-season players are skipped by the game-stats loader until they appear on a roster,
#     so re-scrape rosters periodically. The roster scraper skips teams already present in its
#     CSV, so we reset the CSV first; the loader upserts, so existing players are updated in
#     place, not duplicated.
# (2) The daily job scrapes only the daily scoreboard, which can miss a contest that was posted
#     late or on an off day. The weekly full sweep (a page per team) catches those, then loads /
#     derives / enriches so the reconciled contests land in the DB. Rosters run first so any new
#     players exist before the sweep loads their stats.
#
# Driven by vb-weekly-rosters.timer.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

if [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb weekly roster refresh: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate
docker compose up -d db

# Reset the resume ledger so every team is re-scraped (picks up new players / edits).
ROSTER_CSV="staging/ncaa_wvb_rosters_d1_${SEASON}.csv"
COACH_CSV="staging/ncaa_wvb_coaches_d1_${SEASON}.csv"
[ -f "$ROSTER_CSV" ] && mv -f "$ROSTER_CSV" "${ROSTER_CSV}.$(date +%Y%m%d).bak"
[ -f "$COACH_CSV" ] && mv -f "$COACH_CSV" "${COACH_CSV}.$(date +%Y%m%d).bak"

xvfb-run -a vb scrape rosters --year "$SEASON"
vb load-rosters --season "$SEASON"
vb load-coaches --season "$SEASON"

echo "=== vb weekly schedule refresh: season $SEASON @ $(date -Is) ==="
# Team schedules (upcoming + played) change slowly, so weekly is the right cadence. Reset the
# resume ledger so every team's page is re-scraped (new/rescheduled games); the loader upserts on
# (season, team_id, date, opponent_name), so re-loading updates in place rather than duplicating.
SCHEDULE_CSV="staging/ncaa_wvb_schedule_d1_${SEASON}.csv"
[ -f "$SCHEDULE_CSV" ] && mv -f "$SCHEDULE_CSV" "${SCHEDULE_CSV}.$(date +%Y%m%d).bak"
xvfb-run -a vb scrape schedule --year "$SEASON"
vb load-schedule --season "$SEASON"

echo "=== vb weekly full game-stats reconcile: season $SEASON @ $(date -Is) ==="
# Full team sweep (one page per team) catches any contest the daily scoreboard missed. Resumable:
# only contests without stats yet are fetched, so this is cheap after a week of daily runs.
xvfb-run -a vb scrape game-stats --year "$SEASON"
vb load-game-stats   --season "$SEASON"
vb derive-cumulative --season "$SEASON"
vb enrich rpi

echo "=== vb weekly maintenance complete @ $(date -Is) ==="
