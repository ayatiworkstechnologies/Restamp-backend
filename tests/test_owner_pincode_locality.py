"""Owner listings never discard or replace the pincode the owner entered.

Regression for owner_listings._find_or_create_postcode: it used to fall back
to ANY existing postcode of a same-named locality, so "Anna Nagar" + 600101
was silently stored as Anna Nagar's 600040. Now:
- a pincode already in the catalog is accepted only with its own locality
  (spelling-insensitive, or an alias) -> otherwise 422, nothing written;
- a new pincode is added to the entered locality (no duplicate locality row);
- updates follow the same rules.
Fixture pincodes are free test values. Rollback-isolated.
"""
import uuid

from sqlalchemy import text

from app.services import localities as loc_svc
from app.services.owner_listings import PINCODE_LOCALITY_MISMATCH
from tests.conftest import auth_header, fresh_pincode, register_phone

_n = {"i": 0}


def _owner(c):
    _n["i"] += 1
    tok = register_phone(c, f"+916775{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 200
    return h


def _post(c, h, locality, pincode, title="Pin Flat"):
    return c.post("/owner/listings", json={
        "title": title, "transaction_type": "RENT", "property_type": "APARTMENT",
        "price_rupees": 20000, "locality": locality, "city": "Chennai", "pincode": pincode,
    }, headers=h)


def _counts(session):
    return tuple(session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                 for t in ("property_listings", "physical_properties", "postcodes", "localities"))


def _place(c, session, aliases=()):
    name = f"Pinpet {uuid.uuid4().hex[:6]}"
    pin = fresh_pincode(c)
    loc_svc.load_master(session, [loc_svc.MasterRow(name, pin, tuple(aliases))])
    session.flush()
    return name, pin


def test_new_pincode_for_a_known_locality_keeps_the_owners_pincode(client):
    c, session = client
    h = _owner(c)
    name, catalog_pin = _place(c, session)
    entered = fresh_pincode(c)  # e.g. Anna Nagar + 600101 while the catalog has 600040
    r = _post(c, h, name, entered)
    assert r.status_code == 201, r.text
    assert r.json()["pincode"] == entered and r.json()["pincode"] != catalog_pin
    assert r.json()["locality"] == name
    rows = session.execute(text(
        "SELECT l.id FROM localities l WHERE l.name=:n"), {"n": name}).fetchall()
    assert len(rows) == 1  # the new pincode joined the existing locality, no duplicate
    assert session.execute(text("SELECT locality_id FROM postcodes WHERE pincode=:p"),
                           {"p": entered}).scalar() == rows[0][0]


def test_another_localitys_pincode_is_refused_and_nothing_is_written(client):
    c, session = client
    h = _owner(c)
    _, owned_pin = _place(c, session)
    other, _ = _place(c, session)
    before = _counts(session)
    r = _post(c, h, other, owned_pin)
    assert r.status_code == 422 and r.json()["detail"] == PINCODE_LOCALITY_MISMATCH
    r = _post(c, h, f"Brand New {uuid.uuid4().hex[:5]}", owned_pin)
    assert r.status_code == 422 and r.json()["detail"] == PINCODE_LOCALITY_MISMATCH
    assert _counts(session) == before


def test_spelling_variants_and_aliases_of_the_owning_locality_are_accepted(client, monkeypatch):
    from app.config import settings

    c, session = client
    h = _owner(c)
    run = uuid.uuid4().hex[:5].upper()
    name, pin = f"Q{run}. Nagar", fresh_pincode(c)
    alias = f"Qalias{run}pet"
    loc_svc.load_master(session, [loc_svc.MasterRow(name, pin, (alias,))])
    session.flush()
    # Spelling variants are always accepted (independent of the paused feature).
    for entered in (name, f"q{run} nagar", f"Q{run}-NAGAR"):
        r = _post(c, h, entered, pin, title=f"Variant {entered}")
        assert r.status_code == 201, (entered, r.text)
        assert r.json()["pincode"] == pin and r.json()["locality"] == name  # canonical name shown
    # TEMPORARILY DISABLED — Chennai area seeding paused: aliases count only
    # with the area-seeding flag on.
    assert _post(c, h, alias, pin, title="Alias off").status_code == 422
    monkeypatch.setattr(settings, "AREA_SEEDING_ENABLED", True)
    r = _post(c, h, alias, pin, title="Alias on")
    assert r.status_code == 201 and r.json()["locality"] == name


def test_brand_new_locality_and_pincode_still_create_the_chain(client):
    c, session = client
    h = _owner(c)
    name, pin = f"Freshpet {uuid.uuid4().hex[:6]}", fresh_pincode(c)
    r = _post(c, h, name, pin)
    assert r.status_code == 201 and r.json()["pincode"] == pin and r.json()["locality"] == name


def test_update_follows_the_same_rules(client):
    c, session = client
    h = _owner(c)
    name, pin = _place(c, session)
    other, other_pin = _place(c, session)
    lid = _post(c, h, name, pin).json()["listing_id"]
    # Moving to a pincode that belongs to a different locality: refused.
    r = c.patch(f"/owner/listings/{lid}", json={"pincode": other_pin}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == PINCODE_LOCALITY_MISMATCH
    assert c.get(f"/owner/listings/{lid}", headers=h).json()["pincode"] == pin
    # Moving with the right locality: allowed.
    r = c.patch(f"/owner/listings/{lid}", json={"locality": other, "pincode": other_pin}, headers=h)
    assert r.status_code == 200 and r.json()["pincode"] == other_pin and r.json()["locality"] == other
    # A new pincode for the same locality is stored as entered.
    new_pin = fresh_pincode(c)
    r = c.patch(f"/owner/listings/{lid}", json={"pincode": new_pin}, headers=h)
    assert r.status_code == 200 and r.json()["pincode"] == new_pin and r.json()["locality"] == other
