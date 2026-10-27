"""RESTAMP Phase 4 — authentication routes (phone OTP + Google + self profile).

Phone (registration-capable): unknown phone identities create a user on first
successful verification (standard OTP registration), default role BUYER.
Google: unknown identities NEVER auto-create (404 GOOGLE_UNKNOWN); the client
must onboard through an approved flow before the Google identity can be linked.
"""
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .. import schemas
from ..config import settings
from ..db import get_db
from ..models import AuthIdentity
from ..security import create_access_token, get_current_user
from ..services import google as google_svc
from ..services import users_roles
from ..services.otp import otp_service
from ..services.test_login import test_login_service

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
    """Request an OTP. Indian mobiles only schemas gate; never logged.

    Normal mode: no code leaves the server — delivery happens only through
    the (currently unconnected) delivery seam, and the response shape is
    constant ({message}). Explicitly restricted internal-testing display
    mode (see app/services/otp.py _display_active: env flag + never
    production-classified, also refused at production startup): the
    backend-selected code is added to the response so the frontend can
    display it. Production can never reach the display branch.
    """
    code = otp_service.request_code(body.phone, ip=_client_ip(request))
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


def _parse_test_login_phone(raw: bytes) -> str:
    """Parse the test-login body STRICTLY AFTER the activation guard.

    Deliberately error-generic: any unparseable bytes, non-object JSON, or
    a phone that fails the SAME Indian-mobile schema as /auth/otp/request
    raises the SAME generic 401 as a wrong key — no field-level 422
    detail — so request shape can never reveal whether (or how) the
    feature is active. Runs before throttling, so malformed requests also
    never consume rate-limit budget.
    """
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return schemas.OTPRequest(**payload).phone
    except (ValueError, TypeError) as exc:
        # One generic rejection covers them all: json.JSONDecodeError and
        # UnicodeDecodeError subclass ValueError, and pydantic's
        # ValidationError subclasses ValueError too. The message is never
        # exposed — see the generic detail below.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
        ) from exc


@router.post("/test-login", response_model=schemas.TokenOut)
async def test_login(
    request: Request,
    db: Session = Depends(get_db),
    x_test_login_key: str | None = Header(default=None, alias="X-Test-Login-Key"),
):
    """Restricted test-only phone login: works without SMS, and NO OTP is
    ever generated, stored, returned, or logged (separate from the local
    OTP display mode; see app/services/test_login.py).

    The body is parsed BY HAND so the shape guards can never leak state:

    - Inactive (default) or misconfigured: the body is never read or
      validated — the response is the exact 404 unknown-route body
      ({"detail": "Not Found"}) for EVERY request, valid or malformed, so
      neither the endpoint's existence nor its feature state can be
      probed via a malformed payload.
    - Active: the body must be a JSON object whose `phone` matches the
      SAME Indian-mobile schema as /auth/otp/request; anything else —
      unparseable bytes, non-object JSON, schema-invalid phone — is
      rejected with the SAME generic 401 as a wrong key (never a
      field-level validation error).
    - Authorization is the X-Test-Login-Key header, compared in constant
      time against RESTAMP_TEST_LOGIN_KEY; missing/mismatched keys get a
      generic 401 with no phone-existence signal.
    - The phone must ALSO be on the explicit
      RESTAMP_TEST_LOGIN_ALLOWED_PHONES allowlist: ANY other phone gets a
      generic 403 EVEN WITH a valid key — the empty list rejects every
      phone (fail closed), and no account is ever created or touched for
      a phone outside the list.
    - Authorization MODE (RESTAMP_TEST_LOGIN_MODE): "allowlist" (default)
      behaves exactly as above; "existing" permits ONLY phones that
      ALREADY have a Restamp account — ANY valid Indian mobile with an
      existing account logs in (allowlist not consulted), an unknown
      phone gets the explicit 404 "No account found for this phone
      number." with ZERO rows written, and registration is impossible
      (production refuses "existing" + registration). The mode check
      runs strictly AFTER the key verification, so the account-existence
      message is never an unauthenticated oracle.
    - Attempts are rate limited per IP and per phone AFTER body parsing
      and BEFORE the key check (wrong-key attempts consume budget;
      malformed bodies never do).
    - Account creation is separately feature-gated: with
      RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION unset (the default), existing
      allowlisted accounts log in but an UNKNOWN phone gets the same
      generic 401 as a wrong key and creates no rows; with the flag "1",
      allowlisted unknown phones register exactly like /auth/otp/verify,
      with verified_by="test_login" so test-issued identities remain
      auditable; the key and the phone are never logged.
    """
    test_login_service.require_active()
    phone = _parse_test_login_phone(await request.body())
    ip = _client_ip(request)
    test_login_service.record_attempt(ip=ip, phone=phone)
    test_login_service.require_key(x_test_login_key)
    if test_login_service.mode() == "existing":
        # Existing-accounts-only demo login: the allowlist is not consulted
        # and registration is structurally impossible (the config gate
        # refuses "existing" + registration at production boot). The
        # identity is reused exactly as stored — NO rows are created and
        # verified_by is never rewritten.
        user = users_roles.find_user_by_identity(db, "phone", phone)
        test_login_service.require_existing_user(user)
        db.commit()
        return _token_for(db, user.id)
    test_login_service.require_phone_allowed(phone)
    user = users_roles.find_user_by_identity(db, "phone", phone)
    # Registration is feature-gated SEPARATELY from the key and allowlist
    # authorization: with RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION unset, an
    # allowlisted-but-UNKNOWN phone fails HERE with the same generic 401
    # as a wrong key — BEFORE any INSERT, so not a single row appears.
    test_login_service.require_registration_allowed(user)
    if user is None:
        user = users_roles.create_user(db, display_name=phone)
        users_roles.link_identity(
            db, user.id, "phone", phone, verified_by="test_login"
        )
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
