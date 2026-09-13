"""Team-level season metrics + league-percentile layer for the scout builder.

Nothing else in the repo compares teams against the whole league, so this module is the net-new
comparison layer. It builds, for a season:

* per-team raw metric values (offense, defense, résumé) keyed by ``team_id`` — computed with direct
  team_id-grouped SQL rather than the name-keyed, ``_MAX_LIMIT``-capped ``team_stats``/``team_defense``
  query tools (which top out at 100 rows, below D1's ~340 teams);
* per-team roster stat lines from the ``player_season_stats`` matview (best hitters / blockers /
  setter identity / middle usage);
* for every metric, each team's **percentile** (0-100, oriented so higher is always better),
  **z-score**, and **league rank**, computed only over teams with enough matches to be comparable.

The résumé metrics reuse :func:`vb.api.routers.stats.load_team_records` and
:func:`vb.query.tools.compute_quality_wins`, which already aggregate every team in one call.
"""
from __future__ import annotations

import bisect
import statistics
from collections import defaultdict

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..models import Player, PlayerGameStat, PlayerSeasonStat

# Teams with fewer than this many matches are marked low-sample (prose hedges) and are excluded
# from the RATE distributions (per-set, hit%) so a 1-match team can't distort percentiles. They are
# still ranked against the qualified distribution.
LOW_SAMPLE_MATCHES = 4
_MIN_DIST_MATCHES = 2  # a team needs at least this many matches to enter a rate distribution


# Metric catalog — one entry per league-comparable team metric. ``higher_is_better`` orients the
# percentile so a high percentile always reads as a strength (opponent hitting %, RPI/AVCA rank, and
# errors are inverted). ``rate`` marks metrics that need a match-count floor to be meaningful.
# ``source`` documents where the raw value comes from. Order is roughly narration order.
METRIC_CATALOG: list[dict] = [
    # Offense
    {"key": "hit_pct", "label": "hitting %", "higher_is_better": True, "rate": True,
     "cat": "offense", "kind": "pct3"},
    {"key": "kills_per_set", "label": "kills/set", "higher_is_better": True, "rate": True,
     "cat": "offense", "kind": "rate"},
    {"key": "assists_per_set", "label": "assists/set", "higher_is_better": True, "rate": True,
     "cat": "offense", "kind": "rate"},
    {"key": "aces_per_set", "label": "aces/set", "higher_is_better": True, "rate": True,
     "cat": "serve", "kind": "rate"},
    {"key": "digs_per_set", "label": "digs/set", "higher_is_better": True, "rate": True,
     "cat": "defense", "kind": "rate"},
    {"key": "blocks_per_set", "label": "blocks/set", "higher_is_better": True, "rate": True,
     "cat": "block", "kind": "rate"},
    {"key": "pts_per_set", "label": "points/set", "higher_is_better": True, "rate": True,
     "cat": "offense", "kind": "rate"},
    {"key": "hit_errors_per_set", "label": "hitting errors/set", "higher_is_better": False,
     "rate": True, "cat": "offense", "kind": "rate"},
    {"key": "middle_share", "label": "middle-attack share", "higher_is_better": True, "rate": True,
     "cat": "offense", "kind": "pctshare"},
    # Defense
    {"key": "opp_hit_pct", "label": "opponent hitting %", "higher_is_better": False, "rate": True,
     "cat": "defense", "kind": "pct3"},
    {"key": "opp_kills_per_set", "label": "opponent kills/set", "higher_is_better": False,
     "rate": True, "cat": "defense", "kind": "rate"},
    # Résumé
    {"key": "win_pct", "label": "win %", "higher_is_better": True, "rate": False,
     "cat": "resume", "kind": "pct3"},
    {"key": "set_pct", "label": "set win %", "higher_is_better": True, "rate": False,
     "cat": "resume", "kind": "pct3"},
    {"key": "quality_wins", "label": "quality wins", "higher_is_better": True, "rate": False,
     "cat": "resume", "kind": "int"},
    {"key": "sos", "label": "strength of schedule", "higher_is_better": True, "rate": False,
     "cat": "resume", "kind": "pct3"},
]

_CATALOG_BY_KEY = {m["key"]: m for m in METRIC_CATALOG}


def _team_offense(session: Session, season: int) -> dict[int, dict]:
    """``team_id -> {kills, hit_errors, total_attacks, assists, aces, digs, block_solos,
    block_assists, pts, games}`` — season box-score totals grouped by team."""
    pgs = PlayerGameStat
    rows = session.execute(
        select(
            pgs.team_id,
            func.count(func.distinct(pgs.contest_id)).label("games"),
            func.sum(pgs.kills).label("kills"),
            func.sum(pgs.errors).label("hit_errors"),
            func.sum(pgs.total_attacks).label("total_attacks"),
            func.sum(pgs.assists).label("assists"),
            func.sum(pgs.aces).label("aces"),
            func.sum(pgs.digs).label("digs"),
            func.sum(pgs.block_solos).label("block_solos"),
            func.sum(pgs.block_assists).label("block_assists"),
            func.sum(pgs.pts).label("pts"),
        )
        .where(pgs.season == season)
        .group_by(pgs.team_id)
    ).all()
    return {
        r.team_id: {
            "games": int(r.games or 0),
            "kills": float(r.kills or 0), "hit_errors": float(r.hit_errors or 0),
            "total_attacks": float(r.total_attacks or 0), "assists": float(r.assists or 0),
            "aces": float(r.aces or 0), "digs": float(r.digs or 0),
            "block_solos": float(r.block_solos or 0), "block_assists": float(r.block_assists or 0),
            "pts": float(r.pts or 0),
        }
        for r in rows
    }


def _team_defense(session: Session, season: int) -> dict[int, dict]:
    """``team_id -> {opp_kills, opp_errors, opp_total_attacks, games}`` — the opponent's box-score
    line summed across each team's matches (replicates ``team_defense`` keyed by team_id, uncapped)."""
    pgs = PlayerGameStat
    per = (
        select(
            pgs.contest_id.label("cid"), pgs.team_id.label("tid"),
            func.sum(pgs.kills).label("k"), func.sum(pgs.errors).label("e"),
            func.sum(pgs.total_attacks).label("ta"),
        )
        .where(pgs.season == season)
        .group_by(pgs.contest_id, pgs.team_id)
        .subquery()
    )
    me, opp = per.alias("me"), per.alias("opp")
    rows = session.execute(
        select(
            me.c.tid.label("team_id"),
            func.count(func.distinct(me.c.cid)).label("games"),
            func.sum(opp.c.k).label("opp_kills"), func.sum(opp.c.e).label("opp_errors"),
            func.sum(opp.c.ta).label("opp_total_attacks"),
        )
        .select_from(me)
        .join(opp, and_(opp.c.cid == me.c.cid, opp.c.tid != me.c.tid))
        .group_by(me.c.tid)
    ).all()
    return {
        r.team_id: {
            "games": int(r.games or 0), "opp_kills": float(r.opp_kills or 0),
            "opp_errors": float(r.opp_errors or 0),
            "opp_total_attacks": float(r.opp_total_attacks or 0),
        }
        for r in rows
    }


def team_player_lines(session: Session, season: int) -> dict[int, list[dict]]:
    """``team_id -> [player line dicts]`` from the ``player_season_stats`` matview joined to the
    roster (season scope, mirroring ``stats.team_player_stats`` but for every team at once). Each
    line carries the fields the builder needs to pick leaders and setter system."""
    msv = PlayerSeasonStat
    rows = session.execute(
        select(
            Player.team_id, Player.id.label("player_id"), Player.name, Player.position,
            Player.number, Player.class_year,
            msv.sp.label("sets"), msv.kills, msv.errors, msv.total_attacks, msv.hit_pct,
            msv.assists, msv.aces, msv.digs, msv.total_blocks,
            msv.kills_per_set, msv.assists_per_set, msv.blocks_per_set, msv.digs_per_set,
        )
        .select_from(Player)
        .join(msv, and_(msv.player_id == Player.id, msv.season == season), isouter=True)
        .where(Player.season == season)
    ).all()
    out: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        out[r.team_id].append({
            "player_id": r.player_id, "name": r.name, "position": r.position,
            "number": r.number, "class_year": r.class_year,
            "sets": _f(r.sets), "kills": _f(r.kills), "errors": _f(r.errors),
            "total_attacks": _f(r.total_attacks), "hit_pct": _f(r.hit_pct),
            "assists": _f(r.assists), "aces": _f(r.aces), "digs": _f(r.digs),
            "total_blocks": _f(r.total_blocks), "kills_per_set": _f(r.kills_per_set),
            "assists_per_set": _f(r.assists_per_set), "blocks_per_set": _f(r.blocks_per_set),
            "digs_per_set": _f(r.digs_per_set),
        })
    return out


def _f(v) -> float | None:
    return float(v) if v is not None else None


def _middle_share(lines: list[dict]) -> float | None:
    """Fraction of a team's total attacks taken by middle blockers (position starts with 'M')."""
    total = sum((ln["total_attacks"] or 0) for ln in lines)
    if total <= 0:
        return None
    mids = sum((ln["total_attacks"] or 0) for ln in lines
               if (ln["position"] or "").upper().startswith("M"))
    return round(mids / total, 3)


def compute_team_metrics(
    session: Session, season: int, records: list[dict], quality: list[dict],
    player_lines: dict[int, list[dict]],
) -> dict[int, dict]:
    """Assemble every team's raw metric values (the catalog keys) keyed by team_id.

    ``records`` is ``load_team_records`` output, ``quality`` is ``compute_quality_wins`` output,
    ``player_lines`` is :func:`team_player_lines`. Per-set rates use match sets from ``records``.
    """
    offense = _team_offense(session, season)
    defense = _team_defense(session, season)
    rec_by_team = {r["team_id"]: r for r in records}
    qw_by_team = {q["team_id"]: q.get("quality_wins", 0) for q in quality}

    metrics: dict[int, dict] = {}
    for team_id, off in offense.items():
        rec = rec_by_team.get(team_id)
        sets = (rec["sets_won"] + rec["sets_lost"]) if rec else 0
        games = off["games"]
        ta = off["total_attacks"]
        total_blocks = off["block_solos"] + off["block_assists"] / 2.0
        deff = defense.get(team_id, {})
        opp_ta = deff.get("opp_total_attacks", 0)
        m: dict = {"_games": games, "_sets": sets}
        if sets:
            m["kills_per_set"] = round(off["kills"] / sets, 2)
            m["assists_per_set"] = round(off["assists"] / sets, 2)
            m["aces_per_set"] = round(off["aces"] / sets, 2)
            m["digs_per_set"] = round(off["digs"] / sets, 2)
            m["blocks_per_set"] = round(total_blocks / sets, 2)
            m["pts_per_set"] = round(off["pts"] / sets, 2)
            m["hit_errors_per_set"] = round(off["hit_errors"] / sets, 2)
            m["opp_kills_per_set"] = round(deff.get("opp_kills", 0) / sets, 2) if deff else None
        m["hit_pct"] = round((off["kills"] - off["hit_errors"]) / ta, 3) if ta else None
        m["opp_hit_pct"] = (
            round((deff["opp_kills"] - deff["opp_errors"]) / opp_ta, 3) if opp_ta else None
        )
        m["middle_share"] = _middle_share(player_lines.get(team_id, []))
        if rec:
            g = rec["wins"] + rec["losses"]
            m["win_pct"] = round(rec["wins"] / g, 3) if g else None
            m["set_pct"] = rec["set_pct"]
            ow, ol = rec.get("opp_wins", 0), rec.get("opp_losses", 0)
            m["sos"] = round(ow / (ow + ol), 3) if (ow + ol) else None
        m["quality_wins"] = qw_by_team.get(team_id, 0)
        metrics[team_id] = m
    return metrics


def _percentile(sorted_vals: list[float], v: float, higher_is_better: bool) -> float:
    """Percentile of ``v`` within ``sorted_vals`` (mid-rank for ties), oriented so higher = better."""
    lo = bisect.bisect_left(sorted_vals, v)
    hi = bisect.bisect_right(sorted_vals, v)
    n = len(sorted_vals)
    pct = 100.0 * (lo + (hi - lo) / 2.0) / n
    return round(pct if higher_is_better else 100.0 - pct, 1)


def attach_percentiles(metrics_by_team: dict[int, dict]) -> dict[int, dict]:
    """For each team and catalog metric, compute ``{value, pct, z, rank, n}`` (pct oriented so high
    = better). Rate metrics are ranked against only teams with >= ``_MIN_DIST_MATCHES`` matches."""
    # Build each metric's league distribution once.
    dist: dict[str, dict] = {}
    for spec in METRIC_CATALOG:
        key = spec["key"]
        vals = []
        for m in metrics_by_team.values():
            v = m.get(key)
            if v is None:
                continue
            if spec["rate"] and m.get("_games", 0) < _MIN_DIST_MATCHES:
                continue
            vals.append(float(v))
        vals.sort()
        mean = statistics.fmean(vals) if vals else None
        std = statistics.pstdev(vals) if len(vals) > 1 else None
        dist[key] = {"sorted": vals, "mean": mean, "std": std}

    out: dict[int, dict] = {}
    for team_id, m in metrics_by_team.items():
        team_pct: dict[str, dict] = {}
        for spec in METRIC_CATALOG:
            key = spec["key"]
            v = m.get(key)
            if v is None:
                continue
            d = dist[key]
            svals = d["sorted"]
            if not svals:
                continue
            hib = spec["higher_is_better"]
            pct = _percentile(svals, float(v), hib)
            # league rank (1 = best), computed in the "better" direction
            better = sum(1 for x in svals if (x > v if hib else x < v))
            z = None
            if d["std"]:
                raw_z = (float(v) - d["mean"]) / d["std"]
                z = round(raw_z if hib else -raw_z, 2)
            team_pct[key] = {
                "value": v, "pct": pct, "z": z, "rank": better + 1, "n": len(svals),
                "label": spec["label"], "cat": spec["cat"], "kind": spec["kind"],
            }
        out[team_id] = team_pct
    return out


def catalog_spec(key: str) -> dict | None:
    return _CATALOG_BY_KEY.get(key)
