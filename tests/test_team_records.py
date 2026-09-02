"""Unit tests for compute_team_records (pure — no DB).

Covers W/L, sets & set%, conference vs non-conference split, signed win streak (from the most
recent game), Opponent Record excluding head-to-head, and mean Opponent RPI.
"""
from __future__ import annotations

import pytest

from vb.api.routers.stats import compute_team_records

# Three teams: 1 & 2 in conference A, 3 in conference B. RPI: team2=5, team3=10, team1=none.
TEAMS = {
    1: {"name": "Team One", "team_short": "T1", "conference": "A",
        "conference_id": 100, "rpi_rank": None, "rpi_record": None, "avca_rank": None},
    2: {"name": "Team Two", "team_short": "T2", "conference": "A",
        "conference_id": 100, "rpi_rank": 5, "rpi_record": "1-0", "avca_rank": 3},
    3: {"name": "Team Three", "team_short": "T3", "conference": "B",
        "conference_id": 200, "rpi_rank": 10, "rpi_record": "1-1", "avca_rank": None},
}

# g1 (d1): team1 beats team2 3-1 (conf A game)
# g2 (d2): team3 beats team1 3-2 (team1 away, non-conf)
# g3 (d3): team1 beats team3 3-2 (non-conf)
CONTESTS = [
    {"date": "2026-09-01 18:00", "home_team_id": 1, "away_team_id": 2,
     "home_sets_won": 3, "away_sets_won": 1},
    {"date": "2026-09-05 18:00", "home_team_id": 3, "away_team_id": 1,
     "home_sets_won": 3, "away_sets_won": 2},
    {"date": "2026-09-10 18:00", "home_team_id": 1, "away_team_id": 3,
     "home_sets_won": 3, "away_sets_won": 2},
    # An undecided/unplayed contest must be ignored entirely.
    {"date": "2026-09-12 18:00", "home_team_id": 1, "away_team_id": 2,
     "home_sets_won": None, "away_sets_won": None},
]


def _by_id(rows):
    return {r["team_id"]: r for r in rows}


def test_wins_losses_sets_and_pct():
    r = _by_id(compute_team_records(CONTESTS, TEAMS))[1]
    assert (r["games"], r["wins"], r["losses"]) == (3, 2, 1)
    assert (r["sets_won"], r["sets_lost"]) == (8, 6)         # 3+2+3 won, 1+3+2 lost
    assert r["set_pct"] == pytest.approx(8 / 14, abs=1e-3)


def test_conference_split():
    r = _by_id(compute_team_records(CONTESTS, TEAMS))[1]
    assert (r["conf_wins"], r["conf_losses"]) == (1, 0)      # only g1 vs team2 (conf A)
    assert (r["nonconf_wins"], r["nonconf_losses"]) == (1, 1)  # g2 loss, g3 win vs team3 (conf B)


def test_win_streak_is_signed_from_most_recent():
    rows = _by_id(compute_team_records(CONTESTS, TEAMS))
    assert rows[1]["win_streak"] == 1     # team1: ... loss, then win (most recent) -> W1
    assert rows[3]["win_streak"] == -1    # team3: win then loss (most recent) -> L1


def test_opponent_record_excludes_head_to_head():
    # team1's opponents: team2 (0-1 overall) once, team3 (1-1 overall) twice.
    # Removing each head-to-head meeting -> combined opponents' record 1-1.
    r = _by_id(compute_team_records(CONTESTS, TEAMS))[1]
    assert (r["opp_wins"], r["opp_losses"]) == (1, 1)


def test_opponent_rpi_is_mean_of_faced_ranks():
    # team1 faced team2 (rpi 5) once and team3 (rpi 10) twice -> mean 8.3.
    r = _by_id(compute_team_records(CONTESTS, TEAMS))[1]
    assert r["opp_rpi"] == pytest.approx(8.3, abs=0.05)
