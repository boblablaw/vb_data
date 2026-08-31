"""`vb` command-line interface (typer). Wires scrape -> load -> derive -> export + admin.

Typical flow:
    vb db up && vb db upgrade
    vb load-teams --season 2026
    vb scrape rosters --year 2026 && vb load-rosters --season 2026
    vb scrape game-stats --year 2026 && vb load-game-stats --season 2026
    vb derive-cumulative --season 2026
    vb export merged --season 2026
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer

from .config import REPO_ROOT
from .db import session_scope
from .log import get_logger

log = get_logger("vb.cli")

app = typer.Typer(help="NCAA women's volleyball data pipeline.", no_args_is_help=True)
db_app = typer.Typer(help="Database admin (docker + alembic).", no_args_is_help=True)
scrape_app = typer.Typer(help="Scrapers (write raw CSVs).", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(scrape_app, name="scrape")


def _season_team_ids(year: int, team_id: list[str] | None) -> list[str]:
    from .scrape.teams_json import season_team_ids
    if team_id:
        return [str(t) for t in team_id]
    ids = list(season_team_ids(year).keys())
    if not ids:
        raise typer.BadParameter(f"no NCAA team ids for season {year} in teams.json")
    return ids


# ---------------------------------------------------------------- db admin
@db_app.command("up")
def db_up():
    """docker compose up -d (Postgres)."""
    subprocess.run(["docker", "compose", "up", "-d"], cwd=REPO_ROOT, check=True)


@db_app.command("down")
def db_down():
    """docker compose down."""
    subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@db_app.command("upgrade")
def db_upgrade(revision: str = "head"):
    """alembic upgrade <revision>."""
    subprocess.run(["alembic", "upgrade", revision], cwd=REPO_ROOT, check=True)


@db_app.command("downgrade")
def db_downgrade(revision: str = "-1"):
    """alembic downgrade <revision>."""
    subprocess.run(["alembic", "downgrade", revision], cwd=REPO_ROOT, check=True)


# ---------------------------------------------------------------- scrape
@scrape_app.command("team-list")
def scrape_team_list(year: int = typer.Option(..., help="fall/season year")):
    from .scrape.team_list import scrape_team_list as _run
    out = _run(year)
    typer.echo(f"wrote {out}")


@scrape_app.command("rosters")
def scrape_rosters(
    year: int = typer.Option(..., help="fall/season year"),
    team_id: list[str] | None = typer.Option(None, help="repeatable NCAA team id"),
):
    from .scrape.rosters import scrape_rosters as _run
    ids = _season_team_ids(year, team_id)
    r_out, c_out = _run(ids, year)
    typer.echo(f"wrote {r_out}\nwrote {c_out}")


@scrape_app.command("game-stats")
def scrape_game_stats(
    year: int = typer.Option(..., help="fall/season year"),
    team_id: list[str] | None = typer.Option(None, help="repeatable NCAA team id"),
    max_contests: int | None = typer.Option(None, help="cap contests per team (sampling)"),
):
    from .scrape.game_stats import scrape_game_stats as _run
    ids = _season_team_ids(year, team_id)
    out = _run(ids, year, max_contests=max_contests)
    typer.echo(f"wrote {out}")


@scrape_app.command("season-stats")
def scrape_season_stats(
    year: int = typer.Option(..., help="fall/season year"),
    team_id: list[str] | None = typer.Option(None, help="repeatable NCAA team id"),
):
    from .scrape.season_stats import scrape_season_stats as _run
    ids = _season_team_ids(year, team_id)
    out = _run(ids, year)
    typer.echo(f"wrote {out}")


# ---------------------------------------------------------------- backfill ids
@app.command("backfill-ids")
def backfill_ids_cmd(
    year: int = typer.Option(..., help="fall/season year (= ncaa_team_ids key)"),
    team_list: Path | None = typer.Option(
        None, help="team-list CSV (default exports/ncaa_wvb_team_list_<year>.csv)"
    ),
    min_match: int = typer.Option(340, help="refuse to write below this many matches"),
    write: bool = typer.Option(False, help="write teams.json (dry-run otherwise)"),
):
    """Match a harvested team-list CSV into teams.json ncaa_team_ids[<year>].

    Only overwrites matched entries. To re-point a year at a different season, strip the
    old "<year>" keys from teams.json first, else unmatched entries keep a stale id.
    """
    from .scrape.backfill import backfill_team_ids
    res = backfill_team_ids(year, team_list_path=team_list, min_match=min_match, write=write)
    typer.echo(
        f"matched {res['matched']}/{res['total']} "
        f"({'WROTE' if res['written'] else 'dry-run'})"
    )
    if res["unmatched"]:
        typer.echo(f"unmatched teams.json entries ({len(res['unmatched'])}):")
        for name in res["unmatched"]:
            typer.echo(f"  - {name}")
    if res["unmapped"]:
        typer.echo(f"NCAA list ids with no teams.json home ({len(res['unmapped'])}):")
        for u in res["unmapped"]:
            typer.echo(f"  - {u['team_id']}  {u['name']}")
    if not res["written"]:
        typer.echo("(dry-run — pass --write to save)")


# ---------------------------------------------------------------- load
@app.command("load-teams")
def load_teams_cmd(season: int = typer.Option(...)):
    from .load import load_teams
    with session_scope() as s:
        res = load_teams(s, season)
    typer.echo(json.dumps(res))


@app.command("load-rosters")
def load_rosters_cmd(
    season: int = typer.Option(...),
    csv: Path | None = typer.Option(None, help="override CSV path"),
):
    from .load import load_rosters
    with session_scope() as s:
        res = load_rosters(s, season, csv)
    typer.echo(json.dumps(res))


@app.command("load-game-stats")
def load_game_stats_cmd(
    season: int = typer.Option(...),
    csv: Path | None = typer.Option(None, help="override CSV path"),
):
    from .load import load_game_stats
    with session_scope() as s:
        res = load_game_stats(s, season, csv)
    typer.echo(json.dumps(res))


@app.command("load-season-stats")
def load_season_stats_cmd(
    season: int = typer.Option(...),
    csv: Path | None = typer.Option(None, help="override CSV path"),
):
    from .load import load_season_stats
    with session_scope() as s:
        res = load_season_stats(s, season, csv)
    typer.echo(json.dumps(res))


# ---------------------------------------------------------------- derive
@app.command("derive-cumulative")
def derive_cumulative_cmd(season: int = typer.Option(None, help="unused; matview is global")):
    from .derive import derive_cumulative
    with session_scope() as s:
        res = derive_cumulative(s)
    typer.echo(json.dumps(res))


@app.command("reconcile")
def reconcile_cmd(
    season: int = typer.Option(...),
    limit: int = typer.Option(20, help="max discrepancies to print"),
):
    from .derive import reconcile
    with session_scope() as s:
        res = reconcile(s, season)
    typer.echo(json.dumps(res["summary"], indent=2))
    for d in res["discrepancies"][:limit]:
        typer.echo(f"  {d['name']} (player {d['player_id']}): {d['diffs']}")


# ---------------------------------------------------------------- enrich
@app.command("enrich")
def enrich_cmd(
    what: str = typer.Argument(..., help="logos | photos | rpi"),
    season: int | None = typer.Option(None, help="required for photos"),
    csv: Path | None = typer.Option(None, help="rpi CSV override"),
):
    from .load import enrich_logos, enrich_photos, enrich_rpi
    with session_scope() as s:
        if what == "logos":
            res = enrich_logos(s)
        elif what == "photos":
            if season is None:
                raise typer.BadParameter("--season is required for photos")
            res = enrich_photos(s, season)
        elif what == "rpi":
            res = enrich_rpi(s, csv)
        else:
            raise typer.BadParameter("what must be one of: logos, photos, rpi")
    typer.echo(json.dumps(res))


# ---------------------------------------------------------------- export
@app.command("export")
def export_cmd(
    name: str = typer.Argument(..., help="merged | rosters | game_stats | teams"),
    season: int | None = typer.Option(None),
    output: Path | None = typer.Option(None),
):
    from .export import export_csv
    with session_scope() as s:
        out = export_csv(s, name, season, output)
    typer.echo(f"wrote {out}")


if __name__ == "__main__":
    app()
