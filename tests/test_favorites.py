"""Per-user favorites: add / list / remove / idempotency, plus display enrichment.

Runs against Postgres via TestClient. Uses a synthetic team + player (cleaned up around the test)
so the enrichment join has something real to attach names to.
"""
from __future__ import annotations

import pytest
from conftest import TEST_EMAIL_TAG, requires_db
from sqlalchemy import text

from vb.db import session_scope
from vb.models import Conference, Player, Team

pytestmark = requires_db

_CONF = "_FAV_CONF"
_TEAM = "_FAV_TEAM"
_SEASON = 2103  # sentinel season, won't collide with real data


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
    """Register a tagged user and return (client, headers)."""
    r = client.post(
        "/auth/register",
        json={"email": f"fav{TEST_EMAIL_TAG}@example.com", "password": "favorites-pass-1"},
    )
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


def test_favorites_are_per_user(client, entities):
    """One user's favorite is invisible to another."""
    t1 = client.post(
        "/auth/register",
        json={"email": f"fav-u1{TEST_EMAIL_TAG}@example.com", "password": "user-one-pass"},
    ).json()["token"]
    t2 = client.post(
        "/auth/register",
        json={"email": f"fav-u2{TEST_EMAIL_TAG}@example.com", "password": "user-two-pass"},
    ).json()["token"]
    h1 = {"Authorization": f"Bearer {t1}"}
    h2 = {"Authorization": f"Bearer {t2}"}

    client.post("/favorites", headers=h1,
                json={"entity_type": "team", "entity_id": entities["team"]})
    assert len(client.get("/favorites", headers=h1).json()) == 1
    assert client.get("/favorites", headers=h2).json() == []
