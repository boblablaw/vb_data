# Data Sources & Reconciliation

How `vb_data` gets its data: what is scraped from where, which tables each source feeds, how the
pieces are stitched together when more than one source describes the same game, and the scheduling /
resilience model that keeps it flowing when a host blocks us.

> Design in one line: **CSV → Postgres loaders (idempotent upserts). Scrape writes CSVs; loaders
> ingest them; derive builds the analytics views.** (`src/vb/load/__init__.py`)

---

## 1. The sources (hosts)

There are **four external hosts**, plus a couple of one-off enrichment feeds. They are deliberately
split so that a block on the fragile one doesn't stop the pipeline.

| Source | Host | Transport | Blockable? | Role |
|---|---|---|---|---|
| **stats.ncaa.org** | NCAA official stats (Akamai) | **Real Chrome via Playwright** under `xvfb`, egress through a **rotating residential proxy** | **Yes** — Akamai IP-reputation blocks | The *rich* source: touch-level play-by-play, box scores, rosters, schedules, season stats, per-season conference affiliation |
| **ncaa.com** (via self-hosted **henrygd/ncaa-api** sidecar) | ncaa.com (a **different host** from stats.ncaa.org) | Plain HTTP to a local sidecar container | Not observed to block | The **resilient primary/fallback**: schedules, scoreboards, box scores, and explicit per-set lineups. Keeps working when stats.ncaa.org is blocked |
| **ncaa.com** (public henrygd) | `ncaa-api.henrygd.me` | Plain HTTP | Rate-limited | Team **logos** only |
| **Conference ICS + TPS** | SIDEARM conference calendars (ICS) + a TV-listings provider (TPS) | Plain HTTP | — | **Broadcast / TV-network** assignments |
| avca.org, wikimedia, ncaa.com RPI/AVCA pages, school athletics sites | various | Plain HTTP / Chrome fallback | — | Poll archives, conference logos, live RPI/AVCA ranks, player photos |

### Why the split (the block problem)

`stats.ncaa.org` sits behind Akamai and IP-reputation-blocks the box. The box's IP is a **shared
production IP** that also serves `vballr.com`, `wiki.beattys.org`, and travel-rewards-api, so a block
on the scraper IP is a block on the *serving* IP. Two mitigations are in place:

1. **Egress isolation** — every `stats.ncaa.org` fetch goes out through a **rotating residential
   proxy** (`VB_PROXY_*` in the box `.env`), so a block only ever hits disposable proxy exit IPs,
   never the serving IP.
2. **A resilient second source** — the self-hosted **henrygd/ncaa-api** sidecar wraps `ncaa.com`
   (a different host that isn't doing the Akamai block), and is the primary/fallback for schedules,
   box scores, and lineups so `stats.ncaa.org` is touched *rarely*.

> Consequence for pacing: because per-exit-IP failures over a rotating residential proxy are **normal**
> (some exits are slow/dead/pre-flagged), the full-sweep scrapers' "fail-rate > 25%" abort guard
> (`vb_scrape_fail_threshold`) is raised for the season backfill — otherwise routine proxy flakiness
> would discard a whole night's work. See §6.

---

## 2. The scrape layer (`src/vb/scrape/`) — what comes from where

Each module fetches from a host and writes a staging CSV (or returns rows) consumed by a loader in §3.

| Module | Host | Produces |
|---|---|---|
| `scrape/pbp.py` | **stats.ncaa.org** (Chrome) | `ncaa_wvb_pbp_d1_<season>.csv` — touch-level play-by-play. **Resumable**: appends per contest, skips contests already in the CSV *and* the DB |
| `scrape/game_stats.py` | **stats.ncaa.org** (Chrome) | `ncaa_wvb_game_stats_d1_<season>.csv` — per-contest per-player box lines. Resumable |
| `scrape/rosters.py` | **stats.ncaa.org** (Chrome) | `ncaa_wvb_rosters_d1_<season>.csv` + `..._coaches_d1_<season>.csv` |
| `scrape/schedule.py` | **stats.ncaa.org** (Chrome) | `ncaa_wvb_schedule_d1_<season>.csv` |
| `scrape/season_stats.py` | **stats.ncaa.org** (Chrome) | `ncaa_wvb_player_stats_d1_<season>.csv` — cumulative totals (validation only) |
| `scrape/team_list.py` | **stats.ncaa.org** (Chrome) | Per-season conference membership (realignment-aware) |
| `scrape/ncaa_api.py` | **ncaa.com** via henrygd sidecar | `scoreboard(sport,date)`, `boxscore(game_id)`, `play_by_play(game_id)` — the latter includes **explicit per-set starters** |
| `scrape/ncaa_com_games.py` | **ncaa.com** scoreboard (GraphQL) | Games per date (ncaa.com game ids + seonames) for id-mapping |
| `scrape/broadcasts.py` | conference **ICS** + **TPS** playlist/EPG | `FeedBroadcast` rows (TV networks per game) |
| `scrape/logos.py` | public henrygd (`ncaa-api.henrygd.me`) | Team logo URLs |
| `scrape/conference_logos.py` | wikimedia | Conference logo URLs → `data/conference_logos.json` |
| `scrape/photos.py` | school athletics roster pages (httpx + Chrome fallback) | Player headshots |
| `scrape/avca_archive.py` | avca.org | Historical AVCA poll snapshots |

The single Chrome choke point is `src/vb/fetch/ncaa_fetch.py` — **every** `stats.ncaa.org` fetch goes
through it, which is exactly why the proxy + pacing knobs live there and cover all of the above in one
place.

---

## 3. The load layer (`src/vb/load/`) — source → table

Loaders are idempotent (upsert, or delete-then-insert per natural key). CLI command → loader in §5.

| Loader | Source | Table(s) written | Idempotency |
|---|---|---|---|
| `teams.load_teams` | `data/teams.json` | `conferences`, `teams`, `team_season_ids` | upsert by natural key |
| `teams.load_season_conferences` | stats.ncaa.org membership | `team_season_ids.conference_id` | upsert; Access-Denied cooldown guard |
| `rosters.load_rosters` | rosters CSV | `players` | upsert by `(ncaa_player_id, season)` |
| `coaches.load_coaches` | coaches CSV | `coaches` | season-scoped wholesale replace |
| `schedule.load_schedule` | schedule CSV | `schedule` | upsert by `(season, team_id, date, opponent)`; prunes stale rows for re-scraped teams |
| `game_stats.load_game_stats` | game-stats CSV | `contests` (core), `player_game_stats` (**PRIMARY** stat fact) | upsert by `contest_id` / `(contest_id, player_id)` |
| `pbp.load_pbp` | pbp CSV | `pbp_events`, fills `contests.location/attendance` | delete-then-insert per contest |
| `ncaa_com_games.map_ncaa_games` | ncaa.com scoreboard | `contests.ncaa_game_id`, `schedule.ncaa_game_id` | writes only when id is new/changed |
| `ncaa_api_lineups.load_ncaa_lineups` | henrygd sidecar PBP | `contest_set_starters` (authoritative per-set starters) | delete-then-insert per `(contest, team, set)` |
| `season_stats.load_season_stats` | season-stats CSV | `player_season_stats_scraped` (**VALIDATION ONLY**) | upsert by `(player_id, season)` |
| `enrichment.enrich_logos` / `enrich_conference_logos` | teams.json / conf-logos json | `teams.logo_*` / `conferences.logo` | update |
| `enrichment.enrich_rpi` / `enrich_avca` | ncaa.com RPI / AVCA pages | `teams.rpi_rank/rpi_record` / `teams.avca_rank` | update (clears stale avca first) |
| `enrichment.load_avca_archive` | avca.org | `ranking_snapshots.avca_rank` | upsert by `(season, as_of, team_id)` |
| `enrichment.snapshot_rankings` | (reads `teams`) | `ranking_snapshots` | idempotent per day |
| `broadcasts.ingest_broadcasts` | ICS + TPS | `broadcasts` | delete-then-insert of the today-and-future window |
| `photos.scrape_player_photos` | school roster pages | `players.photo_path` + image files | overwrite in place |

**Derived (not ingested):** `player_season_stats` (matview, `derive-cumulative`), `player_pbp_stats`
(`derive-pbp`), `contest_weeks` (view). `reconcile` (`derive/reconcile.py`) is a **verification-only**
comparison of derived vs scraped season totals — it writes nothing.

---

## 4. Two ids for one game

The two NCAA hosts use **different game ids** (`stats.ncaa.org` `contest_id` ≠ `ncaa.com` game id), so
they can't be joined directly. The bridge is `map_ncaa_games`:

- It matches a ncaa.com scoreboard game to our `contests`/`schedule` rows on **(ISO date + unordered
  team pair)**, teams resolved by slugging short names to ncaa.com's `seoname` style.
- It writes the recovered `ncaa_game_id` **only when it differs** from what's stored.
- That `ncaa_game_id` is the **key that unlocks the ncaa.com sources** for a contest —
  `load_ncaa_lineups` only runs on contests that already carry one.

---

## 5. Reconciliation — when two sources describe the same thing

Six independent merge mechanisms, each with an explicit precedence rule.

### 5.1 Team-name resolution (`src/vb/util/normalize.py`, `load/broadcasts.py`, `load/ncaa_com_games.py`)
Feed/scoreboard team strings → our `teams.id`. Two keys are built over **every** known name form of a
team (`short_name`, `name`, and every `aliases` entry from `data/teams.json`):
- `slug_school` — dash slug matching ncaa.com's `seoname` (keeps "st"): `"Michigan St." → michigan-st`.
- `normalize_school_key` — stop-word-folded, punctuation-to-space (collapses abbreviations).

Both apply `fix_mojibake` (repairs double-encoded UTF-8, e.g. `"San JosÃ© State"`) then `strip_accents`
(`José → Jose`) **before** keying. Lookups try slug then folded; rank prefixes (`#5`, `No. 5`, `(5)`)
are stripped first. **First writer wins** on key collisions (`setdefault`); the schedule loader prefers
an exact NCAA id before falling back to name. A pair "resolves" only when **both** teams map — which is
why a D1-vs-non-D1 game correctly can't map.

### 5.2 Player-name resolution (`src/vb/load/ncaa_api_lineups.py`)
ncaa.com lineup names → our `players.id`, via a `_RosterIndex` with **five progressively fuzzier
tiers**: exact normalized full name → order-insensitive token set → (first initial + surname) → unique
surname → unique ≥2-token overlap. Any key two players share is **nulled** (ambiguous → skipped, never
guessed). A starters line is assigned to whichever team its names best match — the *reported* team id
is ignored (ncaa.com sometimes crosses it). Unmatched names are logged and skipped, not mis-attributed.

### 5.3 Broadcasts (`src/vb/load/broadcasts.py`)
Match on **(date + unordered team pair)** with a ±1-day tolerance (Hawaii/Pacific drift). Precedence:
**ICS > TPS playlist > TPS EPG** (feeds processed in that order; first writer wins per
`(date, pair, network)`). Within a key: **live beats replay**; a later feed only *enriches* (fills a
missing channel number). Finally, a generic `"ESPN/ESPN+"` label is dropped when a specific ESPN
flavor (ESPN2/ESPNU/ESPN+/ACCN/SECN…) is present for the same game. Refresh is **delete-then-insert of
today-and-future only** — past (frozen) rows are never re-deleted.

### 5.4 Lineups — authoritative override (`models.py`, `query/tools.py`, `api/routers/contests.py`)
`contest_set_starters` (from ncaa.com PBP) is **authoritative** and **overrides** the heuristic
starter reconstruction from `pbp_events`: in `per_set_lineups`, an authoritative entry for a
`(team, set)` becomes the starters outright; everyone else who appeared becomes a sub. Team-sets with
no authoritative entry fall through to the heuristic. *Note:* only the API contests router currently
wires in the override; the `match_lineups` query tool still uses pure heuristic reconstruction.

### 5.5 Dual-source stat strategy
`player_game_stats` is populated from the **stats.ncaa.org** box-score CSV (the primary stat fact). The
henrygd `boxscore()` endpoint exists and *could* fill `player_game_stats` when stats.ncaa.org is
blocked, but that loader is **designed, not yet wired** — today only `play_by_play` (→ lineups) is
consumed from the sidecar. Where the two sources overlap for **lineups**, ncaa.com wins (§5.4). Gaps
ncaa.com can't fill (per-rally attack splits, setter-hitting attribution, setter-anchored rotations)
stay on stats.ncaa.org.

### 5.6 Season-scoped conference (`src/vb/season_conf.py`, `load/teams.py`)
Membership is realignment-aware: reads use `coalesce(team_season_ids.conference_id,
teams.conference_id)` — the **per-season value wins**, the global default is the fallback (e.g.
Colorado State: Mountain West 2025 → Pac-12 2026). NCAA's short conference labels ("ACC") are mapped to
the curated full names ("Atlantic Coast Conference") via `_CONF_ALIASES` so the loader reuses the
existing logo-bearing row instead of minting a logo-less duplicate.

---

## 6. Scheduling & resilience (systemd timers, `scripts/`)

Deployed on the box; each `.service` runs a script from `scripts/`. Heavy scrapes run headful
Chromium under `xvfb-run` and share a flock at `/tmp/vb_update.lock` (never two NCAA sweeps at once).

| Timer | Cadence | Script | Does |
|---|---|---|---|
| **vb-hourly** | :07 of 13–23h + midnight (ET) | `hourly_update.sh` | game-stats + pbp (`--days-back 3`) → derive → `map-ncaa-games`. Non-blocking lock (skips if busy) |
| **vb-daily** | 01:00 ET | `daily_update.sh` | Full pass: game-stats/pbp → derive → `enrich rpi/avca` → `snapshot-rankings` → map. Mondays also `scrape-photos` |
| **vb-broadcasts** | hourly (:37) | `broadcasts_update.sh` | `ingest-broadcasts` (`--days-back 3 --days-ahead 10`), plain HTTP |
| **vb-weekly-rosters** | Sun 02:00 | `weekly_rosters.sh` | Rosters/coaches/conf/schedule + full-season game-stats/pbp sweep + derive + enrich |
| **vb-backfill** *(temporary)* | 02:12 ET, 7h `RuntimeMaxSec` | `backfill_season.sh 2025` | One-time historical-season backfill; SIGTERM-stopped before the game slate, resumes next night |

**Resilience details that matter:**
- **Resumable everything** — pbp/game-stats scrapers append per contest and skip what's already in the
  CSV *and* the DB, so a SIGTERM mid-run is safe and the next window continues.
- **Egress isolation** — all stats.ncaa.org traffic exits via the residential proxy; the serving IP is
  never the scraping IP.
- **Abort guard, tuned for the proxy** — the full-sweep scrapers raise a "fail-rate > threshold" abort
  *after* attempting every team (so a non-zero exit never means less was scraped). Because per-exit
  proxy failures are routine, the **backfill raises `VB_SCRAPE_FAIL_THRESHOLD` to 0.9** and runs
  `load-*` **even if the sweep exits non-zero** — so a partial night still imports what it scraped
  instead of discarding it. A genuine near-total block just leaves the CSV empty and the load a no-op.

---

## 7. TL;DR flow

```
stats.ncaa.org ──Chrome/xvfb via residential proxy──┐
  (pbp, box scores, rosters, schedule, season stats, │
   per-season conference)                            │
                                                     ├─► staging CSVs ─► load_* ─► Postgres ─► derive_* ─► API
ncaa.com (henrygd sidecar) ──HTTP──┐                 │        (idempotent upserts)     (matviews/views)
  (scoreboard, box scores,         │                 │
   explicit per-set lineups)  ─────┴─ map_ncaa_games ┘   reconciliation:
                                     (date + team pair       team-name · player-name · broadcasts ·
                                      → shared ncaa_game_id)   lineup-override · dual-source · season-conf
ICS + TPS ──HTTP──► broadcasts
public henrygd / wikimedia / avca.org / school sites ──► logos, conf logos, poll archive, photos
```

**Primary vs fallback:** stats.ncaa.org is primary for the *rich* data (touch-level PBP, box-score
stats, season-accurate conference); ncaa.com (henrygd) is the resilient primary/fallback for
schedules, scoreboards, box scores, and lineups. When both cover lineups, ncaa.com's explicit starters
win. The proxy + resumable + dual-source design means day-to-day data keeps flowing even when
stats.ncaa.org blocks the box.
