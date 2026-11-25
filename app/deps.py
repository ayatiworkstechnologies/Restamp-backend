from __future__ import annotations
"""RESTAMP Phase 4 — backend authorization (never trust frontend role checks).

- Authentication (who you are) comes from app.security; this module decides
  what you may do (authorization), kept strictly separate.
- Ownership checks read the authoritative user_id columns, which role switches
  never modify — so Owner->Buyer->Owner round-trips preserve management rights.
"""
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import PhysicalProperty
from .security import (
    ensure_active_user,
    get_current_user,
    get_current_user_id,
    get_session_amr,
    is_fixed_otp_session,
)
from .services import admin_rbac, capabilities, users_roles


def require_role(*allowed: str, detail: str | None = None):
    def _check(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)) -> int:
        # Suspended/closed accounts are refused before any role decision.
        ensure_active_user(db, user_id)
        role = users_roles.get_current_role(db, user_id)
        if role not in allowed:
            # Actionable detail (caller's own state only, no data leak):
            # e.g. "OWNER role required" tells a 403 recipient exactly why
            # their valid token was refused instead of a bare "Forbidden".
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=detail or f"{'/'.join(allowed)} role required",
            )
        if role in capabilities.POSTER_TYPES:
            # Fail closed on Owner/Dealer exclusivity: a poster mode is usable
            # only when the account's poster type is unset or the same type
            # (mode switches already refuse this; legacy rows are re-checked).
            poster_type = capabilities.get_poster_type(db, user_id)
            if not capabilities.allows(poster_type, role):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=detail or f"{'/'.join(allowed)} role required",
                )
        return user_id

    return _check


def require_buyer(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)) -> int:
    """Buyer capability: EVERY active account has it, in every app mode
    (BUYER, OWNER or BROKER). The mode never removes buyer features."""
    ensure_active_user(db, user_id)
    return user_id


# Buyer activity (wishlist, enquiries, visits, viewed, call taps, owner
# contact) is a capability of every account; agents (BROKER mode) keep their
# area gate inside the endpoints. Kept as a separate name for compatibility.
require_buyer_or_agent = require_buyer
require_owner = require_role("OWNER")
require_broker = require_role("BROKER")
# Listing management (/owner/*) for both poster types: Owner mode or Dealer
# (BROKER) mode, with the Owner/Dealer exclusivity check of require_role.
# Ownership stays the per-object user_id check in the services; restricted
# (paid) operations additionally call capabilities.ensure_can_post. The 403
# text is unchanged for existing clients.
require_poster = require_role("OWNER", "BROKER", detail="OWNER role required")


def _refuse_fixed_otp_session(amr: str | None) -> None:
    """Fixed-OTP sessions are ordinary-user sessions only: same static 403
    as a non-admin, checked BEFORE the admin lookup so the response never
    depends on whether the account is an admin."""
    if is_fixed_otp_session(amr):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_admin(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    amr: str | None = Depends(get_session_amr),
) -> int:
    _refuse_fixed_otp_session(amr)
    ensure_active_user(db, user_id)
    if not users_roles.is_admin(db, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return user_id


def require_admin_permission(permission: str):
    """Admin AND a specific permission of their admin_role (admin_rbac).

    Same identity gate as require_admin (active account + admin_accounts row,
    read from the DB on every request — never from the token or the client);
    then the role's permission set decides. An unknown role has no
    permissions (fail closed). Non-admins get the same static 403 as
    require_admin, so the endpoint's permission name is only ever shown to
    admins. A fixed-OTP session (amr=fixed_otp) gets that same 403 even if
    the account was granted admin after the session was issued.
    """
    if permission not in admin_rbac.PERMISSIONS:  # programming error, not a request error
        raise ValueError(f"Unknown admin permission: {permission}")

    def _check(
        db: Session = Depends(get_db),
        user_id: int = Depends(get_current_user_id),
        amr: str | None = Depends(get_session_amr),
    ) -> int:
        _refuse_fixed_otp_session(amr)
        ensure_active_user(db, user_id)
        admin_role = users_roles.get_admin_role(db, user_id)
        if admin_role is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        if not admin_rbac.has_permission(admin_role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"{permission} permission required"
            )
        return user_id

    return _check


def require_owner_of_physical_property(
    physical_property_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(require_owner),
):
    """Ownership proof: caller must hold the OWNER role AND own the record."""
    row = db.get(PhysicalProperty, physical_property_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return row


__all__ = [
    "get_current_user",
    "get_current_user_id",
    "require_admin",
    "require_admin_permission",
    "require_broker",
    "require_buyer",
    "require_owner",
    "require_owner_of_physical_property",
    "require_poster",
    "require_role",
]
