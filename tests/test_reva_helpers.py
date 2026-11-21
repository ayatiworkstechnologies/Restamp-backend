"""Shared helpers for the Reva endpoint tests (kept out of conftest.py).

These mirror the patterns used by the other suites (register_phone +
auth_header + the users/me/role self-serve switch) so the Reva tests stay
readable. Registration phones use the +916… seed ranges the other tests
already reserve for synthetic identities.
"""
from __future__ import annotations

from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone

_seq = {"n": 0}


def register_buyer(c) -> dict:
    """A fresh signed-in account (BUYER is the default current role)."""
    _seq["n"] += 1
    return register_phone(c, f"+91640010{_seq['n']:04d}")


def switch_role(c, token: str, role: str) -> None:
    r = c.post("/users/me/role", json={"role": role}, headers=auth_header(token))
    assert r.status_code == 200, r.text


def suspend_user(session, token: str) -> None:
    """Flip the caller's account status to a non-active value."""
    payload = decode_token_user_id(token)
    session.execute(
        text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": payload}
    )
    session.flush()


def decode_token_user_id(token: str) -> int:
    from app.security import decode_token

    return int(decode_token(token)["sub"])


def seed_visible_listing(session, title: str = "Reva Test Flat", deal: str = "RENT",
                         ptype: str = "APARTMENT", price_paise: int = 2_000_000,
                         bedrooms: int = 2) -> int:
    """One VERIFIED+AVAILABLE listing (buyer-visible), returns its id."""
    _seq["n"] += 1
    pc = make_postcode(session, f"-reva-{_seq['n']}")
    session.execute(text("INSERT INTO users (display_name) VALUES ('Reva Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id, bedrooms) VALUES (:p, :u, :b)"),
        {"p": pc, "u": owner, "b": bedrooms},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, price_period, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, :t, :pr, 'MONTHLY', 'VERIFIED', 'AVAILABLE', :d, :pt)"
        ),
        {"pp": pp, "u": owner, "t": title, "pr": price_paise, "d": deal, "pt": ptype},
    )
    session.flush()
    return session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
