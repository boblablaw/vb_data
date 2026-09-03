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


def _verify(local: str) -> None:
    """Flip an account to verified (locally the emailed link is a log-only no-op)."""
    from sqlalchemy import select

    from vb.db import session_scope
    from vb.models import User

    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == _email(local)))
        u.email_verified = True


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
    _verify("erin")   # saving fantasy weights is gated behind a verified email
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


def _promote_admin(local: str) -> None:
    from sqlalchemy import select

    from vb.db import session_scope
    from vb.models import User

    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == _email(local)))
        u.is_admin = True


def test_signups_metrics_gated(client):
    token = _register(client, "ivan").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get("/admin/metrics/signups").status_code == 401          # anonymous
    assert client.get("/admin/metrics/signups", headers=hdr).status_code == 403  # non-admin


def test_signups_metrics_series_for_admin(client):
    import datetime
    from itertools import pairwise

    token = _register(client, "judy").json()["token"]
    _promote_admin("judy")
    hdr = {"Authorization": f"Bearer {token}"}

    r = client.get("/admin/metrics/signups", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total"] >= 1
    days = body["days"]
    assert days, "expected at least one day in the series"
    # Series is contiguous (one entry per calendar day) and ends today (UTC, as stored).
    assert days[-1]["date"] == datetime.datetime.now(datetime.UTC).date().isoformat()
    parsed = [datetime.date.fromisoformat(d["date"]) for d in days]
    for a, b in pairwise(parsed):
        assert (b - a).days == 1, "days must be consecutive with no gaps"
    # Cumulative is non-decreasing and finishes at the count of dated accounts (<= total).
    cums = [d["cumulative"] for d in days]
    assert cums == sorted(cums)
    assert cums[-1] <= body["total"]
    # Today's signup (judy) is reflected.
    assert days[-1]["new"] >= 1


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


# ------------------------------------------------------------------ magic-link sign-in


def _link_token(local: str) -> str:
    """Read the most recent EmailVerification token for an account (email send is a local no-op)."""
    from sqlalchemy import select

    from vb.db import session_scope
    from vb.models import EmailVerification, User

    with session_scope() as s:
        uid = s.scalar(select(User.id).where(User.email == _email(local)))
        return s.scalar(
            select(EmailVerification.token).where(EmailVerification.user_id == uid)
        )


def test_magic_link_send_for_existing_email_creates_token(client):
    _register(client, "mona")
    r = client.post("/auth/link/send", json={"email": _email("mona")})
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "sent"}
    assert _link_token("mona")   # a token row now exists


def test_magic_link_send_unknown_email_still_202_no_leak(client):
    """Unknown emails must not be distinguishable (no account enumeration)."""
    from sqlalchemy import func, select

    from vb.db import session_scope
    from vb.models import EmailVerification, User

    r = client.post("/auth/link/send", json={"email": _email("nobody-here")})
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "sent"}
    with session_scope() as s:
        # No such account, and therefore no token row was minted for it.
        assert s.scalar(select(User.id).where(User.email == _email("nobody-here"))) is None
        tagged = (
            select(func.count())
            .select_from(EmailVerification)
            .join(User, User.id == EmailVerification.user_id)
            .where(User.email == _email("nobody-here"))
        )
        assert s.scalar(tagged) == 0


def test_magic_link_consume_signs_in_and_verifies(client):
    _register(client, "nate")
    client.post("/auth/link/send", json={"email": _email("nate")})
    token = _link_token("nate")

    r = client.post("/auth/link/consume", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]                       # a usable JWT comes back
    assert body["user"]["email_verified"] is True
    claims = decode_token(body["token"])
    assert claims and claims["email"] == _email("nate")

    # Single-use: replaying the same token is 410 Gone.
    assert client.post("/auth/link/consume", json={"token": token}).status_code == 410


def test_magic_link_consume_unknown_token_404(client):
    assert client.post(
        "/auth/link/consume", json={"token": "deadbeef-not-a-real-token"}
    ).status_code == 404


def test_magic_link_consume_expired_token_410(client):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from vb.db import session_scope
    from vb.models import EmailVerification, User

    _register(client, "opal")
    client.post("/auth/link/send", json={"email": _email("opal")})
    token = _link_token("opal")
    with session_scope() as s:
        uid = s.scalar(select(User.id).where(User.email == _email("opal")))
        row = s.scalar(select(EmailVerification).where(EmailVerification.user_id == uid))
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)

    assert client.post("/auth/link/consume", json={"token": token}).status_code == 410


# ------------------------------------------------------------------ verification gating


def test_unverified_user_blocked_from_fantasy_weights(client):
    token = _register(client, "pia").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # Saving fantasy weights is gated…
    r = client.patch("/auth/me", headers=hdr, json={"fantasy_weights": {"kills": 1.0}})
    assert r.status_code == 403, r.text
    # …but a plain name change is still allowed while unverified.
    ok = client.patch("/auth/me", headers=hdr, json={"name": "Pia"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "Pia"


def test_unverified_user_blocked_from_ask(client):
    token = _register(client, "quinn").json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # The verified-email gate fires before any Anthropic call.
    r = client.post("/ask", headers=hdr, json={"question": "who leads in kills?"})
    assert r.status_code == 403, r.text
