"""Passkeys / WebAuthn (py-webauthn).

Registration is auth-required (add a passkey to your account); authentication is public and mints
the same JWT as password login. Challenges are held in-memory keyed by a random requestId and
cleared on finish — fine for the single vb-api instance (same approach as travel-rewards).
"""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ...auth.security import create_token
from ...config import settings
from ...models import PasskeyCredential, User
from ..deps import get_session, require_user
from ..schemas import AuthOut, UserOut

router = APIRouter(prefix="/auth/passkey", tags=["passkeys"])

# requestId -> challenge bytes. In-memory; cleared on finish.
_pending_reg: dict[str, bytes] = {}
_pending_auth: dict[str, bytes] = {}


def _rid() -> str:
    return secrets.token_urlsafe(16)


def _user_handle(user: User) -> bytes:
    return str(user.id).encode()


# ------------------------------------------------------------------ registration (auth required)
@router.post("/register/start")
def register_start(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> dict:
    existing = db.scalars(
        select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
    ).all()
    # One passkey per account: remove the existing one before adding a new one (the UI enforces this
    # too, but guard the API so the two-passkey state can't be reached).
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Remove your existing passkey before adding a new one.",
        )
    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=_user_handle(user),
        user_name=user.email,
        user_display_name=user.name or user.email,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    request_id = _rid()
    _pending_reg[request_id] = options.challenge
    return {"request_id": request_id, "options": json.loads(options_to_json(options))}


@router.post("/register/finish", response_model=None)
def register_finish(
    body: dict = Body(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> dict:
    request_id = body.get("request_id")
    credential = body.get("credential")
    challenge = _pending_reg.pop(request_id, None)
    if challenge is None or credential is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No pending registration.")
    try:
        verification = verify_registration_response(
            credential=json.dumps(credential),
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
        )
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Passkey registration failed: {e}")
    cred = PasskeyCredential(
        user_id=user.id,
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        user_handle=bytes_to_base64url(_user_handle(user)),
        display_name=(body.get("display_name") or "Passkey"),
    )
    db.add(cred)
    db.commit()
    return {"status": "ok"}


@router.get("/credentials")
def list_credentials(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    creds = db.scalars(
        select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
    ).all()
    return [
        {
            "id": c.id,
            "display_name": c.display_name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in creds
    ]


@router.delete("/credentials/{cred_id}")
def delete_credential(
    cred_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> dict:
    cred = db.get(PasskeyCredential, cred_id)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Passkey not found.")
    db.delete(cred)
    db.commit()
    return {"status": "deleted"}


# ------------------------------------------------------------------ authentication (public)
@router.post("/login/start")
def login_start(
    body: dict = Body(default={}),
    db: Session = Depends(get_session),
) -> dict:
    allow: list[PublicKeyCredentialDescriptor] = []
    email = (body or {}).get("email")
    if email:
        user = db.scalar(select(User).where(User.email == email.lower().strip()))
        if user:
            creds = db.scalars(
                select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
            ).all()
            allow = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
                for c in creds
            ]
    options = generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request_id = _rid()
    _pending_auth[request_id] = options.challenge
    return {"request_id": request_id, "options": json.loads(options_to_json(options))}


@router.post("/login/finish", response_model=AuthOut)
def login_finish(
    body: dict = Body(...),
    db: Session = Depends(get_session),
) -> AuthOut:
    request_id = body.get("request_id")
    credential = body.get("credential")
    challenge = _pending_auth.pop(request_id, None)
    if challenge is None or credential is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No pending authentication.")
    cred_id = credential.get("id") or credential.get("rawId")
    stored = db.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == cred_id)
    )
    if stored is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown passkey.")
    try:
        verification = verify_authentication_response(
            credential=json.dumps(credential),
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=stored.public_key,
            credential_current_sign_count=stored.sign_count,
            require_user_verification=False,
        )
    except Exception as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Passkey login failed: {e}")
    stored.sign_count = verification.new_sign_count
    stored.last_used_at = datetime.now(UTC)
    user = db.get(User, stored.user_id)
    db.commit()
    token = create_token(user.id, user.email, user.is_admin)
    return AuthOut(token=token, user=UserOut.from_user(user))
