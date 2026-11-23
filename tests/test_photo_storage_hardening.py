"""Hardening of server-disk property-photo storage (post-review fixes).

Covers: production refusal of non-persistent / in-repository media roots
(H1), the Render ephemeral-filesystem guard, the multi-worker startup
warning (H2), privacy metadata stripping (EXIF/GPS, XMP, text chunks) with
orientation preserved, the upload/delete directory race, and static checks
that the deployment templates stay consistent with the code (single worker,
no uninstalled Uvicorn extras, Nginx mapping/headers, no removed OTP
settings). Cloudinary is always mocked; DB rows roll back via the fixture.
"""
import io
import logging
import os
import re
import struct
from pathlib import Path

import pytest

from tests.test_local_photo_storage import _disk_files, local_mode, webp_vp8x  # noqa: F401
from tests.test_owner_photos import PNG_1PX, _create_listing, _setup_owner, _upload, jpeg_bytes

REPO = Path(__file__).resolve().parents[1]
SAFE_ROOT = "/var/www/restamp/uploads"
GPS_MARKER = b"GPS-SECRET-13.0827N-80.2707E"


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------

def _tiff(orientation=None, extra=b""):
    """Little-endian TIFF/EXIF block: optional Orientation + a GPS-like blob."""
    entries = []
    if orientation is not None:
        entries.append(struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0))
    # An ASCII tag whose value lives after the IFD (stands in for GPS data).
    blob_offset = 8 + 2 + 12 * (len(entries) + 1) + 4
    entries.append(struct.pack("<HHII", 0x010E, 2, len(extra), blob_offset))
    return (b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", len(entries))
            + b"".join(entries) + struct.pack("<I", 0) + extra)


def _jpeg_with_exif(orientation=6, width=40, height=30):
    base = jpeg_bytes(width, height)
    exif = b"Exif\x00\x00" + _tiff(orientation, GPS_MARKER)
    app1 = b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>" + GPS_MARKER + b"</x:xmpmeta>"
    app1_xmp = b"\xff\xe1" + (len(xmp) + 2).to_bytes(2, "big") + xmp
    com = b"\xff\xfe" + (len(GPS_MARKER) + 2).to_bytes(2, "big") + GPS_MARKER
    # SOI | APP0 JFIF | APP1 EXIF | APP1 XMP | COM | rest (SOF0, SOS, data, EOI)
    return base[:20] + app1 + app1_xmp + com + base[20:]


def _png_chunk(ctype, data):
    import zlib
    return (len(data).to_bytes(4, "big") + ctype + data
            + zlib.crc32(ctype + data).to_bytes(4, "big"))


def _png_with_text():
    # PNG_1PX = signature | IHDR | IDAT | IEND ; insert metadata after IHDR.
    ihdr_end = 8 + 25
    meta = (_png_chunk(b"tEXt", b"Comment\x00" + GPS_MARKER)
            + _png_chunk(b"eXIf", _tiff(None, GPS_MARKER))
            + _png_chunk(b"iTXt", b"XML\x00\x00\x00\x00\x00" + GPS_MARKER))
    return PNG_1PX[:ihdr_end] + meta + PNG_1PX[ihdr_end:]


def _webp_with_exif(orientation=8):
    base = webp_vp8x(64, 48)
    flags_off = 20
    chunks = base[12:]
    exif = _tiff(orientation, GPS_MARKER)
    xmp = b"<x:xmpmeta>" + GPS_MARKER + b"</x:xmpmeta>"

    def ch(t, p):
        return t + len(p).to_bytes(4, "little") + p + (b"\x00" if len(p) & 1 else b"")
    body = bytearray(chunks + ch(b"EXIF", exif) + ch(b"XMP ", xmp))
    body[flags_off - 12] |= 0x08 | 0x04
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WEBP" + bytes(body)


# --------------------------------------------------------------------------
# H1: persistent media root (production)
# --------------------------------------------------------------------------

def _prod_local(monkeypatch, root):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "local")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "https://api.restamp.in")
    monkeypatch.delenv("RENDER", raising=False)
    if root is None:
        monkeypatch.delenv("RESTAMP_MEDIA_ROOT", raising=False)
        monkeypatch.setattr(config_module.settings, "MEDIA_ROOT", "/tmp/restamp-media")
    else:
        monkeypatch.setenv("RESTAMP_MEDIA_ROOT", root)
    return config_module


@pytest.mark.parametrize("root", [
    None,                              # default /tmp/restamp-media
    "/tmp/restamp-media",
    "/tmp",
    "/var/tmp/photos",
    "/dev/shm/photos",
    "/run/restamp",
    "relative/uploads",
    "",
    "/",
    "/var/www/restamp/../../tmp/x",
    str(REPO),
    str(REPO / "uploads"),
    str(REPO / "app" / "media"),
])
def test_production_refuses_unsafe_media_root(monkeypatch, root):
    config_module = _prod_local(monkeypatch, root)
    with pytest.raises(RuntimeError, match="RESTAMP_MEDIA_ROOT") as exc:
        config_module.validate_photo_storage_config(production=True)
    if root and len(root) > 1:
        assert root not in str(exc.value)  # names the setting, never the value


@pytest.mark.parametrize("root", [SAFE_ROOT, "/var/lib/restamp/property-photos", "/srv/restamp/photos"])
def test_production_accepts_persistent_media_root(monkeypatch, root):
    config_module = _prod_local(monkeypatch, root)
    config_module.validate_photo_storage_config(production=True)


def test_production_refuses_symlink_into_tmp(monkeypatch, tmp_path_factory):
    import tempfile
    link_parent = tmp_path_factory.mktemp("lnk")
    target = Path(tempfile.gettempdir()) / "restamp-sym-target"
    # The link itself lives under pytest's temp dir; resolve() follows it.
    link = link_parent / "photos"
    link.symlink_to(target)
    config_module = _prod_local(monkeypatch, str(link))
    with pytest.raises(RuntimeError, match="RESTAMP_MEDIA_ROOT"):
        config_module.validate_photo_storage_config(production=True)


def test_development_keeps_tmp_media_root(monkeypatch):
    config_module = _prod_local(monkeypatch, None)
    config_module.validate_photo_storage_config(production=False)


def test_cloudinary_mode_with_cloud_skips_media_root_check(monkeypatch):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "cloudinary")
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", "test-cloud")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "")
    monkeypatch.setenv("RESTAMP_MEDIA_ROOT", "/tmp/restamp-media")
    config_module.validate_photo_storage_config(production=True)


def test_cloudinary_fallback_to_disk_also_requires_persistent_root(monkeypatch):
    """No cloud configured + a public base URL means uploads WOULD land on
    disk (historical fallback), so the root is validated there too."""
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "cloudinary")
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", "")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "https://api.restamp.in")
    monkeypatch.setenv("RESTAMP_MEDIA_ROOT", "/tmp/restamp-media")
    with pytest.raises(RuntimeError, match="RESTAMP_MEDIA_ROOT"):
        config_module.validate_photo_storage_config(production=True)


def test_full_security_gate_refuses_tmp_root(monkeypatch):
    config_module = _prod_local(monkeypatch, "/tmp/restamp-media")
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    with pytest.raises(RuntimeError, match="RESTAMP_MEDIA_ROOT"):
        config_module.validate_security_config()


# --------------------------------------------------------------------------
# Render: never local mode on the ephemeral filesystem
# --------------------------------------------------------------------------

def test_render_refuses_local_mode_without_persistent_disk(monkeypatch):
    config_module = _prod_local(monkeypatch, "/var/data/restamp-photos")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("RESTAMP_RENDER_PERSISTENT_DISK", raising=False)
    with pytest.raises(RuntimeError, match="Render"):
        config_module.validate_photo_storage_config(production=True)
    with pytest.raises(RuntimeError, match="Render"):
        config_module.validate_photo_storage_config(production=False)


def test_render_local_mode_allowed_with_acknowledged_disk(monkeypatch):
    config_module = _prod_local(monkeypatch, "/var/data/restamp-photos")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RESTAMP_RENDER_PERSISTENT_DISK", "1")
    config_module.validate_photo_storage_config(production=True)


def test_render_cloudinary_mode_unaffected(monkeypatch):
    from app import config as config_module
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "cloudinary")
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", "test-cloud")
    config_module.validate_photo_storage_config(production=True)


# --------------------------------------------------------------------------
# H2: multi-worker warning (behaviour otherwise unchanged)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("", False), ("1", False), ("2", True), ("4", True), ("x", False)])
def test_multi_worker_detection(monkeypatch, value, expected):
    from app import worker
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    assert worker.multi_worker_configured() is expected


def test_multi_worker_warning_logged(monkeypatch, caplog):
    from app import worker
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    with caplog.at_level(logging.WARNING, logger="restamp.worker"):
        worker.warn_if_multi_worker()
    assert any("WEB_CONCURRENCY" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Privacy: metadata stripping (unit)
# --------------------------------------------------------------------------

def test_jpeg_exif_xmp_comment_stripped_orientation_kept():
    from app.services import image_metadata, storage
    src = _jpeg_with_exif(orientation=6, width=40, height=30)
    out = image_metadata.strip_metadata(src, "image/jpeg")
    assert GPS_MARKER not in out and b"xmpmeta" not in out
    assert out.startswith(b"\xff\xd8\xff\xe0")            # JFIF APP0 still first
    assert storage.image_dimensions(out, "image/jpeg") == (40, 30)
    exif_at = out.index(b"Exif\x00\x00")
    assert image_metadata._exif_orientation(out[exif_at + 6:]) == 6
    assert out.endswith(src[-140:])                        # scan data untouched
    storage.validate_image_content(out, ".jpg")


def test_jpeg_trailing_secondary_image_dropped():
    """Data after the primary EOI (MPF secondary images, gain/depth maps)
    can carry its own EXIF/GPS: it is not published."""
    from app.services import image_metadata
    primary = jpeg_bytes(20, 20)
    trailer = _jpeg_with_exif(orientation=3)           # appended "secondary image"
    out = image_metadata.strip_metadata(primary + trailer, "image/jpeg")
    assert out == primary
    assert GPS_MARKER not in out


def test_jpeg_orientation_1_or_absent_writes_no_exif():
    from app.services import image_metadata
    for src in (_jpeg_with_exif(orientation=1), _jpeg_with_exif(orientation=None)):
        out = image_metadata.strip_metadata(src, "image/jpeg")
        assert b"Exif" not in out and GPS_MARKER not in out


def test_clean_jpeg_and_png_unchanged():
    from app.services import image_metadata
    clean = jpeg_bytes(10, 10)
    assert image_metadata.strip_metadata(clean, "image/jpeg") == clean
    assert image_metadata.strip_metadata(PNG_1PX, "image/png") == PNG_1PX


def test_png_text_and_exif_chunks_stripped():
    from app.services import image_metadata, storage
    out = image_metadata.strip_metadata(_png_with_text(), "image/png")
    assert GPS_MARKER not in out
    assert out == PNG_1PX                                   # only metadata removed
    storage.validate_image_content(out, ".png")


def test_webp_exif_xmp_stripped_orientation_kept():
    from app.services import image_metadata, storage
    src = _webp_with_exif(orientation=8)
    out = image_metadata.strip_metadata(src, "image/webp")
    assert GPS_MARKER not in out and b"XMP " not in out
    assert int.from_bytes(out[4:8], "little") == len(out) - 8   # RIFF size fixed
    assert out[20] & 0x04 == 0                                    # XMP flag cleared
    assert out[20] & 0x08 == 0x08                                 # EXIF = orientation only
    exif_at = out.index(b"EXIF")
    assert image_metadata._exif_orientation(out[exif_at + 8:]) == 8
    assert storage.image_dimensions(out, "image/webp") == (64, 48)


@pytest.mark.parametrize("data,mime", [
    (jpeg_bytes()[:30], "image/jpeg"),                       # truncated before SOS
    (b"\xff\xd8\xff\xe1\xff\xff" + b"0" * 10, "image/jpeg"),  # segment overruns
    (PNG_1PX[:-12], "image/png"),                            # no IEND
    (b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),             # no chunks
])
def test_unwalkable_structures_fail_closed(data, mime):
    from app.services import image_metadata
    with pytest.raises(ValueError):
        image_metadata.strip_metadata(data, mime)


# --------------------------------------------------------------------------
# Privacy: end to end (stored file + Cloudinary bytes)
# --------------------------------------------------------------------------

def test_local_upload_stores_stripped_file(client, local_mode):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545701")
    lid = _create_listing(c, token, "Strip Local Flat")
    src = _jpeg_with_exif(orientation=6)
    r = _upload(c, token, lid, src, "gps.jpg", "image/jpeg")
    assert r.status_code == 201, r.text
    (path,) = _disk_files(root)
    stored = Path(path).read_bytes()
    assert GPS_MARKER not in stored and b"xmpmeta" not in stored
    assert r.json()["byte_size"] == len(stored) < len(src)


def test_cloudinary_upload_receives_stripped_bytes(client, monkeypatch):
    from app import config as config_module
    from app.services import photo_storage as photo_svc
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", "test-cloud")
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "cloudinary")
    seen = []

    def fake_upload(listing_id, data, extension, mime_type):
        seen.append(data)
        return ("https://res.cloudinary.com/test-cloud/image/upload/restamp/x.jpg",
                "restamp/x", len(data))
    monkeypatch.setattr(photo_svc, "upload_listing_image", fake_upload)
    monkeypatch.setattr(photo_svc, "delete_uploaded_image", lambda pid: None)
    c, session = client
    token = _setup_owner(c, session, "+919876545702")
    lid = _create_listing(c, token, "Strip Cloud Flat")
    r = _upload(c, token, lid, _jpeg_with_exif(), "gps.jpg", "image/jpeg")
    assert r.status_code == 201, r.text
    assert r.json()["url"].startswith("https://res.cloudinary.com/")   # contract unchanged
    assert seen and GPS_MARKER not in seen[0]


def test_unwalkable_upload_rejected_without_file(client, local_mode):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545703")
    lid = _create_listing(c, token, "Strip Reject Flat")
    # Valid header + dimensions, but no SOS: cannot be stripped safely.
    no_scan = jpeg_bytes(5, 5)
    no_scan = no_scan[:no_scan.index(b"\xff\xda")] + b"\xff\xd9"
    r = _upload(c, token, lid, no_scan, "a.jpg", "image/jpeg")
    assert r.status_code == 422, r.text
    assert _disk_files(root) == []


# --------------------------------------------------------------------------
# Upload / delete directory race
# --------------------------------------------------------------------------

def test_reservation_retries_when_listing_dir_removed_concurrently(local_mode, monkeypatch):
    from app.services import storage
    real_open = os.open
    state = {"raced": False}

    def racing_open(path, flags, *args):
        # First reservation attempt: a concurrent delete removed the dir.
        if not state["raced"] and str(path).endswith(".jpg") and flags & os.O_EXCL:
            state["raced"] = True
            os.rmdir(os.path.dirname(path))
            raise FileNotFoundError(path)
        return real_open(path, flags, *args)

    monkeypatch.setattr(storage.os, "open", racing_open)
    name, size = storage.save_listing_image(77, jpeg_bytes(8, 8), ".jpg")
    assert state["raced"]
    assert storage.stored_file_path(name).read_bytes() == jpeg_bytes(8, 8)


def test_reservation_gives_up_after_bounded_retries(local_mode, monkeypatch):
    from app.services import storage
    real_open = os.open

    def always_missing(path, flags, *args):
        if str(path).endswith(".jpg") and flags & os.O_EXCL:
            raise FileNotFoundError(path)
        return real_open(path, flags, *args)

    monkeypatch.setattr(storage.os, "open", always_missing)
    with pytest.raises(OSError):
        storage.save_listing_image(78, jpeg_bytes(8, 8), ".jpg")
    root, _ = local_mode
    assert _disk_files(root) == []


def test_delete_then_upload_same_listing_succeeds(client, local_mode):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545704")
    lid = _create_listing(c, token, "Race Flat")
    first = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    assert c.delete(f"/owner/listings/{lid}/photos/{first['id']}",
                    headers={"Authorization": f"Bearer {token}"}).status_code == 204
    assert _disk_files(root) == []               # empty listing dir removed
    again = _upload(c, token, lid, PNG_1PX, "b.png", "image/png")
    assert again.status_code == 201, again.text
    assert len(_disk_files(root)) == 1


# --------------------------------------------------------------------------
# Deployment templates stay consistent with the code
# --------------------------------------------------------------------------

def _read(rel):
    return (REPO / rel).read_text()


def test_systemd_template_single_worker_no_uninstalled_extras():
    unit = _read("deploy/systemd/restamp-backend.service.template")
    exec_start = unit[unit.index("ExecStart="):].split("\n\n")[0]
    assert "--workers 1" in exec_start
    assert "uvloop" not in exec_start and "httptools" not in exec_start
    assert "ReadWritePaths=/var/www/restamp/uploads" in unit
    assert "mysql.service" not in unit.replace("no local mysql.service", "")
    reqs = _read("requirements.txt").lower()
    assert "uvloop" not in reqs and "httptools" not in reqs  # flags would need them


def test_nginx_snippet_mapping_and_headers():
    conf = _read("deploy/nginx/restamp-media.location.conf")
    body = "\n".join(l for l in conf.splitlines() if not l.lstrip().startswith("#"))
    assert "alias /var/www/restamp/uploads/listings/$1/$2.$3;" in body
    assert "try_files" not in body and "expires" not in body
    assert body.count("Cache-Control") == 1
    assert "nosniff" in body and "autoindex off" in body
    assert "limit_except GET HEAD" in body
    regex = re.search(r'location ~ "(\^/media/[^"]+)"', body).group(1)
    # The Nginx regex accepts exactly the names storage.py generates.
    from app.services.storage import _STORAGE_NAME_RE
    pattern = re.compile(regex)
    good = "/media/2197/" + "a" * 32 + ".jpg"
    assert pattern.match(good) and _STORAGE_NAME_RE.fullmatch(good[len("/media/"):])
    for bad in ("/media/2197/../../etc/passwd", "/media/2197/", "/media/0/" + "a" * 32 + ".jpg",
                "/media/1/" + "A" * 32 + ".jpg", "/media/1/" + "a" * 32 + ".php",
                "/media/1/" + "a" * 32 + ".jpeg"):
        assert not pattern.match(bad), bad


def test_upload_limits_consistent_with_nginx_guidance():
    from app.config import PHOTO_MAX_BYTES_LIMITS
    conf = _read("deploy/nginx/restamp-media.location.conf")
    m = re.search(r"client_max_body_size (\d+)m", conf)
    assert m, "snippet must document client_max_body_size"
    assert int(m.group(1)) * 1024 * 1024 >= PHOTO_MAX_BYTES_LIMITS[0] + 1024 * 1024
    assert "client_max_body_size 12m" in _read("deploy/README.md")


def test_env_template_has_no_removed_otp_settings_or_secrets():
    env = _read("deploy/env.production.example")
    assert not re.search(r"^\s*#?\s*RESTAMP_OTP_", env, re.M)
    assert "RESTAMP_MEDIA_ROOT=/var/www/restamp/uploads" in env
    assert "RESTAMP_DB_SSL_CA" in env
    # No literal credentials: every secret-ish assignment is a placeholder.
    for line in env.splitlines():
        if re.match(r"^\s*#?\s*\w*(SECRET|PASSWORD|API_KEY|TOKEN)\w*=", line):
            assert "<" in line, line


def test_render_blueprint_keeps_cloudinary():
    render = _read("render.yaml")
    assert not re.search(r"key:\s*RESTAMP_PHOTO_STORAGE\s*\n\s*value:\s*\"?local", render)
