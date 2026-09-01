"""Password hashing (bcrypt) and JWT bearer tokens.

Mirrors the travel-rewards convention: BCrypt password hashes + stateless HS256 JWTs carried in
``Authorization: Bearer <token>``. There is no server-side session/token store — logout is a
client-side token drop.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from ..config import settings

_ALG = "HS256"


def _to_bytes(password: str) -> bytes:
    # bcrypt only considers the first 72 bytes and raises on longer inputs; truncate to match.
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_to_bytes(password), password_hash.encode("ascii"))
    except Exception:
        return False


def create_token(user_id: int, email: str, is_admin: bool) -> str:
    """Mint a signed JWT for a user. Claims: sub (user id), email, admin, iat, exp."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "admin": bool(is_admin),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expiry_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


def decode_token(token: str) -> dict | None:
    """Return the JWT claims, or None if the token is invalid/expired."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALG])
    except Exception:
        return None
