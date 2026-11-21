"""RESTAMP push notifications — the caller's own devices and preferences.

  GET    /users/me/push                      status (available, devices, preferences)
  PUT    /users/me/push/preferences           optional-push preferences
  POST   /users/me/push/devices               register (or refresh) a device
  DELETE /users/me/push/devices/{device_id}   deactivate a device (204)

Authentication: `get_current_user` (valid JWT AND an active account — a
suspended/closed account gets the usual 403). Strictly user-scoped: no
endpoint accepts a user id or acts on another account. The raw Expo token is
accepted only in the register request body (over TLS), stored only in
encrypted form, and never returned, logged or echoed: a registration response
carries only the masked prefix. A token that is already registered takes over
its single row, so one physical device can never deliver for two accounts.
Push preferences are INDEPENDENT of email and in-app preferences.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..security import get_current_user
from ..services import push as push_svc

router = APIRouter(prefix="/users/me/push", tags=["push"])


@router.get("", response_model=schemas.PushStatusOut)
def my_push(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return push_svc.get_status(db, int(user.id))


@router.put("/preferences", response_model=schemas.PushStatusOut)
def set_my_push_preferences(
    body: schemas.PushPreferences, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    """Optional-push preferences only (enquiries / visits / listings).
    Mandatory account pushes have no switch."""
    state = push_svc.set_preferences(
        db, int(user.id), enquiries=body.enquiries, visits=body.visits, listings=body.listings
    )
    db.commit()
    return state


@router.post("/devices", response_model=schemas.PushDeviceOut, status_code=201)
def register_my_device(
    body: schemas.PushDeviceCreateIn, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    """422 for an invalid Expo token or platform (one static message)."""
    try:
        device = push_svc.register_device(
            db, int(user.id), body.token, body.platform,
            app_version=body.app_version, device_name=body.device_name,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid push token or platform"
        )
    db.commit()
    return device


@router.delete("/devices/{device_id}", status_code=204)
def deactivate_my_device(
    device_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    """Idempotent: the caller logs a device out. 404 for another user's or a
    missing device."""
    push_svc.deactivate_device(db, int(user.id), device_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
