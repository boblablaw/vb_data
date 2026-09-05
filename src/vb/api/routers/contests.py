"""Contest endpoints: list by season + per-contest player stat lines."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...derive.pbp import setter_hitting_by_player
from ...models import Contest, PbpEvent, Player, PlayerGameStat, Team
from ..deps import get_session
from ..schemas import (
    ContestOut,
    GameStatOut,
    PbpOut,
    PbpSetAgg,
    PbpSetOut,
    PbpTimelinePoint,
    TeamRef,
)

router = APIRouter(prefix="/contests", tags=["contests"])


def _team_refs(db: Session, *team_ids: int | None) -> dict[int, TeamRef]:
    ids = {t for t in team_ids if t is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(Team.id, Team.name, Team.short_name, Team.logo_light, Team.logo_dark,
               Team.avca_rank, Team.conference_id)
        .where(Team.id.in_(ids))
    ).all()
    return {
        r.id: TeamRef(id=r.id, name=r.name, short_name=r.short_name,
                      logo_light=r.logo_light, logo_dark=r.logo_dark,
                      avca_rank=r.avca_rank, conference_id=r.conference_id)
        for r in rows
    }


def _contest_out(c: Contest, refs: dict[int, TeamRef]) -> ContestOut:
    return ContestOut(
        contest_id=c.contest_id, season=c.season, date=c.date,
        home_team_id=c.home_team_id, away_team_id=c.away_team_id,
        home_sets_won=c.home_sets_won, away_sets_won=c.away_sets_won,
        set_scores=c.set_scores, ncaa_game_id=c.ncaa_game_id,
        location=c.location, attendance=c.attendance,
        home_team=refs.get(c.home_team_id), away_team=refs.get(c.away_team_id),
    )


@router.get("", response_model=list[ContestOut])
def list_contests(
    season: int = Query(...),
    limit: int = Query(200, le=5000),
    offset: int = 0,
    db: Session = Depends(get_session),
):
    contests = db.scalars(
        select(Contest).where(Contest.season == season)
        .order_by(Contest.contest_id).limit(limit).offset(offset)
    ).all()
    refs = _team_refs(db, *[c.home_team_id for c in contests],
                      *[c.away_team_id for c in contests])
    return [_contest_out(c, refs) for c in contests]


@router.get("/{contest_id}", response_model=ContestOut)
def get_contest(contest_id: str, db: Session = Depends(get_session)):
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "contest not found")
    return _contest_out(c, _team_refs(db, c.home_team_id, c.away_team_id))


@router.get("/{contest_id}/pbp", response_model=PbpOut)
def contest_pbp(contest_id: str, db: Session = Depends(get_session)):
    """Play-by-play summary for a contest: per-set touch aggregates + the scoring timeline.

    Computed on the fly from ``pbp_events`` (~hundreds of rows). Returns 200 with an empty
    ``sets`` list when the contest has no PBP yet, so the frontend can hide the card cleanly.
    """
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "contest not found")
    refs = _team_refs(db, c.home_team_id, c.away_team_id)

    events = db.scalars(
        select(PbpEvent).where(PbpEvent.contest_id == contest_id).order_by(PbpEvent.seq)
    ).all()

    # aggs[set_number][team_id] -> PbpSetAgg; built only for the two known sides.
    sides = {c.away_team_id: "away", c.home_team_id: "home"}
    by_set: dict[int, dict] = {}
    for e in events:
        s = by_set.setdefault(e.set_number, {
            "home": PbpSetAgg(team_id=c.home_team_id),
            "away": PbpSetAgg(team_id=c.away_team_id),
            "timeline": [],
        })
        side = sides.get(e.team_id)
        if not e.is_terminal:
            if side is None:
                continue
            agg = s[side]
            if e.touch_type == "set":
                agg.set_attempts += 1
            elif e.touch_type == "serve":
                agg.serve_attempts += 1
            elif e.touch_type == "attack":
                agg.attack_attempts += 1
            elif e.touch_type == "dig":
                agg.digs += 1
            elif e.touch_type == "reception":
                agg.receptions += 1
            continue
        # terminal: credit the owning team (scoring side for kill/ace/block, erroring for errors)
        if side is not None:
            agg = s[side]
            tt = e.terminal_type
            if tt == "kill":
                agg.kills += 1
            elif tt == "ace":
                agg.aces += 1
            elif tt == "block":
                agg.blocks += 1
            elif tt and tt.endswith("_error"):
                agg.errors += 1
                if tt == "attack_error":
                    agg.attack_errors += 1
        # points: the rally goes to whoever scored (independent of which side owns the touch)
        scorer_side = sides.get(e.scoring_team_id)
        if scorer_side is not None:
            s[scorer_side].points += 1
        s["timeline"].append(PbpTimelinePoint(
            rally=e.rally_number, away_score=e.away_score, home_score=e.home_score,
            scoring_team_id=e.scoring_team_id, terminal_type=e.terminal_type,
        ))

    # Per-set team assists: a kill is credited as an assist to the scoring team when that team
    # made a set touch earlier in the same rally (mirrors how box-score assists track kills off a
    # set). Grouped by rally so we can look back within the rally.
    by_rally: dict[tuple[int, int], list] = defaultdict(list)
    for e in events:
        by_rally[(e.set_number, e.rally_number)].append(e)
    for (set_no, _rally), revs in by_rally.items():
        term = next((e for e in revs if e.is_terminal), None)
        if term is None or term.terminal_type != "kill":
            continue
        scorer_side = sides.get(term.scoring_team_id)
        if scorer_side is None or set_no not in by_set:
            continue
        if any(e.touch_type == "set" and e.team_id == term.scoring_team_id for e in revs):
            by_set[set_no][scorer_side].assists += 1

    sets_out: list[PbpSetOut] = []
    for set_no in sorted(by_set):
        s = by_set[set_no]
        timeline = s["timeline"]
        ties = lead_changes = 0
        prev_leader = 0  # 0 tie, 1 away ahead, -1 home ahead
        for p in timeline:
            if p.away_score is None or p.home_score is None:
                continue
            if p.away_score == p.home_score:
                ties += 1
                leader = 0
            else:
                leader = 1 if p.away_score > p.home_score else -1
            if leader != 0 and prev_leader != 0 and leader != prev_leader:
                lead_changes += 1
            if leader != 0:
                prev_leader = leader
        sets_out.append(PbpSetOut(
            set_number=set_no, home=s["home"], away=s["away"],
            timeline=timeline, ties=ties, lead_changes=lead_changes,
        ))

    return PbpOut(
        contest_id=contest_id,
        home_team=refs.get(c.home_team_id), away_team=refs.get(c.away_team_id),
        sets=sets_out,
    )


@router.get("/{contest_id}/stats", response_model=list[GameStatOut])
def contest_stats(contest_id: str, db: Session = Depends(get_session)):
    rows = db.execute(
        select(PlayerGameStat, Player.name, Player.number, Player.position, Player.height_inches)
        .join(Player, Player.id == PlayerGameStat.player_id, isouter=True)
        .where(PlayerGameStat.contest_id == contest_id)
    ).all()
    # Per-game set attempts (play-by-play): count set touches per player for this contest. Absent
    # for contests without PBP -> set_attempts stays None (dash in the UI).
    set_counts = dict(db.execute(
        select(PbpEvent.player_id, func.count())
        .where(
            PbpEvent.contest_id == contest_id,
            PbpEvent.touch_type == "set",
            PbpEvent.player_id.isnot(None),
        )
        .group_by(PbpEvent.player_id)
    ).all())
    # Per-game serve attempts: count serve touches per player for this contest (same PBP source).
    serve_counts = dict(db.execute(
        select(PbpEvent.player_id, func.count())
        .where(
            PbpEvent.contest_id == contest_id,
            PbpEvent.touch_type == "serve",
            PbpEvent.player_id.isnot(None),
        )
        .group_by(PbpEvent.player_id)
    ).all())
    # Per-game setter hitting %: replay this contest's ordered touches (shared with derive-pbp),
    # linking each set to the attack it fed. Absent for contests without PBP -> None (dash).
    pbp_events = list(db.scalars(
        select(PbpEvent).where(PbpEvent.contest_id == contest_id).order_by(PbpEvent.seq)
    ).all())
    setter_hit = setter_hitting_by_player(pbp_events)
    out: list[GameStatOut] = []
    for pgs, name, number, position, height_inches in rows:
        line = GameStatOut.model_validate(pgs)
        line.player_name = name
        line.number = number
        line.position = position
        line.height_inches = height_inches
        line.set_attempts = set_counts.get(pgs.player_id)
        line.serve_attempts = serve_counts.get(pgs.player_id)
        sh = setter_hit.get(pgs.player_id)
        if sh is not None:
            sk, se, satk = sh
            line.setter_hit_attacks = satk
            line.setter_hitting_pct = ((sk - se) / satk) if satk > 0 else None
        out.append(line)
    return out
