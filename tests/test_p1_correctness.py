"""P1 correctness batch — regression tests (report §15 evidence).

1. Savepoint conflict handling in ``users_roles.grant_admin`` / ``link_identity``:
   a duplicate-key IntegrityError must be contained by a savepoint — never a
   service-level whole-transaction ``db.rollback()`` — so earlier uncommitted
   caller work survives and the session stays usable.
2. 422 error-detail hygiene: validation failures must return static detail
   text; client-supplied values (amenity names, content types, file names)
   are never echoed back.
3. Race-tolerant location-chain get-or-create in
   ``owner_listings._find_or_create_postcode``: a competitor that commits
   between our snapshot and our INSERT must be adopted (unique-keyed levels)
   instead of surfacing a 500; Postcode conflicts under a *different*
   locality deliberately re-raise (reviewed plan). The recovery is
   duplicate-key-specific (MySQL 1062 only): any other IntegrityError
   (e.g. a foreign-key violation) re-raises immediately — no retry, no
   refetch.

The conftest ``client`` fixture joins the test session into one outer
transaction with ``join_transaction_mode="create_savepoint"`` — its commits
only release savepoints and teardown discards everything. A savepoint
mechanism cannot produce a genuine concurrent COMMIT, so the race tests and
conflict tests use ``app.db.SessionLocal`` (real pooled connection): the
competitor inserts and commits real rows. Rows committed on that second
connection are real residue — every such test removes them in a ``finally``
block (``SET FOREIGN_KEY_CHECKS=0`` + deletes keyed by the captured ids, so
the cleanup never touches or waits on this test's own uncommitted rows).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

import app.services.owner_listings as owner_listings
import app.services.users_roles as users_roles
from app.db import SessionLocal
from app.models import AdminAccount, AuthIdentity, District, User
from tests.conftest import _allocate_pincode, auth_header, register_phone


_TAG = None  # refreshed per test via _tag()

_PENDING_CLEANUPS: list = []


@pytest.fixture(scope="session", autouse=True)
def _p1_session_end_cleanup():
    """Run deferred competitor-row cleanups at pytest session end.

    The ``client`` fixture's outer transaction holds row locks on rows the
    location-chain helper adopts via SELECT ... FOR UPDATE; those locks are
    only released when every test fixture has torn down. Deleting the
    competitor's committed rows mid-test would block behind them, so the
    deletions are queued here and executed after the whole session ends.
    The queued rows are uniquely tag-named (and their pincodes are skipped
    by ``_allocate_pincode``), so they never collide with any test that
    runs before cleanup.
    """
    yield
    for fn in _PENDING_CLEANUPS:
        try:
            fn()
        except Exception as exc:  # decorators; residue is tag-unique and harmless
            print(f"P1 deferred cleanup warning: {type(exc).__name__}: {exc}")
    _PENDING_CLEANUPS.clear()


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _owner_token(c) -> str:
    """Unique phone -> owner token (mirrors test_owner_listings._setup_owner)."""
    phone = "+9198" + f"{int(uuid.uuid4().hex[:8], 16) % 10**8:08d}"
    token = register_phone(c, phone)["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


_LISTING_PAYLOAD = {
    "title": "P1 Regression Flat",
    "transaction_type": "RENT",
    "property_type": "APARTMENT",
    "price_rupees": 25000,
    "price_period": "MONTHLY",
    "locality": "Oldwashermenpet",
    "city": "Chennai",
    "pincode": "600021",
}


def _create_listing(c, token: str, **extra) -> int:
    payload = {**_LISTING_PAYLOAD, **extra}
    r = c.post("/owner/listings", json=payload, headers=auth_header(token))
    assert r.status_code == 201, r.text
    return r.json()["listing_id"]


# ---------------------------------------------------------------------------
# Competitor-session helpers (REAL commits — see module docstring)
# ---------------------------------------------------------------------------


def _b_delete_user_row(b, user_id: int) -> None:
    """Delete exactly one committed user (its single-column PK) + dependents."""
    b.execute(sql_text("SET FOREIGN_KEY_CHECKS=0"))
    for table in ("auth_identities", "user_current_role", "role_history", "admin_accounts"):
        b.execute(sql_text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": user_id})
    b.execute(sql_text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    b.commit()


def _b_build_chain(b, city_name: str, dist_name: str, loc_name: str, pin: str) -> dict:
    """Insert a full location chain on the competitor's REAL connection.

    Names mirror what _find_or_create_postcode builds (taluk, village and
    locality all take the locality name), so a same-names request collides
    at every unique-keyed level.
    """
    ids: dict = {}
    b.execute(sql_text("INSERT INTO cities (name) VALUES (:n)"), {"n": city_name})
    ids["c"] = b.execute(sql_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    b.execute(
        sql_text("INSERT INTO districts (city_id, name) VALUES (:c, :n)"),
        {"c": ids["c"], "n": dist_name},
    )
    ids["d"] = b.execute(sql_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    b.execute(
        sql_text("INSERT INTO taluks (district_id, name) VALUES (:d, :n)"),
        {"d": ids["d"], "n": loc_name},
    )
    ids["t"] = b.execute(sql_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    b.execute(
        sql_text("INSERT INTO villages (taluk_id, name) VALUES (:t, :n)"),
        {"t": ids["t"], "n": loc_name},
    )
    ids["v"] = b.execute(sql_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    b.execute(
        sql_text("INSERT INTO localities (village_id, name) VALUES (:v, :n)"),
        {"v": ids["v"], "n": loc_name},
    )
    ids["l"] = b.execute(sql_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    b.execute(
        sql_text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"),
        {"l": ids["l"], "p": pin},
    )
    ids["p"] = b.execute(sql_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    b.commit()
    return ids


def _b_delete_chain(b, ids: dict) -> None:
    """Delete the competitor's committed chain by exact ids (FK checks off)."""
    b.execute(sql_text("SET FOREIGN_KEY_CHECKS=0"))
    for table, key in (
        ("postcodes", "p"),
        ("localities", "l"),
        ("villages", "v"),
        ("taluks", "t"),
        ("districts", "d"),
        ("cities", "c"),
    ):
        b.execute(sql_text(f"DELETE FROM {table} WHERE id = :i"), {"i": ids[key]})
    b.commit()


def _b_close_after(delete_fn, b, *args) -> None:
    """Run a deferred delete, then close the competitor session."""
    try:
        delete_fn(b, *args)
    finally:
        b.close()


def _scalar_count(session, sql: str, params: dict) -> int:
    return session.execute(sql_text(sql), params).scalar_one()


# ---------------------------------------------------------------------------
# 1. Savepoint conflict handling (users_roles)
# ---------------------------------------------------------------------------


def test_link_identity_conflict_preserves_caller_work(client):
    c, session = client
    tag = _tag()
    phone = "+9198" + f"{int(tag, 16) % 10**8:08d}"
    loser = users_roles.create_user(session, display_name=f"second-verify-{tag}")
    # Snapshot prime: the pre-check below must MISS the competitor's row —
    # this SELECT takes the snapshot before the competitor commits.
    session.query(AuthIdentity).filter(
        AuthIdentity.provider == "phone",
        AuthIdentity.provider_identifier == phone,
    ).first()

    b = None
    try:
        b = SessionLocal()
        b_user = users_roles.create_user(b, display_name=f"first-verify-{tag}")
        users_roles.link_identity(b, b_user.id, "phone", phone, verified_by="test")
        b.commit()
        b_user_id = b_user.id

        # Race: identical (provider, identifier) — pre-check misses (snapshot),
        # the flush fails on the live unique key.
        with pytest.raises(HTTPException) as err:
            users_roles.link_identity(session, loser.id, "phone", phone, verified_by="otp")
        assert err.value.status_code == 409, err.value
        assert err.value.detail == "Identity already linked"

        # Earlier uncommitted caller work survived (old code: whole-txn
        # rollback evicted it — this assert fails there).
        assert session.get(User, loser.id) is not None
        # The conflicting identity was reverted, nothing silently attached.
        assert (
            session.query(AuthIdentity)
            .filter(AuthIdentity.user_id == loser.id, AuthIdentity.provider == "phone")
            .count()
            == 0
        )
        # Session healthy: further service calls work.
        users_roles.link_identity(
            session, loser.id, "google", f"g-sub-{tag}", verified_by="test"
        )
    finally:
        if b is not None:
            # Deferred: the failed duplicate INSERT holds an InnoDB S-lock on
            # the winner's committed row for this test's whole lifetime; only
            # session-end cleanup (after all fixture locks release) can remove it.
            _PENDING_CLEANUPS.append(
                lambda b=b, uid=b_user_id: _b_close_after(_b_delete_user_row, b, uid)
            )


def test_grant_admin_conflict_preserves_caller_work(client):
    c, session = client
    tag = _tag()

    b = None
    try:
        b = SessionLocal()
        b_user = users_roles.create_user(b, display_name=f"admin-racer-{tag}")
        b.commit()
        b_user_id = b_user.id

        # A-side caller work + snapshot prime (must see b_user, not the admin row).
        pend = users_roles.create_user(session, display_name=f"admin-pend-{tag}")
        assert session.get(User, b_user_id) is not None  # snapshot prime, after step 1
        session.query(AdminAccount).filter(AdminAccount.user_id == 0).first()

        users_roles.grant_admin(b, b_user_id)
        b.commit()

        with pytest.raises(HTTPException) as err:
            users_roles.grant_admin(session, b_user_id)
        assert err.value.status_code == 409, err.value
        assert err.value.detail == "Already admin"

        assert session.get(User, pend.id) is not None
        session.flush()  # session consistent; pending rows intact
        # Healthy happy path afterwards: granting the (different) user works.
        users_roles.grant_admin(session, pend.id)
        assert session.get(AdminAccount, pend.id) is not None
    finally:
        if b is not None:
            # Deferred: see the note in test_link_identity_conflict_... above.
            _PENDING_CLEANUPS.append(
                lambda b=b, uid=b_user_id: _b_close_after(_b_delete_user_row, b, uid)
            )


# ---------------------------------------------------------------------------
# 2. 422 detail hygiene (static messages, no client echo)
# ---------------------------------------------------------------------------


def test_create_listing_unknown_amenity_detail_is_static(client):
    c, _ = client
    token = _owner_token(c)
    marker = "zqx" + _tag()
    r = c.post(
        "/owner/listings",
        headers=auth_header(token),
        json={**_LISTING_PAYLOAD, "amenities": [f"Nope {marker}"]},
    )
    assert r.status_code == 422, r.text
    assert marker not in r.text  # client input is never reflected
    assert "Unknown amenity" in r.text


def test_upload_photo_bad_content_type_is_static(client):
    c, _ = client
    token = _owner_token(c)
    listing_id = _create_listing(c, token)
    marker = "zqx" + _tag()
    r = c.post(
        f"/owner/listings/{listing_id}/photos",
        headers=auth_header(token),
        files={"file": ("pet.png", b"not-really-png", f"application/x-evil-{marker}")},
        data={"category": "Living Room"},
    )
    assert r.status_code == 422, r.text
    assert marker not in r.text  # the hostile header value is not reflected
    assert "Unsupported image type" in r.text
    assert "image/png" in r.text  # static allowed-set listed instead


def test_upload_photo_bad_extension_is_static(client):
    c, _ = client
    token = _owner_token(c)
    listing_id = _create_listing(c, token)
    marker = _tag()
    r = c.post(
        f"/owner/listings/{listing_id}/photos",
        headers=auth_header(token),
        files={"file": (f"shot.{marker}", b"png-bytes", "image/png")},
        data={"category": "Living Room"},
    )
    assert r.status_code == 422, r.text
    assert marker not in r.text  # the client file name is not reflected
    assert "Unsupported file extension" in r.text
    assert ".jpg" in r.text  # static allowed-set listed instead


# ---------------------------------------------------------------------------
# 3. Location-chain race tolerance
# ---------------------------------------------------------------------------


def test_chain_race_adopts_winner_levels(client):
    """Same-names chain race: unique-keyed levels must adopt the winner's rows.

    A competitor commits the whole chain after this session's snapshot is
    taken. Our inserts then fail on the live unique keys. The call must
    always get PAST city/district/taluk/village (their only sane outcome is
    adoption via the savepoint + FOR UPDATE refetch: re-inserting is
    impossible) and surface the conflict only at the Postcode stage, where
    the approved strictness re-raises because the winner's postcode row
    belongs to the winner's — different — locality row.

    Asserting the duplicate-key target ('postcodes.pincode') proves every
    earlier level was adopted: a non-adopted level would have raised with
    its own key ('cities.name', 'districts.uk_city_name', ...) instead.
    """
    c, session = client
    tag = _tag()
    city_name = f"Testville{tag}"
    dist_name = f"D1{tag}"
    loc_name = f"L1{tag}"
    pin = _allocate_pincode(session)  # also primes the snapshot below
    session.query(AuthIdentity).filter(AuthIdentity.provider == "noop").first()

    b = None
    try:
        b = SessionLocal()
        ids = _b_build_chain(b, city_name, dist_name, loc_name, pin)

        with pytest.raises(Exception) as err:  # noqa: B017 - MySQL dup-key error
            owner_listings._find_or_create_postcode(
                session, city_name, dist_name, loc_name, pin
            )
        assert not isinstance(err.value, HTTPException), err.value
        assert "'postcodes.pincode'" in str(err.value), str(err.value)

        # The loser's own (uncommitted) locality row exists — the documented
        # residual: locality has no unique key, so it created its own row
        # while every unique-keyed level above was adopted from the winner.
        assert _scalar_count(
            session, "SELECT COUNT(*) FROM localities WHERE name = :n", {"n": loc_name}
        ) == 1
        _PENDING_CLEANUPS.append(lambda b=b, ids=ids: _b_delete_chain(b, ids))
    finally:
        pass


def test_chain_race_postcode_cross_locality_keeps_strict(client):
    """Different-locality postcode race: pincode claimed by another chain
    re-raises (no global-pincode adoption) — reviewed plan deviation."""
    c, session = client
    tag = _tag()
    pin = _allocate_pincode(session)
    session.query(AuthIdentity).filter(AuthIdentity.provider == "noop").first()  # prime

    b = None
    try:
        b = SessionLocal()
        ids = _b_build_chain(
            b, f"Otherville{tag}", f"OD1{tag}", f"OL1{tag}", pin
        )

        with pytest.raises(Exception) as err:  # noqa: B017 - MySQL dup-key error
            owner_listings._find_or_create_postcode(
                session, f"Allenville{tag}", f"AD1{tag}", f"AL1{tag}", pin
            )
        assert not isinstance(err.value, HTTPException), err.value
        assert "'postcodes.pincode'" in str(err.value), str(err.value)

        # Exactly one (the winner's) committed postcode row exists for the
        # pincode; the loser created no orphaned postcode row.
        assert b.execute(
            sql_text("SELECT COUNT(*) FROM postcodes WHERE pincode = :p"), {"p": pin}
        ).scalar_one() == 1
        _PENDING_CLEANUPS.append(lambda b=b, ids=ids: _b_delete_chain(b, ids))
    finally:
        pass


def test_chain_non_duplicate_integrity_error_reraises_immediately(client):
    """A non-duplicate-key IntegrityError must re-raise immediately.

    Direct helper call: the candidate references a nonexistent City, so the
    flush fails with a foreign-key violation (MySQL 1452) — an integrity
    error the ``FOR UPDATE`` refetch can never resolve. The helper must roll
    back its savepoint, re-raise the ORIGINAL error on the first attempt,
    and never retry or refetch (recovery is 1062-only). Pre-fix, the wide
    ``except IntegrityError`` looped all three attempts against a
    guaranteed-losing insert instead.
    """
    _, session = client
    name = f"Dghost{_tag()}"
    ghost_city_id = 987654321  # parent id that cannot exist
    lookup_calls: list[int] = []

    def lookup():
        lookup_calls.append(1)  # refetching can never fix an FK violation
        return session.query(District).filter(
            District.city_id == ghost_city_id, District.name == name
        )

    def build():
        return District(city_id=ghost_city_id, name=name)

    with pytest.raises(IntegrityError) as err:
        owner_listings._get_or_create_chain_row(session, lookup, build)

    # The ORIGINAL FK error propagated (1452 is not a duplicate-key code).
    assert err.value.orig.args[0] == 1452
    # Exactly one lookup (the initial miss): no FOR UPDATE refetch ran and
    # no retry attempt was made — the guard short-circuits both.
    assert lookup_calls == [1]
    # The savepoint was rolled back before the raise: the session is clean
    # (no leftover candidate row) and usable for further work.
    assert session.query(District).filter(District.name == name).count() == 0
