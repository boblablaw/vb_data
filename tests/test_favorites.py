"""Per-user favorites: add / list / remove / idempotency, plus display enrichment.

Runs against Postgres via TestClient. Uses a synthetic team + player (cleaned up around the test)
so the enrichment join has something real to attach names to.
"""
from __future__ import annotations

import pytest
from conftest import TEST_EMAIL_TAG, requires_db
from sqlalchemy import select, text

from vb.db import session_scope
from vb.models import Conference, Contest, Player, PlayerGameStat, Team, User

pytestmark = requires_db

_CONF = "_FAV_CONF"
_TEAM = "_FAV_TEAM"
_SEASON = 2103  # sentinel season, won't collide with real data


def _verify(email: str) -> None:
    """Flip a freshly-registered account to verified so write features (favorites) are ungated."""
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == email))
        u.email_verified = True


def _wipe():
    with session_scope() as s:
        s.execute(text("DELETE FROM players WHERE season = :y"), {"y": _SEASON})
        s.execute(text("DELETE FROM teams WHERE name = :n"), {"n": _TEAM})
        s.execute(text("DELETE FROM conferences WHERE name = :n"), {"n": _CONF})


@pytest.fixture
def entities():
    _wipe()
    with session_scope() as s:
        c = Conference(name=_CONF, short_name="_FAV")
        s.add(c); s.flush()
        t = Team(name=_TEAM, conference_id=c.id, short_name="_FAV T")
        s.add(t); s.flush()
        p = Player(team_id=t.id, season=_SEASON, name="_FAV Player", position="OH",
                   ncaa_player_id="FAVP1")
        s.add(p); s.flush()
        ids = {"team": t.id, "player": p.id}
    yield ids
    _wipe()


@pytest.fixture
def auth(client):
    """Register a tagged, verified user and return (client, headers)."""
    email = f"fav{TEST_EMAIL_TAG}@example.com"
    r = client.post(
        "/auth/register",
        json={"email": email, "password": "favorites-pass-1"},
    )
    _verify(email)
    token = r.json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_favorites_require_auth(client):
    assert client.get("/favorites").status_code == 401
    assert client.post("/favorites", json={"entity_type": "team", "entity_id": 1}).status_code == 401


def test_add_list_remove_team(auth, entities):
    client, hdr = auth
    # Empty to start.
    assert client.get("/favorites", headers=hdr).json() == []

    add = client.post("/favorites", headers=hdr,
                      json={"entity_type": "team", "entity_id": entities["team"]})
    assert add.status_code == 201, add.text
    out = add.json()
    assert out["entity_type"] == "team"
    assert out["name"] == _TEAM              # enriched with the team display name
    assert out["conference"] == "_FAV"       # short_name preferred

    listed = client.get("/favorites", headers=hdr).json()
    assert len(listed) == 1 and listed[0]["entity_id"] == entities["team"]

    rm = client.delete(f"/favorites/team/{entities['team']}", headers=hdr)
    assert rm.status_code == 204
    assert client.get("/favorites", headers=hdr).json() == []


def test_add_player_enriches_team_name(auth, entities):
    client, hdr = auth
    add = client.post("/favorites", headers=hdr,
                      json={"entity_type": "player", "entity_id": entities["player"]})
    assert add.status_code == 201, add.text
    out = add.json()
    assert out["name"] == "_FAV Player"
    assert out["position"] == "OH"
    assert out["team"] == _TEAM


def test_add_is_idempotent(auth, entities):
    client, hdr = auth
    body = {"entity_type": "team", "entity_id": entities["team"]}
    assert client.post("/favorites", headers=hdr, json=body).status_code == 201
    # Second add must not create a duplicate row (unique constraint on user+type+id).
    assert client.post("/favorites", headers=hdr, json=body).status_code == 201
    assert len(client.get("/favorites", headers=hdr).json()) == 1


def test_invalid_entity_type_rejected(auth):
    client, hdr = auth
    r = client.post("/favorites", headers=hdr, json={"entity_type": "coach", "entity_id": 1})
    assert r.status_code == 400


# --------------------------------------------------------------------- conferences

_CONF_A = "_FAV_CONF_A"
_CONF_B = "_FAV_CONF_B"
_T1, _T2, _T3 = "_FAV_WORLD_T1", "_FAV_WORLD_T2", "_FAV_WORLD_T3"
_C1, _C2 = "_FAVWC1", "_FAVWC2"


def _wipe_world():
    with session_scope() as s:
        s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": _SEASON})
        s.execute(text("DELETE FROM contests WHERE contest_id IN (:a, :b)"), {"a": _C1, "b": _C2})
        s.execute(text("DELETE FROM players WHERE season = :y"), {"y": _SEASON})
        s.execute(
            text("DELETE FROM teams WHERE name IN (:a, :b, :c)"),
            {"a": _T1, "b": _T2, "c": _T3},
        )
        s.execute(
            text("DELETE FROM conferences WHERE name IN (:a, :b)"), {"a": _CONF_A, "b": _CONF_B}
        )


@pytest.fixture
def conf_world():
    """Two conferences: A={T1,T2}, B={T3}. T1 beat T2 (conf) and T3 (inter-conf); a T1 player
    logged a stat line in the T1-vs-T3 contest."""
    _wipe_world()
    with session_scope() as s:
        a = Conference(name=_CONF_A, short_name="_FA"); s.add(a)
        b = Conference(name=_CONF_B, short_name="_FB"); s.add(b); s.flush()
        t1 = Team(name=_T1, conference_id=a.id, short_name="T1", avca_rank=5)
        t2 = Team(name=_T2, conference_id=a.id, short_name="T2")
        t3 = Team(name=_T3, conference_id=b.id, short_name="T3")
        s.add_all([t1, t2, t3]); s.flush()
        # T1 3-0 over T2 (conference), T1 3-1 over T3 (inter-conference).
        s.add_all([
            Contest(contest_id=_C1, season=_SEASON, date="2103-09-01",
                    home_team_id=t1.id, away_team_id=t2.id, home_sets_won=3, away_sets_won=0),
            Contest(contest_id=_C2, season=_SEASON, date="2103-09-05",
                    home_team_id=t1.id, away_team_id=t3.id, home_sets_won=3, away_sets_won=1),
        ])
        p = Player(team_id=t1.id, season=_SEASON, name="_FAV World Player", position="OH",
                   ncaa_player_id="FAVWP1")
        s.add(p); s.flush()
        s.add(PlayerGameStat(contest_id=_C2, player_id=p.id, team_id=t1.id, season=_SEASON, kills=12))
        ids = {"conf_a": a.id, "conf_b": b.id, "t1": t1.id, "t2": t2.id, "t3": t3.id,
               "player": p.id, "contest": _C2}
    yield ids
    _wipe_world()


def test_add_list_remove_conference(auth, conf_world):
    client, hdr = auth
    add = client.post("/favorites", headers=hdr,
                      json={"entity_type": "conference", "entity_id": conf_world["conf_a"]})
    assert add.status_code == 201, add.text
    out = add.json()
    assert out["entity_type"] == "conference"
    assert out["name"] == _CONF_A               # enriched with the conference name
    assert out["team_short"] == "_FA"           # short_name carried through

    listed = client.get("/favorites", headers=hdr).json()
    assert len(listed) == 1 and listed[0]["entity_id"] == conf_world["conf_a"]

    rm = client.delete(f"/favorites/conference/{conf_world['conf_a']}", headers=hdr)
    assert rm.status_code == 204
    assert client.get("/favorites", headers=hdr).json() == []


def test_conference_summary(client, conf_world):
    r = client.get(f"/conferences/{conf_world['conf_a']}/summary", params={"season": _SEASON})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["team_count"] == 2
    assert d["ranked_count"] == 1               # T1 carries an AVCA rank
    # Standings ordered by conference W-L: T1 (1-0) ahead of T2 (0-1).
    order = [row["team"] for row in d["standings"]]
    assert order == [_T1, _T2]
    assert d["standings"][0]["conf_wins"] == 1 and d["standings"][0]["conf_losses"] == 0
    # T1's win over T3 is an inter-conference win (T3 is in conference B).
    assert d["interconf_wins"] == 1 and d["interconf_losses"] == 0
    assert d["overall_wins"] == 2 and d["overall_losses"] == 1  # T1 2-0, T2 0-1


def test_conference_summary_unknown_404(client):
    assert client.get("/conferences/99999999/summary").status_code == 404


def test_favorite_player_contests(auth, conf_world):
    client, hdr = auth
    # No favorite players yet → empty.
    assert client.get("/favorites/contests", headers=hdr,
                      params={"season": _SEASON}).json() == {"contest_ids": [], "team_ids": []}
    # Favorite the player who logged a stat line in contest _C2 (on team t1).
    client.post("/favorites", headers=hdr,
                json={"entity_type": "player", "entity_id": conf_world["player"]})
    got = client.get("/favorites/contests", headers=hdr, params={"season": _SEASON}).json()
    assert got["contest_ids"] == [conf_world["contest"]]     # played game they appeared in
    assert got["team_ids"] == [conf_world["t1"]]             # their team (to match upcoming games)


def test_favorite_player_contests_requires_auth(client):
    assert client.get("/favorites/contests").status_code == 401


def test_favorites_are_per_user(client, entities):
    """One user's favorite is invisible to another."""
    e1 = f"fav-u1{TEST_EMAIL_TAG}@example.com"
    e2 = f"fav-u2{TEST_EMAIL_TAG}@example.com"
    t1 = client.post(
        "/auth/register", json={"email": e1, "password": "user-one-pass"},
    ).json()["token"]
    t2 = client.post(
        "/auth/register", json={"email": e2, "password": "user-two-pass"},
    ).json()["token"]
    _verify(e1)
    _verify(e2)
    h1 = {"Authorization": f"Bearer {t1}"}
    h2 = {"Authorization": f"Bearer {t2}"}

    client.post("/favorites", headers=h1,
                json={"entity_type": "team", "entity_id": entities["team"]})
    assert len(client.get("/favorites", headers=h1).json()) == 1
    assert client.get("/favorites", headers=h2).json() == []
