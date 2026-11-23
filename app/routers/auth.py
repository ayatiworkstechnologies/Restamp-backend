from __future__ import annotations
"""RESTAMP Phase 4 — authentication routes (phone OTP + Google + self profile).

Phone (registration-capable): unknown phone identities create a user on first
successful verification (standard OTP registration), default role BUYER.
Google: unknown identities NEVER auto-create (404 GOOGLE_UNKNOWN); the client
must onboard through an approved flow before the Google identity can be linked.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import schemas
from ..config import fixed_otp_in_production, otp_display_permitted
from ..db import get_db
from ..models import AuthIdentity, AuditLog, RefreshToken
from ..security import (
    FIXED_OTP_AMR,
    create_access_token,
    ensure_active_user,
    get_current_user,
    get_session_amr,
    is_fixed_otp_session,
)
from ..services import google as google_svc
from ..services import capabilities, users_roles
from ..services import otp as otp_service
from ..services.refresh_tokens import create_refresh_token, validate_refresh_token, rotate_refresh_token, revoke_refresh_token, get_valid_refresh_token

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_for(db: Session, user_id: int, amr: str | None = None) -> schemas.TokenOut:
    # Every login path (OTP, Google) issues its token here: a suspended or
    # closed account gets 403 "Account disabled" instead of a session.
    ensure_active_user(db, user_id)
    role = users_roles.get_current_role(db, user_id)
    token, ttl = create_access_token(user_id, role, amr=amr)
    return schemas.TokenOut(
        access_token=token, expires_in=ttl, user_id=user_id, role=role
    )


@router.post("/otp/request")
def otp_request(body: schemas.OTPRequest):
    """Request the login OTP. Indian mobiles only (schemas gate); never logged.

    The simplified flow has one fixed testing code (app/services/otp.py
    TEST_OTP): local/staging processes return it in the response as
    `debug_code` so the login screen can show it; production with
    RESTAMP_PRODUCTION_FIXED_OTP=1 answers the generic 200 message with NO
    code; production/prod without the opt-in (and unset/unknown
    RESTAMP_ENV) fail closed with a generic 503 before anything else, so no
    OTP is ever returned there. The display guard is
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

    The fixed OTP is accepted in local/staging, and in production only with
    RESTAMP_PRODUCTION_FIXED_OTP=1; every other code — and every code in
    production without the opt-in — is a 401 with the static detail (no
    state, no counters, no lockout). Under the production opt-in an
    existing admin account is refused with that same 401, and the issued
    token carries amr=fixed_otp (ordinary-user session only). On success
    the account resolution is unchanged: the existing user for the phone identity is
    authenticated, a first-time number is registered (BUYER, display name
    = phone), and the normal JWT/session pipeline issues the token
    (ensure_active_user + role lookup included).
    """
    if not otp_service.verify_code(body.phone, body.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid code"
        )
    user = users_roles.find_user_by_identity(db, "phone", body.phone)
    production_fixed_otp = fixed_otp_in_production()
    if production_fixed_otp and user is not None and users_roles.is_admin(db, user.id):
        # The published production code must never open an admin account.
        # Same static 401 as a wrong code: the response does not reveal
        # that the number belongs to an admin.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid code"
        )
    if user is None:
        # The declared account type applies only to a brand-new account: it
        # never changes an existing account's poster type or mode.
        user = users_roles.create_user(
            db,
            display_name=body.phone,
            poster_type=capabilities.normalize_account_type(body.account_type),
        )
        users_roles.link_identity(db, user.id, "phone", body.phone, verified_by="otp")
        db.commit()
    else:
        db.commit()
    # Production fixed-OTP sessions are marked so they can never reach
    # admin endpoints or link a Google identity (app/deps.py, google_link).
    return _token_for(db, user.id, amr=FIXED_OTP_AMR if production_fixed_otp else None)


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
    amr: str | None = Depends(get_session_amr),
):
    """Link the caller's account to a Google identity.

    The user_id comes exclusively from the Bearer session JWT (never the
    body). The Google ID token is verified exactly like /auth/google
    (fail-closed 401; the token itself is never logged or stored — only the
    verified `sub` is persisted). Unknown-subject login via /auth/google is
    untouched (still 404); cross-user links surface the service 409.

    A fixed-OTP session (amr=fixed_otp) is refused with 403 BEFORE the
    Google token is verified: otherwise anyone using the published code
    could attach their own Google account to someone else's phone account
    and keep access after the fixed OTP is switched off.
    """
    if is_fixed_otp_session(amr):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
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
        email=user.email,
        role=users_roles.get_current_role(db, user.id),
        providers=providers,
        is_admin=users_roles.is_admin(db, user.id),
        capabilities=capabilities.capabilities(db, user.id),
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
        email=user.email,
        role=users_roles.get_current_role(db, user.id),
        providers=providers,
        is_admin=users_roles.is_admin(db, user.id),
        capabilities=capabilities.capabilities(db, user.id),
    )


@router.post("/refresh", response_model=schemas.TokenOut)
def refresh_access_token(
    body: schemas.RefreshTokenIn,
    db: Session = Depends(get_db),
):
    """Refresh an access token using a valid refresh token.

    - Validates the refresh token by JTI and stored hash.
    - Rotates the token: invalidates the old one and issues a new one.
    - Prevents reuse of revoked/expired tokens.
    - Returns a new access token and a new refresh token.
    """
    from ..security import decode_token

    # Decode the JWT to extract the JTI
    try:
        payload = decode_token(body.refresh_token)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    # Validate the refresh token against the database
    rt = validate_refresh_token(jti, body.token_hash, db)
    user_id = rt.user_id

    # Rotate: invalidate the old token and issue a new one
    old_rt, new_rt = rotate_refresh_token(jti, body.token_hash, db)

    # Create a new access token for the user
    from ..security import create_access_token as _create_access_token
    role = users_roles.get_current_role(db, user_id)
    new_access_token, ttl = _create_access_token(user_id, role)

    return schemas.TokenOut(
        access_token=new_access_token, expires_in=ttl, user_id=user_id, role=role
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Server-side invalidate the refresh token for the current session.

    - Revokes ALL valid refresh tokens associated with the authenticated user.
    - Idempotent: if tokens are already revoked, no error is raised.
    - After this call, the client's Bearer token must be discarded;
      any subsequent refresh attempt will be rejected (access token
      remains valid until its natural expiry).
    """
    from ..services.refresh_tokens import revoke_refresh_token
    from ..models import RefreshToken

    # Revoke all valid (non-revoked, non-expired) refresh tokens for this user
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id,
        RefreshToken.revoked_at.is_(None),
    ).update({"revoked_at": datetime.now(timezone.utc)}, synchronize_session=False)

    # Also revoke any token that has an explicit revoked_at but was previously
    # active, ensuring complete invalidation.
    db.commit()


@router.post("/close-account", status_code=status.HTTP_204_NO_CONTENT)
def close_account(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Non-destructively close the authenticated user's account.

    - Sets the user's status to ``closed``, preventing future authentication.
    - Preserves ALL account data: property listings, enquiries, conversations,
      messages, site visits, verification records, contact credits, subscriptions,
      payment records, audit logs, and dealer locality entitlements.
    - Idempotent: closing an already-closed account is a no-op.
    - Auditable: the status change is recorded in the user row.
    - Does NOT cancel subscriptions, issue refunds, or delete any records.

    Returns 204 No Content on success. If the account is already closed,
    returns 204 without error (idempotent).
    """
    from ..models import User

    # Only transition from active to closed; if already closed, it is idempotent.
    if user.status == "active":
        user.status = "closed"
        db.add(AuditLog(
            user_id=user.id,
            action="ACCOUNT_CLOSED",
            entity=f"user:{user.id}",
            before_json={"status": "active"},
            after_json={"status": "closed"},
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    elif user.status == "closed":
        # Idempotent: already closed, no error.
        db.commit()
    else:
        # User is suspended or otherwise disabled; still idempotent —
        # nothing to do for "close" operation.
        db.commit()
