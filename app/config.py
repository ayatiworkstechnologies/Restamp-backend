"""RESTAMP Phase 4 — environment-based configuration.

No secrets are hardcoded. Production/cloud configuration is intentionally absent.
Database passwords come from RESTAMP_DATABASE_URL; the JWT secret from RESTAMP_JWT_SECRET.
"""
import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    # Local development MySQL. Example:
    #   mysql+pymysql://restamp_dev:<pwd>@localhost/restamp_dev?unix_socket=/tmp/mysql.sock
    DATABASE_URL: str = _get(
        "RESTAMP_DATABASE_URL",
        "mysql+pymysql://user:pass@localhost/restamp",
    )
    # JWT signing secret — MUST be overridden via env outside local dev.
    JWT_SECRET: str = _get("RESTAMP_JWT_SECRET", "restamp-local-dev-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = _get_int("RESTAMP_JWT_EXPIRE_MINUTES", 30)

    # Phone OTP behaviour.
    OTP_LENGTH: int = 6
    OTP_TTL_SECONDS: int = _get_int("RESTAMP_OTP_TTL_SECONDS", 300)
    OTP_MAX_REQUESTS_PER_HOUR: int = _get_int("RESTAMP_OTP_MAX_REQUESTS_PER_HOUR", 5)
    OTP_MAX_VERIFY_ATTEMPTS: int = _get_int("RESTAMP_OTP_MAX_VERIFY_ATTEMPTS", 5)
    OTP_BLOCK_SECONDS: int = _get_int("RESTAMP_OTP_BLOCK_SECONDS", 900)
    # When "1", OTP responses include the code (LOCAL DEV + TESTS ONLY).
    OTP_DEBUG: bool = _get("RESTAMP_OTP_DEBUG", "0") == "1"
    OTP_DEMO_MODE: bool = _get("RESTAMP_OTP_DEMO_MODE", "0") == "1"
    # LOCAL DEMO ONLY: when "1", a fixed set of demo OTPs is accepted by
    # verify_code (a prior OTP request must still exist). Absent/disabled:
    # no effect whatsoever (production behavior unchanged).

    # Google verification. Real verification needs GOOGLE_CLIENT_ID and a real
    # verifier; dev mode accepts only synthetic "dev:<subject>" tokens.
    GOOGLE_CLIENT_ID: str = _get("RESTAMP_GOOGLE_CLIENT_ID", "")
    GOOGLE_DEV_MODE: bool = _get("RESTAMP_GOOGLE_DEV_MODE", "0") == "1"

    APP_NAME: str = "restamp-backend"
    # Local CORS origins (comma-separated). Empty = built-in local defaults in main.py.
    CORS_ORIGINS: str = _get("RESTAMP_CORS_ORIGINS", "")

    # Local media storage for listing photo uploads (see app/services/storage.py).
    # Directory only; served at MEDIA_URL_PREFIX. Never inside the git repo.
    # An S3/object-storage backend replaces storage.py without config changes
    # beyond these two values.
    MEDIA_ROOT: str = _get("RESTAMP_MEDIA_ROOT", "/tmp/restamp-media")
    MEDIA_URL_PREFIX: str = "/media"

    # Cloudinary (property photo uploads). Credentials come ONLY from the
    # environment — never hardcoded, never logged. When CLOUDINARY_CLOUD_NAME
    # is empty, photo uploads are treated as unconfigured (503).
    CLOUDINARY_CLOUD_NAME: str = _get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY: str = _get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET: str = _get("CLOUDINARY_API_SECRET", "")
    CLOUDINARY_FOLDER: str = _get("CLOUDINARY_FOLDER", "restamp")


settings = Settings()


# Backwards-compatible alias (Phase 3 code read DBConfig.SQLALCHEMY_DATABASE_URI).
class DBConfig:
    SQLALCHEMY_DATABASE_URI = settings.DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
