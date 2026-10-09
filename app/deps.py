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
from .security import get_current_user, get_current_user_id
from .services import users_roles


def require_role(*allowed: str):
    def _check(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)) -> int:
        role = users_roles.get_current_role(db, user_id)
        if role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user_id

    return _check


require_buyer = require_role("BUYER")
require_owner = require_role("OWNER")
require_broker = require_role("BROKER")


def require_admin(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)) -> int:
    if not users_roles.is_admin(db, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return user_id


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
    "require_broker",
    "require_buyer",
    "require_owner",
    "require_owner_of_physical_property",
    "require_role",
]
