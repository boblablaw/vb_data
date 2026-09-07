"""Derive per-player advanced stats from play-by-play events -> player_pbp_stats.

Three things full touch data makes possible that the box score can't:

* **set_attempts** — every ``set`` touch (not just the assists that led to a kill).
* **serve_attempts** — every ``serve`` touch (total serves taken).
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


def setter_hitting_by_player(events: list[PbpEvent]) -> dict[int, tuple[int, int, int]]:
    """Per-player setter-hitting tallies from one game's events: ``pid -> (kills, errors, attacks)``.

    Links each ``set`` touch to the next same-team ``attack`` within the rally; the outcome is
    read from the rally's terminal (kill / attack_error) only when that attack is the terminal
    swing, else it counts as an in-play attempt. Shared by the season derive and the per-game box
    score so the two never diverge.
    """
    by_set: dict[int, list[PbpEvent]] = defaultdict(list)
    for e in events:
        by_set[e.set_number].append(e)

    kills: dict[int, int] = defaultdict(int)
    errors: dict[int, int] = defaultdict(int)
    attacks_off: dict[int, int] = defaultdict(int)
    for set_events in by_set.values():
        set_events.sort(key=lambda e: e.seq)
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
                attacks_off[e.player_id] += 1
                # Is this the terminal swing? (last same-team attack + attack-type outcome)
                last_team_atk = next((a for a in reversed(attacks) if a.team_id == e.team_id), None)
                if terminal is not None and atk is last_team_atk:
                    if terminal.terminal_type == "kill" and terminal.scoring_team_id == e.team_id:
                        kills[e.player_id] += 1
                    elif terminal.terminal_type == "attack_error" and terminal.team_id == e.team_id:
                        errors[e.player_id] += 1
    return {pid: (kills.get(pid, 0), errors.get(pid, 0), attacks_off[pid]) for pid in attacks_off}


def iter_attack_touches(events: list[PbpEvent]):
    """Yield ``(hitter_id, setter_id, phase, is_kill, is_error)`` for every ``attack`` touch.

    ``phase`` is ``"fbso"`` for the receiving team's FIRST attack of a rally (the team that did NOT
    take the rally's ``serve`` touch) — first-ball side-out — and ``"transition"`` for every other
    attack. ``setter_id`` is the most recent same-team ``set`` touch before the attack in the rally
    (``None`` if none). Outcome mirrors :func:`setter_hitting_by_player`: a kill/error is credited
    only to the last same-team attack of the rally when the rally terminal matches, while every
    ``attack`` touch counts as an attempt. Attacks with no ``player_id`` are skipped.

    This is the single source of truth for the FBSO/transition + setter attribution rules; both the
    season derive and the live ``attack-splits`` endpoint aggregate over it.
    """
    by_set: dict[int, list[PbpEvent]] = defaultdict(list)
    for e in events:
        by_set[e.set_number].append(e)
    for set_events in by_set.values():
        set_events.sort(key=lambda e: e.seq)
        rallies: dict[int, list[PbpEvent]] = defaultdict(list)
        for e in set_events:
            rallies[e.rally_number].append(e)
        for revs in rallies.values():
            attacks = [e for e in revs if e.touch_type == "attack"]
            if not attacks:
                continue
            terminal = next((e for e in revs if e.is_terminal), None)
            serve = next((e for e in revs if e.touch_type == "serve"), None)
            serving_team = serve.team_id if serve is not None else None
            # The FBSO swing is the receiving team's first attack; unknown server -> all transition.
            fbso_atk = (next((a for a in attacks if a.team_id != serving_team), None)
                        if serving_team is not None else None)
            last_by_team: dict[int, PbpEvent] = {}
            for a in attacks:  # revs are seq-sorted, so this ends on each team's last attack
                last_by_team[a.team_id] = a
            for i, e in enumerate(revs):
                if e.touch_type != "attack" or e.player_id is None:
                    continue
                setter_id = next(
                    (r.player_id for r in reversed(revs[:i])
                     if r.touch_type == "set" and r.team_id == e.team_id and r.player_id is not None),
                    None,
                )
                phase = "fbso" if e is fbso_atk else "transition"
                is_last = last_by_team.get(e.team_id) is e
                is_kill = bool(terminal is not None and is_last
                               and terminal.terminal_type == "kill"
                               and terminal.scoring_team_id == e.team_id)
                is_error = bool(terminal is not None and is_last
                                and terminal.terminal_type == "attack_error"
                                and terminal.team_id == e.team_id)
                yield e.player_id, setter_id, phase, is_kill, is_error


def attack_splits_by_player(events: list[PbpEvent]) -> dict[int, dict[str, tuple[int, int, int]]]:
    """Per-player phase splits from one game: ``pid -> {"fbso": (k,e,ta), "transition": (k,e,ta)}``."""
    res: dict[int, dict[str, list[int]]] = defaultdict(
        lambda: {"fbso": [0, 0, 0], "transition": [0, 0, 0]}
    )
    for hitter, _setter, phase, is_kill, is_error in iter_attack_touches(events):
        cell = res[hitter][phase]
        cell[2] += 1
        if is_kill:
            cell[0] += 1
        elif is_error:
            cell[1] += 1
    return {pid: {ph: tuple(v) for ph, v in d.items()} for pid, d in res.items()}


def attack_lines(
    events: list[PbpEvent], *, setter_id: int | None = None, phase: str | None = None,
) -> dict[int, tuple[int, int, int]]:
    """Per-hitter ``(kills, errors, attacks)`` restricted to an optional setter and/or phase.

    ``setter_id`` keeps only attacks set by that player; ``phase`` (``"fbso"``/``"transition"``)
    keeps only that phase. Both ``None`` returns every hitter's full attacking line.
    """
    res: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
    for hitter, s, ph, is_kill, is_error in iter_attack_touches(events):
        if setter_id is not None and s != setter_id:
            continue
        if phase is not None and ph != phase:
            continue
        cell = res[hitter]
        cell[2] += 1
        if is_kill:
            cell[0] += 1
        elif is_error:
            cell[1] += 1
    return {pid: tuple(v) for pid, v in res.items()}


def _process_contest(events: list[PbpEvent], acc: dict) -> None:
    """Fold one contest's ordered events into the season accumulators in ``acc``."""
    # --- setter hitting: link each set -> next same-team attack within the rally ---
    for pid, (sk, se, satk) in setter_hitting_by_player(events).items():
        acc["sh_kills"][pid] += sk
        acc["sh_errors"][pid] += se
        acc["sh_attacks"][pid] += satk

    # --- FBSO / transition attack splits (per hitter) ---
    for pid, split in attack_splits_by_player(events).items():
        fk, fe, fta = split["fbso"]
        tk, te, tta = split["transition"]
        acc["fbso_kills"][pid] += fk
        acc["fbso_errors"][pid] += fe
        acc["fbso_attacks"][pid] += fta
        acc["trans_kills"][pid] += tk
        acc["trans_errors"][pid] += te
        acc["trans_attacks"][pid] += tta

    # Group by set, then by rally, preserving seq order.
    by_set: dict[int, list[PbpEvent]] = defaultdict(list)
    for e in events:
        by_set[e.set_number].append(e)

    for set_events in by_set.values():
        set_events.sort(key=lambda e: e.seq)

        # --- set_attempts / serve_attempts (every set / serve touch) ---
        for e in set_events:
            if e.player_id is None:
                continue
            if e.touch_type == "set":
                acc["set_attempts"][e.player_id] += 1
            elif e.touch_type == "serve":
                acc["serve_attempts"][e.player_id] += 1

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


def aggregate_pbp(
    session: Session,
    contest_ids: list[str],
    *,
    assists_by_pid: dict[int, float] | None = None,
) -> dict[int, dict]:
    """Replay ``contest_ids`` and return per-player pbp aggregates (no DB write).

    Shared by the batch season derive (:func:`derive_pbp`) and the live week/game endpoints, so the
    FBSO/transition, setter-hitting and set/serve/points-played math stays single-sourced. Keys of
    each per-player dict match ``PlayerStatLine``/``PlayerPbpStat`` field names.

    ``assist_pct`` needs a box-score assists denominator (``assists_by_pid``); the season derive
    passes season assists, the week branch passes the week's summed assists. Omit it to leave
    ``assist_pct`` null (the raw pbp counts don't carry assists).
    """
    acc = {
        "set_attempts": defaultdict(int),
        "serve_attempts": defaultdict(int),
        "sh_kills": defaultdict(int),
        "sh_errors": defaultdict(int),
        "sh_attacks": defaultdict(int),
        "points_played": defaultdict(int),
        "fbso_kills": defaultdict(int),
        "fbso_errors": defaultdict(int),
        "fbso_attacks": defaultdict(int),
        "trans_kills": defaultdict(int),
        "trans_errors": defaultdict(int),
        "trans_attacks": defaultdict(int),
    }

    for cid in contest_ids:
        events = list(session.scalars(
            select(PbpEvent).where(PbpEvent.contest_id == cid).order_by(PbpEvent.seq)
        ).all())
        _process_contest(events, acc)

    assists = assists_by_pid or {}
    players = (set(acc["set_attempts"]) | set(acc["serve_attempts"])
               | set(acc["points_played"]) | set(acc["sh_attacks"])
               | set(acc["fbso_attacks"]) | set(acc["trans_attacks"]))
    out: dict[int, dict] = {}
    for pid in players:
        sa = acc["set_attempts"].get(pid, 0)
        sk = acc["sh_kills"].get(pid, 0)
        se = acc["sh_errors"].get(pid, 0)
        satk = acc["sh_attacks"].get(pid, 0)
        a = assists.get(pid)
        out[pid] = {
            "set_attempts": sa,
            "serve_attempts": acc["serve_attempts"].get(pid, 0),
            "assist_pct": (float(a) / sa) if (a is not None and sa > 0) else None,
            "setter_hit_kills": sk,
            "setter_hit_errors": se,
            "setter_hit_attacks": satk,
            "setter_hitting_pct": ((sk - se) / satk) if satk > 0 else None,
            "points_played": acc["points_played"].get(pid, 0),
            "fbso_kills": acc["fbso_kills"].get(pid, 0),
            "fbso_errors": acc["fbso_errors"].get(pid, 0),
            "fbso_attacks": acc["fbso_attacks"].get(pid, 0),
            "trans_kills": acc["trans_kills"].get(pid, 0),
            "trans_errors": acc["trans_errors"].get(pid, 0),
            "trans_attacks": acc["trans_attacks"].get(pid, 0),
        }
    return out


def derive_pbp(session: Session, season: int) -> dict:
    """Compute player_pbp_stats for a season from pbp_events. Returns a small summary."""
    contest_ids = [c for (c,) in session.execute(
        select(PbpEvent.contest_id).where(PbpEvent.season == season).distinct()
    ).all()]

    # Season assists (box score) for assist_pct.
    assists = {
        pid: a for pid, a in session.execute(
            select(PlayerSeasonStat.player_id, PlayerSeasonStat.assists)
            .where(PlayerSeasonStat.season == season)
        ).all()
    }

    agg = aggregate_pbp(session, contest_ids, assists_by_pid=assists)
    written = 0
    for pid, v in agg.items():
        row = session.get(PlayerPbpStat, (pid, season))
        if row is None:
            row = PlayerPbpStat(player_id=pid, season=season)
            session.add(row)
        row.set_attempts = v["set_attempts"]
        row.serve_attempts = v["serve_attempts"]
        row.assist_pct = v["assist_pct"]
        row.setter_hit_kills = v["setter_hit_kills"]
        row.setter_hit_errors = v["setter_hit_errors"]
        row.setter_hit_attacks = v["setter_hit_attacks"]
        row.setter_hitting_pct = v["setter_hitting_pct"]
        row.points_played = v["points_played"]
        row.fbso_kills = v["fbso_kills"]
        row.fbso_errors = v["fbso_errors"]
        row.fbso_attacks = v["fbso_attacks"]
        row.trans_kills = v["trans_kills"]
        row.trans_errors = v["trans_errors"]
        row.trans_attacks = v["trans_attacks"]
        written += 1

    session.flush()
    log.info("derive_pbp: %d contests, %d player rows (season %d)",
             len(contest_ids), written, season)
    return {"contests": len(contest_ids), "players": written}
