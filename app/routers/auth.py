"""RESTAMP Phase 4 — authentication routes (phone OTP + Google + self profile).

Phone (registration-capable): unknown phone identities create a user on first
successful verification (standard OTP registration), default role BUYER.
Google: unknown identities NEVER auto-create (404 GOOGLE_UNKNOWN); the client
must onboard through an approved flow before the Google identity can be linked.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import schemas
from ..config import otp_display_permitted
from ..db import get_db
from ..models import AuthIdentity
from ..security import create_access_token, ensure_active_user, get_current_user
from ..services import google as google_svc
from ..services import users_roles
from ..services import otp as otp_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_for(db: Session, user_id: int) -> schemas.TokenOut:
    # Every login path (OTP, Google) issues its token here: a suspended or
    # closed account gets 403 "Account disabled" instead of a session.
    ensure_active_user(db, user_id)
    role = users_roles.get_current_role(db, user_id)
    token, ttl = create_access_token(user_id, role)
    return schemas.TokenOut(
        access_token=token, expires_in=ttl, user_id=user_id, role=role
    )


@router.post("/otp/request")
def otp_request(body: schemas.OTPRequest):
    """Request the login OTP. Indian mobiles only (schemas gate); never logged.

    The simplified flow has one fixed testing code (app/services/otp.py
    TEST_OTP): local/staging processes return it in the response as
    `debug_code` so the login screen can show it; production/prod (and
    unset/unknown RESTAMP_ENV) fail closed with a generic 503 before
    anything else, so no OTP is ever returned there. The display guard is
    re-checked here independently of the service: a production process
    never puts a code in a response, whatever the service returned.
    """
    code = otp_service.request_code(body.phone)
    resp = {"message": "If the number is valid, an OTP was sent."}
    # Second, independent guard: only a local/staging process puts a code
    # in a response, whatever the OTP service returned.
    if code is not None and otp_display_permitted():
        resp["debug_code"] = code
    return resp


@router.post("/otp/verify", response_model=schemas.TokenOut)
def otp_verify(body: schemas.OTPVerify, db: Session = Depends(get_db)):
    """Verify the code and authenticate the requested phone account.

    The fixed testing OTP is accepted only in local/staging; every other
    code — and every code at all in production — is a 401 with the static
    detail (no state, no counters, no lockout). On success the account
    resolution is unchanged: the existing user for the phone identity is
    authenticated, a first-time number is registered (BUYER, display name
    = phone), and the normal JWT/session pipeline issues the token
    (ensure_active_user + role lookup included).
    """
    if not otp_service.verify_code(body.phone, body.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid code"
        )
    user = users_roles.find_user_by_identity(db, "phone", body.phone)
    if user is None:
        user = users_roles.create_user(db, display_name=body.phone)
        users_roles.link_identity(db, user.id, "phone", body.phone, verified_by="otp")
        db.commit()
    else:
        db.commit()
    return _token_for(db, user.id)


@router.post("/google", response_model=schemas.TokenOut)
def google_auth(body: schemas.GoogleAuthIn, db: Session = Depends(get_db)):
    subject = google_svc.get_verifier().verify(body.id_token)
    user = users_roles.find_user_by_identity(db, "google", subject)
    if user is None:
        # Deliberately no user creation: unknown Google identity cannot sign in.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="GOOGLE_UNKNOWN"
        )
    return _token_for(db, user.id)


@router.post("/google/link", response_model=schemas.MeOut)
def google_link(
    body: schemas.GoogleAuthIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Link the caller's account to a Google identity.

    The user_id comes exclusively from the Bearer session JWT (never the
    body). The Google ID token is verified exactly like /auth/google
    (fail-closed 401; the token itself is never logged or stored — only the
    verified `sub` is persisted). Unknown-subject login via /auth/google is
    untouched (still 404); cross-user links surface the service 409.
    """
    subject = google_svc.get_verifier().verify(body.id_token)
    users_roles.link_identity(db, user.id, "google", subject, verified_by="google")
    db.commit()
    providers = [
        r.provider
        for r in db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    ]
    return schemas.MeOut(
        id=user.id,
        display_name=user.display_name,
        role=users_roles.get_current_role(db, user.id),
        providers=providers,
        is_admin=users_roles.is_admin(db, user.id),
    )


@router.get("/me", response_model=schemas.MeOut)
def me(db: Session = Depends(get_db), user=Depends(get_current_user)):
    providers = [
        r.provider
        for r in db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    ]
    return schemas.MeOut(
        id=user.id,
        display_name=user.display_name,
        role=users_roles.get_current_role(db, user.id),
        providers=providers,
        is_admin=users_roles.is_admin(db, user.id),
    )
