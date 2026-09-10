"""Unit tests for the game-time normalizers (no DB, no network)."""
from __future__ import annotations

from vb.util import epoch_to_et_game_time, is_unset_game_time


def test_epoch_to_et_game_time_formats_eastern_12h():
    # 1789077600 == 2026-09-10 18:00 US-Eastern (the TCU vs South Fla. game the fix was built for).
    assert epoch_to_et_game_time(1789077600) == "06:00 PM"


def test_epoch_to_et_game_time_none_and_zero():
    assert epoch_to_et_game_time(None) is None
    assert epoch_to_et_game_time(0) is None


def test_epoch_to_et_game_time_midnight_is_treated_as_unset():
    # 1788897600 == 2026-09-08 16:00 US-Eastern -> a real afternoon time...
    assert epoch_to_et_game_time(1788897600) == "04:00 PM"
    # ...but 1788840000 == exactly 2026-09-08 00:00 US-Eastern, which is itself the "unknown time"
    # sentinel (midnight), so we return None rather than re-create a TBD.
    assert epoch_to_et_game_time(1788840000) is None


def test_is_unset_game_time_sentinels():
    assert is_unset_game_time(None) is True
    assert is_unset_game_time("") is True
    assert is_unset_game_time("   ") is True
    assert is_unset_game_time("12:00 AM") is True
    assert is_unset_game_time("12:00 am") is True
    assert is_unset_game_time("00:00") is True


def test_is_unset_game_time_real_times():
    assert is_unset_game_time("06:00 PM") is False
    assert is_unset_game_time("7:30 PM") is False
    assert is_unset_game_time("11:00 AM") is False
