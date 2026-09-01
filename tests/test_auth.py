"""Account auth: register / login / JWT round-trip / profile / admin gate.

Runs against Postgres via TestClient (skipped when the DB is unreachable). Every account uses the
``TEST_EMAIL_TAG`` so the ``client`` fixture purges them; passkeys and live email are out of scope
here (email send is a log-only no-op locally).
"""
from __future__ import annotations

from conftest import TEST_EMAIL_TAG, requires_db

from vb.auth.security import decode_token

pytestmark = requires_db


def _email(local: str) -> str:
    return f"{local}{TEST_EMAIL_TAG}@example.com"


def _register(client, local: str, password: str = "s3cret-passw0rd", name: str | None = None):
    return client.post(
        "/auth/register",
        json={"email": _email(local), "password": password, "name": name},
    )


def test_register_returns_token_and_user(client):
    r = _register(client, "alice", name="Alice")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token"]
    user = body["user"]
    assert user["email"] == _email("alice")
    assert user["name"] == "Alice"
    assert user["email_verified"] is False   # verification still required
    # The JWT encodes the user id in `sub` and is decodable with the app secret.
    claims = decode_token(body["token"])
    assert claims and int(claims["sub"]) == user["id"]
    assert claims["email"] == _email("alice")


def test_register_rejects_short_password(client):
    r = _register(client, "shorty", password="tiny")
    assert r.status_code == 422   # RegisterIn enforces min_length=8


def test_register_duplicate_email_conflicts(client):
    assert _register(client, "dup").status_code == 201
    r = _register(client, "dup")
    assert r.status_code == 409


def test_login_success_and_wrong_password(client):
    _register(client, "bob", password="correct-horse-battery")
    ok = client.post("/auth/login", json={"email": _email("bob"), "password": "correct-horse-battery"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["token"]

    bad = client.post("/auth/login", json={"email": _email("bob"), "password": "nope-nope-nope"})
    assert bad.status_code == 401

    unknown = client.post("/auth/login", json={"email": _email("ghost"), "password": "whatever12"})
    assert unknown.status_code == 401


def test_login_is_case_insensitive_on_email(client):
    _register(client, "carol", password="password-carol")
    r = client.post(
        "/auth/login",
        json={"email": _email("carol").upper(), "password": "password-carol"},
    )
    assert r.status_code == 200, r.text


def test_me_requires_bearer_and_returns_profile(client):
    token = _register(client, "dave").json()["token"]
    assert client.get("/auth/me").status_code == 401   # no bearer
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == _email("dave")


def test_me_rejects_garbage_token(client):
    r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert r.status_code == 401


def test_patch_me_persists_fantasy_weights(client):
    token = _register(client, "erin").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    weights = {"kills": 2.0, "aces": 3.0}
    r = client.patch("/auth/me", headers=hdr, json={"fantasy_weights": weights})
    assert r.status_code == 200, r.text
    assert r.json()["fantasy_weights"] == weights
    # Round-trips on a fresh /me read.
    assert client.get("/auth/me", headers=hdr).json()["fantasy_weights"] == weights


def test_patch_me_password_change_requires_current(client):
    token = _register(client, "frank", password="original-pass-1").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # Wrong current password is rejected.
    bad = client.patch(
        "/auth/me", headers=hdr,
        json={"current_password": "wrong-one", "new_password": "brand-new-pass-2"},
    )
    assert bad.status_code == 400
    # Correct current password rotates it; the new one then logs in.
    ok = client.patch(
        "/auth/me", headers=hdr,
        json={"current_password": "original-pass-1", "new_password": "brand-new-pass-2"},
    )
    assert ok.status_code == 200, ok.text
    assert client.post(
        "/auth/login", json={"email": _email("frank"), "password": "brand-new-pass-2"}
    ).status_code == 200


def test_admin_endpoints_forbidden_for_normal_user(client):
    token = _register(client, "grace").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get("/admin/users").status_code == 401          # anonymous
    assert client.get("/admin/users", headers=hdr).status_code == 403  # non-admin


def test_email_verification_flow(client):
    """Register, then verify via the token row (locally email send is a log-only no-op)."""
    from sqlalchemy import select

    from vb.db import session_scope
    from vb.models import EmailVerification, User

    token = _register(client, "heidi").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=hdr).json()["email_verified"] is False

    with session_scope() as s:
        uid = s.scalar(select(User.id).where(User.email == _email("heidi")))
        verif = s.scalar(select(EmailVerification).where(EmailVerification.user_id == uid))
        vtoken = verif.token

    ok = client.post(f"/auth/email/verify/{vtoken}")
    assert ok.status_code == 200, ok.text
    assert ok.json()["verified"] is True
    assert client.get("/auth/me", headers=hdr).json()["email_verified"] is True

    # Token is single-use: replaying it is 410 Gone.
    assert client.post(f"/auth/email/verify/{vtoken}").status_code == 410
    # Unknown token is 404.
    assert client.post("/auth/email/verify/deadbeef-not-real").status_code == 404
