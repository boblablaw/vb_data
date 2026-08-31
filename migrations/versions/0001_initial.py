"""initial schema: dimensions, facts, and the derived player_season_stats matview

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-31
"""
from alembic import op

from vb.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# player_season_stats is a MATERIALIZED VIEW, not a table — created via raw SQL below.
_MATVIEW = "player_season_stats"

CREATE_MATVIEW = f"""
CREATE MATERIALIZED VIEW {_MATVIEW} AS
SELECT
    pgs.player_id,
    pgs.season,
    max(pgs.team_id)                                  AS team_id,
    count(DISTINCT pgs.contest_id)                    AS gp,
    NULL::int                                         AS gs,
    sum(pgs.sets)                                     AS sp,
    sum(pgs.kills)                                    AS kills,
    sum(pgs.errors)                                   AS errors,
    sum(pgs.total_attacks)                            AS total_attacks,
    CASE WHEN sum(pgs.total_attacks) > 0
         THEN (sum(pgs.kills) - sum(pgs.errors)) / sum(pgs.total_attacks)
    END                                               AS hit_pct,
    sum(pgs.assists)                                  AS assists,
    sum(pgs.aces)                                     AS aces,
    sum(pgs.serr)                                     AS serr,
    sum(pgs.digs)                                     AS digs,
    sum(pgs.retatt)                                   AS retatt,
    sum(pgs.rerr)                                     AS rerr,
    sum(pgs.block_solos)                              AS block_solos,
    sum(pgs.block_assists)                            AS block_assists,
    (sum(pgs.block_solos) + sum(pgs.block_assists))   AS total_blocks,
    sum(pgs.berr)                                     AS berr,
    sum(pgs.pts)                                      AS pts,
    sum(pgs.bhe)                                      AS bhe,
    sum(pgs.kills)         / NULLIF(sum(pgs.sets), 0) AS kills_per_set,
    sum(pgs.assists)      / NULLIF(sum(pgs.sets), 0)  AS assists_per_set,
    sum(pgs.aces)         / NULLIF(sum(pgs.sets), 0)  AS aces_per_set,
    sum(pgs.digs)         / NULLIF(sum(pgs.sets), 0)  AS digs_per_set,
    (sum(pgs.block_solos) + sum(pgs.block_assists))
                          / NULLIF(sum(pgs.sets), 0)  AS blocks_per_set,
    sum(pgs.pts)          / NULLIF(sum(pgs.sets), 0)  AS pts_per_set
FROM player_game_stats pgs
GROUP BY pgs.player_id, pgs.season
WITH NO DATA;
"""


def upgrade() -> None:
    bind = op.get_bind()
    # Create every real table from the ORM metadata EXCEPT the matview-mapped one.
    tables = [t for t in Base.metadata.sorted_tables if t.name != _MATVIEW]
    Base.metadata.create_all(bind=bind, tables=tables)

    op.create_index("idx_players_team_season", "players", ["team_id", "season"])
    op.create_index("idx_pgs_team_season", "player_game_stats", ["team_id", "season"])

    op.execute(CREATE_MATVIEW)
    # Unique index enables REFRESH MATERIALIZED VIEW CONCURRENTLY.
    op.execute(
        f"CREATE UNIQUE INDEX uq_{_MATVIEW} ON {_MATVIEW} (player_id, season);"
    )


def downgrade() -> None:
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_MATVIEW};")
    tables = [t for t in reversed(Base.metadata.sorted_tables) if t.name != _MATVIEW]
    Base.metadata.drop_all(bind=op.get_bind(), tables=tables)
