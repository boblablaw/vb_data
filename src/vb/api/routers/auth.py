"""Accounts: register / login / profile + email verification.

Open signup (email verification still required). Stateless JWT bearer auth — logout is client-side.
The first-ever registered user becomes an admin. Mirrors travel-rewards' AuthController shape.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...auth import email as email_svc
from ...auth.security import create_token, hash_password, verify_password
from ...models import EmailVerification, User
from ..deps import get_session, require_user
from ..schemas import AuthOut, EmailIn, LoginIn, RegisterIn, TokenIn, UpdateMeIn, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_verification(db: Session, user: User) -> None:
    """Delete any prior tokens for the user, mint a fresh 24h token, and send it."""
    db.query(EmailVerification).filter(EmailVerification.user_id == user.id).delete()
    token = email_svc.new_token()
    db.add(EmailVerification(user_id=user.id, token=token, expires_at=email_svc.token_expiry()))
    db.commit()
    email_svc.send_verification(user.email, token)


def _issue_login_link(db: Session, user: User) -> None:
    """Mint a fresh single-use token (same table as verification) and email a magic sign-in link."""
    db.query(EmailVerification).filter(EmailVerification.user_id == user.id).delete()
    token = email_svc.new_token()
    db.add(EmailVerification(user_id=user.id, token=token, expires_at=email_svc.token_expiry()))
    db.commit()
    email_svc.send_login_link(user.email, token)


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, db: Session = Depends(get_session)) -> AuthOut:
    email = body.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists.")
    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    user = User(
        email=email,
        password_hash=hash_password(body.password),
        name=body.name,
        is_admin=is_first,        # first-ever user bootstraps as admin
        email_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _issue_verification(db, user)
    token = create_token(user.id, user.email, user.is_admin)
    return AuthOut(token=token, user=UserOut.from_user(user))


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn, db: Session = Depends(get_session)) -> AuthOut:
    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")
    token = create_token(user.id, user.email, user.is_admin)
    return AuthOut(token=token, user=UserOut.from_user(user))


# ------------------------------------------------------------------ magic-link sign-in (public)
@router.post("/link/send", status_code=status.HTTP_202_ACCEPTED)
def request_login_link(body: EmailIn, db: Session = Depends(get_session)) -> dict:
    """Email a one-time sign-in link. Always 202 — never reveals whether the account exists."""
    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        _issue_login_link(db, user)
    return {"status": "sent"}


@router.post("/link/consume", response_model=AuthOut)
def consume_login_link(body: TokenIn, db: Session = Depends(get_session)) -> AuthOut:
    """Consume a magic link: verifies the email and signs the user in (mints a JWT)."""
    row = db.scalar(select(EmailVerification).where(EmailVerification.token == body.token))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid sign-in link.")
    now = datetime.now(UTC)
    if row.used_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "This link has already been used.")
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < now:
        raise HTTPException(status.HTTP_410_GONE, "This link has expired.")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.")
    user.email_verified = True
    row.used_at = now
    db.commit()
    token = create_token(user.id, user.email, user.is_admin)
    return AuthOut(token=token, user=UserOut.from_user(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(require_user)) -> UserOut:
    return UserOut.from_user(user)


@router.patch("/me", response_model=UserOut)
def update_me(
    body: UpdateMeIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> UserOut:
    if body.name is not None:
        user.name = body.name
    if body.fantasy_weights is not None:
        if not user.email_verified:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Verify your email to save fantasy weights."
            )
        user.fantasy_weights = body.fantasy_weights
    if body.prefs is not None:
        user.prefs = body.prefs
    if body.new_password is not None:
        if not verify_password(body.current_password or "", user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
        user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)
    return UserOut.from_user(user)


# ------------------------------------------------------------------ email verification
email_router = APIRouter(prefix="/auth/email", tags=["auth"])


@email_router.post("/send", status_code=status.HTTP_202_ACCEPTED)
def send_verification(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> dict:
    if user.email_verified:
        return {"status": "already_verified"}
    _issue_verification(db, user)
    return {"status": "sent"}


@email_router.post("/verify/{token}")
def verify_email(token: str, db: Session = Depends(get_session)) -> dict:
    row = db.scalar(select(EmailVerification).where(EmailVerification.token == token))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown verification token.")
    now = datetime.now(UTC)
    if row.used_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "This link has already been used.")
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < now:
        raise HTTPException(status.HTTP_410_GONE, "This link has expired.")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.")
    user.email_verified = True
    row.used_at = now
    db.commit()
    return {"verified": True, "email": user.email}
