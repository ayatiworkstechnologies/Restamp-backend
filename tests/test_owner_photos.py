"""Tests for POST /owner/listings/{id}/photos with Cloudinary storage.

Cloudinary is fully mocked (no credentials, no network): upload success,
upload failure, and orphan-destroy calls are asserted through monkeypatched
app/services/photo_storage.py. Validation/auth/ownership tests never reach
the uploader. DB rows roll back via the shared fixture.
"""
import base64
import io

import pytest
from sqlalchemy import text
from tests.conftest import auth_header

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0FAKEJPEG" + b"0" * 128

FAKE_URL = "https://res.cloudinary.com/demo/image/upload/restamp/listings/1/photos/abc123.png"
FAKE_PUBLIC_ID = "restamp/listings/1/photos/abc123"


def _mock_cloud(monkeypatch, calls, fail_with=None):
    """Stub Cloudinary upload/destroy; records calls for assertions."""
    from app.services import photo_storage as photo_svc

    def fake_upload(listing_id, data, extension, mime_type):
        calls.append(("upload", listing_id, len(data), extension, mime_type))
        if fail_with is not None:
            raise fail_with
        return FAKE_URL, FAKE_PUBLIC_ID, len(data)

    def fake_destroy(public_id):
        calls.append(("destroy", public_id))

    monkeypatch.setattr(photo_svc, "upload_listing_image", fake_upload)
    monkeypatch.setattr(photo_svc, "delete_uploaded_image", fake_destroy)


def _setup_owner(c, session, phone: str, role: str = "OWNER") -> str:
    """Create a user directly (no OTP flow dependency) and return a JWT.

    Bypasses register_phone so these tests are immune to the temporary
    local plaintext-OTP work in the tree; mirrors otp_verify registration.
    """
    from app.security import create_access_token

    uid = session.execute(text(
        "INSERT INTO users (display_name) VALUES (:n)"), {"n": f"Photo User {phone}"}
    ).lastrowid
    session.execute(text(
        "INSERT INTO auth_identities (user_id, provider, provider_identifier) "
        "VALUES (:u, 'phone', :p)"), {"u": uid, "p": phone})
    session.execute(text(
        "INSERT INTO user_current_role (user_id, role) VALUES (:u, :r)"),
        {"u": uid, "r": role})
    session.commit()
    if role != "BUYER":
        session.execute(text(
            "UPDATE user_current_role SET role=:r WHERE user_id=:u"),
            {"u": uid, "r": role})
        session.execute(text(
            "INSERT INTO role_history (user_id, role) VALUES (:u, :r)"),
            {"u": uid, "r": role})
        session.commit()
    token, _ = create_access_token(uid, role)
    return token


def _create_listing(c, token: str, title: str, **extra) -> int:
    payload = {
        "title": title, "transaction_type": "RENT", "property_type": "APARTMENT",
        "price_rupees": 25000, "price_period": "MONTHLY",
        "locality": "Photo Nagar", "city": "Chennai", "pincode": "600090",
        **extra,
    }
    r = c.post("/owner/listings", json=payload, headers=auth_header(token))
    assert r.status_code == 201, r.text
    return r.json()["listing_id"]


def _upload(c, token, listing_id, content: bytes, filename: str, mime: str, category="Living Room"):
    return c.post(
        f"/owner/listings/{listing_id}/photos",
        files={"file": (filename, io.BytesIO(content), mime)},
        data={"category": category},
        headers=auth_header(token),
    )


def test_upload_succeeds_and_persists_all_metadata(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876543301")
    lid = _create_listing(c, token, "Photo Full Flat")
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Bedroom")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["category"] == "Bedroom"
    assert body["mime_type"] == "image/png"
    assert body["byte_size"] == len(PNG_1PX)
    assert body["order_index"] == 0 and body["is_cover"] is True
    assert body["url"] == FAKE_URL
    assert ("upload", lid, len(PNG_1PX), ".png", "image/png") in calls
    row = session.execute(text(
        "SELECT url, category, mime_type, byte_size, order_index, media_type, uploaded_by "
        "FROM property_media WHERE property_listing_id=:i"), {"i": lid}).fetchone()
    assert tuple(row) == (FAKE_URL, "Bedroom", "image/png", len(PNG_1PX), 0, "IMAGE",
                          session.execute(text(
                              "SELECT user_id FROM property_listings WHERE id=:i"),
                              {"i": lid}).fetchone()[0])


def test_first_photo_is_cover_second_is_not(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876543302")
    lid = _create_listing(c, token, "Photo Order Flat")
    first = _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Living Room").json()
    second = _upload(c, token, lid, JPEG_BYTES, "b.jpg", "image/jpeg", "Kitchen").json()
    assert (first["order_index"], first["is_cover"]) == (0, True)
    assert (second["order_index"], second["is_cover"]) == (1, False)
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert detail["cover_image_url"] == first["url"]
    assert [p["url"] for p in detail["photos"]] == [first["url"], second["url"]]


def test_upload_appends_after_existing_url_media(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876543303")
    lid = _create_listing(c, token, "Photo Append Flat",
                          images=["https://t.test/cover.jpg"])
    body = _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Balcony").json()
    assert (body["order_index"], body["is_cover"]) == (1, False)
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert detail["cover_image_url"] == "https://t.test/cover.jpg"


def test_unsupported_mime_rejected(client):
    c, session = client
    token = _setup_owner(c, session, "+919876543304")
    lid = _create_listing(c, token, "Photo Mime Flat")
    r = _upload(c, token, lid, b"GIF89a", "a.gif", "image/gif")
    assert r.status_code == 422, r.text


def test_mismatched_extension_rejected(client):
    c, session = client
    token = _setup_owner(c, session, "+919876543305")
    lid = _create_listing(c, token, "Photo Ext Flat")
    r = _upload(c, token, lid, PNG_1PX, "evil.exe", "image/png")
    assert r.status_code == 422, r.text
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/jpeg")
    assert r.status_code == 422, r.text


def test_oversized_file_rejected(client):
    c, session = client
    token = _setup_owner(c, session, "+919876543306")
    lid = _create_listing(c, token, "Photo Big Flat")
    r = _upload(c, token, lid, b"0" * (10 * 1024 * 1024 + 1), "big.jpg", "image/jpeg")
    assert r.status_code == 422, r.text


def test_empty_file_rejected(client):
    c, session = client
    token = _setup_owner(c, session, "+919876543307")
    lid = _create_listing(c, token, "Photo Empty Flat")
    r = _upload(c, token, lid, b"", "empty.jpg", "image/jpeg")
    assert r.status_code == 422, r.text


def test_invalid_category_rejected(client):
    c, session = client
    token = _setup_owner(c, session, "+919876543308")
    lid = _create_listing(c, token, "Photo Cat Flat")
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Sauna")
    assert r.status_code == 422, r.text


def test_unauthenticated_is_401(client):
    c, _ = client
    r = c.post("/owner/listings/1/photos",
               files={"file": ("a.png", io.BytesIO(PNG_1PX), "image/png")},
               data={"category": "Living Room"})
    assert r.status_code == 401, r.text


def test_buyer_is_403(client):
    c, session = client
    buyer_token = _setup_owner(c, session, "+919876543309", role="BUYER")
    owner_token = _setup_owner(c, session, "+919876543310")
    lid = _create_listing(c, owner_token, "Photo Buyer Flat")
    r = _upload(c, buyer_token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 403, r.text


def test_cross_owner_is_404_and_destroys_orphan(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token_a = _setup_owner(c, session, "+919876543311")
    token_b = _setup_owner(c, session, "+919876543312")
    lid = _create_listing(c, token_a, "Photo Private Flat")
    before = session.execute(text("SELECT COUNT(*) FROM property_media")).fetchone()[0]
    r = _upload(c, token_b, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 404, r.text
    after = session.execute(text("SELECT COUNT(*) FROM property_media")).fetchone()[0]
    assert after == before
    # Uploaded image was destroyed (no orphan on Cloudinary either).
    assert ("destroy", FAKE_PUBLIC_ID) in calls


def test_db_failure_destroys_cloudinary_orphan(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876543313")
    lid = _create_listing(c, token, "Photo Orphan Flat")
    before = session.execute(text("SELECT COUNT(*) FROM property_media")).fetchone()[0]
    from app.services import owner_listings as svc
    monkeypatch.setattr(svc, "add_listing_photo",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 500, r.text
    after = session.execute(text("SELECT COUNT(*) FROM property_media")).fetchone()[0]
    assert after == before
    assert ("destroy", FAKE_PUBLIC_ID) in calls


def test_cloudinary_failure_creates_no_row(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls, fail_with=OSError("cloud down"))
    c, session = client
    token = _setup_owner(c, session, "+919876543314")
    lid = _create_listing(c, token, "Photo StoreFail Flat")
    before = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 500, r.text
    after = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert after == before
    assert not [x for x in calls if x[0] == "destroy"]


def test_unconfigured_storage_is_500(client, monkeypatch):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", "")
    c, session = client
    token = _setup_owner(c, session, "+919876543317")
    lid = _create_listing(c, token, "Photo Unconfigured Flat")
    before = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    r = _upload(c, token, lid, PNG_1PX, "a.png", "image/png")
    assert r.status_code == 500, r.text
    after = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert after == before


def test_existing_listing_data_intact(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876543315")
    lid = _create_listing(c, token, "Photo Intact Flat",
                          description="Keep me",
                          rent_terms={"lock_in_months": 12},
                          amenities=["Gym"])
    _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Exterior")
    card = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert card["description"] == "Keep me"
    assert card["rent_terms"]["lock_in_months"] == 12
    assert card["amenities"] == ["Gym"]
    assert card["verification_status"] == "PENDING"


def test_cloudinary_url_shape_is_permanent_https(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner(c, session, "+919876543316")
    lid = _create_listing(c, token, "Photo Url Flat")
    body = _upload(c, token, lid, PNG_1PX, "a.png", "image/png", "Exterior").json()
    assert body["url"].startswith("https://res.cloudinary.com/")
    assert "localhost" not in body["url"] and "127.0.0.1" not in body["url"]
    upload_calls = [x for x in calls if x[0] == "upload"]
    assert len(upload_calls) == 1 and upload_calls[0][1] == lid
