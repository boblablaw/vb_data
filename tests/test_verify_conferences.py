"""Unit tests for the pure conference-diff logic (no network).

Exercises the membership-mode inference: a team is flagged when NCAA groups it with a set of
conference-mates that teams.json mostly labels differently — the realignment fingerprint — while
tolerating that NCAA and teams.json spell the same conference differently.
"""
from __future__ import annotations

from vb.verify import diff_conferences

YEAR = 2026


def _t(name, conf, tid):
    return {"team": name, "conference": conf, "ncaa_team_ids": {str(YEAR): tid}}


def test_flags_realignment_minority():
    # NCAA puts A, B, C in "Pac-12 Conference". teams.json has A/B correct but C still "Mountain
    # West" -> C is the minority and must be flagged, with the inferred expected conference.
    json_teams = [
        _t("A", "Pac-12 Conference", "1"),
        _t("B", "Pac-12 Conference", "2"),
        _t("C", "Mountain West Conference", "3"),
    ]
    ncaa = {
        "1": ("Pac-12 Conference", "A"),
        "2": ("Pac-12 Conference", "B"),
        "3": ("Pac-12 Conference", "C"),
    }
    report = diff_conferences(json_teams, ncaa, YEAR)
    assert report["counts"]["mismatches"] == 1
    m = report["mismatches"][0]
    assert m["team"] == "C"
    assert m["json_conference"] == "Mountain West Conference"
    assert m["expected_conference"] == "Pac-12 Conference"


def test_name_disagreement_is_not_a_mismatch():
    # NCAA calls it "The American"; teams.json calls it "American Conference". All members agree,
    # so the mode maps the names and nothing is flagged.
    json_teams = [_t("X", "American Conference", "10"), _t("Y", "American Conference", "11")]
    ncaa = {"10": ("The American", "X"), "11": ("The American", "Y")}
    report = diff_conferences(json_teams, ncaa, YEAR)
    assert report["counts"]["mismatches"] == 0


def test_missing_in_json_and_in_ncaa():
    json_teams = [
        _t("Kept", "Big Ten Conference", "20"),
        _t("Dropped", "Big Ten Conference", "99"),   # no NCAA row -> missing_in_ncaa
    ]
    ncaa = {
        "20": ("Big Ten Conference", "Kept"),
        "21": ("Big Ten Conference", "NewSchool"),   # no json entry -> missing_in_json
    }
    report = diff_conferences(json_teams, ncaa, YEAR)
    assert [m["team_id"] for m in report["missing_in_json"]] == ["21"]
    assert [m["team_id"] for m in report["missing_in_ncaa"]] == ["99"]
    assert report["counts"]["mismatches"] == 0


def test_ids_scoped_to_year():
    # A team whose id is for a different season is ignored (no id for YEAR).
    json_teams = [{"team": "Old", "conference": "X", "ncaa_team_ids": {"2024": "5"}}]
    ncaa = {"5": ("X", "Old")}
    report = diff_conferences(json_teams, ncaa, YEAR)
    assert report["counts"]["json_teams_with_id"] == 0
    assert [m["team_id"] for m in report["missing_in_json"]] == ["5"]
