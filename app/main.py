"""RESTAMP Phase 4 — FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import is_production, parse_cors_origins, settings
from .db import check_db
from .routers import admin as admin_router
from .routers import auth as auth_router
from .routers import broker as broker_router
from .routers import buyer as buyer_router
from .routers import health as health_router
from .routers import owner as owner_router
from .routers import reva as reva_router
from .routers import users as users_router
from .services import storage as storage_svc
from .worker import worker_lifespan

log = logging.getLogger("restamp")

# Allowed browser origins (native apps are unaffected by CORS).
# - Production: ONLY the exact origins in RESTAMP_CORS_ORIGINS (validated at
#   startup: exact public https origins, never "*", never localhost/LAN).
# - Development: RESTAMP_CORS_ORIGINS if set, else these local defaults
#   (Expo web :8081, classic :19006), plus any localhost / private-LAN origin
#   (_DEV_ORIGIN_REGEX) for on-device testing.
# Credentials are allowed only for those explicitly listed/matched origins.
_DEFAULT_LOCAL_ORIGINS = [
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
]


_DEV_ORIGIN_REGEX = (
    r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$"
)


def _cors_settings() -> dict:
    """CORSMiddleware origin settings for the current environment."""
    configured = parse_cors_origins(settings.CORS_ORIGINS)
    if is_production():
        # Exactly the configured origins; never a regex, never "*" (an
        # invalid list is refused at startup by validate_security_config).
        return {"allow_origins": [o for o in configured if o != "*"], "allow_origin_regex": None}
    return {"allow_origins": configured or _DEFAULT_LOCAL_ORIGINS, "allow_origin_regex": _DEV_ORIGIN_REGEX}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 1 hardening: fail fast in production when the JWT secret is
    # missing/weak or dev auth modes are enabled. Messages are
    # non-sensitive (setting names only, never values).
    from .config import is_production, validate_security_config

    try:
        validate_security_config()
    except RuntimeError as exc:
        log.error("Insecure production configuration: %s", exc)
        raise
    # Phone OTP: no SMS provider is integrated, and the fixed testing OTP
    # (app/services/otp.py TEST_OTP) is environment-gated to local/staging.
    # Production therefore has no working phone-OTP login: the request
    # endpoint fails closed with a generic 503 and every verify is
    # rejected. Say so loudly at startup instead of failing the whole API
    # (browsing does not need OTP).
    if is_production():
        log.warning(
            "Phone OTP login is unavailable in production (no SMS provider "
            "is integrated): POST /auth/otp/request returns 503 and every "
            "code is rejected."
        )
    if is_production() and settings.DOCS_ENABLED:
        log.warning("RESTAMP_DOCS_ENABLED=1: /docs, /redoc and /openapi.json are public in production.")
    if not is_production() and not settings.JWT_SECRET:
        log.warning("JWT secret is empty in local development.")
    elif settings.JWT_SECRET == "restamp-local-dev-secret-change-me":
        log.warning("Using default local JWT secret; override RESTAMP_JWT_SECRET outside local dev.")
    if settings.DATABASE_URL == "mysql+pymysql://user:pass@localhost/restamp":
        log.warning("Using built-in default DATABASE_URL; set RESTAMP_DATABASE_URL explicitly.")
    # Fail fast when the database is unreachable at startup (no traffic
    # served without a database; readiness stays red until it returns).
    if not check_db():
        log.error("Database unreachable at startup.")
        raise RuntimeError("Database unreachable at startup.")

    # Start background worker for automatic property verification
    async with worker_lifespan():
        yield

    # Engine disposal/pool cleanup on shutdown.
    from .db import engine

    engine.dispose()


def create_app() -> FastAPI:
    # Interactive API docs/openapi: always on in local development; in
    # production only with the explicit RESTAMP_DOCS_ENABLED=1 opt-in (they
    # enumerate the whole route surface publicly, but grant no access: every
    # endpoint keeps its own authentication and authorization).
    _doc_kwargs: dict = (
        {"docs_url": None, "redoc_url": None, "openapi_url": None}
        if is_production() and not settings.DOCS_ENABLED
        else {}
    )
    app = FastAPI(
        title=settings.APP_NAME,
        version="4.0.0",
        lifespan=lifespan,
        **_doc_kwargs,
    )
    app.add_middleware(
        CORSMiddleware,
        **_cors_settings(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Credentials are granted only together with an allowed origin.
    # Starlette's CORSMiddleware adds "Access-Control-Allow-Credentials" to
    # every response that carries an Origin header, even when the origin is
    # refused (no Access-Control-Allow-Origin); browsers ignore it then, but
    # it is dropped here so a refused origin is never told credentials are
    # allowed. Registered after CORSMiddleware, so it wraps it.
    @app.middleware("http")
    async def _credentials_only_for_allowed_origins(request: Request, call_next):
        response = await call_next(request)
        if "access-control-allow-origin" not in response.headers:
            if "access-control-allow-credentials" in response.headers:
                del response.headers["access-control-allow-credentials"]
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        # Safe error responses: status + message only, never internals/secrets.
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):  # noqa: BLE001
        log.exception("Unhandled error")
        return JSONResponse(status_code=500, content={"detail": "Internal error"})

    # Uploaded images are served as-is (client-declared MIME): the nosniff
    # header stops browsers from re-sniffing content types on the /media
    # static mount (downloaded, JSON error, or otherwise).
    @app.middleware("http")
    async def _media_nosniff(request: Request, call_next):
        response = await call_next(request)
        try:
            path = request.url.path
            if path == settings.MEDIA_URL_PREFIX or path.startswith(
                settings.MEDIA_URL_PREFIX + "/"
            ):
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
        except Exception:
            log.exception("media nosniff middleware error")
        return response

    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(users_router.router)
    app.include_router(buyer_router.router)
    app.include_router(owner_router.router)
    app.include_router(broker_router.router)
    app.include_router(admin_router.router)
    app.include_router(reva_router.router)

    # Servable uploaded listing photos (local-dev storage; see storage.py).
    # Never a source directory — RESTAMP_MEDIA_ROOT lives outside the repo.
    # Mounted dir is <root>/listings so /media/<id>/<file> resolves exactly
    # to the stored layout.
    app.mount(
        settings.MEDIA_URL_PREFIX,
        StaticFiles(directory=str(storage_svc.ensure_listings_dir())),
        name="media",
    )
    return app


app = create_app()
