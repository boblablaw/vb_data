"""Unit tests for linescore parsing (pure — no DB, no network).

The individual_stats page carries a small linescore table whose last column ("S") is sets won
and whose 1..5 columns are per-set points; the visiting team is the first data row, the home
team the second. See ``_parse_linescore`` / ``contest_meta`` in ``vb.scrape.game_stats``.
"""
from __future__ import annotations

from vb.scrape.game_stats import _parse_linescore, contest_meta

# Real linescore markup (trimmed) from /contests/6602467/individual_stats:
# away = New Mexico St. (2 sets), home = UTEP (3 sets).
LINESCORE_HTML = """<html><body>
<a href="/teams/625347">New Mexico St. Aggies</a>
<a href="/teams/624930">UTEP Miners</a>
<table style="border-collapse: collapse"><tbody>
  <tr><td></td><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>S</td></tr>
  <tr><td>New Mexico St.</td><td>23</td><td>25</td><td>26</td><td>22</td><td>13</td><td>2</td></tr>
  <tr><td>UTEP</td><td>25</td><td>13</td><td>24</td><td>25</td><td>15</td><td>3</td></tr>
</tbody></table>
</body></html>"""

# A sweep in 3 (unplayed sets 4/5 are blank) — trailing blanks must be trimmed.
SWEEP_HTML = """<html><body>
<table><tbody>
  <tr><td></td><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>S</td></tr>
  <tr><td>Team A</td><td>25</td><td>25</td><td>25</td><td></td><td></td><td>3</td></tr>
  <tr><td>Team B</td><td>20</td><td>18</td><td>22</td><td></td><td></td><td>0</td></tr>
</tbody></table>
</body></html>"""

# A page with only a per-player stat table (no linescore) -> no result.
NO_LINESCORE_HTML = """<html><body>
<table><tbody>
  <tr><td>Name</td><td>Kills</td></tr>
  <tr><td>Some Player</td><td>10</td></tr>
</tbody></table>
</body></html>"""


def test_parse_linescore_away_first_home_second():
    ls = _parse_linescore(LINESCORE_HTML)
    assert ls == {
        "away_sets_won": 2,
        "home_sets_won": 3,
        "away_points": [23, 25, 26, 22, 13],
        "home_points": [25, 13, 24, 25, 15],
    }


def test_parse_linescore_trims_unplayed_sets():
    ls = _parse_linescore(SWEEP_HTML)
    assert ls["away_sets_won"] == 3 and ls["home_sets_won"] == 0
    assert ls["away_points"] == [25, 25, 25]   # blank sets 4 & 5 dropped
    assert ls["home_points"] == [20, 18, 22]


def test_parse_linescore_absent_returns_none():
    assert _parse_linescore(NO_LINESCORE_HTML) is None


def test_contest_meta_carries_result_and_set_scores():
    meta = contest_meta(LINESCORE_HTML)
    assert meta["AwayTeamNcaaId"] == "625347"      # first team link -> away
    assert meta["HomeTeamNcaaId"] == "624930"      # second -> home
    assert meta["AwaySetsWon"] == 2 and meta["HomeSetsWon"] == 3
    assert meta["SetScores"] == {
        "away": [23, 25, 26, 22, 13], "home": [25, 13, 24, 25, 15],
    }


def test_contest_meta_no_result_is_none():
    meta = contest_meta(NO_LINESCORE_HTML)
    assert meta["AwaySetsWon"] is None and meta["HomeSetsWon"] is None
    assert meta["SetScores"] is None
