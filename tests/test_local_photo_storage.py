"""Server-disk property-photo storage (RESTAMP_PHOTO_STORAGE=local).

Covers: local upload + real file on disk, absolute https URLs from
RESTAMP_MEDIA_PUBLIC_BASE_URL (never request.base_url), ordering/cover,
the storage-source-aware deletion dispatcher (DELETE, PATCH reconcile,
upload-failure cleanup), mixed Cloudinary + local galleries, magic-byte
validation, atomic writes, path-traversal refusal, persistence across an
app restart, and the production startup gate.

Cloudinary is always mocked (no network, no credentials). Every file lives
under pytest's tmp_path via RESTAMP_MEDIA_ROOT. DB rows roll back via the
shared fixture.
"""
import base64
import io
import os
import re
import stat
import zlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_photos import (
    JPEG_BYTES,
    PNG_1PX,
    _create_listing,
    _mock_cloud,
    _setup_owner,
    _upload,
)

BASE = "https://media.restamp.test"


def webp_vp8x(width: int = 1, height: int = 1) -> bytes:
    """Minimal extended-format (VP8X) WebP header carrying a canvas size."""
    return (b"RIFF" + (22).to_bytes(4, "little") + b"WEBPVP8X" + (10).to_bytes(4, "little")
            + b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


WEBP_BYTES = webp_vp8x()
LEGACY_URL = "https://res.cloudinary.com/test-cloud/image/upload/restamp/listings/9/photos/old.jpg"
LEGACY_PID = "restamp/listings/9/photos/old"
_URL_RE = re.compile(r"^https://media\.restamp\.test/media/(\d+)/([0-9a-f]{32})\.(png|jpg|webp)$")


@pytest.fixture
def local_mode(monkeypatch, tmp_path):
    """Local mode with a public base URL; Cloudinary configured + mocked so
    any accidental Cloudinary call is recorded. Returns (root, calls)."""
    from app import config as config_module

    calls: list = []
    _mock_cloud(monkeypatch, calls)  # also sets CLOUDINARY_CLOUD_NAME
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "local")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("RESTAMP_MEDIA_ROOT", str(tmp_path))
    return tmp_path, calls


def _disk_files(root):
    """Every regular file under <root>/listings (incl. temp leftovers)."""
    out = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "listings")):
        out.extend(os.path.join(dirpath, f) for f in files)
    return sorted(out)


def _path_for(root, url):
    m = _URL_RE.match(url)
    assert m, url
    return os.path.join(root, "listings", m.group(1), f"{m.group(2)}.{m.group(3)}")


def _cloud_uploads(calls):
    return [x for x in calls if x[0] == "upload"]


def _cloud_destroys(calls):
    return [x for x in calls if x[0] == "destroy"]


# --------------------------------------------------------------------------
# Upload, URL, ordering
# --------------------------------------------------------------------------

def test_local_upload_writes_file_with_https_url(client, local_mode):
    root, calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545601")
    lid = _create_listing(c, token, "Local Upload Flat")
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Bedroom")
    assert r.status_code == 201, r.text
    body = r.json()
    # Same PhotoUploadOut fields as before; URL is absolute https on BASE.
    assert set(body) == {"id", "url", "category", "mime_type", "byte_size", "order_index", "is_cover"}
    m = _URL_RE.match(body["url"])
    assert m and int(m.group(1)) == lid
    assert "127.0.0.1" not in body["url"] and "testserver" not in body["url"]
    assert body["byte_size"] == len(PNG_1PX) and body["is_cover"] is True
    path = _path_for(root, body["url"])
    with open(path, "rb") as fh:
        assert fh.read() == PNG_1PX
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert _disk_files(root) == [path]  # no temp file left
    pid = session.execute(text("SELECT public_id, url FROM property_media WHERE id=:i"),
                          {"i": body["id"]}).fetchone()
    assert pid[0] == f"local:{lid}/{m.group(2)}.png" and pid[1] == body["url"]
    assert _cloud_uploads(calls) == []


def test_multiple_uploads_keep_order_and_cover(client, local_mode):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545602")
    lid = _create_listing(c, token, "Local Multi Flat")
    urls = []
    for content, name, mime in ((PNG_1PX, "a.png", "image/png"),
                                (JPEG_BYTES, "b.jpg", "image/jpeg"),
                                (WEBP_BYTES, "c.webp", "image/webp")):
        r = _upload(c, token, lid, content, name, mime)
        assert r.status_code == 201, r.text
        urls.append(r.json()["url"])
    assert len(set(urls)) == 3 and len(_disk_files(root)) == 3
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert [p["url"] for p in detail["photos"]] == urls
    assert [p["order_index"] for p in detail["photos"]] == [0, 1, 2]
    assert [p["is_cover"] for p in detail["photos"]] == [True, False, False]
    assert detail["cover_image_url"] == urls[0]


def test_gallery_cap_still_20_and_no_orphan(client, local_mode, monkeypatch):
    root, _ = local_mode
    from app.services import owner_listings as owner_svc
    c, session = client
    token = _setup_owner(c, session, "+919876545603")
    lid = _create_listing(c, token, "Local Cap Flat")
    monkeypatch.setattr(owner_svc, "listing_photo_count", lambda db, i: 20)
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 422 and "20" in r.json()["detail"]
    assert _disk_files(root) == []


def test_development_without_base_url_uses_request_base(client, local_mode, monkeypatch):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "")
    c, session = client
    token = _setup_owner(c, session, "+919876545604")
    lid = _create_listing(c, token, "Local Dev Base Flat")
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 201, r.text
    assert r.json()["url"].startswith("http://testserver/media/")


def test_production_without_base_url_fails_closed_no_file(client, local_mode, monkeypatch):
    root, _ = local_mode
    from app import config as config_module
    from app.services import storage as storage_svc
    c, session = client
    token = _setup_owner(c, session, "+919876545605")
    lid = _create_listing(c, token, "Local Prod No Base Flat")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(storage_svc, "is_production", lambda: True)
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 500 and r.json()["detail"] == "Upload failed"
    assert _disk_files(root) == []
    n = session.execute(text("SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
                        {"i": lid}).fetchone()[0]
    assert n == 0


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("content,name,mime", [
    (b"just some text, not an image" * 4, "a.png", "image/png"),      # no signature
    (b"GIF89a" + b"\x00" * 64, "a.png", "image/png"),                 # GIF disguised
    (JPEG_BYTES, "a.png", "image/png"),                                # real JPEG, says PNG
    (PNG_1PX, "a.jpg", "image/jpeg"),                                  # real PNG, says JPEG
    (b"<?php echo 1; ?>" + b"\x00" * 32, "a.webp", "image/webp"),      # script
    (b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16, "a.webp", "image/webp"),  # RIFF non-WebP
])
def test_invalid_image_signature_rejected_no_file(client, local_mode, content, name, mime):
    root, calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545606")
    lid = _create_listing(c, token, "Local Bad Sig Flat")
    r = _upload(c, token, lid, content, name, mime)
    assert r.status_code == 422, r.text
    assert _disk_files(root) == [] and _cloud_uploads(calls) == []


def test_signature_also_enforced_in_cloudinary_mode(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876545607")
    lid = _create_listing(c, token, "Cloud Bad Sig Flat")
    r = _upload(c, token, lid, b"not an image at all" * 3, "a.jpg", "image/jpeg")
    assert r.status_code == 422 and _cloud_uploads(calls) == []


@pytest.mark.parametrize("content,name,mime", [
    (PNG_1PX, "a.png", "image/jpeg"),     # extension vs MIME
    (PNG_1PX, "a.exe", "image/png"),      # disallowed extension
    (PNG_1PX, "a.png", "image/gif"),      # disallowed MIME
    (b"0" * (10 * 1024 * 1024 + 1), "big.jpg", "image/jpeg"),
    (b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024), "big.jpg", "image/jpeg"),
    (b"", "empty.jpg", "image/jpeg"),
])
def test_mime_extension_size_and_empty_rejected_no_file(client, local_mode, content, name, mime):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545608")
    lid = _create_listing(c, token, "Local Reject Flat")
    r = _upload(c, token, lid, content, name, mime)
    assert r.status_code == 422, r.text
    assert _disk_files(root) == []


def test_sniff_image_mime_unit():
    from app.services.storage import sniff_image_mime
    assert sniff_image_mime(PNG_1PX) == "image/png"
    assert sniff_image_mime(JPEG_BYTES) == "image/jpeg"
    assert sniff_image_mime(WEBP_BYTES) == "image/webp"
    for bad in (b"", b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"RIFF1234WEBPXXXX", b"GIF89a" * 4):
        assert sniff_image_mime(bad) is None


# --------------------------------------------------------------------------
# Atomic writes / failure cleanup
# --------------------------------------------------------------------------

def test_failed_write_leaves_no_partial_or_temp_file(client, local_mode, monkeypatch):
    root, _ = local_mode
    from app.services import storage as storage_svc
    c, session = client
    token = _setup_owner(c, session, "+919876545609")
    lid = _create_listing(c, token, "Local Write Fail Flat")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(storage_svc.os, "replace", boom)
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 500 and r.json()["detail"] == "Upload failed"
    assert _disk_files(root) == []  # neither reservation nor temp file
    n = session.execute(text("SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
                        {"i": lid}).fetchone()[0]
    assert n == 0


def test_db_failure_after_save_removes_local_file(client, local_mode, monkeypatch):
    root, calls = local_mode
    from app.services import owner_listings as owner_svc
    c, session = client
    token = _setup_owner(c, session, "+919876545610")
    lid = _create_listing(c, token, "Local Db Fail Flat")

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(owner_svc, "add_listing_photo", boom)
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 500
    assert _disk_files(root) == []
    assert _cloud_destroys(calls) == []  # local: id never sent to Cloudinary


def test_exclusive_creation_never_overwrites(local_mode, monkeypatch):
    root, _ = local_mode
    import uuid as uuid_module
    from app.services import storage as storage_svc
    fixed = uuid_module.UUID("0" * 31 + "1")
    monkeypatch.setattr(storage_svc.uuid, "uuid4", lambda: fixed)
    name, _ = storage_svc.save_listing_image(7, PNG_1PX, ".png")
    with pytest.raises(FileExistsError):
        storage_svc.save_listing_image(7, PNG_1PX + b"x", ".png")
    path = storage_svc.stored_file_path(name)
    with open(path, "rb") as fh:
        assert fh.read() == PNG_1PX  # original untouched
    assert _disk_files(root) == [str(path)]


# --------------------------------------------------------------------------
# Deletion dispatcher
# --------------------------------------------------------------------------

def test_delete_local_photo_removes_file_and_promotes_cover(client, local_mode):
    root, calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545611")
    lid = _create_listing(c, token, "Local Delete Flat")
    first = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    second = _upload(c, token, lid, JPEG_BYTES, "b.jpg", "image/jpeg").json()
    r = c.delete(f"/owner/listings/{lid}/photos/{first['id']}", headers=auth_header(token))
    assert r.status_code == 204, r.text
    assert not os.path.exists(_path_for(root, first["url"]))
    assert os.path.exists(_path_for(root, second["url"]))
    assert _cloud_destroys(calls) == []
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert detail["cover_image_url"] == second["url"]
    assert detail["photos"][0]["is_cover"] is True
    # Deleting the last photo also drops the now-empty listing directory.
    c.delete(f"/owner/listings/{lid}/photos/{second['id']}", headers=auth_header(token))
    assert not os.path.exists(os.path.join(root, "listings", str(lid)))


def test_patch_replace_and_remove_local_photos(client, local_mode):
    root, calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545612")
    lid = _create_listing(c, token, "Local Patch Flat")
    a = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    b = _upload(c, token, lid, JPEG_BYTES, "b.jpg", "image/jpeg").json()
    # Replace a with a new URL and move b to cover.
    r = c.patch(f"/owner/listings/{lid}", headers=auth_header(token), json={"photos": [
        {"url": b["url"], "category": "Kitchen"},
        {"url": "https://t.test/replacement.jpg", "category": "Bedroom"},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["cover_image_url"] == b["url"]
    assert not os.path.exists(_path_for(root, a["url"]))
    assert os.path.exists(_path_for(root, b["url"]))
    # Clear the gallery: the remaining local file is removed too.
    r = c.patch(f"/owner/listings/{lid}", headers=auth_header(token), json={"photos": []})
    assert r.status_code == 200, r.text
    assert _disk_files(root) == []
    assert _cloud_destroys(calls) == []


def _insert_legacy_cloud_row(session, lid, order_index=0):
    return session.execute(text(
        "INSERT INTO property_media (property_listing_id, url, media_type, order_index, "
        "category, mime_type, byte_size, public_id) VALUES "
        "(:l, :u, 'IMAGE', :o, 'Exterior', 'image/jpeg', 1234, :p)"),
        {"l": lid, "u": LEGACY_URL, "o": order_index, "p": LEGACY_PID}).lastrowid


def test_mixed_gallery_routes_each_delete_to_its_own_store(client, local_mode):
    root, calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545613")
    lid = _create_listing(c, token, "Mixed Gallery Flat")
    legacy_id = _insert_legacy_cloud_row(session, lid)
    session.commit()
    local = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    assert local["order_index"] == 1 and local["is_cover"] is False
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert [p["url"] for p in detail["photos"]] == [LEGACY_URL, local["url"]]
    assert detail["cover_image_url"] == LEGACY_URL  # legacy URL untouched
    # Legacy delete -> Cloudinary destroy only; local file untouched.
    r = c.delete(f"/owner/listings/{lid}/photos/{legacy_id}", headers=auth_header(token))
    assert r.status_code == 204
    assert _cloud_destroys(calls) == [("destroy", LEGACY_PID)]
    assert os.path.exists(_path_for(root, local["url"]))
    # Local delete -> disk only; no further Cloudinary call.
    r = c.delete(f"/owner/listings/{lid}/photos/{local['id']}", headers=auth_header(token))
    assert r.status_code == 204
    assert _cloud_destroys(calls) == [("destroy", LEGACY_PID)]
    assert _disk_files(root) == []


def test_patch_removing_mixed_rows_dispatches_both(client, local_mode):
    root, calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545614")
    lid = _create_listing(c, token, "Mixed Patch Flat")
    _insert_legacy_cloud_row(session, lid)
    session.commit()
    local = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    r = c.patch(f"/owner/listings/{lid}", headers=auth_header(token), json={"photos": []})
    assert r.status_code == 200, r.text
    assert _cloud_destroys(calls) == [("destroy", LEGACY_PID)]
    assert not os.path.exists(_path_for(root, local["url"]))


def test_legacy_cloudinary_photo_visible_to_buyers_unchanged(client, local_mode):
    _root, _calls = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545615")
    lid = _create_listing(c, token, "Legacy Buyer Flat")
    _insert_legacy_cloud_row(session, lid)
    session.commit()
    local = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    session.execute(text(
        "UPDATE property_listings SET verification_status='VERIFIED', "
        "listing_status='AVAILABLE' WHERE id=:i"), {"i": lid})
    session.commit()
    r = c.get(f"/buyer/listings/{lid}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["cover_image_url"] == LEGACY_URL
    assert detail["gallery"] == [LEGACY_URL, local["url"]]
    assert all(u.startswith("https://") for u in detail["gallery"])


def test_unauthorized_delete_keeps_file(client, local_mode):
    root, _ = local_mode
    c, session = client
    token_a = _setup_owner(c, session, "+919876545616")
    token_b = _setup_owner(c, session, "+919876545617")
    buyer = _setup_owner(c, session, "+919876545618", role="BUYER")
    lid = _create_listing(c, token_a, "Local Authz Flat")
    photo = _upload(c, token_a, lid, PNG_1PX, "a.png", "image/png").json()
    path = _path_for(root, photo["url"])
    url = f"/owner/listings/{lid}/photos/{photo['id']}"
    assert c.delete(url).status_code == 401
    assert c.delete(url, headers=auth_header(buyer)).status_code == 403
    assert c.delete(url, headers=auth_header(token_b)).status_code == 404
    assert os.path.exists(path)


def test_soft_delete_listing_keeps_local_files(client, local_mode):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545619")
    lid = _create_listing(c, token, "Local Soft Delete Flat")
    photo = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    assert c.delete(f"/owner/listings/{lid}", headers=auth_header(token)).status_code == 204
    assert os.path.exists(_path_for(root, photo["url"]))


# --------------------------------------------------------------------------
# Path traversal
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../outside.png", "1/../../outside.png", "/etc/passwd", "1/../2/" + "a" * 32 + ".png",
    "1/" + "A" * 32 + ".png", "1/" + "a" * 32 + ".gif", "0/" + "a" * 32 + ".png",
    "1/2/" + "a" * 32 + ".png", "", "1\\" + "a" * 32 + ".png",
])
def test_delete_stored_file_refuses_unsafe_names(local_mode, name):
    root, _ = local_mode
    from app.services import storage as storage_svc
    victim = root / "outside.png"
    victim.write_bytes(b"keep")
    assert storage_svc.delete_stored_file(name) is False
    assert victim.read_bytes() == b"keep"


def test_dispatcher_never_sends_local_ids_to_cloudinary(local_mode):
    _root, calls = local_mode
    from app.services import storage as storage_svc
    for pid in ("local:../../etc/passwd", "local:", "local:1/x.png"):
        storage_svc.delete_photo(pid)
    storage_svc.delete_photo(None)
    storage_svc.delete_photo("")
    assert _cloud_destroys(calls) == []
    storage_svc.delete_photo(LEGACY_PID)
    assert _cloud_destroys(calls) == [("destroy", LEGACY_PID)]


def test_missing_local_file_is_safe_noop(local_mode):
    from app.services import storage as storage_svc
    assert storage_svc.delete_stored_file("5/" + "b" * 32 + ".png") is False


def test_symlinked_listing_dir_cannot_escape_root(local_mode, tmp_path_factory):
    root, _ = local_mode
    from app.services import storage as storage_svc
    outside = tmp_path_factory.mktemp("outside")
    name = "c" * 32 + ".png"
    (outside / name).write_bytes(b"keep")
    listings = storage_svc.ensure_listings_dir()
    os.symlink(outside, listings / "42")
    assert storage_svc.delete_stored_file(f"42/{name}") is False
    assert (outside / name).read_bytes() == b"keep"
    with pytest.raises(ValueError):
        storage_svc.stored_file_path(f"42/{name}")


# --------------------------------------------------------------------------
# Persistence across restart + static serving
# --------------------------------------------------------------------------

def test_files_persist_and_serve_after_app_restart(client, local_mode):
    root, _ = local_mode
    c, session = client
    token = _setup_owner(c, session, "+919876545620")
    lid = _create_listing(c, token, "Local Restart Flat")
    photo = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    from app.main import create_app
    restarted = TestClient(create_app())  # new process state, same disk root
    path_part = photo["url"][len(BASE):]
    r = restarted.get(path_part)
    assert r.status_code == 200
    assert r.content == PNG_1PX
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert restarted.get("/media/").status_code == 404  # no directory listing
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert detail["photos"][0]["url"] == photo["url"]


# --------------------------------------------------------------------------
# Existing Cloudinary behaviour (default mode)
# --------------------------------------------------------------------------

def test_default_mode_still_uses_cloudinary_and_writes_no_file(client, monkeypatch, tmp_path):
    from app import config as config_module
    calls = []
    _mock_cloud(monkeypatch, calls)
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "cloudinary")
    monkeypatch.setenv("RESTAMP_MEDIA_ROOT", str(tmp_path))
    c, session = client
    token = _setup_owner(c, session, "+919876545621")
    lid = _create_listing(c, token, "Cloud Default Flat")
    body = _upload(c, token, lid, PNG_1PX, "a.png", "image/png").json()
    assert body["url"].startswith("https://res.cloudinary.com/")
    assert len(_cloud_uploads(calls)) == 1 and _disk_files(tmp_path) == []
    pid = session.execute(text("SELECT public_id FROM property_media WHERE id=:i"),
                          {"i": body["id"]}).fetchone()[0]
    assert c.delete(f"/owner/listings/{lid}/photos/{body['id']}",
                    headers=auth_header(token)).status_code == 204
    assert ("destroy", pid) in calls


# --------------------------------------------------------------------------
# Startup validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,base,production,ok", [
    ("cloudinary", "", True, True),
    ("local", "https://api.restamp.in", True, True),
    ("local", "https://restamp.in/api", True, True),
    ("local", "", True, False),
    ("local", "http://api.restamp.in", True, False),
    ("local", "https://api.restamp.in/", True, False),
    ("local", "https://127.0.0.1", True, False),
    ("local", "https://localhost", True, False),
    ("local", "https://media.internal", True, False),
    ("local", "https://u:p@api.restamp.in", True, False),
    ("local", "https://api.restamp.in?x=1", True, False),
    ("local", "https://api.restamp.in:abc", True, False),
    ("local", "", False, True),
    ("local", "http://192.168.1.5:8000", False, True),
    ("local", "ftp://x.test", False, False),
    ("s3", "", False, False),
    ("", "", True, False),
])
def test_photo_storage_startup_validation(monkeypatch, mode, base, production, ok):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", mode)
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", base)
    if ok:
        config_module.validate_photo_storage_config(production=production)
    else:
        with pytest.raises(RuntimeError) as exc:
            config_module.validate_photo_storage_config(production=production)
        if base:
            assert base not in str(exc.value)  # names the setting, never the value


def test_full_security_gate_refuses_local_production_without_base(monkeypatch):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "local")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "")
    with pytest.raises(RuntimeError, match="RESTAMP_MEDIA_PUBLIC_BASE_URL"):
        config_module.validate_security_config()


# --------------------------------------------------------------------------
# Configurable limits, dimension parsing, and authorization tests
# --------------------------------------------------------------------------

def png_bytes(width: int, height: int) -> bytes:
    """Create a minimal structurally valid PNG with arbitrary width/height."""
    ihdr = b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    crc = zlib.crc32(ihdr).to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\r" + ihdr + crc


def webp_vp8(width: int, height: int) -> bytes:
    """Create a minimal lossy VP8 WebP with keyframe and 14-bit dimensions."""
    frame = b"\x00\x00\x00\x9d\x01\x2a" + (width & 0x3FFF).to_bytes(2, "little") + (height & 0x3FFF).to_bytes(2, "little")
    return b"RIFF" + (len(frame) + 12).to_bytes(4, "little") + b"WEBPVP8 " + len(frame).to_bytes(4, "little") + frame


def webp_vp8l(width: int, height: int) -> bytes:
    """Create a minimal lossless VP8L WebP with signature 0x2f and 14-bit sizes."""
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    data = b"\x2f" + bits.to_bytes(4, "little")
    return b"RIFF" + (len(data) + 12).to_bytes(4, "little") + b"WEBPVP8L" + len(data).to_bytes(4, "little") + data


def test_configurable_max_bytes_enforced_in_upload(client, local_mode, monkeypatch):
    root, _ = local_mode
    from app import config as config_module
    c, session = client
    token = _setup_owner(c, session, "+919876545630")
    lid = _create_listing(c, token, "Local Limit Bytes Flat")

    # Set custom limit of 1 MB (1048576 bytes)
    monkeypatch.setattr(config_module.settings, "PHOTO_MAX_BYTES_RAW", "1048576")
    # File slightly over 1 MB (JPEG with pad)
    oversized = JPEG_BYTES + b"0" * (1024 * 1024)
    r = _upload(c, token, lid, oversized, "large.jpg", "image/jpeg")
    assert r.status_code == 422, r.text
    assert "Image exceeds 1 MB" in r.json()["detail"]
    assert _disk_files(root) == []

    # File within 1 MB (standard JPEG ~150 bytes)
    r = _upload(c, token, lid, JPEG_BYTES, "small.jpg", "image/jpeg")
    assert r.status_code == 201, r.text
    assert len(_disk_files(root)) == 1

    # Non-megabyte multiple formatting (e.g. 200000 bytes)
    monkeypatch.setattr(config_module.settings, "PHOTO_MAX_BYTES_RAW", "200000")
    r = _upload(c, token, lid, b"\xff\xd8\xff" + b"0" * 200001, "over.jpg", "image/jpeg")
    assert r.status_code == 422
    assert "Image exceeds 200000 bytes" in r.json()["detail"]


def test_configurable_max_dimension_enforced_in_upload(client, local_mode, monkeypatch):
    root, _ = local_mode
    from app import config as config_module
    c, session = client
    token = _setup_owner(c, session, "+919876545631")
    lid = _create_listing(c, token, "Local Limit Dimension Flat")

    # Set custom limit of 500 pixels
    monkeypatch.setattr(config_module.settings, "PHOTO_MAX_DIMENSION_RAW", "500")

    # Image with dimension 501x100 -> rejected
    big_png = png_bytes(501, 100)
    r = _upload(c, token, lid, big_png, "big.png", "image/png")
    assert r.status_code == 422, r.text
    assert "Image dimensions exceed 500 pixels" in r.json()["detail"]
    assert _disk_files(root) == []

    # Image with dimension 500x500 -> accepted
    ok_png = png_bytes(500, 500)
    r = _upload(c, token, lid, ok_png, "ok.png", "image/png")
    assert r.status_code == 201, r.text
    assert len(_disk_files(root)) == 1


def test_dimension_parsing_png_jpeg_webp():
    from app.services.storage import image_dimensions

    # PNG parsing
    assert image_dimensions(png_bytes(1, 1), "image/png") == (1, 1)
    assert image_dimensions(png_bytes(1920, 1080), "image/png") == (1920, 1080)
    assert image_dimensions(b"\x89PNG\r\n\x1a\n", "image/png") is None  # truncated

    # JPEG parsing (standard SOI + JFIF APP0 + SOF0)
    assert image_dimensions(JPEG_BYTES, "image/jpeg") == (1, 1)
    from tests.test_owner_photos import jpeg_bytes
    assert image_dimensions(jpeg_bytes(3840, 2160), "image/jpeg") == (3840, 2160)
    assert image_dimensions(b"\xff\xd8\xff\xd9", "image/jpeg") is None  # SOI + EOI without SOF
    assert image_dimensions(b"\xff\xd8\x00\x00", "image/jpeg") is None  # invalid marker

    # WebP VP8X (extended)
    assert image_dimensions(webp_vp8x(1280, 720), "image/webp") == (1280, 720)
    assert image_dimensions(b"RIFF\x16\x00\x00\x00WEBPVP8X", "image/webp") is None  # truncated

    # WebP VP8 (lossy)
    assert image_dimensions(webp_vp8(640, 480), "image/webp") == (640, 480)
    # WebP VP8 invalid keyframe code
    bad_vp8 = b"RIFF\x20\x00\x00\x00WEBPVP8 \x10\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    assert image_dimensions(bad_vp8, "image/webp") is None

    # WebP VP8L (lossless)
    assert image_dimensions(webp_vp8l(800, 600), "image/webp") == (800, 600)
    # WebP VP8L wrong signature
    bad_vp8l = b"RIFF\x14\x00\x00\x00WEBPVP8L\x08\x00\x00\x00\x00\x00\x00\x00\x00"
    assert image_dimensions(bad_vp8l, "image/webp") is None


@pytest.mark.parametrize("bytes_raw,dim_raw,valid", [
    ("", "", True),
    ("65536", "256", True),
    ("52428800", "30000", True),
    ("10485760", "10000", True),
    ("65535", "10000", False),      # bytes below min (64 KB)
    ("52428801", "10000", False),     # bytes above max (50 MB)
    ("10485760", "255", False),       # dim below min (256)
    ("10485760", "30001", False),     # dim above max (30000)
    ("not-an-int", "10000", False),
    ("10485760", "not-an-int", False),
    ("-100", "10000", False),
    ("10485760", "-500", False),
])
def test_photo_limit_startup_validation(monkeypatch, bytes_raw, dim_raw, valid):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "PHOTO_STORAGE", "local")
    monkeypatch.setattr(config_module.settings, "MEDIA_PUBLIC_BASE_URL", "https://api.restamp.in")
    monkeypatch.setattr(config_module.settings, "PHOTO_MAX_BYTES_RAW", bytes_raw)
    monkeypatch.setattr(config_module.settings, "PHOTO_MAX_DIMENSION_RAW", dim_raw)

    if valid:
        config_module.validate_photo_storage_config(production=True)
    else:
        with pytest.raises(RuntimeError) as exc:
            config_module.validate_photo_storage_config(production=True)
        assert "Invalid configuration:" in str(exc.value)


def test_upload_authorization_matrix(client, local_mode):
    root, _ = local_mode
    c, session = client
    owner_a = _setup_owner(c, session, "+919876545632")
    owner_b = _setup_owner(c, session, "+919876545633")
    buyer = _setup_owner(c, session, "+919876545634", role="BUYER")
    lid = _create_listing(c, owner_a, "Auth Matrix Flat")

    url = f"/owner/listings/{lid}/photos"
    files = {"file": ("test.png", io.BytesIO(PNG_1PX), "image/png")}
    data = {"category": "Living Room"}

    # 1. Anonymous request -> 401
    r = c.post(url, files=files, data=data)
    assert r.status_code == 401
    assert _disk_files(root) == []

    # 2. Buyer role -> 403
    r = c.post(url, files=files, data=data, headers=auth_header(buyer))
    assert r.status_code == 403
    assert _disk_files(root) == []

    # 3. Different owner -> 404 (ownership check before file save)
    r = c.post(url, files=files, data=data, headers=auth_header(owner_b))
    assert r.status_code == 404
    assert _disk_files(root) == []

    # 4. Correct owner -> 201
    r = c.post(url, files=files, data=data, headers=auth_header(owner_a))
    assert r.status_code == 201
    assert len(_disk_files(root)) == 1

