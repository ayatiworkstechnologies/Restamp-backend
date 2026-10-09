"""RESTAMP Phase 4 — FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

# Load .env (local dev). On Render/production, env vars are injected by the platform.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars set directly in production

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import check_db
from .routers import auth as auth_router
from .routers import buyer as buyer_router
from .routers import health as health_router
from .routers import owner as owner_router
from .routers import users as users_router
from .services import storage as storage_svc

log = logging.getLogger("restamp")

# Local-development browser origins only (Expo web :8081, classic :19006).
# Overridable via RESTAMP_CORS_ORIGINS (comma-separated).
# Set RESTAMP_CORS_ORIGINS=* on Render to allow all origins (fine for mobile API).
_DEFAULT_LOCAL_ORIGINS = [
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
]


def _cors_origins() -> list[str]:
    raw = settings.CORS_ORIGINS.strip()
    if raw == "*":
        return ["*"]
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return _DEFAULT_LOCAL_ORIGINS


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.JWT_SECRET == "restamp-local-dev-secret-change-me":
        log.warning("Using default local JWT secret; override RESTAMP_JWT_SECRET outside local dev.")
    # Fail fast when the database is unreachable at startup.
    if not check_db():
        log.error("Database unreachable at startup.")
    yield
    # Engine disposal/pool cleanup on shutdown.
    from .db import engine

    engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME, version="4.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(users_router.router)
    app.include_router(buyer_router.router)
    app.include_router(owner_router.router)

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
