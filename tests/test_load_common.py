"""Unit tests for load/common helpers (pure — no DB).

Regression guard for the NCAA-id dtype bug: when ``HomeTeamNcaaId`` has any blank cell,
pandas would infer a float column and read ``624845`` as ``624845.0``. ``clean_str`` then
yields ``"624845.0"``, which never matches the ``"624845"`` key in ``ncaa_id_to_team`` — so
``contests.home_team_id`` silently stayed NULL for ~90% of contests. Forcing the id columns
to ``str`` in ``read_csv`` keeps them as plain ``"624845"``.
"""
from __future__ import annotations

from vb.load.common import clean_str, read_csv


def test_ncaa_id_columns_stay_string_even_with_blanks(tmp_path):
    p = tmp_path / "game_stats.csv"
    # HomeTeamNcaaId has a blank on the second row -> would trigger float inference.
    p.write_text(
        "ContestID,AwayTeamNcaaId,HomeTeamNcaaId\n"
        "6597539,625130,624845\n"
        "6612857,624635,\n",
        encoding="utf-8",
    )
    df = read_csv(p)

    # Present values are exact id strings, not float-suffixed ("624845", not "624845.0").
    assert clean_str(df.iloc[0]["HomeTeamNcaaId"]) == "624845"
    assert clean_str(df.iloc[0]["AwayTeamNcaaId"]) == "625130"
    # A blank id resolves to None, not the string "nan".
    assert clean_str(df.iloc[1]["HomeTeamNcaaId"]) is None
