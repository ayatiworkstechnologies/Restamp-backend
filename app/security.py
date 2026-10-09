"""RESTAMP Phase 4 — JWT session security (authentication, not authorization).

- HS256 access tokens only; no refresh/session table (no new tables allowed).
- Secret comes from RESTAMP_JWT_SECRET. Invalid/expired tokens are rejected.
- Role enforcement lives in app.deps (authorization), kept separate here.
"""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db

bearer_scheme = HTTPBearer(auto_error=False)

TOKEN_TYPE = "access"


def create_access_token(user_id: int, role: str | None) -> tuple[str, int]:
    """Return (token, expires_in_seconds). Role is carried for convenience only."""
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": TOKEN_TYPE,
        "exp": expires,
    }
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


def get_current_user(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)):
    """Load the authenticated User row; a token for a deleted user is rejected."""
    from .models import User

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return user
