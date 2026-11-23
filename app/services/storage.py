"""RESTAMP local media storage for property-listing photos.

The one storage abstraction over the server disk: routers/services never
touch the filesystem directly. Files are NEVER stored inside the git
repository (RESTAMP_MEDIA_ROOT; production: /var/www/restamp/uploads).

Layout: <root>/listings/<listing_id>/<uuid32hex>.<jpg|png|webp>
Served at settings.MEDIA_URL_PREFIX ("/media") — by Nginx in production,
by the FastAPI StaticFiles mount (main.py) in development/tests — so the
public URL is <base>/media/<listing_id>/<uuid>.<ext>.

Storage-source invariant (no schema change): property_media.public_id is
``local:<listing_id>/<uuid>.<ext>`` for disk photos and a Cloudinary public
id otherwise. delete_photo() dispatches on that prefix, so legacy Cloudinary
photos keep being destroyed in Cloudinary and disk photos on disk — a
``local:`` id is never sent to Cloudinary.

Security invariants:
- No client input reaches a path: names are server-generated UUIDs, the
  listing id is an int, and every path is re-validated against a strict
  pattern and resolved inside the listings directory (no traversal, no
  symlink escape) before any read/write/delete.
- Content is validated by magic bytes (JPEG/PNG/WebP), and must agree with
  both the declared MIME type and the client extension.
- Writes are atomic: the final name is reserved with exclusive creation
  (O_CREAT|O_EXCL — an existing file is never overwritten), the bytes go to
  a private temp file that is fsynced and then os.replace()d onto the
  reservation. Any failure removes both, so no partial file is ever served.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

from ..config import (
    PHOTO_MAX_BYTES_LIMITS,
    PHOTO_MAX_DIMENSION_LIMITS,
    is_production,
    parse_photo_limit,
    settings,
)

log = logging.getLogger("restamp.storage")

# Default per-image cap (historical contract: 10 MB). The effective cap is
# max_image_bytes() (RESTAMP_PHOTO_MAX_BYTES); this name stays for
# backward compatibility with existing imports.
MAX_IMAGE_BYTES = PHOTO_MAX_BYTES_LIMITS[0]

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

LOCAL_PREFIX = "local:"
_FILE_MODE = 0o644   # world-readable so Nginx (a different user) can serve it
_DIR_MODE = 0o755
# The ONLY storage-name shape this module ever produces or accepts.
_STORAGE_NAME_RE = re.compile(r"^([1-9][0-9]{0,19})/([0-9a-f]{32})\.(jpg|png|webp)$")
_TMP_PREFIX = ".upload-"


# --------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------

def get_media_root() -> Path:
    """Root dir for uploads. Read per call so tests can redirect via env;
    falls back to settings.MEDIA_ROOT (same env var, read at import)."""
    return Path(os.environ.get("RESTAMP_MEDIA_ROOT") or settings.MEDIA_ROOT)


def _mkdir(path: Path) -> None:
    """Create a directory (and parents) with 0755; never loosens an
    existing directory owned by someone else (chmod is best-effort)."""
    existed = path.is_dir()
    path.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    if not existed:
        try:
            os.chmod(path, _DIR_MODE)  # mkdir mode is filtered by the umask
        except OSError:
            pass


def ensure_media_root() -> Path:
    root = get_media_root()
    _mkdir(root)
    return root


def ensure_listings_dir() -> Path:
    """Servable directory mounted at MEDIA_URL_PREFIX (see main.py)."""
    directory = ensure_media_root() / "listings"
    _mkdir(directory)
    return directory


def uploads_use_local_storage() -> bool:
    """Where NEW listing photos go.

    "local" mode -> disk. "cloudinary" mode keeps its historical behaviour,
    including the existing fallback to disk when no Cloudinary cloud is
    configured (production logs a startup warning for that case).
    """
    if (settings.PHOTO_STORAGE or "").strip().lower() == "local":
        return True
    return not bool(settings.CLOUDINARY_CLOUD_NAME)


def public_base_url(request_base_url: str | None) -> str:
    """Base URL for local photo URLs (no trailing slash).

    RESTAMP_MEDIA_PUBLIC_BASE_URL when configured. Otherwise only
    development may use the request's own base URL; production fails
    closed (OSError -> generic error) instead of persisting an internal
    upstream address such as http://127.0.0.1:8000.
    """
    base = (settings.MEDIA_PUBLIC_BASE_URL or "").strip().rstrip("/")
    if base:
        return base
    if is_production():
        raise OSError("Photo uploads are not configured")
    return (request_base_url or "").rstrip("/")


def public_url_for(storage_name: str, request_base_url: str | None = None) -> str:
    """Absolute public URL for a stored file (validated name only)."""
    _check_storage_name(storage_name)
    return f"{public_base_url(request_base_url)}{settings.MEDIA_URL_PREFIX}/{storage_name}"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_image_upload(content_type: str | None, client_filename: str | None) -> str:
    """Return the canonical extension for an upload, or raise ValueError.

    Both the declared MIME type and the client filename extension must agree
    on an allowed image format; the client extension alone never decides.
    """
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_IMAGE_TYPES:
        # Static detail only: never echo the client-supplied type header
        # back into the 422 body.
        raise ValueError(
            "Unsupported image type; allowed: " + ", ".join(sorted(ALLOWED_IMAGE_TYPES))
        )
    client_ext = Path(client_filename or "").suffix.lower()
    if client_ext and client_ext not in _CLIENT_EXT_TO_MIME:
        # Static detail only: never echo the client-supplied file name
        # back into the 422 body.
        raise ValueError(
            "Unsupported file extension; allowed: " + ", ".join(sorted(_CLIENT_EXT_TO_MIME))
        )
    if client_ext and _CLIENT_EXT_TO_MIME[client_ext] != mime:
        raise ValueError("File extension does not match image type")
    return ALLOWED_IMAGE_TYPES[mime]


def sniff_image_mime(data: bytes) -> str | None:
    """Detect JPEG/PNG/WebP from magic bytes; None for anything else.

    JPEG: FF D8 FF. PNG: the 8-byte PNG signature followed by an IHDR
    chunk. WebP: "RIFF" <size> "WEBP" plus a VP8/VP8L/VP8X chunk tag.
    No new dependency; deliberately strict about the header layout.
    """
    if not data:
        return None
    if len(data) >= 4 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 16 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return "image/png"
    if (len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
            and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X")):
        return "image/webp"
    return None


def _limit(raw: str | None, limits: tuple[int, int, int]) -> int:
    """Effective limit; a malformed value (refused at startup, so only
    reachable if settings are mutated at runtime) falls back to the safe
    default rather than disabling the check."""
    try:
        return parse_photo_limit(raw, limits)
    except ValueError:
        return limits[0]


def max_image_bytes() -> int:
    """Effective per-photo byte cap (RESTAMP_PHOTO_MAX_BYTES, default 10 MB)."""
    return _limit(settings.PHOTO_MAX_BYTES_RAW, PHOTO_MAX_BYTES_LIMITS)


def max_image_dimension() -> int:
    """Effective max width/height in pixels (RESTAMP_PHOTO_MAX_DIMENSION)."""
    return _limit(settings.PHOTO_MAX_DIMENSION_RAW, PHOTO_MAX_DIMENSION_LIMITS)


def size_exceeded_detail() -> str:
    """Static 422 detail for an oversized upload (no client input)."""
    cap = max_image_bytes()
    if cap % (1024 * 1024) == 0:
        return f"Image exceeds {cap // (1024 * 1024)} MB"
    return f"Image exceeds {cap} bytes"


# JPEG start-of-frame markers carrying the frame size (all SOFn except
# DHT C4, JPG C8 and DAC CC, which share the C0-CF range).
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers with no length field (TEM, RSTn, SOI).
_JPEG_STANDALONE = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG marker segments to the first SOFn; every read is bounds
    checked, and hitting SOS/EOI (or garbage) first means no frame size."""
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            return None
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _JPEG_STANDALONE:
            continue
        if marker in (0xD9, 0xDA) or i + 2 > n:
            return None
        seg_len = int.from_bytes(data[i:i + 2], "big")
        if seg_len < 2 or i + seg_len > n:
            return None
        if marker in _JPEG_SOF:
            if seg_len < 7:
                return None
            height = int.from_bytes(data[i + 3:i + 5], "big")
            width = int.from_bytes(data[i + 5:i + 7], "big")
            return width, height
        i += seg_len
    return None


def image_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    """(width, height) read from the image header only, or None when the
    header is truncated/malformed. Pure byte parsing: no decoder, no new
    dependency, nothing beyond the fixed header is interpreted."""
    if mime == "image/png":
        # IHDR is the mandatory first chunk: width/height at bytes 16..24.
        if len(data) < 24:
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime == "image/jpeg":
        return _jpeg_dimensions(data)
    if mime == "image/webp":
        chunk = data[12:16]
        if chunk == b"VP8X":  # extended: 24-bit (canvas - 1) at 24..30
            if len(data) < 30:
                return None
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if chunk == b"VP8 ":  # lossy: key-frame start code then 14-bit sizes
            if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
                return None
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if chunk == b"VP8L":  # lossless: 0x2f signature then 14-bit (size - 1)
            if len(data) < 25 or data[20] != 0x2F:
                return None
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def validate_image_content(data: bytes, extension: str) -> None:
    """Raise ValueError unless the bytes are a non-empty, size-capped image
    whose real format matches the already-validated extension/MIME and whose
    header-declared dimensions are present and within the configured cap."""
    if not data:
        raise ValueError("Empty file")
    if len(data) > max_image_bytes():
        raise ValueError(size_exceeded_detail())
    detected = sniff_image_mime(data)
    if detected is None:
        raise ValueError("File content is not a supported image")
    if ALLOWED_IMAGE_TYPES[detected] != extension:
        raise ValueError("File content does not match image type")
    dims = image_dimensions(data, detected)
    if dims is None or dims[0] <= 0 or dims[1] <= 0:
        raise ValueError("Image dimensions could not be read")
    cap = max_image_dimension()
    if dims[0] > cap or dims[1] > cap:
        raise ValueError(f"Image dimensions exceed {cap} pixels")


def _check_storage_name(storage_name: str) -> re.Match:
    match = _STORAGE_NAME_RE.fullmatch(storage_name or "")
    if match is None:
        raise ValueError("Invalid storage name")
    return match


def _resolve_storage_path(storage_name: str) -> Path:
    """Validated absolute path for a storage name, or ValueError.

    Pattern check first (no '..', no absolute paths, no separators beyond
    the one listing dir), then a resolved-containment check so a symlinked
    listing directory can never redirect a write/delete outside the root.
    """
    _check_storage_name(storage_name)
    base = ensure_listings_dir().resolve()
    path = base / storage_name
    resolved_parent = path.parent.resolve()
    if resolved_parent.parent != base:
        raise ValueError("Invalid storage name")
    return resolved_parent / path.name


def stored_file_path(storage_name: str) -> Path:
    return _resolve_storage_path(storage_name)


# --------------------------------------------------------------------------
# Write / delete
# --------------------------------------------------------------------------

def _unlink_quietly(path: str | Path | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("media cleanup failed for an incomplete upload")


def save_listing_image(
    listing_id: int, data: bytes, extension: str
) -> tuple[str, int]:
    """Persist validated bytes atomically; returns (storage_name, byte_size).

    storage_name is ``<listing_id>/<uuid>.<ext>`` (unique, no client input).
    Raises ValueError for invalid input and OSError for storage failures; on
    any failure neither the reservation nor the temp file is left behind.
    """
    listing_id = int(listing_id)
    if listing_id <= 0:
        raise ValueError("Invalid listing")
    if extension not in ALLOWED_IMAGE_TYPES.values():
        raise ValueError("Unsupported image type")
    validate_image_content(data, extension)

    storage_name = f"{listing_id}/{uuid.uuid4().hex}{extension}"
    _check_storage_name(storage_name)
    listing_dir = ensure_listings_dir() / str(listing_id)
    _mkdir(listing_dir)
    final_path = _resolve_storage_path(storage_name)

    reserved = False
    tmp_path: str | None = None
    try:
        # 1) Exclusive reservation of the final name: never overwrites.
        fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        reserved = True
        # 2) Bytes into a private temp file in the same directory/filesystem.
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=".tmp",
                                            dir=str(final_path.parent))
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _FILE_MODE)
        # 3) Atomic swap onto the reservation: readers see all or nothing.
        os.replace(tmp_path, final_path)
        tmp_path = None
        try:  # persist the directory entry (best effort; not on all OSes)
            dir_fd = os.open(final_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        _unlink_quietly(tmp_path)
        if reserved:
            _unlink_quietly(final_path)
        raise
    return storage_name, len(data)


def delete_stored_file(storage_name: str) -> bool:
    """Best-effort removal of one stored photo; never raises.

    Returns True when a file was removed. An unsafe/malformed name is
    rejected (nothing touched); a missing file is a no-op. Only a regular
    file (never a symlink or directory) is unlinked, and only the photo's
    own listing directory is removed when it is left empty.
    """
    try:
        path = _resolve_storage_path(storage_name)
    except (ValueError, OSError):
        log.warning("refused to delete media with an invalid storage name")
        return False
    removed = False
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed = True
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("media delete failed")
        return False
    try:
        path.parent.rmdir()  # only succeeds when the listing dir is empty
    except OSError:
        pass
    return removed


def delete_photo(public_id: str | None) -> None:
    """Storage-source-aware deletion dispatcher; never raises.

    ``local:<name>`` -> remove the server file (validated name only).
    Any other non-empty id -> Cloudinary destroy (legacy photos).
    A ``local:`` id is never forwarded to Cloudinary.
    """
    if not public_id:
        return
    if public_id.startswith(LOCAL_PREFIX):
        delete_stored_file(public_id[len(LOCAL_PREFIX):])
        return
    try:
        # Lazy import: photo_storage imports this module for validation.
        from . import photo_storage

        photo_storage.delete_uploaded_image(public_id)
    except Exception:
        log.warning("cloudinary media delete failed")
