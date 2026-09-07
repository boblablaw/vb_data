"""contest_weeks view: derive weeks from schedule too, so upcoming weeks exist

Revision ID: 0019_weeks_from_schedule
Revises: 0018_user_ai_enabled
Create Date: 2026-09-07

The original view (migration 0002) derived its Mon–Sun week numbers ONLY from the ``contests``
table, i.e. games that have been played *and* loaded. That means the current/upcoming week never
appears in the week picker until at least one of its games is final — so mid-week the Games tab and
the "Week" leaderboard scope dead-end at the last completed week ("no new week added").

Fix: build the week universe from the UNION of ``contests`` dates (which keep their ``contest_id``
so callers can still map a played contest to its week) AND ``schedule`` dates (contest_id NULL —
they only extend week coverage forward to scheduled-but-unplayed weeks). The season anchor
(min week Monday) is unchanged by this because every season's first scheduled game falls in the
same Monday-based week as its first played game (verified for 2025/2026), so existing week numbers
do not shift. Schedule rows carry NULL contest_id, so the contest_id→week joins and the
``count(contest_id)`` per-week tally are unaffected (upcoming weeks simply report 0 played games).
"""
from alembic import op

revision = "0019_weeks_from_schedule"
down_revision = "0018_user_ai_enabled"
branch_labels = None
depends_on = None

_VIEW = "contest_weeks"

# New definition: contests ∪ schedule.
CREATE_NEW = f"""
CREATE VIEW {_VIEW} AS
WITH dated AS (
    -- Played contests keep their id so a contest can be mapped to its week.
    SELECT c.contest_id, c.season, c.date AS raw_date FROM contests c
    UNION ALL
    -- Scheduled games extend week coverage forward; no contest_id (won't join to contests).
    SELECT NULL::varchar AS contest_id, s.season, s.date AS raw_date FROM schedule s
),
parsed AS (
    SELECT contest_id, season,
           CASE WHEN raw_date ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                THEN to_date(substring(raw_date FROM 1 FOR 10), 'YYYY-MM-DD')
           END AS game_date
    FROM dated
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

# Original definition (migration 0002): contests only. Used for downgrade.
CREATE_OLD = f"""
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
    op.execute(f"DROP VIEW IF EXISTS {_VIEW};")
    op.execute(CREATE_NEW)


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW};")
    op.execute(CREATE_OLD)
