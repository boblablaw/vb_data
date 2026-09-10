"""Contest endpoints: list by season + per-contest player stat lines."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...derive.pbp import attack_splits_by_player, setter_hitting_by_player
from ...models import Contest, ContestSetStarter, PbpEvent, Player, PlayerGameStat, Team
from ...query.tools import per_rotation_stats, per_set_lineups
from ...season_conf import season_conf_map
from ..deps import get_session
from ..schemas import (
    ContestOut,
    GameStatOut,
    LineupChange,
    LineupPlayer,
    LineupSet,
    PbpOut,
    PbpSetAgg,
    PbpSetOut,
    PbpTimelinePoint,
    RotationAgg,
    RotationSet,
    TeamLineups,
    TeamRef,
    TeamRotations,
)

router = APIRouter(prefix="/contests", tags=["contests"])


def _team_refs(db: Session, *team_ids: int | None, season: int | None = None) -> dict[int, TeamRef]:
    ids = {t for t in team_ids if t is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(Team.id, Team.name, Team.short_name, Team.logo_light, Team.logo_dark,
               Team.avca_rank, Team.conference_id)
        .where(Team.id.in_(ids))
    ).all()
    # Fav-conference badges must reflect the season's affiliation (realignment), not the current one.
    conf_map = season_conf_map(db, season, list(ids)) if season is not None else {}
    return {
        r.id: TeamRef(id=r.id, name=r.name, short_name=r.short_name,
                      logo_light=r.logo_light, logo_dark=r.logo_dark,
                      avca_rank=r.avca_rank,
                      conference_id=conf_map[r.id][0] if r.id in conf_map else r.conference_id)
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
                      *[c.away_team_id for c in contests], season=season)
    return [_contest_out(c, refs) for c in contests]


@router.get("/{contest_id}", response_model=ContestOut)
def get_contest(contest_id: str, db: Session = Depends(get_session)):
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "contest not found")
    return _contest_out(c, _team_refs(db, c.home_team_id, c.away_team_id, season=c.season))


@router.get("/{contest_id}/pbp", response_model=PbpOut)
def contest_pbp(contest_id: str, db: Session = Depends(get_session)):
    """Play-by-play summary for a contest: per-set touch aggregates + the scoring timeline.

    Computed on the fly from ``pbp_events`` (~hundreds of rows). Returns 200 with an empty
    ``sets`` list when the contest has no PBP yet, so the frontend can hide the card cleanly.
    """
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "contest not found")
    refs = _team_refs(db, c.home_team_id, c.away_team_id, season=c.season)

    events = db.scalars(
        select(PbpEvent).where(PbpEvent.contest_id == contest_id).order_by(PbpEvent.seq)
    ).all()

    # aggs[set_number][team_id] -> PbpSetAgg; built only for the two known sides.
    sides = {c.away_team_id: "away", c.home_team_id: "home"}

    # Assisting setter per kill rally: the last same-team ``set`` touch earlier in the rally
    # (seq order). Mirrors the per-set assist look-back below; used to attribute "assisted by" on
    # the rally log. Keyed by (set_number, rally_number).
    rally_events: dict[tuple[int, int], list] = defaultdict(list)
    for e in events:
        rally_events[(e.set_number, e.rally_number)].append(e)
    assist_for: dict[tuple[int, int], object] = {}
    for key, revs in rally_events.items():
        term = next((e for e in revs if e.is_terminal), None)
        if term is None or term.terminal_type != "kill" or term.scoring_team_id is None:
            continue
        setter = next(
            (e for e in reversed(revs)
             if e.touch_type == "set" and e.team_id == term.scoring_team_id),
            None,
        )
        if setter is not None:
            assist_for[key] = setter

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
        setter = assist_for.get((e.set_number, e.rally_number))
        s["timeline"].append(PbpTimelinePoint(
            rally=e.rally_number, away_score=e.away_score, home_score=e.home_score,
            scoring_team_id=e.scoring_team_id, terminal_type=e.terminal_type,
            scorer_name=e.player_name, scorer_player_id=e.player_id,
            assist_name=setter.player_name if setter is not None else None,
            assist_player_id=setter.player_id if setter is not None else None,
        ))

    # Per-set team assists: a kill is credited as an assist to the scoring team when that team made
    # a set touch earlier in the rally — exactly the kills for which ``assist_for`` found a setter.
    for (set_no, _rally), setter in assist_for.items():
        scorer_side = sides.get(setter.team_id)
        if scorer_side is not None and set_no in by_set:
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

    # Per-set starting lineups + lineup changes, reconstructed from the same events (shared with the
    # match_lineups query tool). Roster gives canonical name/position/number; team_names label sides.
    roster = {
        p.id: p for p in db.scalars(
            select(Player).where(
                Player.season == c.season,
                Player.team_id.in_([c.home_team_id, c.away_team_id]),
            )
        ).all()
    }
    team_names = {tid: (r.name if (r := refs.get(tid)) else None)
                  for tid in (c.away_team_id, c.home_team_id)}
    # Authoritative starters from ncaa.com (load-ncaa-lineups), when present: {(team_id, set): {pid}}.
    # per_set_lineups uses these verbatim and falls back to the pbp reconstruction where absent.
    authoritative: dict[tuple[int, int], set[int]] = defaultdict(set)
    for row in db.scalars(
        select(ContestSetStarter).where(ContestSetStarter.contest_id == contest_id)
    ).all():
        authoritative[(row.team_id, row.set_number)].add(row.player_id)
    lineups_raw = per_set_lineups(events, c.away_team_id, c.home_team_id, roster, team_names,
                                  authoritative=authoritative or None)
    lineups_out = [
        TeamLineups(
            team_id=t["team_id"], team=name, side=t["side"],
            sets=[LineupSet(
                set_number=s["set_number"],
                starters=[LineupPlayer(**p) for p in s["starters"]],
                subs=[LineupPlayer(**p) for p in s["subs"]],
            ) for s in t["sets"]],
            starters_changed=t["starters_changed"],
            starter_changes=[LineupChange(**ch) for ch in t["starter_changes"]],
        )
        for name, t in lineups_raw.items()
    ]

    # Per-rotation (R1-R6) stats, reconstructed from the same events (R1 anchored to the setter).
    rotations_raw = per_rotation_stats(events, c.away_team_id, c.home_team_id, team_names)
    rotations_out = [
        TeamRotations(
            team_id=t["team_id"], team=name, side=t["side"],
            sets=[RotationSet(
                set_number=s["set_number"],
                rotations=[RotationAgg(**r) for r in s["rotations"]],
            ) for s in t["sets"]],
            totals=[RotationAgg(**r) for r in t["totals"]],
        )
        for name, t in rotations_raw.items()
    ]

    return PbpOut(
        contest_id=contest_id,
        home_team=refs.get(c.home_team_id), away_team=refs.get(c.away_team_id),
        sets=sets_out, lineups=lineups_out, rotations=rotations_out,
    )


@router.get("/{contest_id}/stats", response_model=list[GameStatOut])
def contest_stats(contest_id: str, db: Session = Depends(get_session)):
    rows = db.execute(
        select(PlayerGameStat, Player.name, Player.number, Player.position,
               Player.class_year, Player.height_inches)
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
    # Per-game FBSO / transition attack splits (same shared classifier as derive-pbp). Absent for
    # contests without PBP -> fbso_*/trans_* stay None (dash / no ATK% FBSO-TRANS column value).
    splits = attack_splits_by_player(pbp_events)
    out: list[GameStatOut] = []
    for pgs, name, number, position, class_year, height_inches in rows:
        line = GameStatOut.model_validate(pgs)
        line.player_name = name
        line.number = number
        line.position = position
        line.class_year = class_year
        line.height_inches = height_inches
        line.set_attempts = set_counts.get(pgs.player_id)
        line.serve_attempts = serve_counts.get(pgs.player_id)
        sh = setter_hit.get(pgs.player_id)
        if sh is not None:
            _sk, _se, satk = sh
            line.setter_hit_attacks = satk
        sp = splits.get(pgs.player_id)
        if sp is not None:
            line.fbso_kills, line.fbso_errors, line.fbso_attacks = sp["fbso"]
            line.trans_kills, line.trans_errors, line.trans_attacks = sp["transition"]
        out.append(line)
    return out
