"""Shared live-score overlay from the henrygd ncaa.com sidecar.

Both the league scoreboard (``/games``) and a team's schedule (``/teams/{id}/games``) show the same
in-progress / just-finished ncaa.com scores overlaid onto their *upcoming* stubs. The two endpoints
carry different row shapes, so the reusable pieces here are the two shape-agnostic halves:

* :func:`board_index` fetches the sidecar scoreboard for the given days and indexes the live/final
  games by ncaa.com game id and by (date, team-slug pair) — the two ways a caller matches its own
  row to a sidecar game.
* :func:`orient` takes a matched sidecar game plus the caller's *home*-side slug and returns the
  sets-won / per-set scores / status / period already oriented onto home/away slots (ncaa.com lists
  neutral-site sides in its own order, which need not match ours).

Best-effort throughout: any sidecar failure is swallowed so the caller's plain board is served
unchanged. ``ncaa_api`` is referenced as a module attribute so tests can monkeypatch it.
"""
from __future__ import annotations

from ..log import get_logger
from ..scrape import ncaa_api
from ..util import slug_school

log = get_logger(__name__)


def board_index(days) -> tuple[dict, dict]:
    """Index the sidecar scoreboard's live/final games for ``days`` (an iterable of dates).

    Returns ``(by_id, by_pair)``: ``by_id`` maps ncaa.com game id -> ``ApiGame``; ``by_pair`` maps
    ``(date_str, frozenset_of_two_slugs) -> ApiGame`` for games not yet mapped to a ncaa id on our
    side. A sidecar failure for one day is logged and skipped, leaving that day out of the index.
    """
    by_id: dict = {}
    by_pair: dict = {}
    for day in days:
        try:
            board = ncaa_api.scoreboard_cached(day)
        except Exception as e:  # sidecar down/slow/blocked — degrade to the plain board
            log.warning("live scoreboard merge skipped for %s: %s", day, e)
            continue
        for ag in board:
            if (ag.game_state or "").lower() not in ("live", "final"):
                continue  # 'pre' games are already the right 'upcoming' card
            if ag.ncaa_game_id:
                by_id[ag.ncaa_game_id] = ag
            slugs = frozenset(s for s in ag.seonames if s)
            if len(slugs) == 2:
                by_pair[(ag.date, slugs)] = ag
    return by_id, by_pair


def slug_side(short_or_name: str | None) -> str:
    """ncaa.com-style slug for one side of a game from a team's short name or full name."""
    return slug_school(short_or_name) if short_or_name else ""


def orient(ag, home_slug: str) -> dict:
    """Orient a matched sidecar ``ApiGame`` onto the caller's home/away slots.

    ncaa.com lists ``seonames`` as (away, home); if the caller's home side is ncaa.com's *away*
    team (common at neutral sites) the sets-won and per-set points are swapped. Returns a dict of
    ``home_sets_won``, ``away_sets_won``, ``set_scores`` ({"home": [...], "away": [...]} or None),
    ``status`` ('live' | 'final_pending'), and ``live_period``.
    """
    ncaa_away_seo = ag.seonames[0] if ag.seonames else ""
    flipped = bool(home_slug and home_slug == ncaa_away_seo)
    if flipped:
        home_sets, away_sets = ag.away_sets_won, ag.home_sets_won
    else:
        home_sets, away_sets = ag.home_sets_won, ag.away_sets_won
    # Per-set point scores live on the per-game endpoint, not the board. Overlay them oriented onto
    # our home/away slots (same flip as the sets-won). Best-effort: a sidecar miss just leaves the
    # sets-won lines.
    set_scores = None
    ls = ncaa_api.game_linescores(ag.ncaa_game_id)
    if ls:
        home_pts, away_pts = (ls.visit, ls.home) if flipped else (ls.home, ls.visit)
        set_scores = {"home": list(home_pts), "away": list(away_pts)}
    live = (ag.game_state or "").lower() == "live"
    return {
        "home_sets_won": home_sets, "away_sets_won": away_sets, "set_scores": set_scores,
        "status": "live" if live else "final_pending",
        "live_period": ag.current_period if live else None,
    }
