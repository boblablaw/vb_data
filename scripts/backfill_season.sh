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
CHECKIN_ID="$(sentry_checkin_start "vb-backfill" "12 2 * * *" 480 30)"
# The systemd unit hard-stops this run with SIGTERM at its RuntimeMaxSec window (a planned,
# resumable cutoff — NOT a failure). Catch SIGTERM so the run exits 0 and the Sentry check-in below
# reports "ok" instead of paging every night; the next window picks up where it left off.
WINDOW_STOP=0
trap 'WINDOW_STOP=1; exit 0' TERM
_backfill_finish() {
  local rc=$?
  if [ "$rc" -eq 0 ] || [ "$WINDOW_STOP" -eq 1 ]; then st=ok; else st=error; fi
  sentry_checkin_finish "vb-backfill" "$CHECKIN_ID" "$st"
}
trap _backfill_finish EXIT

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

# Scraper egress is isolated onto a rotating residential proxy (VB_PROXY_* in .env), so a per-team
# fetch failure is usually just a slow/dead/pre-flagged exit IP — NOT a serving-IP block. The default
# 25% abort guard (vb_scrape_fail_threshold) was tuned for scraping off the serving IP; over the proxy
# that rate is normal and tripping it would throw away a whole night's work. Raise the guard for the
# backfill so only a NEAR-TOTAL failure (a genuine outage/block) aborts; anything less is tolerated and
# the successfully-scraped contests are still loaded below (the scrape appends to a resumable CSV and
# the load steps run even if the sweep exits non-zero). Respects an explicit env override.
export VB_SCRAPE_FAIL_THRESHOLD="${VB_SCRAPE_FAIL_THRESHOLD:-0.9}"

# Ensure Postgres is up and accepting connections (idempotent).
docker compose up -d db
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U vb -d vb >/dev/null 2>&1; then break; fi
  sleep 2
done

# Teams/conferences are global; this just (re)asserts the season's team_season_ids mapping.
vb load-teams --season "$SEASON"

# Per-season conference affiliations (realignment-aware; overrides the global default for this
# season). Needs real Chrome — stats.ncaa.org is Akamai-gated, like the other scrapes.
echo "--- season conferences @ $(date -Is) ---"
xvfb-run -a vb load-season-conferences --season "$SEASON"

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
# The scrape appends every success to a resumable CSV, so if the sweep exits non-zero (its post-loop
# abort guard, or an interruption) we STILL load what it managed to scrape rather than discard the
# night's work; a true near-total block just leaves the CSV empty and the load a no-op.
echo "--- game-stats @ $(date -Is) ---"
xvfb-run -a vb scrape game-stats --year "$SEASON" \
  || echo "!! game-stats sweep exited non-zero (partial); loading what was scraped"
vb load-game-stats --season "$SEASON"

# Refresh the cumulative matview NOW, before the heavy/interruptible PBP scrape below. This step
# runs on every (resumable) night right after whatever new box scores loaded, so the season's
# cumulative totals stay consistent with the loaded game stats even if the run is SIGTERM'd
# mid-PBP-sweep and never reaches the derive-pbp step. (Global refresh; the --season flag is a
# no-op but kept for symmetry.)
vb derive-cumulative --season "$SEASON"

# Play-by-play — one fetch per contest without pbp_events yet (the heavy part). Resumable.
# Same contract as game-stats above: the sweep appends each contest to a resumable CSV and only
# raises its failure-rate guard AFTER attempting every team, so a non-zero exit never means less was
# scraped — load whatever landed on disk regardless (this is what was silently discarded before).
echo "--- play-by-play @ $(date -Is) ---"
xvfb-run -a vb scrape pbp --year "$SEASON" \
  || echo "!! pbp sweep exited non-zero (partial); loading what was scraped"
vb load-pbp --season "$SEASON"

# Derived setter/PBP stats over the full season (needs the PBP loaded above).
echo "--- derive @ $(date -Is) ---"
vb derive-pbp --season "$SEASON"

# ncaa.com game-id mapping so played games link out (plain HTTP, one call per date; idempotent).
vb map-ncaa-games --season "$SEASON"

echo "=== vb season backfill complete: season $SEASON @ $(date -Is) ==="
