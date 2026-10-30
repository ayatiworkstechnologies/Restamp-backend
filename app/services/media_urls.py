"""RESTAMP listing photo URL validation (client-supplied media URLs).

Owners may send photo URLs in ``images`` / ``photos[].url`` on
POST /owner/listings and PATCH /owner/listings/{id}. Those URLs are stored in
property_media and served to every buyer, so they must point only at our own
image storage — never at an arbitrary site, an internal address, or a
non-HTTP(S) scheme (javascript:, data:, file:, ...).

Production rule (fail closed): the URL must be
    https://res.cloudinary.com/<CLOUDINARY_CLOUD_NAME>/image/upload/...
i.e. HTTPS, the Cloudinary delivery host, and OUR configured cloud only (the
exact shape photo_storage.upload_listing_image returns as secure_url). No
credentials, explicit non-443 ports, IP literals, query, fragment or
path-traversal sequences. With no cloud configured, no client URL passes.

Development: RESTAMP_DEV_MEDIA_URL_PREFIXES may list exact base URLs (each
ending in "/") that are also accepted. It is ignored whenever
is_production() is true, so it can never widen production validation.

Server-generated upload URLs (POST /owner/listings/{id}/photos) do not pass
through here: the server, not the client, chooses them. On PATCH, a URL
already attached to the SAME listing is kept as-is (see
owner_listings._normalize_media_items) so existing galleries — including
local-disk /media URLs — can still be reordered and pruned.

Errors are static strings; the submitted URL is never echoed.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from ..config import is_production, settings

# Matches the frontend gallery cap (src/api/owner.js slices to 20).
MAX_LISTING_PHOTOS = 20
MAX_URL_LENGTH = 500

_CLOUDINARY_HOST = "res.cloudinary.com"
_INVALID = "photo url must be an approved HTTPS image URL"
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")
_ENCODED_TRAVERSAL = ("%2e", "%2f", "%5c")


def dev_media_url_prefixes() -> list[str]:
    """Explicit development prefixes; always empty in production."""
    if is_production():
        return []
    out = []
    for raw in (settings.DEV_MEDIA_URL_PREFIXES or "").split(","):
        prefix = raw.strip()
        parts = urlsplit(prefix) if prefix else None
        # Only well-formed http(s) base URLs ending in "/" count, so a prefix
        # always pins the full host (no "https://a.test" matching
        # "https://a.test.evil.com").
        if (parts is not None and parts.scheme in ("http", "https") and parts.hostname
                and "@" not in parts.netloc and prefix.endswith("/")):
            out.append(prefix)
    return out


def _has_traversal(path: str) -> bool:
    low = path.lower()
    return any(seg in ("..", ".") for seg in path.split("/")) or any(t in low for t in _ENCODED_TRAVERSAL)


def validate_listing_image_url(value: object) -> str:
    """Return the trimmed URL when it is an approved image URL, else raise
    ValueError (the router maps it to 422)."""
    if value is None or not str(value).strip():
        raise ValueError("photo url cannot be empty")
    url = str(value).strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("photo url must be at most 500 characters")
    # Whitespace/control characters and backslashes are never valid in a
    # stored URL and are classic parser-confusion vectors.
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
        raise ValueError(_INVALID)

    for prefix in dev_media_url_prefixes():
        if url.startswith(prefix) and len(url) > len(prefix):
            if _has_traversal(urlsplit(url).path):
                raise ValueError(_INVALID)
            return url

    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise ValueError(_INVALID) from None
    if parts.scheme.lower() != "https":
        raise ValueError(_INVALID)
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError(_INVALID)
    host = (parts.hostname or "").lower()
    if not host or port not in (None, 443):
        raise ValueError(_INVALID)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(_INVALID)  # IP literals: loopback/private/metadata
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError(_INVALID)
    if parts.query or parts.fragment or "#" in url or "?" in url:
        raise ValueError(_INVALID)
    if _has_traversal(parts.path):
        raise ValueError(_INVALID)

    cloud = (settings.CLOUDINARY_CLOUD_NAME or "").strip()
    if not cloud or host != _CLOUDINARY_HOST or not parts.path.startswith(f"/{cloud}/image/upload/"):
        raise ValueError(_INVALID)
    if len(parts.path) <= len(f"/{cloud}/image/upload/"):
        raise ValueError(_INVALID)
    return url
