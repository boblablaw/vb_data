"""Manual corrections for known-bad upstream (NCAA) roster data.

stats.ncaa.org roster pages occasionally carry data-entry errors that the scrape
faithfully reproduces (e.g. a height typo). Durable, source-of-truth corrections
live here so they survive every roster re-scrape.

Keyed by NCAA player id. Note NCAA assigns a *new* player id each season, so a
player who needs the same fix across seasons gets one entry per season id. Always
cite the authoritative source (usually the school's own roster) in a comment.
"""
from __future__ import annotations

from ..log import get_logger
from ..models import Player

log = get_logger(__name__)

# ncaa_player_id -> {Player attribute: forced value}
ROSTER_OVERRIDES: dict[str, dict] = {
    # Ellie Rink (Saint Louis, OH). NCAA lists 6-10 (82"); school roster says 6-0 (72").
    # https://slubillikens.com/sports/womens-volleyball/roster
    "11429162": {"height_inches": 72},  # season 2026
    "9998402":  {"height_inches": 72},  # season 2025
}


def apply_overrides(player: Player) -> None:
    """Force any known-good values over what the roster scrape loaded for this player."""
    ov = ROSTER_OVERRIDES.get(player.ncaa_player_id or "")
    if not ov:
        return
    for attr, value in ov.items():
        if getattr(player, attr, None) != value:
            log.info("override: %s (%s) %s -> %r", player.name, player.ncaa_player_id, attr, value)
            setattr(player, attr, value)
