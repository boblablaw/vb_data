"""Fantasy-volleyball stat endpoints: weeks, leaderboards, fantasy score, team stats, search.

Two aggregation paths (see the front-end plan):
  * **season scope** reads the pre-aggregated ``player_season_stats`` matview (one row/player) —
    fast.
  * **week scope** and all team/conference aggregates ``SUM(...)`` over ``player_game_stats``
    joined to the ``contest_weeks`` view live (the matview is season-cumulative and can't be
    week-sliced). At this data size (tens of thousands of game-stat rows) that is milliseconds.

Fantasy Points is a weighted sum of counting stats (defaults in ``config.FANTASY_WEIGHTS``,
overridable per-request via ``w_<stat>`` params) computed in SQL in both scopes so it stays
consistent. Every stat term is COALESCE'd to 0 (the counting columns are nullable), and weight
keys are whitelisted against ``FANTASY_STATS`` before touching SQL.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from functools import reduce
from operator import add

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, and_, desc, func, literal, nulls_last, or_, select
from sqlalchemy.orm import Session

from ...config import FANTASY_WEIGHTS
from ...models import (
    Conference,
    Contest,
    ContestWeek,
    Player,
    PlayerGameStat,
    PlayerSeasonStat,
    Team,
)
from ...util import current_season
from ..deps import get_session
from ..schemas import (
    GameLogRow,
    LeaderRow,
    PlayerOut,
    PlayerStatLine,
    SearchOut,
    TeamOut,
    TeamRecordRow,
    TeamStatRow,
    WeekOut,
)

router = APIRouter(tags=["stats"])

# Counting-stat columns that may carry a fantasy weight. Present on BOTH PlayerGameStat and the
# player_season_stats matview, so the same weights work in either scope. This is the SQL whitelist.
FANTASY_STATS: frozenset[str] = frozenset(FANTASY_WEIGHTS)

# Leaderboard stats. Season scope reads these matview columns directly; week scope recomputes the
# equivalent from per-game sums (see _week_value). Order-by is always descending, NULLs last.
SEASON_STATS: frozenset[str] = frozenset({
    "kills", "errors", "total_attacks", "assists", "aces", "serr", "digs", "retatt", "rerr",
    "block_solos", "block_assists", "total_blocks", "berr", "pts", "bhe", "hit_pct",
    "kills_per_set", "assists_per_set", "aces_per_set", "digs_per_set", "blocks_per_set",
    "pts_per_set",
})

_WEIGHT_MIN, _WEIGHT_MAX = -10.0, 10.0
_MAX_LIMIT = 500


def _season(season: int | None) -> int:
    return season if season is not None else current_season()


def resolve_weights(
    w_kills: float | None = Query(None),
    w_aces: float | None = Query(None),
    w_digs: float | None = Query(None),
    w_assists: float | None = Query(None),
    w_block_solos: float | None = Query(None),
    w_block_assists: float | None = Query(None),
    w_errors: float | None = Query(None),
    w_serr: float | None = Query(None),
    w_rerr: float | None = Query(None),
    w_berr: float | None = Query(None),
    w_bhe: float | None = Query(None),
) -> dict[str, float]:
    """Merge per-request weight overrides onto the defaults, clamped to a sane magnitude."""
    overrides = {
        "kills": w_kills, "aces": w_aces, "digs": w_digs, "assists": w_assists,
        "block_solos": w_block_solos, "block_assists": w_block_assists, "errors": w_errors,
        "serr": w_serr, "rerr": w_rerr, "berr": w_berr, "bhe": w_bhe,
    }
    weights = dict(FANTASY_WEIGHTS)
    for k, v in overrides.items():
        if v is not None and k in FANTASY_STATS:
            weights[k] = max(_WEIGHT_MIN, min(_WEIGHT_MAX, float(v)))
    return weights


def _fantasy_season_expr(weights: dict[str, float]) -> ColumnElement:
    terms = [
        float(w) * func.coalesce(getattr(PlayerSeasonStat, k), 0)
        for k, w in weights.items() if w and k in FANTASY_STATS
    ]
    return reduce(add, terms) if terms else literal(0.0)


def _fantasy_week_expr(weights: dict[str, float]) -> ColumnElement:
    terms = [
        float(w) * func.coalesce(func.sum(getattr(PlayerGameStat, k)), 0)
        for k, w in weights.items() if w and k in FANTASY_STATS
    ]
    return reduce(add, terms) if terms else literal(0.0)


def _sum(col_name: str) -> ColumnElement:
    return func.sum(func.coalesce(getattr(PlayerGameStat, col_name), 0))


def _week_value(stat: str) -> ColumnElement:
    """Aggregate expression matching ``stat`` for week-scope leaderboards (per-game sums)."""
    if stat == "total_blocks":
        return _sum("block_solos") + _sum("block_assists")
    if stat == "hit_pct":
        return (_sum("kills") - _sum("errors")) / func.nullif(_sum("total_attacks"), 0)
    if stat.endswith("_per_set"):
        base = stat[: -len("_per_set")]
        num = _sum("block_solos") + _sum("block_assists") if base == "blocks" else _sum(base)
        return num / func.nullif(_sum("sets"), 0)
    return _sum(stat)


def _conf_filter(stmt, conference: str | None, conference_id: int | None):
    if conference_id is not None:
        return stmt.where(Conference.id == conference_id)
    if conference:
        return stmt.where(Conference.name == conference)
    return stmt


@router.get("/seasons", response_model=list[int])
def list_seasons(db: Session = Depends(get_session)):
    """Distinct seasons that have contests, newest first (drives the season picker)."""
    rows = db.scalars(
        select(Contest.season).distinct().order_by(Contest.season.desc())
    ).all()
    return [int(s) for s in rows if s is not None]


@router.get("/weeks", response_model=list[WeekOut])
def list_weeks(season: int | None = None, db: Session = Depends(get_session)):
    """Season-anchored Mon–Sun weeks with contest counts (drives the week picker)."""
    season = _season(season)
    rows = db.execute(
        select(
            ContestWeek.week_number,
            func.min(ContestWeek.week_monday).label("start"),
            func.count(ContestWeek.contest_id).label("n"),
        )
        .where(ContestWeek.season == season)
        .group_by(ContestWeek.week_number)
        .order_by(nulls_last(ContestWeek.week_number.asc()))
    ).all()
    out: list[WeekOut] = []
    for wk, start, n in rows:
        end = (start + timedelta(days=6)).isoformat() if start else None
        out.append(WeekOut(
            week_number=wk, start=start.isoformat() if start else None, end=end, contest_count=n,
        ))
    return out


def _player_leaderboard(
    db: Session, *, stat: str, scope: str, season: int, week: int | None,
    conference: str | None, conference_id: int | None, position: str | None,
    team_id: int | None = None, min_sets: float, min_attacks: float = 0,
    limit: int, offset: int,
) -> list[LeaderRow]:
    if stat not in SEASON_STATS:
        raise HTTPException(422, f"unknown stat '{stat}'")
    if scope == "week":
        if week is None:
            raise HTTPException(422, "week is required when scope=week")
        value = _week_value(stat)
        games = func.count(func.distinct(PlayerGameStat.contest_id))
        sets_sum = _sum("sets")
        stmt = (
            select(
                Player.id.label("player_id"), Player.name, Player.position,
                Player.team_id, Team.name.label("team"),
                Team.short_name.label("team_short"), Conference.name.label("conference"),
                games.label("games"), sets_sum.label("sets"), value.label("value"),
            )
            .select_from(PlayerGameStat)
            .join(ContestWeek, ContestWeek.contest_id == PlayerGameStat.contest_id)
            .join(Player, Player.id == PlayerGameStat.player_id)
            .join(Team, Team.id == Player.team_id, isouter=True)
            .join(Conference, Conference.id == Team.conference_id, isouter=True)
            .where(PlayerGameStat.season == season, ContestWeek.week_number == week)
            .group_by(Player.id, Player.name, Player.position, Player.team_id,
                      Team.name, Team.short_name, Conference.name)
        )
        if position:
            stmt = stmt.where(Player.position.ilike(f"%{position}%"))
        if team_id is not None:
            stmt = stmt.where(Player.team_id == team_id)
        stmt = _conf_filter(stmt, conference, conference_id)
        if min_sets:
            stmt = stmt.having(sets_sum >= min_sets)
        if min_attacks:
            stmt = stmt.having(_sum("total_attacks") >= min_attacks)
    else:  # season scope — read the matview
        msv = PlayerSeasonStat
        value = getattr(msv, stat)
        stmt = (
            select(
                msv.player_id.label("player_id"), Player.name, Player.position,
                Player.team_id, Team.name.label("team"),
                Team.short_name.label("team_short"), Conference.name.label("conference"),
                msv.gp.label("games"), msv.sp.label("sets"), value.label("value"),
            )
            .select_from(msv)
            .join(Player, Player.id == msv.player_id)
            .join(Team, Team.id == Player.team_id, isouter=True)
            .join(Conference, Conference.id == Team.conference_id, isouter=True)
            .where(msv.season == season)
        )
        if position:
            stmt = stmt.where(Player.position.ilike(f"%{position}%"))
        if team_id is not None:
            stmt = stmt.where(Player.team_id == team_id)
        stmt = _conf_filter(stmt, conference, conference_id)
        if min_sets:
            stmt = stmt.where(msv.sp >= min_sets)
        if min_attacks:
            stmt = stmt.where(msv.total_attacks >= min_attacks)

    stmt = stmt.order_by(nulls_last(desc(value))).limit(limit).offset(offset)
    return [
        LeaderRow(
            player_id=r.player_id, name=r.name, team_id=r.team_id, team=r.team,
            team_short=r.team_short, conference=r.conference, position=r.position,
            games=int(r.games) if r.games is not None else None,
            sets=float(r.sets) if r.sets is not None else None,
            value=float(r.value) if r.value is not None else None,
        )
        for r in db.execute(stmt).all()
    ]


@router.get("/leaderboards", response_model=list[LeaderRow])
def leaderboards(
    stat: str = Query("kills", description="stat column to rank by"),
    scope: str = Query("season", pattern="^(season|week)$"),
    season: int | None = None,
    week: int | None = None,
    conference: str | None = None,
    conference_id: int | None = None,
    position: str | None = Query(None, description="position substring, e.g. 'OH'"),
    team_id: int | None = Query(None, description="restrict to one team's roster"),
    min_sets: float = Query(0, ge=0, description="minimum sets played (per-set-rate floor)"),
    min_attacks: float = Query(
        0, ge=0, description="minimum total attacks (hit% qualifier floor)"
    ),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
):
    return _player_leaderboard(
        db, stat=stat, scope=scope, season=_season(season), week=week,
        conference=conference, conference_id=conference_id, position=position,
        team_id=team_id, min_sets=min_sets, min_attacks=min_attacks,
        limit=limit, offset=offset,
    )


@router.get("/leaderboards/fantasy", response_model=list[LeaderRow])
def fantasy_leaderboard(
    scope: str = Query("season", pattern="^(season|week)$"),
    season: int | None = None,
    week: int | None = None,
    conference: str | None = None,
    conference_id: int | None = None,
    position: str | None = None,
    min_sets: float = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    weights: dict[str, float] = Depends(resolve_weights),
    db: Session = Depends(get_session),
):
    """Top players by the configurable Fantasy Points composite."""
    season = _season(season)
    if scope == "week":
        if week is None:
            raise HTTPException(422, "week is required when scope=week")
        value = _fantasy_week_expr(weights)
        games = func.count(func.distinct(PlayerGameStat.contest_id))
        sets_sum = _sum("sets")
        stmt = (
            select(
                Player.id.label("player_id"), Player.name, Player.position, Player.team_id,
                Team.name.label("team"), Team.short_name.label("team_short"),
                Conference.name.label("conference"),
                games.label("games"), sets_sum.label("sets"), value.label("value"),
            )
            .select_from(PlayerGameStat)
            .join(ContestWeek, ContestWeek.contest_id == PlayerGameStat.contest_id)
            .join(Player, Player.id == PlayerGameStat.player_id)
            .join(Team, Team.id == Player.team_id, isouter=True)
            .join(Conference, Conference.id == Team.conference_id, isouter=True)
            .where(PlayerGameStat.season == season, ContestWeek.week_number == week)
            .group_by(Player.id, Player.name, Player.position, Player.team_id,
                      Team.name, Team.short_name, Conference.name)
        )
        if position:
            stmt = stmt.where(Player.position.ilike(f"%{position}%"))
        stmt = _conf_filter(stmt, conference, conference_id)
        if min_sets:
            stmt = stmt.having(sets_sum >= min_sets)
    else:
        msv = PlayerSeasonStat
        value = _fantasy_season_expr(weights)
        stmt = (
            select(
                msv.player_id.label("player_id"), Player.name, Player.position, Player.team_id,
                Team.name.label("team"), Team.short_name.label("team_short"),
                Conference.name.label("conference"),
                msv.gp.label("games"), msv.sp.label("sets"), value.label("value"),
            )
            .select_from(msv)
            .join(Player, Player.id == msv.player_id)
            .join(Team, Team.id == Player.team_id, isouter=True)
            .join(Conference, Conference.id == Team.conference_id, isouter=True)
            .where(msv.season == season)
        )
        if position:
            stmt = stmt.where(Player.position.ilike(f"%{position}%"))
        stmt = _conf_filter(stmt, conference, conference_id)
        if min_sets:
            stmt = stmt.where(msv.sp >= min_sets)

    stmt = stmt.order_by(nulls_last(desc(value))).limit(limit).offset(offset)
    return [
        LeaderRow(
            player_id=r.player_id, name=r.name, team_id=r.team_id, team=r.team,
            team_short=r.team_short, conference=r.conference, position=r.position,
            games=int(r.games) if r.games is not None else None,
            sets=float(r.sets) if r.sets is not None else None,
            value=round(float(r.value), 2) if r.value is not None else None,
        )
        for r in db.execute(stmt).all()
    ]


@router.get("/team-stats", response_model=list[TeamStatRow])
def team_stats(
    season: int | None = None,
    week: int | None = None,
    conference: str | None = None,
    conference_id: int | None = None,
    limit: int = Query(400, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    weights: dict[str, float] = Depends(resolve_weights),
    db: Session = Depends(get_session),
):
    """Team-aggregate stat lines (sum of the roster's game stats), optionally week-scoped."""
    season = _season(season)
    games = func.count(func.distinct(PlayerGameStat.contest_id))
    fantasy = _fantasy_week_expr(weights)
    stmt = (
        select(
            Team.id.label("team_id"), Team.name.label("team"),
            Team.short_name.label("team_short"),
            Conference.name.label("conference"), games.label("games"),
            _sum("kills").label("kills"), _sum("assists").label("assists"),
            _sum("aces").label("aces"), _sum("digs").label("digs"),
            (_sum("block_solos") + _sum("block_assists")).label("total_blocks"),
            _sum("pts").label("pts"), fantasy.label("fantasy_points"),
        )
        .select_from(PlayerGameStat)
        .join(Team, Team.id == PlayerGameStat.team_id)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(PlayerGameStat.season == season)
        .group_by(Team.id, Team.name, Team.short_name, Conference.name)
    )
    if week is not None:
        stmt = stmt.join(ContestWeek, ContestWeek.contest_id == PlayerGameStat.contest_id).where(
            ContestWeek.week_number == week
        )
    stmt = _conf_filter(stmt, conference, conference_id)
    stmt = stmt.order_by(nulls_last(desc(fantasy))).limit(limit).offset(offset)
    return [
        TeamStatRow(
            team_id=r.team_id, team=r.team, team_short=r.team_short, conference=r.conference,
            games=int(r.games) if r.games is not None else None,
            kills=r.kills, assists=r.assists, aces=r.aces, digs=r.digs,
            total_blocks=r.total_blocks, pts=r.pts,
            fantasy_points=round(float(r.fantasy_points), 2) if r.fantasy_points is not None else None,
        )
        for r in db.execute(stmt).all()
    ]


def compute_team_records(contests: list[dict], teams: dict[int, dict]) -> list[dict]:
    """Derive per-team season records from contest linescores. Pure (no DB) — unit-testable.

    ``contests`` items carry ``date, home_team_id, away_team_id, home_sets_won, away_sets_won``;
    only *decided* contests (all four present and sets_won differ) are counted. ``teams`` maps
    ``team_id`` -> ``{name, team_short, conference, conference_id, rpi_rank, rpi_record}``.
    Opponent Record excludes head-to-head meetings; Opp RPI is the mean of faced opponents' RPI
    ranks; win_streak is the signed run from the most recent game (+wins / −losses).
    """
    appearances: dict[int, list[dict]] = defaultdict(list)
    for c in contests:
        h, a = c.get("home_team_id"), c.get("away_team_id")
        hs, as_ = c.get("home_sets_won"), c.get("away_sets_won")
        if None in (h, a, hs, as_) or hs == as_:
            continue
        date = c.get("date") or ""
        appearances[h].append({"date": date, "opp": a, "own": hs, "them": as_, "won": hs > as_})
        appearances[a].append({"date": date, "opp": h, "own": as_, "them": hs, "won": as_ > hs})

    overall = {  # season-wide W-L per team, incl. head-to-head (removed per meeting below)
        tid: (sum(g["won"] for g in gs), sum(not g["won"] for g in gs))
        for tid, gs in appearances.items()
    }

    out: list[dict] = []
    for tid, gs in appearances.items():
        t = teams.get(tid)
        if t is None:
            continue
        wins = sum(g["won"] for g in gs)
        sets_won = sum(g["own"] for g in gs)
        sets_lost = sum(g["them"] for g in gs)
        conf_w = conf_l = nonconf_w = nonconf_l = opp_w = opp_l = 0
        rpis: list[int] = []
        for g in gs:
            opp = teams.get(g["opp"])
            same_conf = (
                opp is not None and opp["conference_id"] is not None
                and opp["conference_id"] == t["conference_id"]
            )
            if same_conf:
                conf_w, conf_l = (conf_w + 1, conf_l) if g["won"] else (conf_w, conf_l + 1)
            else:
                nonconf_w, nonconf_l = (
                    (nonconf_w + 1, nonconf_l) if g["won"] else (nonconf_w, nonconf_l + 1)
                )
            o_w, o_l = overall.get(g["opp"], (0, 0))  # remove this head-to-head meeting
            opp_w += o_w - (0 if g["won"] else 1)
            opp_l += o_l - (1 if g["won"] else 0)
            if opp is not None and opp["rpi_rank"] is not None:
                rpis.append(opp["rpi_rank"])

        streak = 0
        for g in sorted(gs, key=lambda x: x["date"], reverse=True):
            if streak == 0:
                streak = 1 if g["won"] else -1
            elif g["won"] and streak > 0:
                streak += 1
            elif not g["won"] and streak < 0:
                streak -= 1
            else:
                break

        total_sets = sets_won + sets_lost
        out.append({
            "team_id": tid, "team": t["name"], "team_short": t["team_short"],
            "conference": t["conference"], "games": len(gs), "wins": wins,
            "losses": len(gs) - wins, "sets_won": sets_won, "sets_lost": sets_lost,
            "set_pct": round(sets_won / total_sets, 3) if total_sets else None,
            "conf_wins": conf_w, "conf_losses": conf_l,
            "nonconf_wins": nonconf_w, "nonconf_losses": nonconf_l,
            "opp_wins": opp_w, "opp_losses": opp_l,
            "opp_rpi": round(sum(rpis) / len(rpis), 1) if rpis else None,
            "win_streak": streak, "rpi_rank": t["rpi_rank"], "rpi_record": t["rpi_record"],
        })
    return out


@router.get("/team-records", response_model=list[TeamRecordRow])
def team_records(
    season: int | None = None,
    conference: str | None = None,
    conference_id: int | None = None,
    db: Session = Depends(get_session),
):
    """Team season records (W-L, sets, conf/non-conf, streak, opponent record) from linescores."""
    season = _season(season)
    teams = {
        r.id: {
            "name": r.name, "team_short": r.short_name, "conference": r.conference,
            "conference_id": r.conference_id, "rpi_rank": r.rpi_rank, "rpi_record": r.rpi_record,
        }
        for r in db.execute(
            select(
                Team.id, Team.name, Team.short_name, Conference.name.label("conference"),
                Team.conference_id, Team.rpi_rank, Team.rpi_record,
            ).join(Conference, Conference.id == Team.conference_id, isouter=True)
        ).all()
    }
    contests = [
        {"date": c.date, "home_team_id": c.home_team_id, "away_team_id": c.away_team_id,
         "home_sets_won": c.home_sets_won, "away_sets_won": c.away_sets_won}
        for c in db.execute(
            select(
                Contest.date, Contest.home_team_id, Contest.away_team_id,
                Contest.home_sets_won, Contest.away_sets_won,
            ).where(Contest.season == season)
        ).all()
    ]
    records = compute_team_records(contests, teams)
    if conference:
        records = [r for r in records if r["conference"] == conference]
    if conference_id is not None:
        records = [r for r in records if teams[r["team_id"]]["conference_id"] == conference_id]
    records.sort(key=lambda r: (-r["wins"], r["losses"], -(r["set_pct"] or 0)))
    return [TeamRecordRow(**r) for r in records]


@router.get("/teams/{team_id}/player-stats", response_model=list[PlayerStatLine])
def team_player_stats(
    team_id: int,
    scope: str = Query("season", pattern="^(season|week)$"),
    season: int | None = None,
    week: int | None = None,
    weights: dict[str, float] = Depends(resolve_weights),
    db: Session = Depends(get_session),
):
    """Full per-player stat lines for one team's roster (every category) — the team detail table.

    Every rostered player for the team+season is returned, including those with no game stats yet
    (their stat cells come back null). Season scope reads the matview; week scope aggregates game
    stats live. Both are rooted on the roster (``Player``) and outer-joined to the stats so bench
    players still appear. ``fantasy_points`` uses the same configurable weights as the fantasy
    leaderboards.
    """
    season = _season(season)
    if scope == "week":
        if week is None:
            raise HTTPException(422, "week is required when scope=week")
        fp = _fantasy_week_expr(weights)
        hit = (_sum("kills") - _sum("errors")) / func.nullif(_sum("total_attacks"), 0)
        # Per-player week aggregate, then outer-joined to the full roster below.
        agg = (
            select(
                PlayerGameStat.player_id.label("player_id"),
                func.count(func.distinct(PlayerGameStat.contest_id)).label("games"),
                _sum("sets").label("sets"), _sum("kills").label("kills"),
                _sum("errors").label("errors"), _sum("total_attacks").label("total_attacks"),
                hit.label("hit_pct"), _sum("assists").label("assists"),
                _sum("aces").label("aces"), _sum("serr").label("serr"),
                _sum("digs").label("digs"), _sum("retatt").label("retatt"),
                _sum("rerr").label("rerr"), _sum("block_solos").label("block_solos"),
                _sum("block_assists").label("block_assists"),
                (_sum("block_solos") + _sum("block_assists")).label("total_blocks"),
                _sum("berr").label("berr"), _sum("bhe").label("bhe"),
                _sum("pts").label("pts"),
                _week_value("kills_per_set").label("kills_per_set"),
                _week_value("assists_per_set").label("assists_per_set"),
                _week_value("aces_per_set").label("aces_per_set"),
                _week_value("digs_per_set").label("digs_per_set"),
                _week_value("blocks_per_set").label("blocks_per_set"),
                _week_value("pts_per_set").label("pts_per_set"),
                fp.label("fantasy_points"),
            )
            .select_from(PlayerGameStat)
            .join(ContestWeek, ContestWeek.contest_id == PlayerGameStat.contest_id)
            .where(PlayerGameStat.season == season, ContestWeek.week_number == week)
            .group_by(PlayerGameStat.player_id)
            .subquery()
        )
        stmt = (
            select(
                Player.id.label("player_id"), Player.name, Player.position,
                agg.c.games, agg.c.sets, agg.c.kills, agg.c.errors, agg.c.total_attacks,
                agg.c.hit_pct, agg.c.assists, agg.c.aces, agg.c.serr, agg.c.digs,
                agg.c.retatt, agg.c.rerr, agg.c.block_solos, agg.c.block_assists,
                agg.c.total_blocks, agg.c.berr, agg.c.bhe, agg.c.pts,
                agg.c.kills_per_set, agg.c.assists_per_set, agg.c.aces_per_set,
                agg.c.digs_per_set, agg.c.blocks_per_set, agg.c.pts_per_set,
                agg.c.fantasy_points,
            )
            .select_from(Player)
            .join(agg, agg.c.player_id == Player.id, isouter=True)
            .where(Player.team_id == team_id, Player.season == season)
            .order_by(nulls_last(desc(agg.c.fantasy_points)), Player.name)
        )
    else:
        msv = PlayerSeasonStat
        fp = _fantasy_season_expr(weights)  # coalesces null matview cols -> 0 for statless players
        stmt = (
            select(
                Player.id.label("player_id"), Player.name, Player.position,
                msv.gp.label("games"), msv.sp.label("sets"), msv.kills, msv.errors,
                msv.total_attacks, msv.hit_pct, msv.assists, msv.aces, msv.serr, msv.digs,
                msv.retatt, msv.rerr, msv.block_solos, msv.block_assists, msv.total_blocks,
                msv.berr, msv.bhe, msv.pts,
                msv.kills_per_set, msv.assists_per_set, msv.aces_per_set,
                msv.digs_per_set, msv.blocks_per_set, msv.pts_per_set,
                fp.label("fantasy_points"),
            )
            .select_from(Player)
            .join(msv, and_(msv.player_id == Player.id, msv.season == season), isouter=True)
            .where(Player.team_id == team_id, Player.season == season)
            .order_by(nulls_last(desc(fp)), Player.name)
        )
    return [
        PlayerStatLine(
            player_id=r.player_id, name=r.name, position=r.position,
            games=int(r.games) if r.games is not None else None,
            sets=r.sets, kills=r.kills, errors=r.errors, total_attacks=r.total_attacks,
            hit_pct=r.hit_pct, assists=r.assists, aces=r.aces, serr=r.serr, digs=r.digs,
            retatt=r.retatt, rerr=r.rerr, block_solos=r.block_solos,
            block_assists=r.block_assists, total_blocks=r.total_blocks, berr=r.berr,
            bhe=r.bhe, pts=r.pts,
            kills_per_set=r.kills_per_set, assists_per_set=r.assists_per_set,
            aces_per_set=r.aces_per_set, digs_per_set=r.digs_per_set,
            blocks_per_set=r.blocks_per_set, pts_per_set=r.pts_per_set,
            fantasy_points=round(float(r.fantasy_points), 2) if r.fantasy_points is not None else None,
        )
        for r in db.execute(stmt).all()
    ]


@router.get("/conferences/{conference_id}/leaders", response_model=list[LeaderRow])
def conference_leaders(
    conference_id: int,
    stat: str = Query("pts"),
    scope: str = Query("season", pattern="^(season|week)$"),
    season: int | None = None,
    week: int | None = None,
    position: str | None = None,
    min_sets: float = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    db: Session = Depends(get_session),
):
    """Top players within one conference (thin wrapper over the player leaderboard)."""
    return _player_leaderboard(
        db, stat=stat, scope=scope, season=_season(season), week=week,
        conference=None, conference_id=conference_id, position=position,
        min_sets=min_sets, limit=limit, offset=0,
    )


@router.get("/search", response_model=SearchOut)
def search(
    q: str = Query(..., min_length=1, description="name substring"),
    season: int | None = None,
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_session),
):
    """Unified search over players (current/given season) and teams."""
    season = _season(season)
    like = f"%{q}%"
    players = db.scalars(
        select(Player).where(Player.season == season, Player.name.ilike(like))
        .order_by(Player.name).limit(limit)
    ).all()
    teams = db.scalars(
        select(Team).where(or_(Team.name.ilike(like), Team.short_name.ilike(like)))
        .order_by(Team.name).limit(limit)
    ).all()
    return SearchOut(
        players=[PlayerOut.from_player(p) for p in players],
        teams=[
            TeamOut(
                id=t.id, name=t.name, short_name=t.short_name,
                conference=t.conference.name if t.conference else None,
                city=t.city, state=t.state, logo_light=t.logo_light, logo_dark=t.logo_dark,
                rpi_rank=t.rpi_rank, rpi_record=t.rpi_record,
            )
            for t in teams
        ],
    )


@router.get("/players/{player_id}/game-log", response_model=list[GameLogRow])
def player_game_log(
    player_id: int, season: int | None = None, db: Session = Depends(get_session)
):
    """Per-game lines enriched with date, week number, and opponent — the fantasy player card."""
    stmt = (
        select(
            PlayerGameStat, ContestWeek.week_number, ContestWeek.game_date,
            Contest.date, Contest.home_team_id, Contest.away_team_id,
        )
        .select_from(PlayerGameStat)
        .join(ContestWeek, ContestWeek.contest_id == PlayerGameStat.contest_id, isouter=True)
        .join(Contest, Contest.contest_id == PlayerGameStat.contest_id, isouter=True)
        .where(PlayerGameStat.player_id == player_id)
    )
    if season is not None:
        stmt = stmt.where(PlayerGameStat.season == season)
    rows = db.execute(stmt.order_by(nulls_last(ContestWeek.game_date.asc()),
                                    PlayerGameStat.contest_id)).all()

    # Resolve opponent team names in one query.
    opp_ids: set[int] = set()
    for _pgs, _wk, _gd, _date, home, away in rows:
        opp = home if _pgs.team_id == away else away
        if opp is not None:
            opp_ids.add(opp)
    names: dict[int, str] = {}
    shorts: dict[int, str | None] = {}
    if opp_ids:
        for tid, nm, sn in db.execute(
            select(Team.id, Team.name, Team.short_name).where(Team.id.in_(opp_ids))
        ).all():
            names[tid] = nm
            shorts[tid] = sn

    out: list[GameLogRow] = []
    for pgs, wk, gd, date_str, home, away in rows:
        opp = home if pgs.team_id == away else away
        bs = pgs.block_solos or 0
        ba = pgs.block_assists or 0
        fp = round(sum(float(w) * (getattr(pgs, k) or 0) for k, w in FANTASY_WEIGHTS.items()), 2)
        out.append(GameLogRow(
            contest_id=pgs.contest_id,
            date=date_str or (gd.isoformat() if gd else None),
            week_number=wk, opponent_id=opp, opponent=names.get(opp),
            opponent_short=shorts.get(opp),
            sets=pgs.sets, kills=pgs.kills, errors=pgs.errors, total_attacks=pgs.total_attacks,
            assists=pgs.assists, aces=pgs.aces, serr=pgs.serr, digs=pgs.digs, retatt=pgs.retatt,
            rerr=pgs.rerr, block_solos=pgs.block_solos, block_assists=pgs.block_assists,
            total_blocks=bs + ba, berr=pgs.berr, pts=pgs.pts, bhe=pgs.bhe, fantasy_points=fp,
        ))
    return out
