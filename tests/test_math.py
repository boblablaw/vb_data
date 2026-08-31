"""Pure-Python checks of the cumulative-derivation formulas and helpers.

The production derivation lives in SQL (the player_season_stats matview). These tests lock
the *formulas* it must implement against a hand-computed fixture, so a drift in the SQL is
caught by test_derive's DB comparison and the formula intent is documented here.
"""
from __future__ import annotations

from vb.load.common import num, num_int
from vb.util import height_to_inches, normalize_class

# A single player's two contests. (sets, kills, errors, total_attacks)
FIXTURE = [
    {"contest": "A", "sets": 3, "kills": 10, "errors": 2, "total_attacks": 25},
    {"contest": "B", "sets": 4, "kills": 6, "errors": 4, "total_attacks": 20},
]


def _cumulative(rows):
    """Reference implementation mirroring the matview aggregation."""
    gp = len({r["contest"] for r in rows})
    sp = sum(r["sets"] for r in rows)
    kills = sum(r["kills"] for r in rows)
    errors = sum(r["errors"] for r in rows)
    ta = sum(r["total_attacks"] for r in rows)
    hit_pct = (kills - errors) / ta if ta else None
    return {
        "gp": gp, "sp": sp, "kills": kills, "errors": errors,
        "total_attacks": ta, "hit_pct": hit_pct,
        "kills_per_set": kills / sp if sp else None,
    }


def test_cumulative_formulas():
    c = _cumulative(FIXTURE)
    assert c["gp"] == 2
    assert c["sp"] == 7
    assert c["kills"] == 16
    assert c["errors"] == 6
    assert c["total_attacks"] == 45
    # (16 - 6) / 45
    assert abs(c["hit_pct"] - (10 / 45)) < 1e-9
    assert abs(c["kills_per_set"] - (16 / 7)) < 1e-9


def test_num_coercion():
    assert num("12") == 12.0
    assert num("0.091") == 0.091
    assert num("") is None
    assert num("nan") is None
    assert num("-") is None
    assert num_int("5.0") == 5
    assert num_int(None) is None


def test_height_and_class():
    assert height_to_inches("6-2") == 74
    assert height_to_inches("6'2\"") == 74
    assert height_to_inches("") is None
    assert normalize_class("Junior") == "Jr"
    assert normalize_class("Redshirt Sophomore") == "R-So"
    assert normalize_class("Redshirt Freshman") == "R-Fr"
