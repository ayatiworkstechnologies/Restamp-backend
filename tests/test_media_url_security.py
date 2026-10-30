"""Listing photo URL security (client-supplied images / photos[].url).

Before this change any string up to 500 chars (any scheme, any host, no
count cap) was stored in property_media and served to every buyer. Now:
- only https://res.cloudinary.com/<our cloud>/image/upload/... is accepted
  in production (no credentials, ports, IP literals, query/fragment or
  traversal);
- RESTAMP_DEV_MEDIA_URL_PREFIXES (conftest: https://t.test/) is honoured only
  outside production;
- at most 20 photos, no duplicates, on create, PATCH and upload;
- URLs already attached to the same listing stay editable on PATCH, so
  existing galleries (server-generated uploads, local /media) still reorder
  and prune.
Rollback-isolated.
"""
import base64

import pytest
from sqlalchemy import text

from app import config as config_module
from app.config import validate_security_config
from app.services.media_urls import MAX_LISTING_PHOTOS, validate_listing_image_url
from tests.conftest import auth_header
from tests.test_owner_update import _post_listing, _setup_owner_with_phone

CLOUD = "restamp-cloud"
GOOD = f"https://res.cloudinary.com/{CLOUD}/image/upload/v1700000000/restamp/listings/7/photos/abc.png"
GOOD_2 = f"https://res.cloudinary.com/{CLOUD}/image/upload/c_fill,w_800/v1700000001/restamp/listings/7/photos/def.jpg"
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def strict(monkeypatch):
    """Production-equivalent allowlist: our cloud only, no dev prefixes."""
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", CLOUD)
    monkeypatch.setattr(config_module.settings, "DEV_MEDIA_URL_PREFIXES", "")


def _cloud_url(n):
    return f"https://res.cloudinary.com/{CLOUD}/image/upload/v1700000000/restamp/listings/1/photos/p{n}.jpg"


def _media(session, lid):
    return session.execute(text(
        "SELECT url, order_index FROM property_media WHERE property_listing_id=:i ORDER BY order_index"),
        {"i": lid}).fetchall()


# --- validator ---------------------------------------------------------------

def test_valid_cloudinary_urls_accepted(strict):
    assert validate_listing_image_url(GOOD) == GOOD
    assert validate_listing_image_url(GOOD_2) == GOOD_2
    assert validate_listing_image_url(f"  {GOOD}  ") == GOOD  # trimmed
    assert validate_listing_image_url(GOOD.replace("res.cloudinary.com", "res.cloudinary.com:443")).endswith("abc.png")


@pytest.mark.parametrize("url", [
    # scheme
    GOOD.replace("https://", "http://"),
    "javascript:alert(1)",
    "JavaScript:alert(document.cookie)",
    "data:image/png;base64,iVBORw0KGgo=",
    "file:///etc/passwd",
    "ftp://res.cloudinary.com/restamp-cloud/image/upload/a.jpg",
    "//res.cloudinary.com/restamp-cloud/image/upload/a.jpg",
    # loopback / private / internal
    "https://localhost/a.jpg",
    "https://app.localhost/a.jpg",
    "https://127.0.0.1/a.jpg",
    "https://10.0.0.5/a.jpg",
    "https://172.16.4.4/a.jpg",
    "https://192.168.1.2/a.jpg",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/a.jpg",
    "https://metadata.internal/a.jpg",
    "https://printer.local/a.jpg",
    # unapproved hosts / other clouds / look-alikes
    "https://evil.example.com/a.jpg",
    "https://images.unsplash.com/photo-1?w=800",
    "https://res.cloudinary.com/someone-else/image/upload/v1/a.jpg",
    f"https://res.cloudinary.com.evil.com/{CLOUD}/image/upload/a.jpg",
    f"https://evil.com/res.cloudinary.com/{CLOUD}/image/upload/a.jpg",
    f"https://res.cloudinary.com./{CLOUD}/image/upload/a.jpg",
    f"https://res.cloudinary.com/{CLOUD}/video/upload/a.mp4",
    f"https://res.cloudinary.com/{CLOUD}/image/upload/",
    # credentials
    f"https://user:pass@res.cloudinary.com/{CLOUD}/image/upload/a.jpg",
    f"https://res.cloudinary.com@evil.com/{CLOUD}/image/upload/a.jpg",
    # ports / malformed
    f"https://res.cloudinary.com:8443/{CLOUD}/image/upload/a.jpg",
    f"https://res.cloudinary.com:abc/{CLOUD}/image/upload/a.jpg",
    "https://",
    "not a url",
    f"https://res.cloudinary.com/{CLOUD}/image/upload/a b.jpg",
    f"https://res.cloudinary.com/{CLOUD}/image/upload\\a.jpg",
    f"https://res.cloudinary.com/{CLOUD}/image/upload/a\n.jpg",
    f"https://res.cloudinary.com/{CLOUD}/image/upload/a\t.jpg",
    # query / fragment / traversal
    GOOD + "?x=1",
    GOOD + "#frag",
    f"https://res.cloudinary.com/{CLOUD}/image/upload/../../other/a.jpg",
    f"https://res.cloudinary.com/{CLOUD}/image/upload/%2e%2e/a.jpg",
])
def test_rejected_urls(strict, url):
    with pytest.raises(ValueError) as exc:
        validate_listing_image_url(url)
    assert url.strip() not in str(exc.value)  # static message, never echoed


def test_empty_and_overlong_rejected(strict):
    for bad in (None, "", "   "):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_listing_image_url(bad)
    with pytest.raises(ValueError, match="at most 500"):
        validate_listing_image_url(GOOD[:-4] + "a" * 500 + ".png")


def test_no_cloud_configured_rejects_everything(monkeypatch):
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", "")
    monkeypatch.setattr(config_module.settings, "DEV_MEDIA_URL_PREFIXES", "")
    with pytest.raises(ValueError):
        validate_listing_image_url(GOOD)


def test_dev_prefix_only_outside_production(monkeypatch):
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", CLOUD)
    monkeypatch.setattr(config_module.settings, "DEV_MEDIA_URL_PREFIXES", "http://127.0.0.1:8000/media/")
    assert validate_listing_image_url("http://127.0.0.1:8000/media/listings/a.png")
    with pytest.raises(ValueError):  # traversal still refused under a dev prefix
        validate_listing_image_url("http://127.0.0.1:8000/media/../etc/passwd")
    with pytest.raises(ValueError):  # other local ports/paths are not covered
        validate_listing_image_url("http://127.0.0.1:8000/admin/a.png")
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    with pytest.raises(ValueError):
        validate_listing_image_url("http://127.0.0.1:8000/media/listings/a.png")
    assert validate_listing_image_url(GOOD) == GOOD  # production rule unchanged


def test_dev_prefix_without_trailing_slash_is_ignored(monkeypatch):
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", CLOUD)
    monkeypatch.setattr(config_module.settings, "DEV_MEDIA_URL_PREFIXES", "https://t.test")
    for url in ("https://t.test.evil.com/a.jpg", "https://t.test/a.jpg"):
        with pytest.raises(ValueError):
            validate_listing_image_url(url)


def test_production_with_dev_prefix_warns_not_raises(monkeypatch, caplog):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", False)
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", CLOUD)
    monkeypatch.setattr(config_module.settings, "DEV_MEDIA_URL_PREFIXES", "https://secret-dev-host.test/")
    with caplog.at_level("WARNING", logger="restamp.config"):
        validate_security_config()
    assert "RESTAMP_DEV_MEDIA_URL_PREFIXES" in caplog.text
    assert "secret-dev-host" not in caplog.text  # setting name only, never its value


# --- create ------------------------------------------------------------------

def test_create_with_cloudinary_urls_keeps_order(client, strict):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543481")
    r = _post_listing(c, token, "Cloud Photos", photos=[
        {"url": GOOD_2, "category": "Kitchen"}, {"url": GOOD, "category": "Living Room"}])
    assert r.status_code == 201, r.text
    assert r.json()["cover_image_url"] == GOOD_2
    assert [u for u, _ in _media(session, r.json()["listing_id"])] == [GOOD_2, GOOD]
    # legacy images[] list is validated the same way
    r = _post_listing(c, token, "Cloud Images", photos=None, images=[GOOD])
    assert r.status_code == 201, r.text
    assert r.json()["cover_image_url"] == GOOD


@pytest.mark.parametrize("field", ["photos", "images"])
@pytest.mark.parametrize("bad", [
    "http://res.cloudinary.com/restamp-cloud/image/upload/a.jpg",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "https://192.168.0.10/a.jpg",
    "https://evil.example.com/a.jpg",
    "https://u:p@res.cloudinary.com/restamp-cloud/image/upload/a.jpg",
])
def test_create_rejects_bad_url_and_stores_nothing(client, strict, field, bad):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543482")
    before = session.execute(text("SELECT COUNT(*) FROM property_listings")).scalar()
    value = [{"url": GOOD, "category": "Kitchen"}, {"url": bad, "category": "Bedroom"}] \
        if field == "photos" else [GOOD, bad]
    overrides = {"photos": value} if field == "photos" else {"photos": None, "images": value}
    r = _post_listing(c, token, "Bad Photo", **overrides)
    assert r.status_code == 422, r.text
    assert r.json() == {"detail": "photo url must be an approved HTTPS image URL"}
    assert session.execute(text("SELECT COUNT(*) FROM property_listings")).scalar() == before


def test_create_photo_count_cap(client, strict):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543483")
    ok = [{"url": _cloud_url(i), "category": "Bedroom"} for i in range(MAX_LISTING_PHOTOS)]
    r = _post_listing(c, token, "Twenty Photos", photos=ok)
    assert r.status_code == 201, r.text
    assert len(_media(session, r.json()["listing_id"])) == MAX_LISTING_PHOTOS
    too_many = ok + [{"url": _cloud_url(99), "category": "Bedroom"}]
    r = _post_listing(c, token, "Too Many Photos", photos=too_many)
    assert r.status_code == 422
    assert r.json() == {"detail": "A listing can have at most 20 photos"}
    r = _post_listing(c, token, "Too Many Images", photos=None,
                      images=[_cloud_url(i) for i in range(MAX_LISTING_PHOTOS + 1)])
    assert r.status_code == 422


def test_create_rejects_duplicate_urls(client, strict):
    c, _ = client
    token = _setup_owner_with_phone(c, "+919876543484")
    r = _post_listing(c, token, "Dup", photos=[{"url": GOOD, "category": "Kitchen"},
                                               {"url": f" {GOOD} ", "category": "Bedroom"}])
    assert r.status_code == 422 and r.json() == {"detail": "duplicate photo url"}


# --- PATCH -------------------------------------------------------------------

def test_patch_rejects_new_bad_url_and_keeps_gallery(client, strict):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543485")
    lid = _post_listing(c, token, "Patch Bad", photos=[{"url": GOOD, "category": "Kitchen"}]).json()["listing_id"]
    for bad in ("https://evil.example.com/x.jpg", "javascript:alert(1)", "https://10.1.1.1/x.jpg"):
        r = c.patch(f"/owner/listings/{lid}", json={"photos": [
            {"url": GOOD, "category": "Kitchen"}, {"url": bad, "category": "Bedroom"}]},
            headers=auth_header(token))
        assert r.status_code == 422, r.text
        r = c.patch(f"/owner/listings/{lid}", json={"images": [bad]}, headers=auth_header(token))
        assert r.status_code == 422, r.text
    assert [u for u, _ in _media(session, lid)] == [GOOD]


def test_patch_count_cap_and_duplicates(client, strict):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543486")
    lid = _post_listing(c, token, "Patch Cap", photos=[{"url": GOOD, "category": "Kitchen"}]).json()["listing_id"]
    r = c.patch(f"/owner/listings/{lid}", json={"photos": [
        {"url": _cloud_url(i), "category": "Bedroom"} for i in range(MAX_LISTING_PHOTOS + 1)]},
        headers=auth_header(token))
    assert r.status_code == 422 and r.json() == {"detail": "A listing can have at most 20 photos"}
    r = c.patch(f"/owner/listings/{lid}", json={"photos": [
        {"url": GOOD, "category": "Kitchen"}, {"url": GOOD, "category": "Bedroom"}]},
        headers=auth_header(token))
    assert r.status_code == 422 and r.json() == {"detail": "duplicate photo url"}
    assert [u for u, _ in _media(session, lid)] == [GOOD]


def test_patch_keeps_already_attached_urls_editable(client, strict):
    """Existing rows (legacy hosts, local /media) can be reordered/pruned;
    only NEW URLs must be approved, and a URL attached to ANOTHER listing is
    not grandfathered."""
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543487")
    lid = _post_listing(c, token, "Legacy Gallery", photos=[{"url": GOOD, "category": "Kitchen"}]).json()["listing_id"]
    legacy = "http://127.0.0.1:8000/media/listings/legacy.png"
    session.execute(text(
        "INSERT INTO property_media (property_listing_id, url, media_type, order_index) "
        "VALUES (:l, :u, 'IMAGE', 1)"), {"l": lid, "u": legacy})
    r = c.patch(f"/owner/listings/{lid}", json={"photos": [
        {"url": legacy, "category": "Bedroom"}, {"url": GOOD, "category": "Kitchen"},
        {"url": GOOD_2, "category": "Balcony"}]}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert [(u, i) for u, i in _media(session, lid)] == [(legacy, 0), (GOOD, 1), (GOOD_2, 2)]
    # Pruning works and the legacy URL cannot then be re-added.
    r = c.patch(f"/owner/listings/{lid}", json={"photos": [{"url": GOOD, "category": "Kitchen"}]},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    r = c.patch(f"/owner/listings/{lid}", json={"photos": [{"url": legacy, "category": "Kitchen"}]},
                headers=auth_header(token))
    assert r.status_code == 422
    # Another listing cannot borrow the first listing's legacy URL either.
    session.execute(text(
        "INSERT INTO property_media (property_listing_id, url, media_type, order_index) "
        "VALUES (:l, :u, 'IMAGE', 1)"), {"l": lid, "u": legacy})
    other = _post_listing(c, token, "Other", photos=[{"url": GOOD, "category": "Kitchen"}]).json()["listing_id"]
    r = c.patch(f"/owner/listings/{other}", json={"photos": [{"url": legacy, "category": "Kitchen"}]},
                headers=auth_header(token))
    assert r.status_code == 422


def test_uploaded_cloudinary_photo_survives_patch(client, monkeypatch, strict):
    """A server-generated upload URL keeps working through the edit flow."""
    from app.services import photo_storage as photo_svc
    uploaded = f"https://res.cloudinary.com/{CLOUD}/image/upload/v1/restamp/listings/1/photos/up.png"
    monkeypatch.setattr(photo_svc, "upload_listing_image",
                        lambda listing_id, data, ext, mime: (uploaded, "restamp/listings/1/photos/up", len(data)))
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543488")
    lid = _post_listing(c, token, "Upload Then Edit", photos=[{"url": GOOD, "category": "Kitchen"}]).json()["listing_id"]
    r = c.post(f"/owner/listings/{lid}/photos", files={"file": ("a.png", PNG_1PX, "image/png")},
               data={"category": "Bedroom"}, headers=auth_header(token))
    assert r.status_code == 201, r.text
    r = c.patch(f"/owner/listings/{lid}", json={"photos": [
        {"url": uploaded, "category": "Bedroom"}, {"url": GOOD, "category": "Kitchen"}]},
        headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert [u for u, _ in _media(session, lid)] == [uploaded, GOOD]


def test_upload_refused_at_cap_before_storage(client, monkeypatch, strict):
    from app.services import photo_storage as photo_svc
    calls = []
    monkeypatch.setattr(photo_svc, "upload_listing_image",
                        lambda *a: calls.append("upload") or (GOOD, "x", 1))
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543489")
    lid = _post_listing(c, token, "Full Gallery", photos=[
        {"url": _cloud_url(i), "category": "Bedroom"} for i in range(MAX_LISTING_PHOTOS)]).json()["listing_id"]
    r = c.post(f"/owner/listings/{lid}/photos", files={"file": ("a.png", PNG_1PX, "image/png")},
               data={"category": "Bedroom"}, headers=auth_header(token))
    assert r.status_code == 422 and r.json() == {"detail": "A listing can have at most 20 photos"}
    assert calls == []  # no storage spent
    assert len(_media(session, lid)) == MAX_LISTING_PHOTOS
