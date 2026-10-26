"""Property visits: buyer/owner scheduling, state rules, isolation.

Uses the rollback-isolated `client` fixture on local restamp_dev only.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, register_phone

_SEQ = {"n": 0}
FUTURE = lambda days=7: (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
PAST = lambda days=1: (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _owner(c, phone=None):
    _SEQ["n"] += 1
    out = register_phone(c, phone or f"+91920000{_SEQ['n']:04d}")
    token = out["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return out, token


def _buyer(c, phone=None):
    _SEQ["n"] += 1
    return register_phone(c, phone or f"+91930000{_SEQ['n']:04d}")


def _listing(c, token, title=None):
    _SEQ["n"] += 1
    r = c.post(
        "/owner/listings",
        json={
            "title": title or f"Visit Flat {_SEQ['n']}",
            "transaction_type": "BUY",
            "property_type": "APARTMENT",
            "price_rupees": 5000000,
            "locality": f"Visit Nagar {_SEQ['n']}",
            "city": "Chennai",
            "pincode": "600080",
        },
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify",
                  headers=auth_header(token)).status_code == 200
    return lid


def _no_phone(payload, where, phones):
    blob = json.dumps(payload, default=str)
    for phone in phones:
        digits = phone.replace("+", "")
        assert phone not in blob, f"{where} leaks {phone}"
        assert digits not in blob, f"{where} leaks digits of {phone}"
    keys = set()

    def _walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                keys.add(str(key).lower())
                _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(payload)
    assert "phone" not in keys and "contact_phone" not in keys, f"{where} keys: {sorted(keys)}"


# --- Buyer scheduling ---------------------------------------------------------

def test_buyer_schedules_lists_details(client):
    c, _ = client
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    r = c.post("/buyer/visits",
               json={"property_listing_id": lid, "scheduled_at": FUTURE(), "note": "Evening"},
               headers=auth_header(buyer["access_token"]))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "REQUESTED" and body["initiator"] == "BUYER"
    assert body["buyer_user_id"] == buyer["user_id"] and body["note"] == "Evening"
    assert body["listing"]["title"] and body["created_at"] and body["updated_at"]
    vid = body["id"]
    assert c.get("/buyer/visits", headers=auth_header(buyer["access_token"])).json()["total"] == 1
    r = c.get(f"/buyer/visits/{vid}", headers=auth_header(buyer["access_token"]))
    assert r.status_code == 200 and r.json()["id"] == vid


def test_buyer_schedule_rejected_cases(client):
    c, session = client
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    h = auth_header(buyer["access_token"])
    # Past time.
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": PAST()},
                  headers=h).status_code == 422
    # Invisible listing (sold via status endpoint).
    assert c.post(f"/owner/listings/{lid}/status", json={"status": "SOLD"},
                  headers=auth_header(otoken)).status_code == 200
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE()},
                  headers=h).status_code == 404
    # Unknown listing.
    assert c.post("/buyer/visits", json={"property_listing_id": 999999999,
                                         "scheduled_at": FUTURE()}, headers=h).status_code == 404
    # Self-visit: switch the owner to BUYER and schedule on their own listing.
    lid2 = _listing(c, otoken)
    c.post("/users/me/role", json={"role": "BUYER"}, headers=auth_header(otoken))
    r = c.post("/buyer/visits", json={"property_listing_id": lid2, "scheduled_at": FUTURE()},
               headers=auth_header(otoken))
    assert r.status_code == 422, r.text
    # Overlong note and empty PATCH.
    assert c.post("/buyer/visits", json={"property_listing_id": lid2, "scheduled_at": FUTURE(),
                                         "note": "x" * 2001}, headers=h).status_code == 422


def test_visit_auth_and_roles(client):
    c, _ = client
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE()},
                 headers=auth_header(buyer["access_token"])).json()["id"]
    buyer_paths = [("post", "/buyer/visits"), ("get", "/buyer/visits"),
                   ("get", f"/buyer/visits/{vid}"), ("patch", f"/buyer/visits/{vid}")]
    owner_paths = [("post", "/owner/visits"), ("get", "/owner/visits"),
                   ("get", f"/owner/visits/{vid}"), ("patch", f"/owner/visits/{vid}")]
    for method, path in buyer_paths + owner_paths:
        assert getattr(c, method)(path).status_code == 401, (method, path)
    # Wrong roles: buyer on owner routes -> 403, owner on buyer routes -> 403.
    for method, path in owner_paths:
        r = getattr(c, method)(path, headers=auth_header(buyer["access_token"]))
        assert r.status_code == 403, (method, path, r.text)
    for method, path in buyer_paths:
        r = getattr(c, method)(path, headers=auth_header(otoken))
        assert r.status_code == 403, (method, path, r.text)


# --- Owner propose + relationship gate ------------------------------------------

def test_owner_propose_requires_relationship(client):
    c, _ = client
    _, otoken = _owner(c)
    buyer = _buyer(c, "+919300999011")
    stranger = _buyer(c, "+919300999012")
    lid = _listing(c, otoken)
    h = auth_header(otoken)
    # No relationship yet -> 422 (buyer id is real, just unrelated).
    r = c.post("/owner/visits",
               json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                     "scheduled_at": FUTURE()}, headers=h)
    assert r.status_code == 422, r.text
    # Unknown buyer id -> 404 (nothing invented).
    r = c.post("/owner/visits",
               json={"property_listing_id": lid, "buyer_user_id": 999999999,
                     "scheduled_at": FUTURE()}, headers=h)
    assert r.status_code == 404
    # Foreign listing -> 404.
    _, otoken2 = _owner(c)
    lid2 = _listing(c, otoken2)
    r = c.post("/owner/visits",
               json={"property_listing_id": lid2, "buyer_user_id": buyer["user_id"],
                     "scheduled_at": FUTURE()}, headers=h)
    assert r.status_code == 404
    # Create the relationship via an enquiry, then propose works.
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid},
                  headers=auth_header(buyer["access_token"])).status_code in (200, 201)
    r = c.post("/owner/visits",
               json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                     "scheduled_at": FUTURE(), "note": "Saturday?"},
               headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PROPOSED" and r.json()["initiator"] == "OWNER"
    # Contact-reveal relationship also qualifies (second buyer via contact).
    assert c.post(f"/buyer/listings/{lid}/contact",
                  headers=auth_header(stranger["access_token"])).status_code == 200
    r = c.post("/owner/visits",
               json={"property_listing_id": lid, "buyer_user_id": stranger["user_id"],
                     "scheduled_at": FUTURE()}, headers=h)
    assert r.status_code == 201, r.text


# --- State machine --------------------------------------------------------------

def test_buyer_request_owner_confirm_complete(client):
    c, _ = client
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    bh, oh = auth_header(buyer["access_token"]), auth_header(otoken)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE()},
                 headers=bh).json()["id"]
    # Buyer cannot confirm their own request; owner confirms.
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CONFIRMED"},
                   headers=bh).status_code == 422
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"},
                   headers=oh).status_code == 200
    # Either side may reschedule a confirmed visit; past times rejected.
    assert c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": FUTURE(14)},
                   headers=bh).status_code == 200
    assert c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": PAST()},
                   headers=bh).status_code == 422
    # Only the owner completes; terminal afterwards.
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "COMPLETED"},
                   headers=bh).status_code == 422
    assert c.patch(f"/owner/visits/{vid}", json={"status": "COMPLETED"},
                   headers=oh).status_code == 200
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CANCELLED"},
                   headers=oh).status_code == 422


def test_proposed_buyer_confirm_decline_and_cancel(client):
    c, _ = client
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    bh, oh = auth_header(buyer["access_token"]), auth_header(otoken)
    c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)
    vid = c.post("/owner/visits",
                 json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                       "scheduled_at": FUTURE()}, headers=oh).json()["id"]
    # Owner cannot confirm their own proposal; buyer confirms.
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"},
                   headers=oh).status_code == 422
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CONFIRMED"},
                   headers=bh).status_code == 200
    # Second visit: buyer declines, then nothing moves.
    vid2 = c.post("/owner/visits",
                  json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                        "scheduled_at": FUTURE(3)}, headers=oh).json()["id"]
    assert c.patch(f"/buyer/visits/{vid2}", json={"status": "DECLINED"},
                   headers=bh).status_code == 200
    assert c.patch(f"/buyer/visits/{vid2}", json={"status": "CONFIRMED"},
                   headers=bh).status_code == 422
    # Third visit: owner declines the buyer's request, buyer cancels path stays shut.
    vid3 = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(5)},
                  headers=bh).json()["id"]
    assert c.patch(f"/owner/visits/{vid3}", json={"status": "DECLINED"},
                   headers=oh).status_code == 200
    assert c.patch(f"/buyer/visits/{vid3}", json={"scheduled_at": FUTURE(9)},
                   headers=bh).status_code == 422
    # Cancel flow from requested by either side.
    vid4 = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(6)},
                  headers=bh).json()["id"]
    assert c.patch(f"/buyer/visits/{vid4}", json={"status": "CANCELLED"},
                   headers=bh).status_code == 200
    assert c.get(f"/buyer/visits?status=CANCELLED", headers=bh).json()["total"] >= 1


def test_combined_confirm_and_reschedule_checks_original_state(client):
    c, _ = client
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    bh, oh = auth_header(buyer["access_token"]), auth_header(otoken)
    c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)

    # A buyer confirming an owner proposal may not smuggle in a time change.
    vid = c.post("/owner/visits",
                 json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                       "scheduled_at": FUTURE()}, headers=oh).json()["id"]
    r = c.patch(f"/buyer/visits/{vid}",
                json={"status": "CONFIRMED", "scheduled_at": FUTURE(8)}, headers=bh)
    assert r.status_code == 422, r.text
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["status"] == "PROPOSED"

    # An owner confirming a buyer request may not smuggle in a time change.
    vid2 = c.post("/buyer/visits",
                  json={"property_listing_id": lid, "scheduled_at": FUTURE(2)},
                  headers=bh).json()["id"]
    r = c.patch(f"/owner/visits/{vid2}",
                json={"status": "CONFIRMED", "scheduled_at": FUTURE(9)}, headers=oh)
    assert r.status_code == 422, r.text
    assert c.get(f"/owner/visits/{vid2}", headers=oh).json()["status"] == "REQUESTED"


def test_cross_user_isolation_and_filters(client):
    c, _ = client
    _, otoken_a = _owner(c)
    _, otoken_b = _owner(c)
    buyer_a = _buyer(c)
    buyer_b = _buyer(c)
    lid_a = _listing(c, otoken_a)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid_a, "scheduled_at": FUTURE()},
                 headers=auth_header(buyer_a["access_token"])).json()["id"]
    # Other buyer: 404 everywhere.
    bh2 = auth_header(buyer_b["access_token"])
    assert c.get(f"/buyer/visits/{vid}", headers=bh2).status_code == 404
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CANCELLED"},
                   headers=bh2).status_code == 404
    # Other owner: 404 everywhere, including filtered lists.
    oh2 = auth_header(otoken_b)
    assert c.get(f"/owner/visits/{vid}", headers=oh2).status_code == 404
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"},
                   headers=oh2).status_code == 404
    assert c.get(f"/owner/visits?property_listing_id={lid_a}", headers=oh2).status_code == 404
    assert c.get(f"/owner/visits?property_listing_id=999999999",
                 headers=auth_header(otoken_a)).status_code == 404
    # Empty PATCH is a 422, not a no-op write.
    assert c.patch(f"/buyer/visits/{vid}", json={},
                   headers=auth_header(buyer_a["access_token"])).status_code == 422


def test_visit_privacy_no_phones(client):
    c, _ = client
    owner_out, otoken = _owner(c, "+919200888001")
    buyer = _buyer(c, "+919300888002")
    lid = _listing(c, otoken)
    bh = auth_header(buyer["access_token"])
    c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE()},
                 headers=bh).json()["id"]
    phones = ("+919200888001", "+919300888002")
    for r in (
        c.get("/buyer/visits", headers=bh),
        c.get(f"/buyer/visits/{vid}", headers=bh),
        c.get("/owner/visits", headers=auth_header(otoken)),
        c.get(f"/owner/visits/{vid}", headers=auth_header(otoken)),
    ):
        assert r.status_code == 200, r.text
        _no_phone(r.json(), "visits", phones)


# --- Migration consistency ---------------------------------------------------------

def test_visits_table_matches_model_and_head(client):
    c, session = client
    cols = {r[0]: r[1] for r in session.execute(text(
        "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'property_visits'")).fetchall()}
    assert set(cols) == {"id", "property_listing_id", "buyer_user_id", "owner_user_id",
                         "scheduled_at", "status", "initiator", "note",
                         "created_at", "updated_at"}
    assert "REQUESTED" in cols["status"] and "COMPLETED" in cols["status"]
    assert "BUYER" in cols["initiator"] and "OWNER" in cols["initiator"]
    fks = session.execute(text(
        "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'property_visits'")).fetchone()[0]
    assert fks == 3
    idx = {r[0] for r in session.execute(text(
        "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'property_visits'")).fetchall()}
    assert {"idx_visits_buyer", "idx_visits_owner", "idx_visits_listing"} <= idx
    assert session.execute(text("SELECT version_num FROM alembic_version")).fetchone()[0] == \
        "f2a3b4c5d6e7"
    from app.models import Base
    assert "property_visits" in Base.metadata.tables
    assert len(Base.metadata.tables) == 34
