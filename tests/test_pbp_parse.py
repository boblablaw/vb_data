"""Parser tests for the play-by-play scraper — pure, no DB.

Runs against a saved copy of contest 6595050's PBP page (LMU @ Hawaii, a 3-2 Hawaii win). The spike
that motivated this feature reconstructed the exact set scores from this page, so these assertions
lock that in: set finals, terminal counts by scoring side, touch-type breakdown, sub capture, and
venue/attendance parsing.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from vb.scrape.pbp import parse_pbp, parse_venue_attendance

FIXTURE = Path(__file__).parent / "fixtures" / "pbp_6595050.html"


@pytest.fixture(scope="module")
def parsed() -> tuple[list[dict], dict]:
    html = FIXTURE.read_text()
    return parse_pbp(html, "6595050", 2026)


def test_five_sets(parsed):
    events, _ = parsed
    assert sorted({e["Set"] for e in events}) == [1, 2, 3, 4, 5]


def test_set_finals_reconstructed(parsed):
    """Last terminal of each set carries the running score = the set's final (away=LMU, home=Hawaii)."""
    events, _ = parsed
    finals = {}
    for e in events:
        if e["IsTerminal"]:
            finals[e["Set"]] = (e["AwayScore"], e["HomeScore"])
    assert finals == {
        1: (10, 25),
        2: (18, 25),
        3: (25, 22),
        4: (28, 26),
        5: (11, 15),
    }


def test_terminals_reconcile_to_match_totals(parsed):
    """205 scored points total; the scoring-side split matches the box (LMU 92 / Hawaii 113)."""
    events, _ = parsed
    terms = [e for e in events if e["IsTerminal"]]
    assert len(terms) == 205
    by_side = Counter(e["ScoringSide"] for e in terms)
    assert by_side["away"] == 92
    assert by_side["home"] == 113


def test_terminal_types(parsed):
    events, _ = parsed
    types = Counter(e["TerminalType"] for e in events if e["IsTerminal"])
    assert types["kill"] == 114
    assert types["ace"] == 17
    assert types["block"] == 18
    assert types["attack_error"] == 29
    assert types["service_error"] == 25
    assert types["set_error"] == 2


def test_touch_types(parsed):
    events, _ = parsed
    touches = Counter(e["TouchType"] for e in events)
    assert touches["serve"] == 205
    assert touches["attack"] == 341
    assert touches["set"] == 331
    assert touches["reception"] == 163
    assert touches["dig"] == 146
    assert touches["block"] == 29


def test_subs_captured_with_side(parsed):
    """Sub rows are their own events (sub_in / sub_out), each tagged with the player's side."""
    events, _ = parsed
    subs = [e for e in events if e["TouchType"].startswith("sub_")]
    assert len(subs) == 368
    assert all(e["Side"] in ("away", "home") for e in subs)
    assert all(e["PlayerName"] for e in subs)


def test_markers_and_timeouts_skipped(parsed):
    """"Set started" / timeouts / challenges never become events (only real touches/subs do)."""
    events, _ = parsed
    valid = {
        "serve", "reception", "set", "attack", "dig", "block",
        "terminal", "sub_in", "sub_out",
    }
    assert {e["TouchType"] for e in events} <= valid


def test_error_charged_to_erring_side(parsed):
    """An error terminal is charged to the NON-scoring side (the team that erred)."""
    events, _ = parsed
    for e in events:
        if e["IsTerminal"] and (e["TerminalType"] or "").endswith("_error"):
            assert e["Side"] != e["ScoringSide"]


def test_venue_and_attendance(parsed):
    _, meta = parsed
    assert meta["Location"] == "Bankoh Arena at Stan Sheriff Center (Honolulu, HI)"
    assert meta["Attendance"] == 5227
    assert meta["AwayNcaaId"] == "624889"
    assert meta["HomeNcaaId"] == "624883"


def test_parse_venue_attendance_standalone():
    loc, att = parse_venue_attendance(FIXTURE.read_text())
    assert loc == "Bankoh Arena at Stan Sheriff Center (Honolulu, HI)"
    assert att == 5227


def test_no_set_tables_yields_empty():
    events, meta = parse_pbp("<html><body><p>no tables</p></body></html>", "x", 2026)
    assert events == []
    assert meta["Location"] is None
    assert meta["Attendance"] is None
