"""RESTAMP Phase 4 — Google identity verification abstraction.

Approved rules:
- Google identities live ONLY in auth_identities (never duplicated to users).
- An unknown Google identity must NOT auto-create a user (404, no side effects).
- A known identity (provider='google' row exists) may authenticate/link.

Real verification requires RESTAMP_GOOGLE_CLIENT_ID plus a production verifier
(not configured in this phase). DevGoogleVerifier accepts ONLY synthetic
"dev:<subject>" tokens when RESTAMP_GOOGLE_DEV_MODE=1, so tests and local
development can exercise the flow honestly without pretending real Google auth works.
"""
from fastapi import HTTPException, status

import jwt
from jwt import PyJWKClient

from ..config import settings


_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    """Process-wide Google JWKS client (fetches and caches signing keys)."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_GOOGLE_JWKS_URL)
    return _jwks_client


class GoogleVerifier:
    def verify(self, id_token: str) -> str:
        """Return the Google subject (stable provider_identifier) for a valid token."""
        raise NotImplementedError


class DevGoogleVerifier(GoogleVerifier):
    def verify(self, id_token: str) -> str:
        if not settings.GOOGLE_DEV_MODE:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Google authentication is not configured",
            )
        if not id_token.startswith("dev:") or len(id_token) <= 4:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google credential"
            )
        return id_token[4:]


class ProductionGoogleVerifier(GoogleVerifier):
    """Real Google ID-token verifier (Google JWKS + PyJWT).

    Validates the RS256 signature against Google's published certs and
    enforces audience (our OAuth client ID), issuer, and expiration.
    Returns the `sub` claim as the stable provider_identifier.
    Any verification failure is a 401 in the existing auth error style;
    fail-closed, no user creation here (router keeps the 404 rule).
    """

    def verify(self, id_token: str) -> str:
        if not id_token or not isinstance(id_token, str):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google credential"
            )
        try:
            signing_key = _get_jwks_client().get_signing_key_from_jwt(id_token)
            payload = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=settings.GOOGLE_CLIENT_ID,
                issuer=_GOOGLE_ISSUERS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google credential has expired",
            )
        except jwt.PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google credential"
            )
        subject = payload.get("sub")
        if not subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google credential"
            )
        return subject


def get_verifier() -> GoogleVerifier:
    if settings.GOOGLE_CLIENT_ID:
        return ProductionGoogleVerifier()
    return DevGoogleVerifier()
