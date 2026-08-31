"""Backfill contest date + home/away for contests already in the DB.

Early loads only stored ``contest_id`` + ``season``; the individual_stats page (which the
game-stats scrape already reads) also carries the date and both teams. This re-fetches that
page per contest and fills ``contests.date`` / ``home_team_id`` / ``away_team_id`` directly,
without rewriting the raw game-stats CSV. Idempotent: contests already populated are skipped
unless ``force=True``.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..fetch import fetch_html
from ..load.common import ncaa_id_to_team
from ..log import get_logger
from ..models import Contest
from .game_stats import contest_meta

log = get_logger(__name__)


def backfill_contest_meta(session: Session, season: int, force: bool = False) -> dict:
    """Fill date/home/away for the season's contests. Returns a summary dict."""
    ncaa_team = {nid: t.id for nid, t in ncaa_id_to_team(session, season).items()}

    contests = session.execute(
        select(Contest).where(Contest.season == season)
    ).scalars().all()
    todo = [
        c for c in contests
        if force or c.date is None or c.home_team_id is None or c.away_team_id is None
    ]
    log.info("[backfill contest-meta] %d contest(s), %d to fill (season %d)",
             len(contests), len(todo), season)

    updated = unresolved = 0
    for i, c in enumerate(todo, 1):
        url = f"https://stats.ncaa.org/contests/{c.contest_id}/individual_stats"
        try:
            html = fetch_html(url, wait_selectors=("table",))
        except Exception as e:
            log.warning("    [%d/%d] contest %s: fetch failed (%s)", i, len(todo), c.contest_id, e)
            continue
        meta = contest_meta(html)
        home = ncaa_team.get(meta["HomeTeamNcaaId"])
        away = ncaa_team.get(meta["AwayTeamNcaaId"])
        if meta["Date"]:
            c.date = meta["Date"]
        if home:
            c.home_team_id = home
        if away:
            c.away_team_id = away
        session.flush()
        if home and away and meta["Date"]:
            updated += 1
        else:
            unresolved += 1
            log.info("    [%d/%d] contest %s: partial (date=%s home=%s away=%s)",
                     i, len(todo), c.contest_id, meta["Date"], home, away)

    log.info("[backfill contest-meta] %d fully filled, %d partial (season %d)",
             updated, unresolved, season)
    return {"season": season, "total": len(contests), "processed": len(todo),
            "updated": updated, "partial": unresolved}
