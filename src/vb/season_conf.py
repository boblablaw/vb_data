"""Season-aware conference resolution.

Conference membership changes year to year (realignment), so a team's conference is *season-specific*
(e.g. Colorado State: Mountain West in 2025, Pac-12 in 2026). ``team_season_ids.conference_id`` holds
the per-season value — populated by ``vb load-season-conferences`` from the authoritative NCAA
membership scrape — while ``teams.conference_id`` is only the current/global default.

Everywhere a query needs a team's conference *for a given season*, resolve through here so the two are
coalesced: the season value wins, the global default is the fallback for rows not yet backfilled.

Two idioms are supported:
  * SQL joins — replace ``.join(Conference, Conference.id == Team.conference_id, isouter=True)`` with
    the two joins :func:`join_conference` builds (``team_season_ids`` on ``(team_id, season)`` — ≤1
    row, no fan-out — then ``Conference`` on :func:`season_conf_id`). After it, ``Conference.name`` /
    ``Conference.id`` are the season-correct values.
  * Dict/ORM reads — use :func:`season_conf_map` for a batched ``team_id -> (conf_id, name, short)``.
"""
from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .models import Conference, Team, TeamSeasonId


def season_conf_id(season: int, team=Team, tsi=TeamSeasonId):
    """Effective conference-id SQL expression: the season's value, else the global default.

    Requires ``tsi`` to be (outer)joined for ``season`` (see :func:`join_conference`). ``team``/``tsi``
    may be aliases when a query references more than one team.
    """
    return func.coalesce(tsi.conference_id, team.conference_id)


def join_conference(stmt, season: int, *, team=Team, conf=Conference, tsi=TeamSeasonId):
    """Season-awarely outer-join ``conf`` to ``team`` (which must already be in the FROM).

    Adds the ``team_season_ids`` join on ``(team_id, season)`` then the ``Conference`` join on the
    coalesced id. Pass aliased ``team``/``conf``/``tsi`` when the query joins more than one team.
    """
    stmt = stmt.join(
        tsi, and_(tsi.team_id == team.id, tsi.season == season), isouter=True
    )
    stmt = stmt.join(
        conf, conf.id == func.coalesce(tsi.conference_id, team.conference_id), isouter=True
    )
    return stmt


def season_conf_map(
    db: Session, season: int, team_ids=None
) -> dict[int, tuple[int | None, str | None, str | None]]:
    """Batched ``team_id -> (conference_id, conference_name, conference_short)`` for ``season``.

    Season-aware (coalesces the per-season value with the global default). Restrict to ``team_ids``
    when you only need a few teams; omit for every team. Use for ORM/dict reads where a SQL join
    isn't convenient.
    """
    stmt = (
        select(
            Team.id,
            func.coalesce(TeamSeasonId.conference_id, Team.conference_id),
            Conference.name,
            Conference.short_name,
        )
        .select_from(Team)
        .join(
            TeamSeasonId,
            and_(TeamSeasonId.team_id == Team.id, TeamSeasonId.season == season),
            isouter=True,
        )
        .join(
            Conference,
            Conference.id == func.coalesce(TeamSeasonId.conference_id, Team.conference_id),
            isouter=True,
        )
    )
    if team_ids is not None:
        ids = [t for t in team_ids if t is not None]
        if not ids:
            return {}
        stmt = stmt.where(Team.id.in_(ids))
    return {r[0]: (r[1], r[2], r[3]) for r in db.execute(stmt).all()}
