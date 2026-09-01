"""Unit tests for backfill_short_names (pure file I/O — no DB).

Verifies short_name is set from the NCAA team-list display name, matched by ncaa_team_ids[<year>];
that entries without a matching id are left untouched; and that the min_match guard refuses a
partial harvest.
"""
from __future__ import annotations

import json

import pytest

from vb.scrape.backfill import backfill_short_names

YEAR = 2026
Y = str(YEAR)


def _teams_json(tmp_path, entries):
    p = tmp_path / "teams.json"
    p.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _team_list_csv(tmp_path, rows):
    p = tmp_path / "team_list.csv"
    lines = ["team_id,team_name,div,yr"]
    lines += [f"{tid},{name},1,{YEAR}" for tid, name in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_sets_short_name_from_ncaa_and_reports_changes(tmp_path):
    teams = [
        {"team": "California State University, Fresno", "short_name": "California State University, Fresno",
         "ncaa_team_ids": {Y: "111"}},
        {"team": "Boise State University", "short_name": "Boise State University",
         "ncaa_team_ids": {Y: "222"}},
    ]
    tj = _teams_json(tmp_path, teams)
    tl = _team_list_csv(tmp_path, [("111", "Fresno St."), ("222", "Boise St.")])

    res = backfill_short_names(
        YEAR, team_list_path=tl, teams_json_path=tj, min_match=2, write=True
    )
    assert res["matched"] == 2
    assert {c["new"] for c in res["changed"]} == {"Fresno St.", "Boise St."}

    saved = json.loads(tj.read_text(encoding="utf-8"))
    assert [t["short_name"] for t in saved] == ["Fresno St.", "Boise St."]


def test_leaves_unmatched_entries_untouched(tmp_path):
    teams = [
        {"team": "Fresno", "short_name": "Fresno", "ncaa_team_ids": {Y: "111"}},
        {"team": "No Season Id", "short_name": "Keep Me", "ncaa_team_ids": {}},
    ]
    tj = _teams_json(tmp_path, teams)
    tl = _team_list_csv(tmp_path, [("111", "Fresno St.")])

    res = backfill_short_names(
        YEAR, team_list_path=tl, teams_json_path=tj, min_match=1, write=True
    )
    assert res["matched"] == 1
    assert res["unmatched"] == ["No Season Id"]
    saved = json.loads(tj.read_text(encoding="utf-8"))
    assert saved[1]["short_name"] == "Keep Me"  # untouched


def test_min_match_guard_refuses_partial_harvest(tmp_path):
    teams = [{"team": "Fresno", "short_name": "Fresno", "ncaa_team_ids": {Y: "111"}}]
    tj = _teams_json(tmp_path, teams)
    tl = _team_list_csv(tmp_path, [("111", "Fresno St.")])
    with pytest.raises(SystemExit):
        backfill_short_names(YEAR, team_list_path=tl, teams_json_path=tj, min_match=340, write=True)


def test_missing_csv_raises(tmp_path):
    tj = _teams_json(tmp_path, [{"team": "X", "short_name": "X", "ncaa_team_ids": {Y: "1"}}])
    with pytest.raises(FileNotFoundError):
        backfill_short_names(YEAR, team_list_path=tmp_path / "nope.csv", teams_json_path=tj)
