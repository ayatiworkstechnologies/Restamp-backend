"""RESTAMP Phase 4 — user/role/admin foundation over the existing 28-table schema.

Business rules enforced here (never in the frontend):
- Roles are exactly BUYER / OWNER / BROKER (+ ADMIN via the separate admin_accounts table).
- One account holds exactly ONE current role, so Owner+Broker simultaneity is
  structurally impossible; switching roles appends role_history, never deletes.
- Owner -> Buyer (or any switch) never deletes physical_properties / listings /
  verifications / media / enquiries rows: this service performs zero deletes.
- Switching back to Owner restores management because ownership checks read the
  authoritative user_id columns, which role switches never touch.
- ADMIN is never assignable through role switching or public registration; only
  grant_admin() (itself gated behind the admin-only endpoint) creates it.
"""
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AdminAccount, AuthIdentity, RoleHistory, User, UserCurrentRole

USER_ROLES = ("BUYER", "OWNER", "BROKER")


def create_user(db: Session, display_name: str, avatar_url: str | None = None) -> User:
    user = User(display_name=display_name, avatar_url=avatar_url, status="active")
    db.add(user)
    db.flush()  # assign id without committing (caller owns the transaction)
    db.add(UserCurrentRole(user_id=user.id, role="BUYER"))
    db.add(RoleHistory(user_id=user.id, role="BUYER"))
    return user


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def update_user_display_name(db: Session, user_id: int, display_name: str) -> User:
    cleaned = display_name.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="display_name cannot be empty or whitespace-only",
        )
    if len(cleaned) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="display_name cannot exceed 255 characters",
        )
    user = get_user(db, user_id)
    user.display_name = cleaned
    db.flush()
    return user


def get_current_role(db: Session, user_id: int) -> str | None:
    row = db.get(UserCurrentRole, user_id)
    return row.role if row else None


def list_role_history(db: Session, user_id: int) -> list[RoleHistory]:
    get_user(db, user_id)  # 404 if unknown
    return (
        db.query(RoleHistory)
        .filter(RoleHistory.user_id == user_id)
        .order_by(RoleHistory.id)
        .all()
    )


def set_current_role(db: Session, user_id: int, role: str) -> str:
    """Switch current role; appends history. ADMIN is rejected here by design."""
    if role not in USER_ROLES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown role")
    get_user(db, user_id)
    current = db.get(UserCurrentRole, user_id)
    if current is None:
        db.add(UserCurrentRole(user_id=user_id, role=role))
    else:
        current.role = role
    db.add(RoleHistory(user_id=user_id, role=role))
    return role


def is_admin(db: Session, user_id: int) -> bool:
    return db.get(AdminAccount, user_id) is not None


def grant_admin(db: Session, user_id: int) -> None:
    """Create the admin link. No public flow calls this — only the admin-only endpoint."""
    get_user(db, user_id)
    if db.get(AdminAccount, user_id) is None:
        db.add(AdminAccount(user_id=user_id, admin_role="ADMIN"))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already admin")


def find_user_by_identity(db: Session, provider: str, identifier: str) -> User | None:
    row = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.provider == provider, AuthIdentity.provider_identifier == identifier)
        .first()
    )
    return db.get(User, row.user_id) if row else None


def link_identity(
    db: Session,
    user_id: int,
    provider: str,
    identifier: str,
    verified_by: str | None = None,
) -> AuthIdentity:
    """Attach a provider identity; UNIQUE(user_id, provider) keeps one identity per provider."""
    get_user(db, user_id)
    existing = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.provider == provider, AuthIdentity.provider_identifier == identifier)
        .first()
    )
    if existing is not None:
        if existing.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Identity belongs to another user"
            )
        return existing
    row = AuthIdentity(
        user_id=user_id,
        provider=provider,
        provider_identifier=identifier,
        verified_by=verified_by,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Identity already linked"
        )
    return row
