#!/usr/bin/env bash
#
# Weekly roster refresh. New mid-season players are skipped by the game-stats loader until they
# appear on a roster, so re-scrape rosters periodically. The roster scraper skips teams already
# present in its CSV, so we reset the CSV first; the loader upserts, so existing players are
# updated in place, not duplicated. Driven by vb-weekly-rosters.timer.
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
ROSTER_CSV="exports/ncaa_wvb_rosters_d1_${SEASON}.csv"
COACH_CSV="exports/ncaa_wvb_coaches_d1_${SEASON}.csv"
[ -f "$ROSTER_CSV" ] && mv -f "$ROSTER_CSV" "${ROSTER_CSV}.$(date +%Y%m%d).bak"
[ -f "$COACH_CSV" ] && mv -f "$COACH_CSV" "${COACH_CSV}.$(date +%Y%m%d).bak"

xvfb-run -a vb scrape rosters --year "$SEASON"
vb load-rosters --season "$SEASON"
vb load-coaches --season "$SEASON"

echo "=== vb weekly roster refresh complete @ $(date -Is) ==="
