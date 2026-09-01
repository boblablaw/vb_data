"""Admin: user management + runtime settings (MCP token, global AI key).

Every handler requires an admin (per-method gate, mirroring travel-rewards). Secrets are set here
but never read back — the settings endpoint returns only ``has_*`` booleans.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...app_settings import KEY_ANTHROPIC, KEY_MCP_TOKEN, get_setting, set_setting
from ...models import User
from ..deps import get_session, require_admin
from ..schemas import AdminSettingsIn, AdminSettingsOut, AdminUserOut, AdminUserPatchIn

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[AdminUserOut]:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [
        AdminUserOut(
            id=u.id, email=u.email, name=u.name, is_admin=u.is_admin,
            email_verified=u.email_verified,
            created_at=u.created_at.isoformat() if u.created_at else None,
        )
        for u in users
    ]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def patch_user(
    user_id: int,
    body: AdminUserPatchIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AdminUserOut:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if body.is_admin is not None:
        if u.id == admin.id and body.is_admin is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot remove your own admin.")
        u.is_admin = body.is_admin
    if body.email_verified is not None:
        u.email_verified = body.email_verified
    db.commit()
    db.refresh(u)
    return AdminUserOut(
        id=u.id, email=u.email, name=u.name, is_admin=u.is_admin,
        email_verified=u.email_verified,
        created_at=u.created_at.isoformat() if u.created_at else None,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account.")
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    db.delete(u)
    db.commit()


@router.get("/settings", response_model=AdminSettingsOut)
def get_settings(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AdminSettingsOut:
    return AdminSettingsOut(
        has_mcp_token=bool(get_setting(db, KEY_MCP_TOKEN)),
        has_global_ai_key=bool(get_setting(db, KEY_ANTHROPIC)),
    )


@router.put("/settings", response_model=AdminSettingsOut)
def put_settings(
    body: AdminSettingsIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AdminSettingsOut:
    # None => leave unchanged; "" => clear; other => set.
    if body.mcp_token is not None:
        set_setting(db, KEY_MCP_TOKEN, body.mcp_token)
    if body.anthropic_api_key_global is not None:
        set_setting(db, KEY_ANTHROPIC, body.anthropic_api_key_global)
    db.commit()
    return AdminSettingsOut(
        has_mcp_token=bool(get_setting(db, KEY_MCP_TOKEN)),
        has_global_ai_key=bool(get_setting(db, KEY_ANTHROPIC)),
    )
