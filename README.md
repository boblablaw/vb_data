# vb_data

NCAA Division I **women's volleyball** data pipeline — a DB-first re-architecture of the
older `vb_scraper`. Scrapes rosters, per-game stats, and team schedules from stats.ncaa.org,
loads them into **Postgres**, **derives** cumulative season stats in-database (single source of
truth), and serves everything through a **FastAPI** API plus a build-step-free vanilla-JS web UI.
Games, schedules, and box scores are surfaced directly in the app.

```
stats.ncaa.org ──scrape──▶ raw CSVs ──load──▶ Postgres ──derive──▶ matview ──▶ FastAPI + web UI
```

Raw scraped CSVs are staging only (`staging/`, gitignored) — they feed the loaders and act as
resume ledgers; **the database is the product**, and everything in it is served through the API/UI.

## Stack

- **Python 3.12**, packaged with `pyproject.toml`; a Typer CLI (`vb`) wires the whole pipeline.
- **Postgres 16** (host port **5435**), **SQLAlchemy 2.0** ORM, **Alembic** for linear migrations.
- **FastAPI** + **pydantic-settings**; served by **uvicorn** (container `vb-api`, port 8091).
- **Playwright** driving **real Google Chrome** (Akamai bypass) for scraping — see
  `src/vb/fetch/ncaa_fetch.py`; jobs run headless via `xvfb-run`.
- **Web UI**: hand-written HTML/CSS/vanilla-JS in `src/vb/api/static/`, served same-origin at
  `/ui/` — **no build step, no bundler, no npm**.
- **Auth**: email/password (bcrypt) + WebAuthn passkeys, JWT bearer tokens; an admin surface sets
  the MCP API token and a global AI key.
- **MCP**: a Model Context Protocol server (`src/vb/mcp/`) mounted at `/mcp` exposes the query
  tools for LLM clients; the in-app **Ask** tab uses the same tools.
- **Deploy**: push `main` → GitHub Actions → `deploy/deploy.sh` on OCI (`git reset --hard` →
  `alembic upgrade head` → rebuild `vb-api`); public HTTPS via the shared `edge-caddy`.

## Why DB-first

The old pipeline scraped a web of intermediate CSVs and merged them with pandas into SQLite,
and scraped cumulative season totals directly. Here the **database is the source of truth**:

- We scrape **rosters** (+ head coaches), **per-game (contest) player stats**, and **team
  schedules** (upcoming + played games).
- Cumulative season stats are **derived** from the per-game rows in a materialized view, so
  they're always internally consistent — and, as it turns out, *more complete* than NCAA's
  season-to-date page (which lags behind by a contest or two).
- Played-game **box scores** need no extra scraping — they come straight from the stored
  `contests` + `player_game_stats`; only **upcoming** games need the schedule scrape (they have
  no contest id yet), stored in the `schedule` table keyed per team.
- The season-to-date scraper is kept **only for validation** (`vb reconcile`).

## Layout

```
src/vb/
  config.py            pydantic-settings (DATABASE_URL, paths, scrape delays)
  db.py                SQLAlchemy engine / session
  models.py            SQLAlchemy 2.0 ORM (schema managed by Alembic)
  fetch/ncaa_fetch.py  real-Chrome / Akamai bypass (carried over verbatim)
  scrape/              team-list, rosters (+coaches), game-stats, season-stats, schedule
                         (raw CSVs -> staging/)
  load/                CSV -> Postgres upserts + enrichment (logos/photos/rpi)
  derive/              cumulative matview refresh + reconcile
  auth/                password hashing, JWT, WebAuthn passkey helpers
  mcp/                 Model Context Protocol server (mounted at /mcp)
  query/               shared query tools used by the API "Ask" tab and MCP
  api/                 FastAPI app (routers: health, conferences, teams, players, contests,
                         games, stats, auth, passkeys, favorites, admin, ask)
  api/static/          vanilla-JS web UI (served at /ui/, no build step)
  cli.py               `vb` typer CLI wiring the whole pipeline
migrations/            Alembic, linear history 0001..0011 (0001 tables + player_season_stats
                         matview; 0002 contest_weeks view; 0005 contest results; 0008 auth
                         tables; 0011 schedule table)
data/teams.json        master team dimension (identity, conference, location, logos, aliases,
                         per-season NCAA ids)
tests/                 pure math/helpers + Postgres-backed derive, stats/fantasy, schedule & games
```

## Setup

```bash
python3 -m venv venv && ./venv/bin/pip install -e ".[dev]"
./venv/bin/playwright install chromium        # scraping also needs real Google Chrome
cp .env.example .env                           # tweak DATABASE_URL if needed
docker compose up -d                           # Postgres 16 on host port 5435
./venv/bin/alembic upgrade head                # create schema + matview
```

Scraping requires **real Google Chrome** installed — stats.ncaa.org sits behind Akamai and
blocks Playwright's bundled Chromium. See `src/vb/fetch/ncaa_fetch.py`.

## Pipeline (`vb` CLI)

```bash
vb load-teams --season 2026                    # teams.json -> conferences / teams / season-ids
vb scrape rosters --year 2026                  # -> staging/ncaa_wvb_rosters_d1_2026.csv (+ coaches CSV)
vb load-rosters --season 2026
vb load-coaches --season 2026                  # head coaches scraped alongside rosters
vb scrape game-stats --year 2026               # -> staging/ncaa_wvb_game_stats_d1_2026.csv
vb load-game-stats --season 2026
vb scrape schedule --year 2026                 # -> staging/ncaa_wvb_schedule_d1_2026.csv
vb load-schedule --season 2026                 # upcoming + played games into the schedule table
vb derive-cumulative --season 2026             # REFRESH MATERIALIZED VIEW player_season_stats
vb scrape season-stats --year 2026             # validation only
vb load-season-stats --season 2026
vb reconcile --season 2026                     # derived vs scraped diff report
vb enrich logos                                # logos from teams.json
vb enrich photos --season 2026                 # match assets/player_photos to players
vb enrich rpi                                  # NCAA RPI table -> teams.rpi_rank/record
```

Coaches come from the **roster scrape** (each team's roster page carries the head-coach card), not
from `teams.json`; `vb scrape rosters` writes both the roster CSV and a coaches CSV.

Scrapers target specific teams with repeatable `--team-id`, and default to all teams that
have an NCAA id for the season in `data/teams.json`. Raw CSVs are written incrementally and
runs are resumable; loaders upsert idempotently.

### Year semantics (the gotcha)

`data/teams.json` `ncaa_team_ids` keys are the **fall (season) year**. stats.ncaa.org labels
academic years by their **ending** year, so the fall-`YYYY` season lives at
`academic_year = YYYY + 1`. NCAA team ids differ every season (fall-2025 ≈ `604xxx`,
fall-2026 ≈ `62xxxx`). `vb scrape team-list --year` takes the fall year and queries
`academic_year = year + 1` internally.

## API

```bash
./venv/bin/uvicorn vb.api.main:app --reload    # http://localhost:8000/docs · UI at /ui/
```

Core read endpoints:
- `GET /teams`, `/teams/{id}`, `/teams/{id}/roster?season=`, `/teams/{id}/coaches`
- `GET /players?season=&team=&q=`, `/players/{id}`
- `GET /players/{id}/season-stats` (derived; `gs` coalesced from the scraped table)
- `GET /players/{id}/game-stats`, `/contests?season=`, `/contests/{id}/stats`

Games, schedules & box scores:
- `GET /teams/{id}/games?season=` — one team's schedule: **played** games (from `contests`, with
  result + `contest_id` for the box-score link) merged with **upcoming** games (from the `schedule`
  table), date-ordered
- `GET /games?season=&date=|start=&end=|week=` — league-wide scoreboard for a day/week; played
  contests + scheduled games, the two per-team schedule perspectives deduped into one game
- `GET /contests/{id}` — box-score header: both teams (id/name/logos), set wins, per-set line score
- `GET /contests/{id}/stats` — every player's line for the contest (with `player_name`)

Fantasy/stat endpoints (`routers/stats.py`) powering the web UI:
- `GET /seasons`, `/weeks?season=` — season-anchored Mon–Sun weeks (Week 1 = week of the season's
  first match; null/unparseable dates fall in the "unknown" bucket, via the `contest_weeks` view)
- `GET /leaderboards?stat=&scope=season|week&season=&week=&conference=&position=&team_id=&min_sets=`
  — Top Players by any counting/rate stat (season scope reads the matview; week scope aggregates live)
- `GET /leaderboards/fantasy?...&w_<stat>=` — configurable **Fantasy Points** composite
  (defaults in `config.FANTASY_WEIGHTS`; per-request weight overrides)
- `GET /team-stats?conference=&season=&week=`, `/conferences/{id}/leaders`, `/search?q=`,
  `/players/{id}/game-log?season=`

The **web UI** (`/ui/`) is vanilla HTML/CSS/JS served same-origin — Top Players, Fantasy, Teams
(by conference), a **Games** tab (browse by date/week, with an Upcoming toggle) that opens
clickable **box scores** (both teams' lines + set scores), team pages with a Schedule & Results
section, This Week's top performers, player detail + game log (each row links to its box score),
compare, and search. It is containerized and hosted publicly over HTTPS behind the shared
edge-caddy; see `deploy/OCI_SETUP.md` §10.

## Derivation & reconcile

Cumulative stats are a `MATERIALIZED VIEW` over `player_game_stats`: counting stats are
sums, `hit_pct = (Σkills − Σerrors)/Σtotal_attacks`, per-set rates are `Σstat/Σsets`, and
`gp = count(distinct contest_id)`. **Games started (GS)** has no per-game equivalent, so the
matview's `gs` is null and the authoritative value comes from
`player_season_stats_scraped` (served via join by the API). `vb reconcile` compares derived
vs scraped totals; larger derived numbers usually mean the per-game scrape found contests
the season-to-date page hasn't aggregated yet.

## Data / config

- `data/teams.json` — the master team dimension (identity, conference, location, logos,
  aliases, per-season NCAA ids). Coaches are **not** here — they're scraped from each team's
  roster page. External school/reference data (scorecard, airports, niche) is intentionally
  **not** carried over.
- `assets/logos/`, `assets/player_photos/` — gitignored binaries used by enrichment.
- Postgres runs on host port **5435** (5432/5433/5434 are taken by other local stacks).

## Tests

```bash
./venv/bin/pytest            # pure math + helpers always run; the derive test needs Postgres
```
