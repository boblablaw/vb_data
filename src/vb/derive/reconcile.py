"""Reconcile DERIVED cumulative stats against the SCRAPED season-to-date totals.

Confirms the derive logic matches NCAA's published numbers and surfaces gaps (e.g. contests
not yet loaded). Counting-stat diffs should be ~0 for players whose contests are all loaded.

GS note: games-started has no per-game equivalent, so the matview's gs is always null; the
authoritative gs lives in player_season_stats_scraped and is served via join by the API
(matviews are read-only — there is nothing to physically backfill).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..log import get_logger
from ..models import Player, PlayerSeasonStat, PlayerSeasonStatScraped

log = get_logger(__name__)

# Counting stats present in both the derived matview and the scraped table.
COMPARE_FIELDS = (
    "sp", "kills", "errors", "total_attacks", "assists", "aces", "serr", "digs",
    "retatt", "rerr", "block_solos", "block_assists", "berr", "pts", "bhe",
)


def reconcile(session: Session, season: int, tolerance: float = 0.5) -> dict:
    """Compare derived vs scraped per player. Returns a summary + list of discrepancies."""
    derived = {
        r.player_id: r
        for r in session.scalars(
            select(PlayerSeasonStat).where(PlayerSeasonStat.season == season)
        ).all()
    }
    scraped = {
        r.player_id: r
        for r in session.scalars(
            select(PlayerSeasonStatScraped).where(PlayerSeasonStatScraped.season == season)
        ).all()
    }
    names = {
        p.id: p.name
        for p in session.scalars(select(Player).where(Player.season == season)).all()
    }

    discrepancies: list[dict] = []
    both = set(derived) & set(scraped)
    for pid in both:
        d, s = derived[pid], scraped[pid]
        diffs = {}
        for f in COMPARE_FIELDS:
            dv = getattr(d, f) or 0
            sv = getattr(s, f) or 0
            if abs(float(dv) - float(sv)) > tolerance:
                diffs[f] = {"derived": dv, "scraped": sv}
        if diffs:
            discrepancies.append({"player_id": pid, "name": names.get(pid), "diffs": diffs})

    only_scraped = sorted(set(scraped) - set(derived))
    only_derived = sorted(set(derived) - set(scraped))
    summary = {
        "season": season,
        "derived_players": len(derived),
        "scraped_players": len(scraped),
        "compared": len(both),
        "matching": len(both) - len(discrepancies),
        "discrepancies": len(discrepancies),
        "only_scraped": len(only_scraped),
        "only_derived": len(only_derived),
    }
    log.info(
        "reconcile season %d: compared=%d matching=%d discrepancies=%d "
        "only_scraped=%d only_derived=%d",
        season, summary["compared"], summary["matching"], summary["discrepancies"],
        summary["only_scraped"], summary["only_derived"],
    )
    return {"summary": summary, "discrepancies": discrepancies,
            "only_scraped": only_scraped, "only_derived": only_derived}
