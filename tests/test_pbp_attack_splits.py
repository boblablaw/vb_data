"""Unit tests for the FBSO/transition + setter attack classifier (vb.derive.pbp).

DB-free: builds transient PbpEvent objects for a few hand-computed rallies and asserts the phase
split, setter attribution, and kill/error outcomes. This is the single source of truth shared by the
season derive and the live ``/teams/{id}/attack-splits`` endpoint.
"""
from __future__ import annotations

from vb.derive.pbp import attack_lines, attack_splits_by_player
from vb.models import PbpEvent

# Team ids and player ids used across the synthetic rallies.
A, B = 1, 2           # teams
SRV_A, SRV_B = 10, 20  # servers
REC_A, REC_B = 30, 21  # (also used as passers)
SET_A, SET_B = 31, 21  # setters (B's setter is 21)
HIT_A, HIT_B = 32, 22  # hitters

_seq = 0


def _ev(set_no, rally, touch, player, team, *, terminal=False, tt=None, scoring=None):
    global _seq
    _seq += 1
    return PbpEvent(
        set_number=set_no, rally_number=rally, seq=_seq, touch_type=touch,
        player_id=player, team_id=team, is_terminal=terminal, terminal_type=tt,
        scoring_team_id=scoring,
    )


def _events():
    """Three rallies in set 1:

    R1: A serves, B sides out on the first ball (B hitter 22, set by 21) -> FBSO kill.
    R2: B serves, A attacks first ball (dug, in-play), then B counters -> B transition kill (set 21).
    R3: A serves, B swings first ball and errors (set 21) -> FBSO attack error.
    """
    global _seq
    _seq = 0
    return [
        # --- R1: FBSO kill for B ---
        _ev(1, 1, "serve", SRV_A, A),
        _ev(1, 1, "reception", REC_B, B),
        _ev(1, 1, "set", SET_B, B),
        _ev(1, 1, "attack", HIT_B, B, terminal=True, tt="kill", scoring=B),
        # --- R2: A first-ball attack in play, then B transition kill ---
        _ev(1, 2, "serve", SRV_B, B),
        _ev(1, 2, "reception", REC_A, A),
        _ev(1, 2, "set", SET_A, A),
        _ev(1, 2, "attack", HIT_A, A),                      # A's FBSO, kept in play
        _ev(1, 2, "dig", 23, B),
        _ev(1, 2, "set", SET_B, B),
        _ev(1, 2, "attack", HIT_B, B, terminal=True, tt="kill", scoring=B),  # B transition kill
        # --- R3: B FBSO attack error ---
        _ev(1, 3, "serve", SRV_A, A),
        _ev(1, 3, "reception", REC_B, B),
        _ev(1, 3, "set", SET_B, B),
        _ev(1, 3, "attack", HIT_B, B, terminal=True, tt="attack_error", scoring=A),
    ]


def test_attack_splits_phase_and_outcomes():
    splits = attack_splits_by_player(_events())
    # Hitter B: FBSO = R1 kill + R3 error over 2 swings; transition = R2 kill over 1 swing.
    assert splits[HIT_B]["fbso"] == (1, 1, 2)
    assert splits[HIT_B]["transition"] == (1, 0, 1)
    # Hitter A: only the R2 first-ball swing, kept in play (no kill/error).
    assert splits[HIT_A]["fbso"] == (0, 0, 1)
    assert splits[HIT_A]["transition"] == (0, 0, 0)


def test_attack_lines_setter_and_phase_filters():
    evs = _events()
    # All of B's swings were set by 21; A's lone swing was set by 31.
    off_21 = attack_lines(evs, setter_id=SET_B)
    assert off_21[HIT_B] == (2, 1, 3)         # 2 kills, 1 error, 3 attempts off setter 21
    assert HIT_A not in off_21                 # 32 was set by 31, not 21
    # Phase-restricted off setter 21.
    assert attack_lines(evs, setter_id=SET_B, phase="fbso")[HIT_B] == (1, 1, 2)
    assert attack_lines(evs, setter_id=SET_B, phase="transition")[HIT_B] == (1, 0, 1)
    # Setter 31 only set A's in-play first-ball swing.
    assert attack_lines(evs, setter_id=SET_A) == {HIT_A: (0, 0, 1)}


def test_attack_lines_unfiltered_totals_equal_splits():
    evs = _events()
    lines = attack_lines(evs)
    splits = attack_splits_by_player(evs)
    for pid, (k, e, ta) in lines.items():
        fk, fe, fta = splits[pid]["fbso"]
        tk, te, tta = splits[pid]["transition"]
        assert (k, e, ta) == (fk + tk, fe + te, fta + tta)
