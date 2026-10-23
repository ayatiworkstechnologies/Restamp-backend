"""RESTAMP Phase 4 — authentication routes (phone OTP + Google + self profile).

Phone (registration-capable): unknown phone identities create a user on first
successful verification (standard OTP registration), default role BUYER.
Google: unknown identities NEVER auto-create (404 GOOGLE_UNKNOWN); the client
must onboard through an approved flow before the Google identity can be linked.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .. import schemas
from ..config import settings
from ..db import get_db
from ..models import AuthIdentity
from ..security import create_access_token, get_current_user
from ..services import google as google_svc
from ..services import users_roles
from ..services.otp import otp_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request | None) -> str | None:
    """Best-effort client IP for per-IP OTP throttling (never logged).

    Default (RESTAMP_TRUST_PROXY unset or "0"): the socket peer host — the
    same value this helper always returned. When RESTAMP_TRUST_PROXY=1
    (deployed behind the platform's own reverse proxy, e.g. Render) and the
    peer is NOT loopback, the peer is treated as a trusted proxy and the
    RIGHTMOST X-Forwarded-For entry becomes the client IP: it is the only
    value the trusted hop appended, while earlier entries are
    client-controlled and deliberately ignored. A missing, empty, or
    malformed header falls back to the peer, and the header is ignored
    entirely for loopback peers so local dev behavior is unchanged. The
    derived value is used only as an in-memory rate-limit comparison key;
    it is never logged.
    """
    try:
        if request is None:
            return None
        peer = None
        if request.client is not None:
            peer = (request.client.host or "").strip() or None
        if peer is None:
            return None
        if (
            not settings.TRUST_PROXY
            or peer in ("127.0.0.1", "::1", "localhost")
        ):
            return peer
        forwarded = (request.headers.get("x-forwarded-for") or "").strip()
        if not forwarded:
            return peer
        candidate = forwarded.split(",")[-1].strip()
        return candidate or peer
    except Exception:
        return None


def _token_for(db: Session, user_id: int) -> schemas.TokenOut:
    role = users_roles.get_current_role(db, user_id)
    token, ttl = create_access_token(user_id, role)
    return schemas.TokenOut(
        access_token=token, expires_in=ttl, user_id=user_id, role=role
    )


@router.post("/otp/request")
def otp_request(body: schemas.OTPRequest, request: Request):
    code = otp_service.request_code(body.phone, ip=_client_ip(request))
    # The code leaves the server ONLY in OTP_DEBUG (local dev/tests).
    resp = {"message": "If the number is valid, an OTP was sent."}
    if code is not None:
        resp["debug_code"] = code
    return resp


@router.post("/otp/verify", response_model=schemas.TokenOut)
def otp_verify(body: schemas.OTPVerify, request: Request, db: Session = Depends(get_db)):
    if not otp_service.verify_code(body.phone, body.code, ip=_client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired code"
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
