"""M3: legacy site visits (REQUESTED / PROPOSED rows created before Phase 5)
keep the agreement they were made under, while new PENDING visits keep the
strict Phase 5 rules unchanged.

Approved legacy rules (authority by STATUS, never by initiator):

    REQUESTED -> CONFIRMED / DECLINED   owner
    REQUESTED -> CANCELLED              buyer or owner
    PROPOSED  -> CONFIRMED / DECLINED   buyer (also for an owner
                                        counter-proposal stored with
                                        initiator=BUYER)
    PROPOSED  -> CANCELLED              buyer or owner
    CONFIRMED -> CANCELLED              buyer or owner
    CONFIRMED -> COMPLETED              owner
    DECLINED / CANCELLED / COMPLETED    terminal
    no REJECTED for legacy rows; no time or note edits

Legacy rows can no longer be created through the API, so each test inserts
them directly (exactly as they exist in older databases) inside the
rollback-isolated `client` fixture.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from tests.test_communication_audit import _buyer, _dealer_poster, _enquire, _notifs, _owner, _patch

_SCHEDULED = datetime(2030, 12, 15, 10, 30, 0)
_CREATED = datetime(2026, 10, 1, 9, 0, 0)
_UPDATED = datetime(2026, 10, 2, 9, 0, 0)
_RESCHEDULED = datetime(2026, 10, 3, 9, 0, 0)
_COLUMNS = ("id", "property_listing_id", "buyer_user_id", "owner_user_id", "scheduled_at", "status",
            "initiator", "note", "created_at", "updated_at", "rescheduled_at")


def _legacy(session, lid, owner_id, buyer_id, status, initiator):
    """Insert a pre-Phase 5 visit row as older databases hold it."""
    session.execute(text(
        "INSERT INTO property_visits (property_listing_id, buyer_user_id, owner_user_id, scheduled_at, status, "
        "initiator, note, created_at, updated_at, rescheduled_at) VALUES "
        "(:l, :b, :o, :s, :st, :i, :n, :c, :u, :r)"),
        {"l": lid, "b": buyer_id, "o": owner_id, "s": _SCHEDULED, "st": status, "i": initiator,
         "n": f"legacy {status}/{initiator}", "c": _CREATED, "u": _UPDATED, "r": _RESCHEDULED})
    session.flush()
    return session.execute(text("SELECT LAST_INSERT_ID()")).scalar()


def _row(session, vid):
    r = session.execute(text(f"SELECT {', '.join(_COLUMNS)} FROM property_visits WHERE id=:v"), {"v": vid}).fetchone()
    return dict(zip(_COLUMNS, r))


def _visit_notifs(c, h):
    return [n for n in _notifs(c, h)["items"] if n["type"].startswith("VISIT_")]


@pytest.fixture()
def pair(client):
    """An owner with a live listing and an unrelated buyer."""
    c, session = client
    oh, owner_id, lid = _owner(c, session)
    bh, buyer_id = _buyer(c)
    return c, session, oh, owner_id, lid, bh, buyer_id


def _assert_persisted(session, vid, before, status):
    """New status saved; updated_at moved; rescheduled_at cleared; every
    other column (incl. created_at and initiator) unchanged."""
    after = _row(session, vid)
    assert after["status"] == status
    assert after["updated_at"] > before["updated_at"]
    assert after["rescheduled_at"] is None
    for col in ("property_listing_id", "buyer_user_id", "owner_user_id", "scheduled_at", "initiator", "note",
                "created_at"):
        assert after[col] == before[col], col


# --- 1-2: REQUESTED ----------------------------------------------------------------------

@pytest.mark.parametrize("to_status,notif", [("CONFIRMED", "VISIT_CONFIRMED"), ("DECLINED", "VISIT_DECLINED")])
def test_requested_owner_decides_and_buyer_is_notified(pair, to_status, notif):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "REQUESTED", "BUYER")
    before = _row(session, vid)
    assert _patch(c, bh, "buyer", vid, status=to_status).status_code == 422  # buyer cannot decide its own request
    r = _patch(c, oh, "owner", vid, status=to_status)
    assert r.status_code == 200 and r.json()["status"] == to_status
    _assert_persisted(session, vid, before, to_status)
    items = _visit_notifs(c, bh)
    assert [(n["type"], n["audience"], n["target"]) for n in items] == [(notif, "BUYER", {"kind": "BUYER_VISIT", "id": vid})]
    assert _visit_notifs(c, oh) == []  # the actor is never notified


@pytest.mark.parametrize("who", ["buyer", "owner"])
def test_requested_cancel_by_either_party_notifies_the_other(pair, who):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "REQUESTED", "BUYER")
    before = _row(session, vid)
    actor_h, other_h, audience = (bh, oh, "OWNER") if who == "buyer" else (oh, bh, "BUYER")
    r = _patch(c, actor_h, who, vid, status="CANCELLED")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    _assert_persisted(session, vid, before, "CANCELLED")
    assert [(n["type"], n["audience"]) for n in _visit_notifs(c, other_h)] == [("VISIT_CANCELLED", audience)]
    assert _visit_notifs(c, actor_h) == []


# --- 3-5: PROPOSED ----------------------------------------------------------------------

@pytest.mark.parametrize("initiator", ["OWNER", "BUYER"])  # BUYER = an owner counter-proposal
@pytest.mark.parametrize("to_status,notif", [("CONFIRMED", "VISIT_CONFIRMED"), ("DECLINED", "VISIT_DECLINED")])
def test_proposed_buyer_decides_whatever_the_initiator(pair, initiator, to_status, notif):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "PROPOSED", initiator)
    before = _row(session, vid)
    # The owner can never accept or decline a time awaiting the buyer.
    r = _patch(c, oh, "owner", vid, status=to_status)
    assert r.status_code == 422 and r.json()["detail"] == f"Only the buyer may move visit from PROPOSED to {to_status}"
    assert _row(session, vid)["status"] == "PROPOSED"
    r = _patch(c, bh, "buyer", vid, status=to_status)
    assert r.status_code == 200 and r.json()["status"] == to_status
    _assert_persisted(session, vid, before, to_status)
    items = _visit_notifs(c, oh)
    assert [(n["type"], n["audience"], n["target"]) for n in items] == [(notif, "OWNER", {"kind": "OWNER_VISIT", "id": vid})]
    assert _visit_notifs(c, bh) == []


@pytest.mark.parametrize("initiator", ["OWNER", "BUYER"])
@pytest.mark.parametrize("who", ["buyer", "owner"])
def test_proposed_cancel_by_either_party(pair, initiator, who):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "PROPOSED", initiator)
    before = _row(session, vid)
    actor_h, other_h, audience = (bh, oh, "OWNER") if who == "buyer" else (oh, bh, "BUYER")
    assert _patch(c, actor_h, who, vid, status="CANCELLED").status_code == 200
    _assert_persisted(session, vid, before, "CANCELLED")
    assert [(n["type"], n["audience"]) for n in _visit_notifs(c, other_h)] == [("VISIT_CANCELLED", audience)]


# --- 6-7: CONFIRMED ---------------------------------------------------------------------

@pytest.mark.parametrize("who", ["buyer", "owner"])
def test_confirmed_cancel_by_either_party(pair, who):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "CONFIRMED", "BUYER")
    before = _row(session, vid)
    actor_h, other_h, audience = (bh, oh, "OWNER") if who == "buyer" else (oh, bh, "BUYER")
    assert _patch(c, actor_h, who, vid, status="CANCELLED").status_code == 200
    _assert_persisted(session, vid, before, "CANCELLED")
    assert [(n["type"], n["audience"]) for n in _visit_notifs(c, other_h)] == [("VISIT_CANCELLED", audience)]
    assert _visit_notifs(c, actor_h) == []


def test_confirmed_completed_by_owner_only_and_silent(pair):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "CONFIRMED", "OWNER")
    before = _row(session, vid)
    r = _patch(c, bh, "buyer", vid, status="COMPLETED")
    assert r.status_code == 422 and r.json()["detail"] == "Only the owner may move visit from CONFIRMED to COMPLETED"
    assert _patch(c, oh, "owner", vid, status="COMPLETED").status_code == 200
    _assert_persisted(session, vid, before, "COMPLETED")
    assert _visit_notifs(c, bh) == [] and _visit_notifs(c, oh) == []  # completion is not notified


# --- 8-9: terminal statuses, no REJECTED ------------------------------------------------

@pytest.mark.parametrize("terminal", ["DECLINED", "CANCELLED", "COMPLETED"])
def test_terminal_statuses_are_protected(pair, terminal):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, terminal, "BUYER")
    before = _row(session, vid)
    for who, h in (("buyer", bh), ("owner", oh)):
        for target in ("CONFIRMED", "REJECTED", "DECLINED", "CANCELLED", "COMPLETED"):
            if target == terminal:
                continue
            r = _patch(c, h, who, vid, status=target)
            assert r.status_code == 422, (who, target)
            assert r.json()["detail"] == f"Cannot move visit from {terminal} to {target}"
        # A same-state repeat is an idempotent no-op: nothing written, nothing sent.
        r = _patch(c, h, who, vid, status=terminal)
        assert r.status_code == 200 and r.json()["status"] == terminal
    assert _row(session, vid) == before
    assert _visit_notifs(c, bh) == [] and _visit_notifs(c, oh) == []


@pytest.mark.parametrize("status,initiator", [("REQUESTED", "BUYER"), ("PROPOSED", "OWNER"), ("PROPOSED", "BUYER")])
def test_rejected_is_refused_for_legacy_rows(pair, status, initiator):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, status, initiator)
    before = _row(session, vid)
    for who, h in (("owner", oh), ("buyer", bh)):
        r = _patch(c, h, who, vid, status="REJECTED")
        assert r.status_code == 422 and r.json()["detail"] == f"Cannot move visit from {status} to REJECTED"
    assert _row(session, vid) == before
    assert _visit_notifs(c, bh) == [] and _visit_notifs(c, oh) == []


# --- 10: dealer acting as owner ---------------------------------------------------------

def test_dealer_poster_follows_owner_side_rules(client):
    c, session = client
    dh, dealer_id, lid = _dealer_poster(c, session)
    bh, buyer_id = _buyer(c)
    req = _legacy(session, lid, dealer_id, buyer_id, "REQUESTED", "BUYER")
    assert _patch(c, dh, "owner", req, status="CONFIRMED").status_code == 200  # BROKER mode, /owner/visits
    assert [n["type"] for n in _visit_notifs(c, bh)] == ["VISIT_CONFIRMED"]
    prop = _legacy(session, lid, dealer_id, buyer_id, "PROPOSED", "BUYER")
    assert _patch(c, dh, "owner", prop, status="CONFIRMED").status_code == 422
    assert _patch(c, bh, "buyer", prop, status="CONFIRMED").status_code == 200
    dealer_items = _visit_notifs(c, dh)
    assert [(n["type"], n["audience"], n["target"]["kind"]) for n in dealer_items] == [
        ("VISIT_CONFIRMED", "OWNER", "OWNER_VISIT")]
    assert _patch(c, dh, "owner", prop, status="COMPLETED").status_code == 200


# --- 11: unauthorised parties -----------------------------------------------------------

@pytest.mark.parametrize("status,initiator", [("REQUESTED", "BUYER"), ("PROPOSED", "BUYER"), ("CONFIRMED", "OWNER")])
def test_unrelated_buyer_owner_and_dealer_get_404(pair, status, initiator):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, status, initiator)
    before = _row(session, vid)
    other_bh, _ = _buyer(c)
    other_oh, _, _ = _owner(c, session)
    other_dh, _, _ = _dealer_poster(c, session)
    for target in ("CONFIRMED", "DECLINED", "CANCELLED", "COMPLETED"):
        assert _patch(c, other_bh, "buyer", vid, status=target).status_code == 404
        assert _patch(c, other_oh, "owner", vid, status=target).status_code == 404
        assert _patch(c, other_dh, "owner", vid, status=target).status_code == 404
    assert c.get(f"/buyer/visits/{vid}", headers=other_bh).status_code == 404
    assert c.get(f"/owner/visits/{vid}", headers=other_oh).status_code == 404
    assert c.get(f"/owner/visits/{vid}", headers=other_dh).status_code == 404
    assert _row(session, vid) == before
    assert _visit_notifs(c, bh) == [] and _visit_notifs(c, oh) == []


# --- 12: closed lead --------------------------------------------------------------------

def test_closed_lead_cancels_open_legacy_visits_and_freezes_them(pair):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    eid = _enquire(c, bh, lid, "about my old visits")["id"]
    ids = {s: _legacy(session, lid, owner_id, buyer_id, s, i)
           for s, i in (("REQUESTED", "BUYER"), ("PROPOSED", "BUYER"), ("CONFIRMED", "OWNER"))}
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Not Converted"},
                   headers=oh).status_code == 200
    for vid in ids.values():
        assert _row(session, vid)["status"] == "CANCELLED"
        for who, h, target in (("owner", oh, "CONFIRMED"), ("buyer", bh, "CONFIRMED"), ("buyer", bh, "CANCELLED")):
            r = _patch(c, h, who, vid, status=target)
            assert r.status_code == 422 and r.json() == {"detail": "Lead is closed"}


# --- 14: repeats ------------------------------------------------------------------------

def test_repeated_action_writes_and_notifies_once(pair):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    vid = _legacy(session, lid, owner_id, buyer_id, "PROPOSED", "BUYER")
    assert _patch(c, bh, "buyer", vid, status="CONFIRMED").status_code == 200
    first = _row(session, vid)
    for _ in range(2):
        r = _patch(c, bh, "buyer", vid, status="CONFIRMED")
        assert r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    assert _row(session, vid) == first  # no second write (updated_at unchanged)
    assert [n["type"] for n in _visit_notifs(c, oh)] == ["VISIT_CONFIRMED"]


# --- 16: no time or note edits ----------------------------------------------------------

@pytest.mark.parametrize("field", ["scheduled_at", "note"])
def test_time_and_note_edits_refused_with_accurate_message(pair, field):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    value = "2031-01-05T10:00:00Z" if field == "scheduled_at" else "changed note"
    proposer = "The proposer cannot edit a submitted visit proposal"
    cases = [
        ("REQUESTED", "BUYER", "buyer", bh, proposer),  # the buyer's own request
        ("REQUESTED", "BUYER", "owner", oh, "A REQUESTED visit cannot be rescheduled or edited"),
        ("PROPOSED", "OWNER", "owner", oh, proposer),   # the owner's own proposal
        ("PROPOSED", "OWNER", "buyer", bh, "A PROPOSED visit cannot be rescheduled or edited"),
        # Owner counter-proposal stored with initiator=BUYER: the time is the
        # OWNER's, so the buyer is never told they proposed it.
        ("PROPOSED", "BUYER", "buyer", bh, "A PROPOSED visit cannot be rescheduled or edited"),
        ("PROPOSED", "BUYER", "owner", oh, proposer),
        ("CONFIRMED", "BUYER", "buyer", bh, "A CONFIRMED visit cannot be rescheduled or edited"),
        ("CONFIRMED", "BUYER", "owner", oh, "A CONFIRMED visit cannot be rescheduled or edited"),
    ]
    for status, initiator, who, h, message in cases:
        vid = _legacy(session, lid, owner_id, buyer_id, status, initiator)
        before = _row(session, vid)
        r = _patch(c, h, who, vid, **{field: value})
        assert r.status_code == 422 and r.json()["detail"] == message, (status, initiator, who)
        assert _row(session, vid) == before


# --- 17: Phase 5 PENDING unchanged ------------------------------------------------------

def test_pending_phase5_rules_unchanged(pair):
    c, session, oh, owner_id, lid, bh, buyer_id = pair
    _enquire(c, bh, lid, "interested")  # relationship for the owner proposal
    req = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": "2030-12-20T05:00:00Z"},
                 headers=bh).json()
    assert (req["status"], req["initiator"]) == ("PENDING", "BUYER")
    assert _patch(c, bh, "buyer", req["id"], status="CONFIRMED").status_code == 422
    r = _patch(c, bh, "buyer", req["id"], scheduled_at="2030-12-21T05:00:00Z")
    assert r.status_code == 422 and r.json()["detail"] == "The proposer cannot edit a submitted visit proposal"
    assert _patch(c, oh, "owner", req["id"], status="REJECTED").status_code == 200  # REJECTED stays valid for PENDING
    prop = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": buyer_id,
                                         "scheduled_at": "2030-12-22T05:00:00Z"}, headers=oh).json()
    assert (prop["status"], prop["initiator"]) == ("PENDING", "OWNER")
    assert _patch(c, bh, "buyer", prop["id"], status="CONFIRMED").status_code == 422  # strict Phase 5: owner decides
    assert _patch(c, bh, "buyer", prop["id"], status="DECLINED").status_code == 422
    r = _patch(c, oh, "owner", prop["id"], note="x")
    assert r.status_code == 422 and r.json()["detail"] == "The proposer cannot edit a submitted visit proposal"
    assert _patch(c, oh, "owner", prop["id"], status="CONFIRMED").status_code == 200
    assert _patch(c, bh, "buyer", prop["id"], status="CANCELLED").status_code == 200
    types = sorted(n["type"] for n in _visit_notifs(c, bh))
    assert types == ["VISIT_CONFIRMED", "VISIT_PROPOSED", "VISIT_REJECTED"]
    assert sorted(n["type"] for n in _visit_notifs(c, oh)) == ["VISIT_CANCELLED", "VISIT_REQUESTED"]
