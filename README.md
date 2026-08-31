# vb_data

NCAA Division I **women's volleyball** data pipeline — a DB-first re-architecture of the
older `vb_scraper`. Scrapes rosters + per-game stats from stats.ncaa.org, loads them into
**Postgres**, **derives** cumulative season stats in-database (single source of truth), and
serves everything through a **FastAPI** API. CSV exports are a trivial DB dump.

```
stats.ncaa.org ──scrape──▶ raw CSVs ──load──▶ Postgres ──derive──▶ matview ──▶ FastAPI / CSV
```

## Why DB-first

The old pipeline scraped a web of intermediate CSVs and merged them with pandas into SQLite,
and scraped cumulative season totals directly. Here the **database is the source of truth**:

- We scrape only **rosters** and **per-game (contest) player stats**.
- Cumulative season stats are **derived** from the per-game rows in a materialized view, so
  they're always internally consistent — and, as it turns out, *more complete* than NCAA's
  season-to-date page (which lags behind by a contest or two).
- The season-to-date scraper is kept **only for validation** (`vb reconcile`).

## Layout

```
src/vb/
  config.py            pydantic-settings (DATABASE_URL, paths, scrape delays)
  db.py                SQLAlchemy engine / session
  models.py            SQLAlchemy 2.0 ORM (schema managed by Alembic)
  fetch/ncaa_fetch.py  real-Chrome / Akamai bypass (carried over verbatim)
  scrape/              team-list, rosters, game-stats, season-stats (write raw CSVs)
  load/                CSV -> Postgres upserts + enrichment (logos/photos/rpi)
  derive/              cumulative matview refresh + reconcile
  export/              DB -> CSV named exports
  api/                 FastAPI app (routers: health, conferences, teams, players, contests)
  cli.py               `vb` typer CLI wiring the whole pipeline
migrations/            Alembic (0001 = all tables + player_season_stats matview)
tests/                 derivation math (pure) + end-to-end derive (Postgres)
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
vb load-teams --season 2026                    # teams.json -> conferences/teams/season-ids/coaches
vb scrape rosters --year 2026                  # -> exports/ncaa_wvb_rosters_d1_2026.csv
vb load-rosters --season 2026
vb scrape game-stats --year 2026               # -> exports/ncaa_wvb_game_stats_d1_2026.csv
vb load-game-stats --season 2026
vb derive-cumulative --season 2026             # REFRESH MATERIALIZED VIEW player_season_stats
vb scrape season-stats --year 2026             # validation only
vb load-season-stats --season 2026
vb reconcile --season 2026                     # derived vs scraped diff report
vb enrich logos                                # logos from teams.json
vb enrich photos --season 2026                 # match assets/player_photos to players
vb enrich rpi                                  # NCAA RPI table -> teams.rpi_rank/record
vb export merged --season 2026                 # DB -> exports/merged_2026.csv
```

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
./venv/bin/uvicorn vb.api.main:app --reload    # http://localhost:8000/docs
```

- `GET /teams`, `/teams/{id}`, `/teams/{id}/roster?season=`, `/teams/{id}/coaches`
- `GET /players?season=&team=&q=`, `/players/{id}`
- `GET /players/{id}/season-stats` (derived; `gs` coalesced from the scraped table)
- `GET /players/{id}/game-stats`, `/contests?season=`, `/contests/{id}/stats`

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
  aliases, per-season NCAA ids, coaches). External school/reference data (scorecard,
  airports, niche) is intentionally **not** carried over.
- `assets/logos/`, `assets/player_photos/` — gitignored binaries used by enrichment.
- Postgres runs on host port **5435** (5432/5433/5434 are taken by other local stacks).

## Tests

```bash
./venv/bin/pytest            # pure math + helpers always run; the derive test needs Postgres
```
