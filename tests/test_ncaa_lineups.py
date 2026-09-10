"""Authoritative-starters slice: PBP parsing, name reconciliation, and the per_set_lineups override.

All pure (no DB, no network): the ncaa.com PBP fixture is fed through a monkeypatched ``_get``, the
reconciliation helpers are exercised directly, and ``per_set_lineups`` is driven with lightweight
namespace fakes so we can prove authoritative ids win over the heuristic reconstruction.
"""
from __future__ import annotations

from types import SimpleNamespace

import vb.scrape.ncaa_api as api
from vb.load.ncaa_api_lineups import (
    _assign_group,
    _name_key,
    _norm_name,
    _roster_index,
)
from vb.query.tools import per_set_lineups

# --- play-by-play parsing -------------------------------------------------------------------------

_PBP = {
    "teams": [
        {"teamId": "46419", "seoname": "hawaii"},
        {"teamId": "46494", "seoname": "san-jose-st"},
    ],
    "periods": [
        {"periodNumber": 1, "playbyplayStats": [
            {"teamId": "46419", "plays": [
                # ncaa.com's real format: semicolon-delimited, trailing period on the last name.
                {"playText": "Hawaii starters: Bri Gunderson; Kate O'Neil; Talita dos Santos; "
                             "Ana Ruiz; Mia Lee; Jo Park."},
                {"playText": "Kill by Bri Gunderson"},
            ]},
            {"teamId": "46494", "plays": [
                # a comma-delimited variant must still parse (older/other feeds).
                {"playText": "San Jose St. starters: A One, B Two, C Three, D Four, E Five, F Six"},
            ]},
        ]},
        {"periodNumber": 2, "playbyplayStats": [
            {"teamId": "46419", "plays": [
                {"playText": "Hawaii starters: Bri Gunderson; Kate O'Neil; Talita dos Santos; "
                             "Ana Ruiz; Mia Lee; Sam West."},
            ]},
        ]},
    ],
}


def test_play_by_play_parses_per_set_starters(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda path, session=None: _PBP)
    pbp = api.play_by_play("6482659")
    assert pbp.ncaa_game_id == "6482659"
    # 2 Hawaii sets + 1 San Jose St set = 3 starter groups.
    assert len(pbp.set_starters) == 3

    haw1 = next(g for g in pbp.set_starters if g.seoname == "hawaii" and g.set_number == 1)
    assert haw1.ncaa_team_id == "46419"
    assert haw1.player_names == (
        "Bri Gunderson", "Kate O'Neil", "Talita dos Santos", "Ana Ruiz", "Mia Lee", "Jo Park",
    )
    # Only the first starters line per team-group is taken (the Kill play is ignored).
    sjs1 = next(g for g in pbp.set_starters if g.seoname == "san-jose-st")
    assert len(sjs1.player_names) == 6

    # Set 2 for Hawaii swaps Jo Park -> Sam West (a real lineup change we should capture verbatim).
    haw2 = next(g for g in pbp.set_starters if g.seoname == "hawaii" and g.set_number == 2)
    assert "Sam West" in haw2.player_names and "Jo Park" not in haw2.player_names


# --- name reconciliation --------------------------------------------------------------------------

def test_norm_name_strips_accents_and_punctuation():
    assert _norm_name("Kate O'Neil") == "kate oneil"
    assert _norm_name("Núñez-García") == "nunez garcia"
    assert _norm_name("  Talita   dos Santos ") == "talita dos santos"


def test_name_key_is_order_insensitive():
    assert _name_key("Jane Doe") == _name_key("Doe, Jane")
    assert _name_key("Jane Doe") == frozenset({"jane", "doe"})


class _FakeSession:
    """Minimal stand-in: returns rows for the Player(id, name) select in _RosterIndex."""
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _stmt):
        return SimpleNamespace(all=lambda: self._rows)


def _idx(rows):
    return _roster_index(_FakeSession(rows), team_id=7, season=2025)


def test_match_prefers_full_then_tokens_then_lastinit():
    idx = _idx([(1, "Jane Doe"), (2, "Sam West"),
                (3, "Addyson Franz"), (4, "Ava Tiessen-Roodbol")])
    assert idx.match("Jane Doe") == 1        # exact normalized string
    assert idx.match("Doe, Jane") == 1       # token set (order-insensitive)
    assert idx.match("Sam West") == 2
    assert idx.match("Addy Franz") == 3      # nickname -> (first initial, last name) fallback
    assert idx.match("Ava Roodbol") == 4     # dropped middle -> last-name+initial catches it
    assert idx.match("Nobody Here") is None


def test_lastinit_fallback_skips_ambiguous():
    # Two players share (first initial, last name); the fuzzy key must not guess between them.
    idx = _idx([(1, "Sarah Johnson"), (2, "Sydney Johnson")])
    assert idx.match("Steph Johnson") is None    # ('s','johnson') is ambiguous -> skip
    assert idx.match("Sarah Johnson") == 1       # exact still resolves


def test_assign_group_picks_team_by_names_not_reported_id():
    # Nebraska line, but ncaa.com may report it under Pitt's id — assignment must follow the NAMES.
    neb = (111, _idx([(1, "Harper Murray"), (2, "Andi Jackson"), (3, "Bergen Reilly")]))
    pitt = (41, _idx([(10, "Olivia Babcock"), (11, "Dagmar Mourits")]))
    team_id, pids, missed = _assign_group(
        ("Harper Murray", "Andi Jackson", "Bergen Reilly"), [pitt, neb]
    )
    assert team_id == 111            # matched Nebraska's roster, not the (irrelevant) reported team
    assert pids == {1, 2, 3}
    assert missed == []

    # A Pitt line resolves to Pitt even when listed first-team is Nebraska; one name unrostered.
    team_id, pids, missed = _assign_group(
        ("Olivia Babcock", "Dagmar Mourits", "Someone Unknown"), [neb, pitt]
    )
    assert team_id == 41
    assert pids == {10, 11}
    assert missed == ["Someone Unknown"]

    # No name matches either roster -> no team, all names reported missed.
    team_id, pids, missed = _assign_group(("Nobody One", "Nobody Two"), [neb, pitt])
    assert team_id is None and pids == set() and missed == ["Nobody One", "Nobody Two"]


def test_roster_index_drops_ambiguous_collisions():
    # Two different players normalize to the same name -> that key must resolve to None (skip).
    idx = _idx([(1, "Jane Doe"), (2, "Jane Doe"), (3, "Sam West")])
    assert idx.by_full["jane doe"] is None          # ambiguous -> not guessed
    assert idx.by_full["sam west"] == 3
    assert idx.by_tokens[frozenset({"sam", "west"})] == 3
    assert idx.match("Jane Doe") is None            # ambiguity propagates through match()


# --- per_set_lineups authoritative override -------------------------------------------------------

def _ev(set_number, team_id, pid, seq, touch_type="attack", rally_number=1):
    return SimpleNamespace(set_number=set_number, team_id=team_id, player_id=pid,
                           player_name=f"P{pid}", seq=seq, touch_type=touch_type,
                           rally_number=rally_number)


def _roster(pids):
    return {pid: SimpleNamespace(name=f"P{pid}", position="OH", number=pid) for pid in pids}


def test_authoritative_starters_override_heuristic():
    away, home = 1, 2
    # Team 1, set 1: players 10-15 all record a touch (heuristic would call all six starters);
    # authoritative says the six are 10-14 + 16 (16 never touches the ball in our events).
    events = [_ev(1, away, pid, seq) for seq, pid in enumerate(range(10, 16), start=1)]
    events += [_ev(1, home, pid, seq) for seq, pid in enumerate(range(20, 26), start=100)]
    roster = _roster(list(range(10, 17)) + list(range(20, 26)))
    team_names = {away: "Away U", home: "Home U"}

    auth = {(away, 1): {10, 11, 12, 13, 14, 16}}
    out = per_set_lineups(events, away, home, roster, team_names, authoritative=auth)

    aw = out["Away U"]["sets"][0]
    starter_ids = {p["player_id"] for p in aw["starters"]}
    sub_ids = {p["player_id"] for p in aw["subs"]}
    assert starter_ids == {10, 11, 12, 13, 14, 16}   # authoritative wins, incl. no-touch 16
    assert sub_ids == {15}                            # touched but not an authoritative starter

    # Home team has no authoritative entry -> falls back to the heuristic (all six touchers).
    hm = out["Home U"]["sets"][0]
    assert {p["player_id"] for p in hm["starters"]} == set(range(20, 26))


def test_no_authoritative_matches_pure_heuristic():
    away, home = 1, 2
    events = [_ev(1, away, pid, seq) for seq, pid in enumerate(range(10, 16), start=1)]
    events += [_ev(1, home, pid, seq) for seq, pid in enumerate(range(20, 26), start=100)]
    roster = _roster(list(range(10, 16)) + list(range(20, 26)))
    team_names = {away: "Away U", home: "Home U"}

    base = per_set_lineups(events, away, home, roster, team_names)
    with_none = per_set_lineups(events, away, home, roster, team_names, authoritative=None)
    assert base == with_none
