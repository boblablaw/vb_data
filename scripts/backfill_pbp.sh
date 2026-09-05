#!/usr/bin/env bash
#
# One-time play-by-play backfill for a whole season. Does a FULL team sweep (one fetch per contest
# that doesn't already have pbp_events), loads the touch-level events + venue/attendance, then
# derives the season setter stats over everything. Resumable: the scraper skips contests already in
# the CSV *and* the DB, so re-running after an interruption picks up where it left off.
#
# Long-running (one browser fetch per contest, hundreds of contests) — run it under tmux/nohup:
#   tmux new -s pbp 'scripts/backfill_pbp.sh 2026'
# or:
#   nohup scripts/backfill_pbp.sh 2026 >/tmp/pbp_backfill.log 2>&1 &
#
# Season: first positional arg, else $VB_SEASON, else inferred from the date (Aug–Dec -> this year).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

if [ -n "${1:-}" ]; then
  SEASON="$1"
elif [ -n "${VB_SEASON:-}" ]; then
  SEASON="$VB_SEASON"
else
  month=$((10#$(date +%m)))
  year=$(date +%Y)
  if [ "$month" -ge 8 ]; then SEASON="$year"; else SEASON=$((year - 1)); fi
fi

echo "=== vb PBP backfill: season $SEASON @ $(date -Is) ==="

# shellcheck disable=SC1091
source venv/bin/activate

# Ensure Postgres is up and accepting connections (idempotent).
docker compose up -d db
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

# Full team sweep (no --days-back). Headful Chromium under Xvfb to clear Akamai on ARM.
xvfb-run -a vb scrape pbp --year "$SEASON"

# Idempotent per contest (delete-then-insert), also writes venue/attendance onto contests.
vb load-pbp   --season "$SEASON"

# Recompute set attempts / assist % / setter hitting % / points played over the full season.
vb derive-pbp --season "$SEASON"

echo "=== vb PBP backfill complete: season $SEASON @ $(date -Is) ==="
