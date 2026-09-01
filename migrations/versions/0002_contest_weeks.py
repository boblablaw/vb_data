"""contest_weeks view: season-anchored Mon–Sun week number per contest

Revision ID: 0002_contest_weeks
Revises: 0001_initial
Create Date: 2026-08-31

`contests.date` is nullable free-text ("YYYY-MM-DD HH:MM"). This VIEW derives a Monday-based,
1-based **week number per season** for the fantasy front-end: Week 1 = the Postgres week
(date_trunc('week', ...) is Monday-based) containing the season's earliest parseable contest date.

Design notes:
  - to_date (NOT ::date) on the first 10 chars so a regex-matching-but-invalid value never raises
    (it silently rolls over, which is a rare, acceptable data wrinkle).
  - min() OVER (PARTITION BY season) ignores NULLs, so unparseable dates don't corrupt the anchor.
  - NULL/unparseable date -> NULL week_number ("Unknown" bucket in the UI).
  - A live VIEW (not a matview): the computation is trivial and always consistent with contests;
    no refresh wiring needed.
"""
from alembic import op

revision = "0002_contest_weeks"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_VIEW = "contest_weeks"

CREATE_VIEW = f"""
CREATE VIEW {_VIEW} AS
WITH parsed AS (
    SELECT c.contest_id, c.season,
           CASE WHEN c.date ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                THEN to_date(substring(c.date FROM 1 FOR 10), 'YYYY-MM-DD')
           END AS game_date
    FROM contests c
),
weeks AS (
    SELECT contest_id, season, game_date,
           CASE WHEN game_date IS NOT NULL
                THEN date_trunc('week', game_date::timestamp)::date
           END AS week_monday
    FROM parsed
),
anchored AS (
    SELECT w.*, min(w.week_monday) OVER (PARTITION BY w.season) AS season_anchor
    FROM weeks w
)
SELECT contest_id, season, game_date, week_monday,
       CASE WHEN week_monday IS NOT NULL
            THEN ((week_monday - season_anchor) / 7)::int + 1
       END AS week_number
FROM anchored;
"""


def upgrade() -> None:
    op.execute(CREATE_VIEW)


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW};")
