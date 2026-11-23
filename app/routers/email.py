"""RESTAMP contact email — the caller's own address, verification and preferences.

  GET    /users/me/email                 status (address, verified, preferences)
  PUT    /users/me/email                 set or change the address; starts verification
  POST   /users/me/email/verification    send a new code for the unverified address
  POST   /users/me/email/verify          confirm the address with the emailed code
  PUT    /users/me/email/preferences     optional-email preferences
  DELETE /users/me/email                 remove the address (204)

Authentication: `get_current_user` (valid JWT AND an active account — a
suspended/closed account gets the usual 403). Strictly user-scoped: no
endpoint accepts a user id or acts on another account, so nobody can read or
change someone else's address or preferences. No enumeration: no response
depends on whether an address is used by another account. Verification
failures share one static message. GET never writes; writes commit only
after the service call succeeded — with one deliberate exception: a WRONG
code commits its attempt counter, so guesses are limited.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import schemas
from ..config import is_development
from ..db import get_db
from ..security import get_current_user
from ..services import email as email_svc

router = APIRouter(prefix="/users/me/email", tags=["email"])


def _out(state: dict, code: str | None = None) -> schemas.EmailStatusOut:
    # The code is returned ONLY in an explicit development environment.
    return schemas.EmailStatusOut(**state, debug_code=code if (code and is_development()) else None)


@router.get("", response_model=schemas.EmailStatusOut)
def my_email(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _out(email_svc.get_status(db, int(user.id)))


@router.put("", response_model=schemas.EmailStatusOut)
def set_my_email(body: schemas.EmailSetIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """422 "Invalid email address"; 429 when verification requests are rate-limited."""
    try:
        state, code = email_svc.set_email(db, int(user.id), body.email)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid email address")
    except IntegrityError:
        # Two first-time saves for the same account at the same moment: one
        # wins, the other is told to retry (never a 500, nothing half-written).
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Please try again")
    db.commit()
    return _out(state, code)


@router.post("/verification", response_model=schemas.EmailStatusOut)
def resend_my_verification(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """409 when there is no address; 429 when rate-limited; no-op when already verified."""
    state, code = email_svc.resend_code(db, int(user.id))
    db.commit()
    return _out(state, code)


@router.post("/verify", response_model=schemas.EmailStatusOut)
def verify_my_email(body: schemas.EmailVerifyIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """One generic 422 for every failure (wrong, expired, used, exhausted,
    another account's or a previous address's code)."""
    verified = email_svc.verify_code(db, int(user.id), body.code)
    db.commit()  # also persists the attempt counter of a wrong guess
    if not verified:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=email_svc.INVALID_CODE)
    return _out(email_svc.get_status(db, int(user.id)))


@router.put("/preferences", response_model=schemas.EmailStatusOut)
def set_my_email_preferences(
    body: schemas.EmailPreferences, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    """Optional notification email only (enquiries / visits / listings).
    Verification and account-security email cannot be switched off."""
    state = email_svc.set_preferences(
        db, int(user.id), enquiries=body.enquiries, visits=body.visits, listings=body.listings
    )
    db.commit()
    return _out(state)


@router.delete("", status_code=204)
def remove_my_email(db: Session = Depends(get_db), user=Depends(get_current_user)):
    email_svc.remove_email(db, int(user.id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
