"""Refresh the derived cumulative-stats materialized view (player_season_stats).

The matview aggregates player_game_stats (see the initial migration). This is the app's
single source of truth for season totals — always internally consistent with per-game data.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..log import get_logger

log = get_logger(__name__)

MATVIEW = "player_season_stats"


def derive_cumulative(session: Session, concurrently: bool = True) -> dict:
    """REFRESH MATERIALIZED VIEW. Falls back to a plain refresh if CONCURRENTLY can't run
    (it requires the unique index AND a prior non-concurrent populate)."""
    session.flush()
    conn = session.connection()
    # REFRESH cannot run inside the surrounding transaction block for CONCURRENTLY in some
    # setups; commit any pending work first so the refresh sees loaded rows.
    session.commit()
    stmt_conc = text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {MATVIEW}")
    stmt_plain = text(f"REFRESH MATERIALIZED VIEW {MATVIEW}")
    if concurrently:
        try:
            conn.execute(stmt_conc)
            session.commit()
            log.info("derive_cumulative: refreshed %s CONCURRENTLY", MATVIEW)
            return {"matview": MATVIEW, "mode": "concurrent"}
        except Exception as e:
            session.rollback()
            log.info("concurrent refresh unavailable (%s); doing a full refresh", e)
    conn = session.connection()
    conn.execute(stmt_plain)
    session.commit()
    log.info("derive_cumulative: refreshed %s", MATVIEW)
    return {"matview": MATVIEW, "mode": "full"}
