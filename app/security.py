"""RESTAMP Phase 4 — JWT session security (authentication, not authorization).

- HS256 access tokens only; no refresh/session table (no new tables allowed).
- Secret comes from RESTAMP_JWT_SECRET. Invalid/expired tokens are rejected.
- Role enforcement lives in app.deps (authorization), kept separate here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db

bearer_scheme = HTTPBearer(auto_error=False)

TOKEN_TYPE = "access"
# Authentication-method marker for sessions issued by the production fixed
# OTP (RESTAMP_PRODUCTION_FIXED_OTP=1). Such a session is an ordinary-user
# session only: app.deps refuses it on every admin path and the auth router
# refuses it for Google linking. Tokens from every other login path carry
# no `amr` claim, so their behaviour is unchanged.
FIXED_OTP_AMR = "fixed_otp"


def create_access_token(
    user_id: int, role: str | None, amr: str | None = None
) -> tuple[str, int]:
    """Return (token, expires_in_seconds). Role is carried for convenience only.

    `amr` (authentication method) is written only when given; it is signed
    with the rest of the payload, so a client cannot remove or alter it.
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": TOKEN_TYPE,
        "exp": expires,
    }
    if amr is not None:
        payload["amr"] = amr
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), int(
        timedelta(minutes=settings.JWT_EXPIRE_MINUTES).total_seconds()
    )


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if payload.get("type") != TOKEN_TYPE or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return payload


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> int:
    """Authenticated-user dependency: returns the user id or raises 401."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    payload = decode_token(credentials.credentials)
    try:
        return int(payload["sub"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def get_session_amr(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str | None:
    """The verified token's `amr` claim, or None for an ordinary session.

    Runs the same decode_token() checks as get_current_user_id (signature,
    expiry, type, sub), so it can never accept a token that dependency would
    reject. Fail closed: any `amr` value that is present but not a string is
    reported as the fixed-OTP marker, so an unexpected claim shape can only
    restrict a session, never widen it.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    payload = decode_token(credentials.credentials)
    if "amr" not in payload or payload["amr"] is None:
        return None
    amr = payload["amr"]
    return amr if isinstance(amr, str) else FIXED_OTP_AMR


def is_fixed_otp_session(amr: str | None) -> bool:
    """True for any session that carries an authentication-method marker.

    Only the production fixed OTP mints one today; treating every marked
    session as restricted keeps a future/unknown marker fail-closed."""
    return amr is not None


def ensure_active_user(db: Session, user_id: int):
    """Load the user and refuse accounts that may not act.

    A token for a deleted user is 401. A user whose `status` is not
    'active' (suspended/closed) is 403 for every authenticated request and
    at login, so disabling an account in the database takes effect at once,
    even for tokens issued before it was disabled. Fail closed: any value
    other than 'active' is refused. The detail is static and reveals only
    the caller's own state.
    """
    from .models import User

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    return user


def get_current_user(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)):
    """Load the authenticated, active User row (see ensure_active_user)."""
    return ensure_active_user(db, user_id)
