"""Derive per-player advanced stats from play-by-play events -> player_pbp_stats.

Three things full touch data makes possible that the box score can't:

* **set_attempts** — every ``set`` touch (not just the assists that led to a kill).
* **assist_pct** — season assists (box score) / set_attempts.
* **setter hitting %** — the hitting pct of the attack made immediately off each of a player's
  sets: within a rally, the ``attack`` touch by the same team that first follows the player's
  ``set`` is credited to that player; its outcome is read from the rally's terminal (kill /
  attack_error) when that attack is the terminal swing, else it's an in-play attempt.
* **points_played** — rallies the player was on court, inferred from the substitution rows
  (approximate for libero/back-row, which the site doesn't always log as subs).

Processed per contest to bound memory. Idempotent: upserts by (player_id, season).
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..log import get_logger
from ..models import PbpEvent, PlayerPbpStat, PlayerSeasonStat

log = get_logger(__name__)


def _process_contest(events: list[PbpEvent], acc: dict) -> None:
    """Fold one contest's ordered events into the season accumulators in ``acc``."""
    # Group by set, then by rally, preserving seq order.
    by_set: dict[int, list[PbpEvent]] = defaultdict(list)
    for e in events:
        by_set[e.set_number].append(e)

    for set_events in by_set.values():
        set_events.sort(key=lambda e: e.seq)

        # --- setter hitting: link each set -> next same-team attack within the rally ---
        rallies: dict[int, list[PbpEvent]] = defaultdict(list)
        for e in set_events:
            rallies[e.rally_number].append(e)
        for revs in rallies.values():
            terminal = next((e for e in revs if e.is_terminal), None)
            attacks = [e for e in revs if e.touch_type == "attack"]
            for i, e in enumerate(revs):
                if e.touch_type != "set" or e.player_id is None:
                    continue
                atk = next((a for a in revs[i + 1:]
                            if a.touch_type == "attack" and a.team_id == e.team_id), None)
                if atk is None:
                    continue
                acc["sh_attacks"][e.player_id] += 1
                # Is this the terminal swing? (last same-team attack + attack-type outcome)
                last_team_atk = next((a for a in reversed(attacks) if a.team_id == e.team_id), None)
                if terminal is not None and atk is last_team_atk:
                    if terminal.terminal_type == "kill" and terminal.scoring_team_id == e.team_id:
                        acc["sh_kills"][e.player_id] += 1
                    elif terminal.terminal_type == "attack_error" and terminal.team_id == e.team_id:
                        acc["sh_errors"][e.player_id] += 1

        # --- set_attempts ---
        for e in set_events:
            if e.touch_type == "set" and e.player_id is not None:
                acc["set_attempts"][e.player_id] += 1

        # --- points_played: walk subs rally-by-rally, credit on-court players at each serve ---
        first_seen: dict[int, str] = {}
        for e in set_events:
            if e.player_id is not None and e.player_id not in first_seen:
                first_seen[e.player_id] = e.touch_type
        on_court = {pid for pid, tt in first_seen.items() if tt != "sub_in"}
        for e in set_events:
            if e.player_id is None:
                if e.touch_type == "serve":
                    for pid in on_court:
                        acc["points_played"][pid] += 1
                continue
            if e.touch_type == "sub_in":
                on_court.add(e.player_id)
            elif e.touch_type == "sub_out":
                on_court.discard(e.player_id)
            elif e.touch_type == "serve":
                for pid in on_court:
                    acc["points_played"][pid] += 1


def derive_pbp(session: Session, season: int) -> dict:
    """Compute player_pbp_stats for a season from pbp_events. Returns a small summary."""
    acc = {
        "set_attempts": defaultdict(int),
        "sh_kills": defaultdict(int),
        "sh_errors": defaultdict(int),
        "sh_attacks": defaultdict(int),
        "points_played": defaultdict(int),
    }

    contest_ids = [c for (c,) in session.execute(
        select(PbpEvent.contest_id).where(PbpEvent.season == season).distinct()
    ).all()]
    for cid in contest_ids:
        events = list(session.scalars(
            select(PbpEvent).where(PbpEvent.contest_id == cid).order_by(PbpEvent.seq)
        ).all())
        _process_contest(events, acc)

    # Season assists (box score) for assist_pct.
    assists = {
        pid: a for pid, a in session.execute(
            select(PlayerSeasonStat.player_id, PlayerSeasonStat.assists)
            .where(PlayerSeasonStat.season == season)
        ).all()
    }

    players = set(acc["set_attempts"]) | set(acc["points_played"]) | set(acc["sh_attacks"])
    written = 0
    for pid in players:
        sa = acc["set_attempts"].get(pid, 0)
        sk = acc["sh_kills"].get(pid, 0)
        se = acc["sh_errors"].get(pid, 0)
        satk = acc["sh_attacks"].get(pid, 0)
        pp = acc["points_played"].get(pid, 0)
        a = assists.get(pid)
        row = session.get(PlayerPbpStat, (pid, season))
        if row is None:
            row = PlayerPbpStat(player_id=pid, season=season)
            session.add(row)
        row.set_attempts = sa
        row.assist_pct = (float(a) / sa) if (a is not None and sa > 0) else None
        row.setter_hit_kills = sk
        row.setter_hit_errors = se
        row.setter_hit_attacks = satk
        row.setter_hitting_pct = ((sk - se) / satk) if satk > 0 else None
        row.points_played = pp
        written += 1

    session.flush()
    log.info("derive_pbp: %d contests, %d player rows (season %d)",
             len(contest_ids), written, season)
    return {"contests": len(contest_ids), "players": written}
