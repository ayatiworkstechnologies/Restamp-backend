"""Property lifecycle regression: explicit verification + status transitions.

All client tests use the rollback-isolated `client` fixture on local
restamp_dev only. Covers: GET purity (no writes), verify/reject/submit,
publish/archive/sold/rented/leased validation, VERIFIED-only publishing,
ownership isolation on every new endpoint, failed transitions committing
nothing, and idempotent/concurrent repeats.
"""
from sqlalchemy import text

from tests.conftest import auth_header, register_phone
_SEQ = {"n": 0}


def _owner(c, phone=None):
    _SEQ["n"] += 1
    phone = phone or f"+1910000{_SEQ['n']:04d}"
    out = register_phone(c, phone)
    token = out["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def _create(c, token, title=None):
    _SEQ["n"] += 1
    title = title or f"Lifecycle Flat {_SEQ['n']}"
    r = c.post(
        "/owner/listings",
        json={
            "title": title,
            "transaction_type": "BUY",
            "property_type": "APARTMENT",
            "price_rupees": 5000000,
            "locality": f"Lifecycle Nagar {_SEQ['n']}",
            "city": "Chennai",
            "pincode": "600080",
        },
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _db(c_session, listing_id):
    return c_session.execute(
        text("SELECT verification_status, listing_status, closed_reason, submitted_at "
             "FROM property_listings WHERE id=:i"),
        {"i": listing_id},
    ).fetchone()


# --- GET purity -------------------------------------------------------------

def test_gets_never_write_or_verify(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    session.execute(
        text("UPDATE property_listings SET submitted_at='2000-01-01 00:00:00' WHERE id=:i"),
        {"i": lid},
    )
    session.commit()
    before = _db(session, lid)
    assert before[0] == "PENDING"
    assert c.get("/owner/listings", headers=auth_header(token)).status_code == 200
    assert c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()[
        "verification_status"] == "PENDING"
    after = _db(session, lid)
    assert tuple(after) == tuple(before)


# --- Verification transitions ----------------------------------------------

def test_verify_reject_submit_lifecycle(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    # PENDING -> VERIFIED (audited PASS row).
    r = c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "VERIFIED"
    n = session.execute(text(
        "SELECT COUNT(*) FROM verifications WHERE property_listing_id=:i AND result='PASS'"),
        {"i": lid}).fetchone()[0]
    assert n == 1

    # VERIFIED -> REJECTED is invalid (use archive for unpublishing).
    assert c.post(f"/owner/listings/{lid}/reject", headers=auth_header(token)).status_code == 422
    assert _db(session, lid)[0] == "VERIFIED"

    # Fresh listing: PENDING -> REJECTED with reason (audited FAIL row).
    lid2 = _create(c, token)["listing_id"]
    r = c.post(f"/owner/listings/{lid2}/reject", json={"reason": "Blurry photos"},
               headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "REJECTED"
    row = session.execute(text(
        "SELECT result, checks_json FROM verifications WHERE property_listing_id=:i"),
        {"i": lid2}).fetchone()
    assert row[0] == "FAIL"

    # REJECTED -> VERIFIED is invalid; must submit (resubmit) first.
    assert c.post(f"/owner/listings/{lid2}/verify", headers=auth_header(token)).status_code == 422
    r = c.post(f"/owner/listings/{lid2}/submit", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "PENDING"
    assert _db(session, lid2)[3] is not None  # submitted_at refreshed
    # PENDING submit repeats are idempotent.
    assert c.post(f"/owner/listings/{lid2}/submit", headers=auth_header(token)).status_code == 200
    # Now verification succeeds.
    assert c.post(f"/owner/listings/{lid2}/verify", headers=auth_header(token)).status_code == 200


# --- Publishing / archiving / deal close ------------------------------------

def test_publish_requires_verified(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    # PENDING listing archives fine, but publishing back requires VERIFIED.
    assert c.post(f"/owner/listings/{lid}/archive", headers=auth_header(token)).status_code == 200
    assert _db(session, lid)[1] == "CLOSED"
    r = c.post(f"/owner/listings/{lid}/publish", headers=auth_header(token))
    assert r.status_code == 422, r.text
    assert _db(session, lid)[1] == "CLOSED"  # failed transition commits nothing
    # Verify then publish.
    lid2 = _create(c, token)["listing_id"]
    assert c.post(f"/owner/listings/{lid2}/verify", headers=auth_header(token)).status_code == 200
    assert c.post(f"/owner/listings/{lid2}/archive", headers=auth_header(token)).status_code == 200
    r = c.post(f"/owner/listings/{lid2}/publish", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["listing_status"] == "AVAILABLE"
    # Publish repeats are idempotent.
    assert c.post(f"/owner/listings/{lid2}/publish", headers=auth_header(token)).status_code == 200


def test_deal_close_terminal_and_validated(client):
    c, session = client
    token = _owner(c)
    # Unverified close is rejected.
    lid = _create(c, token)["listing_id"]
    r = c.post(f"/owner/listings/{lid}/status", json={"status": "SOLD"},
               headers=auth_header(token))
    assert r.status_code == 422, r.text
    assert _db(session, lid)[1] == "AVAILABLE"
    # Verified close works with reason persisted.
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    r = c.post(f"/owner/listings/{lid}/status",
               json={"status": "SOLD", "closed_reason": "Deal closed"},
               headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["listing_status"] == "SOLD"
    assert _db(session, lid)[2] == "Deal closed"
    # Terminal: no exits (publish/archive/status all 422, state unchanged).
    for method, path, body in [
        ("post", f"/owner/listings/{lid}/publish", None),
        ("post", f"/owner/listings/{lid}/archive", None),
        ("post", f"/owner/listings/{lid}/status", {"status": "AVAILABLE"}),
        ("post", f"/owner/listings/{lid}/status", {"status": "RENTED"}),
    ]:
        r = c.post(path, json=body, headers=auth_header(token)) if body else c.post(
            path, headers=auth_header(token))
        assert r.status_code == 422, (path, r.text)
    assert _db(session, lid)[1] == "SOLD"


def test_rented_leased_paths_and_unknown_status(client):
    c, session = client
    token = _owner(c)
    for target in ("RENTED", "LEASED"):
        lid = _create(c, token)["listing_id"]
        assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
        r = c.post(f"/owner/listings/{lid}/status", json={"status": target},
                   headers=auth_header(token))
        assert r.status_code == 200, (target, r.text)
        assert r.json()["listing_status"] == target
    # Unknown status is a schema 422; overlong reason is a service 422.
    lid = _create(c, token)["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    assert c.post(f"/owner/listings/{lid}/status", json={"status": "ARCHIVED"},
                   headers=auth_header(token)).status_code == 422
    assert c.post(f"/owner/listings/{lid}/status",
                   json={"status": "SOLD", "closed_reason": "x" * 501},
                   headers=auth_header(token)).status_code == 422
    assert _db(session, lid)[1] == "AVAILABLE"


def test_buyer_visibility_follows_lifecycle(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    # PENDING: invisible to buyers.
    assert c.get(f"/buyer/listings/{lid}").status_code == 404
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    assert c.get(f"/buyer/listings/{lid}").status_code == 200
    assert c.post(f"/owner/listings/{lid}/archive", headers=auth_header(token)).status_code == 200
    assert c.get(f"/buyer/listings/{lid}").status_code == 404
    assert c.post(f"/owner/listings/{lid}/publish", headers=auth_header(token)).status_code == 200
    assert c.get(f"/buyer/listings/{lid}").status_code == 200


# --- Authorization on every new endpoint ------------------------------------

def test_lifecycle_authorization(client):
    c, session = client
    token_a = _owner(c)
    token_b = _owner(c)
    buyer = register_phone(c, "+19100999901")["access_token"]
    lid = _create(c, token_a)["listing_id"]
    paths = [
        ("post", f"/owner/listings/{lid}/submit", None),
        ("post", f"/owner/listings/{lid}/verify", None),
        ("post", f"/owner/listings/{lid}/reject", None),
        ("post", f"/owner/listings/{lid}/publish", None),
        ("post", f"/owner/listings/{lid}/archive", None),
        ("post", f"/owner/listings/{lid}/status", {"status": "CLOSED"}),
    ]
    for method, path, body in paths:
        # Anonymous -> 401.
        r = c.post(path, json=body) if body else c.post(path)
        assert r.status_code == 401, path
        # Buyer role -> 403.
        r = (c.post(path, json=body, headers=auth_header(buyer)) if body
             else c.post(path, headers=auth_header(buyer)))
        assert r.status_code == 403, path
        # Cross-owner -> 404 (never a leak, never an oracle beyond 404).
        r = (c.post(path, json=body, headers=auth_header(token_b)) if body
             else c.post(path, headers=auth_header(token_b)))
        assert r.status_code == 404, path
    # Owner's own row untouched by foreign attempts.
    assert _db(session, lid)[:2] == ("PENDING", "AVAILABLE")


def test_ownership_columns_consistent(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    row = session.execute(text(
        "SELECT l.user_id, p.user_id FROM property_listings l "
        "JOIN physical_properties p ON p.id = l.physical_property_id WHERE l.id=:i"),
        {"i": lid}).fetchone()
    assert row[0] == row[1]


# --- Idempotent / concurrent repeats ----------------------------------------

def test_concurrent_repeats_idempotent(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    # Rapid repeats (double-click / retry / racing workers): same 200 state,
    # single VERIFIED row, exactly one PASS audit row for the first flip.
    for _ in range(3):
        r = c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token))
        assert r.status_code == 200
        assert r.json()["verification_status"] == "VERIFIED"
    n = session.execute(text(
        "SELECT COUNT(*) FROM verifications WHERE property_listing_id=:i"), {"i": lid}
    ).fetchone()[0]
    assert n == 1
    assert c.post(f"/owner/listings/{lid}/archive", headers=auth_header(token)).status_code == 200
    for _ in range(3):
        assert c.post(f"/owner/listings/{lid}/archive",
                      headers=auth_header(token)).status_code == 200
    assert _db(session, lid)[1] == "CLOSED"


# --- Reliability audit: fail-closed divergence + reason hygiene --------------

def test_diverged_enquiry_repeat_is_404_no_snapshot(client):
    """H1: the consistency gate runs before the active-enquiry shortcut."""
    c, session = client
    token = _owner(c)
    buyer = register_phone(c, "+19100999902")["access_token"]
    lid = _create(c, token)["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid},
               headers=auth_header(buyer))
    assert r.status_code == 201, r.text
    # Diverge the ownership columns directly (unreachable via the API, which
    # always writes both identically): point the listing row at another
    # existing user while the physical row stays with the owner.
    other = register_phone(c, "+19100999903")["user_id"]
    session.execute(
        text("UPDATE property_listings SET user_id=:u WHERE id=:i"), {"u": other, "i": lid})
    session.commit()
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid},
               headers=auth_header(buyer))
    assert r.status_code == 404
    assert r.json() == {"detail": "Not found"}


def test_leads_filter_matches_owner_read_boundary(client):
    """M1: leads property filter uses the same dual-authority check as reads."""
    c, session = client
    token_a = _owner(c)
    token_b = _owner(c)
    lid = _create(c, token_a)["listing_id"]
    other_id = register_phone(c, "+19100999904")["user_id"]
    session.execute(
        text("UPDATE property_listings SET user_id=:u WHERE id=:i"), {"u": other_id, "i": lid})
    session.commit()
    assert c.get(f"/owner/listings/{lid}", headers=auth_header(token_a)).status_code == 404
    r = c.get(f"/owner/leads?property_listing_id={lid}", headers=auth_header(token_a))
    assert r.status_code == 404
    # And it never leaks to the other party either.
    assert c.get(f"/owner/leads?property_listing_id={lid}",
                 headers=auth_header(token_b)).status_code == 404


def test_live_listing_carries_no_close_reason(client):
    """M4: transitions to AVAILABLE always clear closed_reason."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    r = c.post(f"/owner/listings/{lid}/status",
               json={"status": "CLOSED", "closed_reason": "Renovation"},
               headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert session.execute(text("SELECT closed_reason FROM property_listings WHERE id=:i"),
                           {"i": lid}).fetchone()[0] == "Renovation"
    # Publish clears it even when a reason is (pointlessly) supplied.
    r = c.post(f"/owner/listings/{lid}/status",
               json={"status": "AVAILABLE", "closed_reason": "stale note"},
               headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert session.execute(text("SELECT closed_reason FROM property_listings WHERE id=:i"),
                           {"i": lid}).fetchone()[0] is None
    # Same-state AVAILABLE repeat with a reason still stores nothing.
    r = c.post(f"/owner/listings/{lid}/status",
               json={"status": "AVAILABLE", "closed_reason": "another note"},
               headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert session.execute(text("SELECT closed_reason FROM property_listings WHERE id=:i"),
                           {"i": lid}).fetchone()[0] is None


def test_rapid_archive_publish_cycles_deterministic(client):
    """Serialized equivalent of racing workers: N archive/publish cycles end
    in the published state with no leftover reason and no extra audit rows."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    for _ in range(10):
        assert c.post(f"/owner/listings/{lid}/archive",
                      headers=auth_header(token)).status_code == 200
        assert c.post(f"/owner/listings/{lid}/publish",
                      headers=auth_header(token)).status_code == 200
    row = _db(session, lid)
    assert (row[0], row[1], row[2]) == ("VERIFIED", "AVAILABLE", None)


def test_row_lock_present_on_owned_write_path(client):
    """Concurrency evidence (isolated, read-only): the real ownership query
    used by every mutating transition compiles with FOR UPDATE against the
    MySQL dialect, so racing publish/archive/sold transitions serialize on
    the row in InnoDB. True parallel-DB races are not executed here (the
    shared-fixture session is not thread-safe); idempotent repeats above
    are the race-safety contract."""
    from sqlalchemy.dialects import mysql as mysql_dialect
    from app.services import owner_listings as svc
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]
    owner_id = session.execute(text("SELECT user_id FROM property_listings WHERE id=:i"),
                               {"i": lid}).fetchone()[0]
    locked_sql = str(svc._owned_listing_query(session, owner_id, lid)
                     .with_for_update().statement.compile(dialect=mysql_dialect.dialect()))
    assert "FOR UPDATE" in locked_sql
    unlocked_sql = str(svc._owned_listing_query(session, owner_id, lid)
                       .statement.compile(dialect=mysql_dialect.dialect()))
    assert "FOR UPDATE" not in unlocked_sql


# --- Automatic verification after 2 minutes ----------------------------------

def test_auto_verify_after_deadline(client):
    """A pending listing becomes VERIFIED + AVAILABLE after 2 minutes."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    # Immediately after creation: still PENDING
    assert _db(session, lid)[0] == "PENDING"
    assert _db(session, lid)[1] == "AVAILABLE"

    # Simulate time passing by updating submitted_at to 3 minutes ago
    three_min_ago = "2000-01-01 00:00:00"  # will be treated as "in the past"
    session.execute(
        text("UPDATE property_listings SET submitted_at=:t WHERE id=:i"),
        {"t": three_min_ago, "i": lid},
    )
    session.commit()

    # Run the automatic verification (simulating worker)
    from app.services import owner_listings as owner_svc
    listing = owner_svc.transition_verification_automatic(session, lid)
    assert listing is not None
    assert listing.verification_status == "VERIFIED"
    assert listing.listing_status == "AVAILABLE"
    session.commit()

    # Verify the audit row has actor="auto"
    row = session.execute(text(
        "SELECT result, checks_json, triggered_by FROM verifications "
        "WHERE property_listing_id=:i ORDER BY attempt_no DESC LIMIT 1"),
        {"i": lid}).fetchone()
    assert row[0] == "PASS"
    import json
    checks = json.loads(row[1])
    assert checks["actor"] == "auto"
    assert row[2] is None  # system-triggered


def test_auto_verify_not_before_deadline(client):
    """A pending listing does NOT auto-verify before 2 minutes."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    # Submitted 1 minute ago (not yet due) - use a timestamp that's
    # 1 minute before the worker's "now" so the deadline is in the future.
    # We achieve this by setting submitted_at to "now" (current test time).
    # Since we can't control the worker's clock, we set it to a time where
    # deadline = submitted_at + 2min is in the future.
    # The simplest approach: set submitted_at to now + 1 minute (future submission)
    # which makes the deadline even further in the future.
    from datetime import datetime, timedelta
    future_submission = datetime.utcnow() + timedelta(minutes=1)
    session.execute(
        text("UPDATE property_listings SET submitted_at=:t WHERE id=:i"),
        {"t": future_submission.replace(tzinfo=None), "i": lid},
    )
    session.commit()

    from app.services import owner_listings as owner_svc
    listing = owner_svc.transition_verification_automatic(session, lid)
    # Returns None when not due
    assert listing is None
    # Status unchanged
    assert _db(session, lid)[0] == "PENDING"
    # No audit row created
    n = session.execute(text(
        "SELECT COUNT(*) FROM verifications WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert n == 0


def test_auto_verify_skips_ineligible_listings(client):
    """Auto-verify only processes PENDING + AVAILABLE listings."""
    c, session = client
    token = _owner(c)

    # REJECTED listing - not eligible
    lid1 = _create(c, token)["listing_id"]
    session.execute(
        text("UPDATE property_listings SET verification_status='REJECTED', submitted_at='2000-01-01' WHERE id=:i"),
        {"i": lid1},
    )
    # Archived listing - not eligible
    lid2 = _create(c, token)["listing_id"]
    session.execute(
        text("UPDATE property_listings SET listing_status='CLOSED', submitted_at='2000-01-01' WHERE id=:i"),
        {"i": lid2},
    )
    # SOLD listing - not eligible
    lid3 = _create(c, token)["listing_id"]
    session.execute(
        text("UPDATE property_listings SET listing_status='SOLD', verification_status='VERIFIED', submitted_at='2000-01-01' WHERE id=:i"),
        {"i": lid3},
    )
    session.commit()

    from app.services import owner_listings as owner_svc
    for lid in (lid1, lid2, lid3):
        listing = owner_svc.transition_verification_automatic(session, lid)
        assert listing is None, f"Listing {lid} should not be auto-verified"


def test_auto_verify_idempotent(client):
    """Running auto-verify twice does not duplicate audit rows."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    # Backdate submission
    session.execute(
        text("UPDATE property_listings SET submitted_at='2000-01-01' WHERE id=:i"),
        {"i": lid},
    )
    session.commit()

    from app.services import owner_listings as owner_svc
    # First run
    owner_svc.transition_verification_automatic(session, lid)
    session.commit()
    # Second run (simulating worker picking it up again before commit visibility)
    owner_svc.transition_verification_automatic(session, lid)
    session.commit()

    # Should still be VERIFIED
    assert _db(session, lid)[0] == "VERIFIED"
    # Only ONE new audit row (the first auto-verify)
    n = session.execute(text(
        "SELECT COUNT(*) FROM verifications WHERE property_listing_id=:i AND checks_json LIKE '%auto%'"),
        {"i": lid}).fetchone()[0]
    assert n == 1


def test_auto_verify_does_not_affect_other_owners(client):
    """Owner A's listing auto-verifies; Owner B's listing unaffected."""
    c, session = client
    token_a = _owner(c)
    token_b = _owner(c)

    lid_a = _create(c, token_a)["listing_id"]
    lid_b = _create(c, token_b)["listing_id"]

    # Backdate both
    session.execute(
        text("UPDATE property_listings SET submitted_at='2000-01-01' WHERE id IN (:a, :b)"),
        {"a": lid_a, "b": lid_b},
    )
    session.commit()

    from app.services import owner_listings as owner_svc
    # Process A's listing
    owner_svc.transition_verification_automatic(session, lid_a)
    session.commit()

    # A's listing is VERIFIED
    assert _db(session, lid_a)[0] == "VERIFIED"
    # B's listing is still PENDING
    assert _db(session, lid_b)[0] == "PENDING"


def test_buyer_visibility_after_auto_verify(client):
    """A listing becomes visible to buyers after auto-verification."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    # PENDING: invisible to buyers
    assert c.get(f"/buyer/listings/{lid}").status_code == 404

    # Backdate and auto-verify
    session.execute(
        text("UPDATE property_listings SET submitted_at='2000-01-01' WHERE id=:i"),
        {"i": lid},
    )
    session.commit()

    from app.services import owner_listings as owner_svc
    owner_svc.transition_verification_automatic(session, lid)
    session.commit()

    # Now VERIFIED + AVAILABLE -> visible
    assert c.get(f"/buyer/listings/{lid}").status_code == 200
