"""RESTAMP local media storage (photo-upload phase).

Small storage abstraction over a local directory so routers/services never
touch the filesystem directly; a future S3/object-storage backend only needs
to reimplement save()/delete()/public_url(). Files are NEVER stored inside
the git repository (see RESTAMP_MEDIA_ROOT default + .gitignore).

Layout: <root>/listings/<listing_id>/<uuid>.<ext>
"""
import os
import uuid
from pathlib import Path

# Conservative per-image cap (no prior limit existed in the repo).
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Allowed uploads: MIME reported by the client mapped to the only extension
# we will ever write. The client filename/extension is never trusted.
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

_CLIENT_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def get_media_root() -> Path:
    """Root dir for uploads. Read per-call so tests can redirect via env."""
    return Path(
        os.environ.get("RESTAMP_MEDIA_ROOT", "/tmp/restamp-media")
    )


def ensure_media_root() -> Path:
    root = get_media_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_listings_dir() -> Path:
    """Servable directory mounted at MEDIA_URL_PREFIX (see main.py)."""
    directory = ensure_media_root() / "listings"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def validate_image_upload(content_type: str | None, client_filename: str | None) -> str:
    """Return the canonical extension for an upload, or raise ValueError.

    Both the declared MIME type and the client filename extension must agree
    on an allowed image format; the client extension alone never decides.
    """
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_IMAGE_TYPES:
        raise ValueError(f"Unsupported image type: {content_type or 'unknown'}")
    client_ext = Path(client_filename or "").suffix.lower()
    if client_ext and client_ext not in _CLIENT_EXT_TO_MIME:
        raise ValueError(f"Unsupported file extension: {client_ext}")
    if client_ext and _CLIENT_EXT_TO_MIME[client_ext] != mime:
        raise ValueError("File extension does not match image type")
    return ALLOWED_IMAGE_TYPES[mime]


def save_listing_image(
    listing_id: int, data: bytes, extension: str
) -> tuple[str, int]:
    """Persist validated bytes; returns (storage_name, byte_size).

    storage_name is `<listing_id>/<uuid>.<ext>` (unique, no client input).
    """
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    directory = ensure_listings_dir() / str(int(listing_id))
    directory.mkdir(parents=True, exist_ok=True)
    storage_name = f"{listing_id}/{uuid.uuid4().hex}{extension}"
    path = ensure_listings_dir() / storage_name
    path.write_bytes(data)
    return storage_name, len(data)


def delete_stored_file(storage_name: str) -> None:
    """Best-effort removal (orphan cleanup); never raises."""
    try:
        path = ensure_listings_dir() / storage_name
        if path.is_file():
            path.unlink()
        try:
            path.parent.rmdir()  # drop the listing dir when left empty
        except OSError:
            pass
    except OSError:
        pass


def stored_file_path(storage_name: str) -> Path:
    return ensure_listings_dir() / storage_name
