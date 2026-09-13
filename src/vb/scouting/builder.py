"""Orchestrator for the deterministic scout builder.

:func:`build_scouting` computes the league comparison layer and the season play-by-play rollup
**once**, then assembles one structured payload per team, runs the insight engine and prose
templates over it, and upserts a single ``scouting_reports`` row per ``(team_id, season)`` (the
whole report lives in the ``data`` JSONB column). No LLM is involved — the intelligence is the
percentile/insight/prose pipeline.
"""
from __future__ import annotations

import bisect
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.routers.stats import load_team_records
from ..models import ScoutingReport
from ..query.tools import compute_quality_wins
from . import insights as insights_mod
from . import prose as prose_mod
from .metrics import (
    LOW_SAMPLE_MATCHES,
    attach_percentiles,
    compute_team_metrics,
    team_player_lines,
)
from .pbp_rollup import rollup_pbp, summarize_phase, summarize_rotations

# Floors for picking roster leaders — a player needs a real workload to be a "primary weapon."
_HITTER_MIN_ATTACKS = 30
_BLOCKER_MIN_SETS = 10
_SETTER_MIN_ASSISTS = 30
# Setter-system thresholds on share of team assists.
_SOLO_SETTER_SHARE = 0.60   # one setter with >= this share (and no strong #2) -> 5-1
_SECOND_SETTER_SHARE = 0.30  # a second setter above this share -> 6-2


def _hitter_distribution(player_lines: dict[int, list[dict]]) -> list[float]:
    """Sorted league-wide hitting %s among players with a real attack workload (for 'elite' tiers)."""
    vals = [
        ln["hit_pct"] for lines in player_lines.values() for ln in lines
        if ln.get("hit_pct") is not None and (ln.get("total_attacks") or 0) >= _HITTER_MIN_ATTACKS
    ]
    vals.sort()
    return vals


def _setter_distribution(player_lines: dict[int, list[dict]]) -> list[float]:
    """Sorted attacks/set among rotation setters league-wide — the yardstick for how aggressive a
    setter is at attacking the second ball."""
    vals = [
        (ln.get("total_attacks") or 0) / ln["sets"]
        for lines in player_lines.values() for ln in lines
        if (ln.get("assists") or 0) >= _SETTER_MIN_ASSISTS and (ln.get("sets") or 0) > 0
    ]
    vals.sort()
    return vals


def _pct_in(sorted_vals: list[float], v: float) -> float | None:
    if not sorted_vals or v is None:
        return None
    lo = bisect.bisect_left(sorted_vals, v)
    hi = bisect.bisect_right(sorted_vals, v)
    return round(100.0 * (lo + (hi - lo) / 2.0) / len(sorted_vals), 1)


def _leaders(lines: list[dict], hitter_dist: list[float]) -> dict:
    """Top hitters (with national hit% percentile), primary blocker(s), middles, and setter(s)."""
    hitters = [
        {
            "name": ln["name"], "position": ln.get("position"),
            "kills_per_set": ln.get("kills_per_set"), "hit_pct": ln.get("hit_pct"),
            "total_attacks": ln.get("total_attacks"),
            "nat_pct": _pct_in(hitter_dist, ln["hit_pct"]) if ln.get("hit_pct") is not None else None,
        }
        for ln in lines
        if (ln.get("total_attacks") or 0) >= _HITTER_MIN_ATTACKS
        and ln.get("kills_per_set") is not None
    ]
    hitters.sort(key=lambda h: (h["kills_per_set"] or 0), reverse=True)

    blockers = [
        {"name": ln["name"], "position": ln.get("position"),
         "blocks_per_set": ln.get("blocks_per_set"), "total_blocks": ln.get("total_blocks")}
        for ln in lines
        if (ln.get("sets") or 0) >= _BLOCKER_MIN_SETS and ln.get("blocks_per_set") is not None
    ]
    blockers.sort(key=lambda b: (b["blocks_per_set"] or 0), reverse=True)

    # Primary middles — MB/MH ordered by attack workload (who the team actually sets in the middle).
    middles = [
        {"name": ln["name"], "position": ln.get("position"),
         "kills_per_set": ln.get("kills_per_set"), "total_attacks": ln.get("total_attacks")}
        for ln in lines
        if (ln.get("position") or "").upper().startswith("M")
        and (ln.get("total_attacks") or 0) > 0
    ]
    middles.sort(key=lambda m: (m["total_attacks"] or 0), reverse=True)

    return {"hitters": hitters[:3], "blockers": blockers[:2], "middles": middles[:2]}


def _setter_system(lines: list[dict], setter_dist: list[float]) -> dict:
    """Classify 5-1 vs 6-2 from the assist distribution and name the setter(s)."""
    setters = sorted(
        (ln for ln in lines if (ln.get("assists") or 0) >= _SETTER_MIN_ASSISTS),
        key=lambda ln: ln["assists"], reverse=True,
    )
    total = sum((ln.get("assists") or 0) for ln in lines)
    if not setters or total <= 0:
        return {"type": None, "setters": []}
    top_share = setters[0]["assists"] / total
    second_share = setters[1]["assists"] / total if len(setters) > 1 else 0.0
    if len(setters) >= 2 and second_share >= _SECOND_SETTER_SHARE:
        stype = "6-2"
        names = [setters[0]["name"], setters[1]["name"]]
    elif top_share >= _SOLO_SETTER_SHARE:
        stype = "5-1"
        names = [setters[0]["name"]]
    else:
        # Ambiguous (e.g. a setter injury mid-season) — report the primary but don't force a label.
        stype = None
        names = [setters[0]["name"]]
    result = {"type": stype, "setters": names, "top_share": round(top_share, 3)}
    # Second-ball aggressiveness of the primary setter, percentiled against the setter population so
    # prose can call out an attacking setter vs. a pure set-first distributor.
    primary = setters[0]
    p_sets = primary.get("sets") or 0
    if p_sets > 0:
        aps = round((primary.get("total_attacks") or 0) / p_sets, 2)
        result["attack"] = {
            "name": primary["name"], "attacks_per_set": aps,
            "kills_per_set": primary.get("kills_per_set"), "hit_pct": primary.get("hit_pct"),
            "pct": _pct_in(setter_dist, aps),
        }
    return result


def _build_team_payload(
    team_id: int, name: str, rec: dict | None, metric: dict, pct: dict,
    lines: list[dict], hitter_dist: list[float], setter_dist: list[float],
    team_pbp: dict | None,
) -> dict:
    games = metric.get("_games", 0)
    rot = summarize_rotations(team_pbp) if team_pbp else None
    phase = summarize_phase(team_pbp) if team_pbp else None
    sample = {
        "matches": games, "sets": metric.get("_sets", 0),
        "low_sample": games < LOW_SAMPLE_MATCHES,
        "has_pbp": bool(team_pbp and team_pbp.get("matches_with_pbp", 0) > 0),
    }
    record = None
    if rec:
        record = {
            "wins": rec["wins"], "losses": rec["losses"], "conference": rec.get("conference"),
            "conf_wins": rec.get("conf_wins"), "conf_losses": rec.get("conf_losses"),
            "win_streak": rec.get("win_streak"),
        }
    insights = insights_mod.build_insights(pct, phase)
    payload = {
        "team_id": team_id, "team": name,
        "generated_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "sample": sample,
        "record": record,
        "system": _setter_system(lines, setter_dist),
        "leaders": _leaders(lines, hitter_dist),
        "percentiles": pct,
        "rotations": rot or {},
        "phase": phase or {},
        "insights": insights,
    }
    payload["profile"] = prose_mod.profile_prose(payload)
    payload["keys"] = prose_mod.keys_prose(payload)
    return payload


def build_scouting(session: Session, season: int) -> dict:
    """Build and upsert a scouting report for every team with played games. Returns counts."""
    records, teams_map = load_team_records(session, season)
    quality = compute_quality_wins(session, season=season)
    if isinstance(quality, dict):  # error shape from bad filters — never for our unfiltered call
        quality = []
    player_lines = team_player_lines(session, season)
    metrics = compute_team_metrics(session, season, records, quality, player_lines)
    pct_by_team = attach_percentiles(metrics)
    pbp = rollup_pbp(session, season)
    hitter_dist = _hitter_distribution(player_lines)
    setter_dist = _setter_distribution(player_lines)
    rec_by_team = {r["team_id"]: r for r in records}

    existing = {
        r.team_id: r
        for r in session.scalars(
            select(ScoutingReport).where(ScoutingReport.season == season)
        ).all()
    }

    built = skipped = 0
    for team_id, metric in metrics.items():
        if metric.get("_games", 0) <= 0:
            skipped += 1
            continue
        name = teams_map.get(team_id, {}).get("name") or f"Team {team_id}"
        payload = _build_team_payload(
            team_id, name, rec_by_team.get(team_id), metric,
            pct_by_team.get(team_id, {}), player_lines.get(team_id, []),
            hitter_dist, setter_dist, pbp.get(team_id),
        )
        row = existing.get(team_id)
        if row is None:
            row = ScoutingReport(season=season, team_id=team_id, data=payload)
            session.add(row)
            existing[team_id] = row
        else:
            row.data = payload
        built += 1

    session.flush()
    return {"built": built, "skipped": skipped}
