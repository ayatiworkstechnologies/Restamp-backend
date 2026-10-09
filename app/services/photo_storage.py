"""RESTAMP Cloudinary photo storage (property listing images).

Dedicated upload backend for POST /owner/listings/{id}/photos. Images go to
Cloudinary only: nothing is written to the server filesystem and no binary
data touches MySQL — the existing PropertyMedia row keeps just the permanent
HTTPS URL (plus category/mime/byte_size/order_index as before).

Credentials come ONLY from environment (CLOUDINARY_CLOUD_NAME/API_KEY/
API_SECRET via settings); they are never hardcoded and never logged.
Layout: <CLOUDINARY_FOLDER>/listings/<listing_id>/photos/<uuid>.<ext>
(default folder "restamp").
"""
import io
import uuid

from ..config import settings

# Reuse the exact validation contract (MIME allowlist + 10 MB cap) so the
# endpoint behavior is unchanged by the storage move.
from ..services.storage import MAX_IMAGE_BYTES, validate_image_upload

_configured = False


def _ensure_configured() -> None:
    """Configure the Cloudinary SDK once from settings (idempotent).

    The credential check runs on every call (not just the first) so an
    unconfigured deployment fails closed even if module state was warmed.
    """
    global _configured
    if not settings.CLOUDINARY_CLOUD_NAME:
        raise OSError("Photo uploads are not configured")
    if _configured:
        return
    import cloudinary

    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY or None,
        api_secret=settings.CLOUDINARY_API_SECRET or None,
        secure=True,
    )
    _configured = True


def upload_listing_image(
    listing_id: int, data: bytes, extension: str, mime_type: str
) -> tuple[str, str, int]:
    """Upload validated image bytes; returns (secure_url, public_id, byte_size).

    public_id is returned so a later DB failure can destroy exactly this
    image (orphan cleanup) without storing anything extra.
    """
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    _ensure_configured()
    import cloudinary.uploader

    folder = f"{settings.CLOUDINARY_FOLDER}/listings/{int(listing_id)}/photos".strip("/")
    public_id = f"{uuid.uuid4().hex}{extension}"
    try:
        result = cloudinary.uploader.upload(
            io.BytesIO(data),
            folder=folder,
            public_id=public_id,
            resource_type="image",
        )
    except Exception as e:
        raise OSError(f"Photo upload failed: {type(e).__name__}") from e
    url = result.get("secure_url") or result.get("url")
    if not url:
        raise OSError("Photo upload failed: no URL returned")
    full_public_id = result.get("public_id") or f"{folder}/{public_id}"
    return url, full_public_id, len(data)


def delete_uploaded_image(public_id: str) -> None:
    """Best-effort orphan cleanup (Cloudinary destroy); never raises."""
    if not public_id:
        return
    try:
        _ensure_configured()
        import cloudinary.uploader

        cloudinary.uploader.destroy(public_id, resource_type="image")
    except Exception:
        pass
