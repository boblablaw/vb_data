"""Season-per-team play-by-play rollup for the scout builder.

Per-match PBP analytics already exist (:func:`vb.query.tools.per_rotation_stats`,
:func:`vb.query.tools.per_set_lineups`, :func:`vb.derive.pbp.iter_attack_touches`) but there is no
season-per-team roll-up. This module loops the season's contests once and accumulates, per team:

* **per-rotation** serve/receive rallies & wins, points ±, and the attack line (R1-R6) — from which
  the builder derives season sideout %, hold %, net ±, and the weakest/strongest rotation;
* **phase splits** — first-ball side-out (FBSO) vs transition hitting, i.e. in-system vs
  out-of-system efficiency.

Teams/contests without play-by-play simply contribute nothing (``matches_with_pbp`` stays 0), so the
builder omits the rotation/phase sections for them rather than inventing data.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..derive.pbp import iter_attack_touches
from ..models import Contest, PbpEvent, Player, Team
from ..query.tools import per_rotation_stats

_ROT_FIELDS = (
    "serve_rallies", "serve_won", "recv_rallies", "recv_won",
    "points_won", "points_lost", "kills", "attack_errors", "attack_attempts",
)


def _empty_team() -> dict:
    return {
        "matches_with_pbp": 0,
        "rotations": {r: dict.fromkeys(_ROT_FIELDS, 0) for r in range(1, 7)},
        "phase": {"fbso": [0, 0, 0], "transition": [0, 0, 0]},
    }


def rollup_pbp(session: Session, season: int) -> dict[int, dict]:
    """``team_id -> {matches_with_pbp, rotations{1..6}, phase{fbso,transition}}`` for the season."""
    contests = {
        c.contest_id: (c.away_team_id, c.home_team_id)
        for c in session.execute(
            select(Contest.contest_id, Contest.away_team_id, Contest.home_team_id)
            .where(Contest.season == season)
        ).all()
    }
    team_names = {t.id: t.name for t in session.execute(select(Team.id, Team.name)).all()}
    player_team = {
        p.id: p.team_id
        for p in session.execute(
            select(Player.id, Player.team_id).where(Player.season == season)
        ).all()
    }

    # Group all season events by contest (each list is seq-ordered; the per-match helpers re-sort
    # within each set anyway).
    events_by_contest: dict[str, list] = defaultdict(list)
    for e in session.scalars(
        select(PbpEvent).where(PbpEvent.season == season)
        .order_by(PbpEvent.contest_id, PbpEvent.seq)
    ):
        events_by_contest[e.contest_id].append(e)

    out: dict[int, dict] = defaultdict(_empty_team)
    for contest_id, events in events_by_contest.items():
        sides = contests.get(contest_id)
        if not sides or not events:
            continue
        away_id, home_id = sides

        # Per-rotation totals for both teams (keyed by team name; team_id is inside each entry).
        rot = per_rotation_stats(events, away_id, home_id, team_names)
        for team in rot.values():
            tid = team["team_id"]
            if tid is None:
                continue
            bucket = out[tid]
            bucket["matches_with_pbp"] += 1
            for r in team["totals"]:
                cell = bucket["rotations"][r["rotation"]]
                for f in _ROT_FIELDS:
                    cell[f] += int(r.get(f, 0) or 0)

        # Phase splits (FBSO vs transition) attributed to the hitter's team.
        for hitter_id, _setter, phase, is_kill, is_error in iter_attack_touches(events):
            tid = player_team.get(hitter_id)
            if tid is None:
                continue
            cell = out[tid]["phase"][phase]
            cell[2] += 1
            if is_kill:
                cell[0] += 1
            elif is_error:
                cell[1] += 1

    return dict(out)


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _hit(k: int, e: int, ta: int) -> float | None:
    return round((k - e) / ta, 3) if ta else None


def summarize_rotations(team_pbp: dict, min_recv: int = 10) -> dict | None:
    """Turn a team's raw rotation buckets into a display table + weakest/strongest rotation.

    Each row: ``{rotation, sideout_pct, hold_pct, net, recv_rallies, hit_pct}``. The weakest/
    strongest are chosen by sideout % among rotations with at least ``min_recv`` receive rallies
    (side-out is the clearest lever to "attack rotation N"). Returns None if there's no usable PBP.
    """
    if not team_pbp or team_pbp.get("matches_with_pbp", 0) == 0:
        return None
    table = []
    for r in range(1, 7):
        c = team_pbp["rotations"][r]
        table.append({
            "rotation": r,
            "sideout_pct": _pct(c["recv_won"], c["recv_rallies"]),
            "hold_pct": _pct(c["serve_won"], c["serve_rallies"]),
            "net": c["points_won"] - c["points_lost"],
            "recv_rallies": c["recv_rallies"],
            "hit_pct": _hit(c["kills"], c["attack_errors"], c["attack_attempts"]),
        })
    eligible = [row for row in table if (row["recv_rallies"] or 0) >= min_recv
                and row["sideout_pct"] is not None]
    weakest = min(eligible, key=lambda x: x["sideout_pct"]) if eligible else None
    strongest = max(eligible, key=lambda x: x["sideout_pct"]) if eligible else None
    return {"table": table, "weakest": weakest, "strongest": strongest}


def summarize_phase(team_pbp: dict) -> dict | None:
    """FBSO vs transition hitting line + the in/out-of-system gap. None if no PBP."""
    if not team_pbp or team_pbp.get("matches_with_pbp", 0) == 0:
        return None
    f = team_pbp["phase"]["fbso"]
    t = team_pbp["phase"]["transition"]
    fbso = _hit(*f)
    trans = _hit(*t)
    if fbso is None and trans is None:
        return None
    gap = round(fbso - trans, 3) if (fbso is not None and trans is not None) else None
    return {
        "fbso_hit_pct": fbso, "fbso_attacks": f[2],
        "trans_hit_pct": trans, "trans_attacks": t[2],
        "in_out_gap": gap,
    }
