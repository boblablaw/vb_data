"""Load the play-by-play CSV into ``pbp_events`` (and fill contest venue/attendance).

Each CSV row is one touch/sub/terminal (see ``vb.scrape.pbp``). Team attribution comes from the
row's ``Side`` (away/home) resolved against the contest's two teams; the player is then resolved
*within that team's roster* by canonical name (so identical names on opposing teams don't collide).
Unresolved names are counted, not fatal — ``player_id`` stays NULL.

Idempotent per contest: existing ``pbp_events`` for the contest are DELETEd, then the fresh rows
bulk-inserted (cleaner than get-or-create for ~700 rows/contest).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Contest, PbpEvent, Player
from ..util import canonical_name, normalize_player_name
from .common import clean_str, ncaa_id_to_team, num_int

log = get_logger(__name__)


def _default_path(season: int) -> Path:
    return settings.staging_dir / f"ncaa_wvb_pbp_d1_{season}.csv"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(
        path,
        dtype={"ContestID": str, "AwayNcaaId": str, "HomeNcaaId": str,
               "PlayerName": str, "TouchType": str, "Side": str, "ScoringSide": str,
               "TerminalType": str, "Location": str},
        keep_default_na=True,
    )


def _name_map(session: Session, team_id: int | None, season: int) -> dict[str, int]:
    """canonical player name -> player_id for one team's season roster."""
    if not team_id:
        return {}
    out: dict[str, int] = {}
    for pid, name in session.execute(
        select(Player.id, Player.name).where(
            Player.team_id == team_id, Player.season == season
        )
    ).all():
        key = canonical_name(normalize_player_name(name))
        if key:
            out.setdefault(key, pid)
    return out


def load_pbp(session: Session, season: int, csv_path: Path | None = None) -> dict:
    path = Path(csv_path) if csv_path else _default_path(season)
    if not path.exists():
        raise FileNotFoundError(f"pbp CSV not found: {path}")
    df = _read_csv(path)
    if df.empty:
        log.info("load_pbp: empty CSV (season %d)", season)
        return {"contests": 0, "events": 0, "unresolved_names": 0}

    ncaa_team = {nid: t.id for nid, t in ncaa_id_to_team(session, season).items()}

    contests = 0
    events = 0
    unresolved = 0
    for contest_id, g in df.groupby("ContestID", sort=False):
        contest_id = str(contest_id)
        away_team = ncaa_team.get(clean_str(g.iloc[0].get("AwayNcaaId")))
        home_team = ncaa_team.get(clean_str(g.iloc[0].get("HomeNcaaId")))

        contest = session.get(Contest, contest_id)
        if contest is None:
            contest = Contest(contest_id=contest_id, season=season,
                              home_team_id=home_team, away_team_id=away_team)
            session.add(contest)
        else:
            if home_team and contest.home_team_id is None:
                contest.home_team_id = home_team
            if away_team and contest.away_team_id is None:
                contest.away_team_id = away_team
        loc = clean_str(g.iloc[0].get("Location"))
        att = num_int(g.iloc[0].get("Attendance"))
        if loc:
            contest.location = loc
        if att is not None:
            contest.attendance = att
        session.flush()

        away_names = _name_map(session, away_team, season)
        home_names = _name_map(session, home_team, season)
        side_team = {"away": away_team, "home": home_team}
        side_names = {"away": away_names, "home": home_names}

        session.execute(delete(PbpEvent).where(PbpEvent.contest_id == contest_id))

        rows: list[dict] = []
        for r in g.itertuples(index=False):
            side = clean_str(getattr(r, "Side", None))
            team_id = side_team.get(side) if side else None
            player_name = clean_str(getattr(r, "PlayerName", None))
            player_id = None
            if player_name and side:
                # Terminal blocks can name two players ("A, B"); resolve the first.
                first = player_name.split(",")[0]
                key = canonical_name(normalize_player_name(first))
                player_id = side_names.get(side, {}).get(key)
                if player_id is None and getattr(r, "TouchType", "") != "terminal":
                    unresolved += 1
            scoring_side = clean_str(getattr(r, "ScoringSide", None))
            rows.append({
                "contest_id": contest_id,
                "season": season,
                "set_number": num_int(r.Set),
                "rally_number": num_int(r.Rally),
                "seq": num_int(r.Seq),
                "touch_type": clean_str(r.TouchType),
                "player_name": player_name,
                "player_id": player_id,
                "team_id": team_id,
                "is_terminal": bool(r.IsTerminal),
                "terminal_type": clean_str(getattr(r, "TerminalType", None)),
                "scoring_team_id": side_team.get(scoring_side) if scoring_side else None,
                "away_score": num_int(getattr(r, "AwayScore", None)),
                "home_score": num_int(getattr(r, "HomeScore", None)),
            })
        session.bulk_insert_mappings(PbpEvent, rows)
        contests += 1
        events += len(rows)

    session.flush()
    log.info("load_pbp: %d contests, %d events, %d unresolved names (season %d)",
             contests, events, unresolved, season)
    return {"contests": contests, "events": events, "unresolved_names": unresolved}
