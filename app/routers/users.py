"""RESTAMP Phase 4 — user/role/admin routes plus minimal authorization-proof endpoints.

Role-scoped demo endpoints (buyer/welcome, owner/*, broker/scope) exist ONLY to
prove backend authorization with real data; they are not property, enquiry, or
broker-business features. No property CRUD is implemented here.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import deps, schemas
from ..db import get_db
from ..models import AuthIdentity, BrokerPostcodeAccessCurrent
from ..security import get_current_user, get_current_user_id
from ..services import users_roles

router = APIRouter(tags=["users"])


@router.post("/users", response_model=schemas.UserOut, status_code=201)
def create_user(body: schemas.UserCreate, db: Session = Depends(get_db)):
    """Public registration creates a BUYER only. No role choice, no admin path."""
    user = users_roles.create_user(db, body.display_name, body.avatar_url)
    db.commit()
    db.refresh(user)
    return schemas.UserOut(
        id=user.id, display_name=user.display_name, avatar_url=user.avatar_url, status=user.status
    )


@router.get("/users/{user_id}", response_model=schemas.UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    caller: int = Depends(get_current_user_id),
):
    if caller != user_id and not users_roles.is_admin(db, caller):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    user = users_roles.get_user(db, user_id)
    return schemas.UserOut(
        id=user.id, display_name=user.display_name, avatar_url=user.avatar_url, status=user.status
    )


@router.patch("/users/me", response_model=schemas.MeOut)
def update_my_profile(
    body: schemas.UserUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    updated = users_roles.update_user_display_name(db, user.id, body.display_name)
    db.commit()
    db.refresh(updated)
    providers = [
        r.provider
        for r in db.query(AuthIdentity).filter(AuthIdentity.user_id == updated.id).all()
    ]
    return schemas.MeOut(
        id=updated.id,
        display_name=updated.display_name,
        role=users_roles.get_current_role(db, updated.id),
        providers=providers,
        is_admin=users_roles.is_admin(db, updated.id),
    )


@router.get("/users/me/role", response_model=schemas.RoleOut)
def my_role(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return schemas.RoleOut(user_id=user.id, role=users_roles.get_current_role(db, user.id))


@router.post("/users/me/role", response_model=schemas.RoleOut)
def set_my_role(
    body: schemas.RoleSet, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    role = users_roles.set_current_role(db, user.id, body.role)
    db.commit()
    return schemas.RoleOut(user_id=user.id, role=role)


@router.get("/users/me/history")
def my_history(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return [
        {"role": r.role, "changed_at": r.changed_at.isoformat() if r.changed_at else None}
        for r in users_roles.list_role_history(db, user.id)
    ]


# --- Authorization proofs (not features) ------------------------------------


@router.get("/buyer/welcome")
def buyer_only(user_id: int = Depends(deps.require_buyer)):
    return {"message": "buyer ok", "user_id": user_id}


@router.get("/owner/vault")
def owner_only(user_id: int = Depends(deps.require_owner)):
    return {"message": "owner ok", "user_id": user_id}


@router.get("/owner/check-ownership/{physical_property_id}")
def owner_owns(row=Depends(deps.require_owner_of_physical_property)):
    return {"message": "ownership confirmed", "physical_property_id": row.id}


@router.get("/broker/scope")
def broker_scope(db: Session = Depends(get_db), user_id: int = Depends(deps.require_broker)):
    rows = (
        db.query(BrokerPostcodeAccessCurrent)
        .filter(BrokerPostcodeAccessCurrent.broker_user_id == user_id)
        .all()
    )
    return {
        "broker_user_id": user_id,
        "postcodes": [r.postcode_id for r in rows],
    }


@router.get("/admin/ping")
def admin_ping(user_id: int = Depends(deps.require_admin)):
    return {"message": "admin ok", "user_id": user_id}


@router.post("/admin/grants", status_code=201)
def admin_grant(
    body: schemas.AdminGrant, db: Session = Depends(get_db), _admin: int = Depends(deps.require_admin)
):
    """No public admin registration: caller must already be an admin."""
    users_roles.grant_admin(db, body.user_id)
    db.commit()
    return {"message": "admin granted", "user_id": body.user_id}
