"""M4: viewer-specific `actions` on every visit response.

The flags are computed server-side from the visit status, the authenticated
viewer's side (the stored buyer vs the stored owner; a dealer acting as owner
is the owner side) and whether the lead is closed, using the same rule table
that authorises PATCH. These tests check the full status/viewer matrix on
detail and list responses, prove that every advertised action is accepted by
the real PATCH endpoint and every unadvertised one is refused (on a fresh
visit copy per check), and cover closed leads, unauthorised users, create /
status-change responses, the unchanged response shape and the batched
(non-N+1) closed-lead lookup. Rollback-isolated (`client` fixture).
"""
from __future__ import annotations

import pytest
from sqlalchemy import event

from app import db as db_module
from app.services import visits as visit_svc
from tests.test_communication_audit import _buyer, _dealer_poster, _enquire, _owner, _patch
from tests.test_visit_legacy_lifecycle import _legacy, _row

FLAGS = ("can_confirm", "can_reject", "can_decline", "can_cancel", "can_complete")
TARGET = {"can_confirm": "CONFIRMED", "can_reject": "REJECTED", "can_decline": "DECLINED",
          "can_cancel": "CANCELLED", "can_complete": "COMPLETED"}
NONE = dict.fromkeys(FLAGS, False)


def _flags(*on):
    return {f: f in on for f in FLAGS}


# Approved matrix: (status, initiator) -> {viewer side: allowed actions}.
CASES = {
    ("PENDING", "BUYER"): {"buyer": _flags("can_cancel"),
                           "owner": _flags("can_confirm", "can_reject", "can_cancel")},
    ("PENDING", "OWNER"): {"buyer": _flags("can_cancel"),
                           "owner": _flags("can_confirm", "can_reject", "can_cancel")},
    ("REQUESTED", "BUYER"): {"buyer": _flags("can_cancel"),
                             "owner": _flags("can_confirm", "can_decline", "can_cancel")},
    ("PROPOSED", "OWNER"): {"buyer": _flags("can_confirm", "can_decline", "can_cancel"),
                            "owner": _flags("can_cancel")},
    # Owner counter-proposal stored with initiator=BUYER: still the buyer's decision.
    ("PROPOSED", "BUYER"): {"buyer": _flags("can_confirm", "can_decline", "can_cancel"),
                            "owner": _flags("can_cancel")},
    ("CONFIRMED", "BUYER"): {"buyer": _flags("can_cancel"),
                             "owner": _flags("can_cancel", "can_complete")},
    ("CONFIRMED", "OWNER"): {"buyer": _flags("can_cancel"),
                             "owner": _flags("can_cancel", "can_complete")},
    ("DECLINED", "BUYER"): {"buyer": NONE, "owner": NONE},
    ("CANCELLED", "OWNER"): {"buyer": NONE, "owner": NONE},
    ("COMPLETED", "BUYER"): {"buyer": NONE, "owner": NONE},
    ("REJECTED", "BUYER"): {"buyer": NONE, "owner": NONE},
}
VISIT_KEYS = {"id", "property_listing_id", "buyer_user_id", "owner_user_id", "buyer_display_name",
              "owner_display_name", "scheduled_at", "status", "initiator", "requester_role", "note", "listing",
              "created_at", "updated_at", "rescheduled_at", "actions"}


@pytest.fixture()
def world(client):
    """Owner listing + dealer listing, one buyer related to both, and
    unrelated buyer/owner/dealer accounts."""
    c, session = client
    oh, owner_id, lid = _owner(c, session)
    dh, dealer_id, dlid = _dealer_poster(c, session)
    bh, buyer_id = _buyer(c)
    return {
        "c": c, "session": session, "buyer": (bh, buyer_id),
        "owner": (oh, owner_id, lid), "dealer": (dh, dealer_id, dlid),
    }


def _sides(w):
    """(label, owner-side headers, owner id, listing id) for owner and dealer."""
    return [("owner", *w["owner"]), ("dealer", *w["dealer"])]


def _detail(c, side, h, vid):
    r = c.get(f"/{'buyer' if side == 'buyer' else 'owner'}/visits/{vid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _list_item(c, side, h, vid):
    r = c.get(f"/{'buyer' if side == 'buyer' else 'owner'}/visits", params={"page_size": 100}, headers=h)
    assert r.status_code == 200, r.text
    return next(v for v in r.json()["items"] if v["id"] == vid)


# --- 1-7, 10, 17: matrix on detail and list, buyer vs owner vs dealer -----------------------

@pytest.mark.parametrize("status,initiator", list(CASES))
def test_matrix_on_detail_and_list_for_buyer_owner_and_dealer(world, status, initiator):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    for _label, ph, poster_id, lid in _sides(world):
        vid = _legacy(session, lid, poster_id, buyer_id, status, initiator)
        expected = CASES[(status, initiator)]
        for side, h in (("buyer", bh), ("owner", ph)):
            assert _detail(c, side, h, vid)["actions"] == expected[side], (side, status, initiator)
            assert _list_item(c, side, h, vid)["actions"] == expected[side], (side, status, initiator)
        # The same visit shows each viewer its own actions (never the other side's).
        if expected["buyer"] != expected["owner"]:
            assert _detail(c, "buyer", bh, vid)["actions"] != _detail(c, "owner", ph, vid)["actions"]


# --- 15-16: advertised == executable, unadvertised == refused -------------------------------

@pytest.mark.parametrize("status,initiator", list(CASES))
def test_advertised_actions_execute_and_others_are_refused(world, status, initiator):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    for _label, ph, poster_id, lid in _sides(world):
        for side, h in (("buyer", bh), ("owner", ph)):
            advertised = _detail(c, side, h, _legacy(session, lid, poster_id, buyer_id, status, initiator))["actions"]
            for flag in FLAGS:
                fresh = _legacy(session, lid, poster_id, buyer_id, status, initiator)  # one copy per check
                before = _row(session, fresh)
                r = _patch(c, h, side, fresh, status=TARGET[flag])
                if advertised[flag]:
                    assert r.status_code == 200 and r.json()["status"] == TARGET[flag], (side, status, flag, r.text)
                elif TARGET[flag] == status:
                    # Repeating the current status is never advertised (D4) but is the
                    # existing idempotent no-op: accepted, nothing written or sent.
                    assert r.status_code == 200 and _row(session, fresh) == before, (side, status, flag)
                else:
                    assert r.status_code == 422, (side, status, flag, r.text)
                    assert _row(session, fresh) == before


# --- 8: closed leads -----------------------------------------------------------------------

def test_closed_lead_exposes_no_actions_and_refuses_changes(world):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    oh, owner_id, lid = world["owner"]
    eid = _enquire(c, bh, lid, "hello")["id"]
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Not Converted"},
                   headers=oh).status_code == 200
    for status, initiator in (("PENDING", "BUYER"), ("REQUESTED", "BUYER"), ("PROPOSED", "BUYER"),
                              ("CONFIRMED", "OWNER")):
        vid = _legacy(session, lid, owner_id, buyer_id, status, initiator)  # open status on a closed lead
        for side, h in (("buyer", bh), ("owner", oh)):
            assert _detail(c, side, h, vid)["actions"] == NONE
            assert _list_item(c, side, h, vid)["actions"] == NONE
            for target in ("CONFIRMED", "CANCELLED"):
                r = _patch(c, h, side, vid, status=target)
                assert r.status_code == 422 and r.json() == {"detail": "Lead is closed"}
    # A fresh enquiry opens a new lead: actions come back.
    _enquire(c, bh, lid, "again")
    vid = _legacy(session, lid, owner_id, buyer_id, "PENDING", "BUYER")
    assert _detail(c, "owner", oh, vid)["actions"] == CASES[("PENDING", "BUYER")]["owner"]


# --- 9: unauthorised users -----------------------------------------------------------------

def test_unrelated_users_get_404_and_never_see_actions(world):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    oh, owner_id, lid = world["owner"]
    vid = _legacy(session, lid, owner_id, buyer_id, "PENDING", "BUYER")
    other_bh, _ = _buyer(c)
    other_oh, _, _ = _owner(c, session)
    other_dh, _, _ = _dealer_poster(c, session)
    assert c.get(f"/buyer/visits/{vid}", headers=other_bh).status_code == 404
    for h in (other_oh, other_dh):
        r = c.get(f"/owner/visits/{vid}", headers=h)
        assert r.status_code == 404 and "actions" not in r.json()
    assert vid not in [v["id"] for v in c.get("/buyer/visits", headers=other_bh).json()["items"]]
    for h in (other_oh, other_dh):
        assert vid not in [v["id"] for v in c.get("/owner/visits", headers=h).json()["items"]]


# --- 11: mixed lists -----------------------------------------------------------------------

def test_mixed_list_computes_actions_per_visit(world):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    oh, owner_id, lid = world["owner"]
    ids = {key: _legacy(session, lid, owner_id, buyer_id, *key) for key in CASES}
    for side, h in (("buyer", bh), ("owner", oh)):
        items = {v["id"]: v for v in c.get(f"/{side}/visits", params={"page_size": 100}, headers=h).json()["items"]}
        for key, vid in ids.items():
            assert items[vid]["actions"] == CASES[key][side], (side, key)


# --- 12-13: create and status-change responses ---------------------------------------------

def test_create_responses_carry_creator_actions(world):
    c, _ = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    oh, _, lid = world["owner"]
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": "2030-12-20T05:00:00Z"}, headers=bh)
    assert r.status_code == 201 and r.json()["actions"] == CASES[("PENDING", "BUYER")]["buyer"]
    _enquire(c, bh, lid, "interested")
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": buyer_id,
                                      "scheduled_at": "2030-12-21T05:00:00Z"}, headers=oh)
    assert r.status_code == 201 and r.json()["actions"] == CASES[("PENDING", "OWNER")]["owner"]


def test_status_change_response_describes_the_new_status(world):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    oh, owner_id, lid = world["owner"]
    vid = _legacy(session, lid, owner_id, buyer_id, "PENDING", "BUYER")
    r = _patch(c, oh, "owner", vid, status="CONFIRMED")
    assert r.status_code == 200 and r.json()["actions"] == CASES[("CONFIRMED", "BUYER")]["owner"]
    assert _detail(c, "buyer", bh, vid)["actions"] == CASES[("CONFIRMED", "BUYER")]["buyer"]
    r = _patch(c, bh, "buyer", vid, status="CANCELLED")
    assert r.status_code == 200 and r.json()["actions"] == NONE
    # An idempotent repeat answers with the (unchanged) current actions.
    r = _patch(c, bh, "buyer", vid, status="CANCELLED")
    assert r.status_code == 200 and r.json()["actions"] == NONE
    prop = _legacy(session, lid, owner_id, buyer_id, "PROPOSED", "BUYER")
    r = _patch(c, bh, "buyer", prop, status="CONFIRMED")
    assert r.status_code == 200 and r.json()["actions"] == CASES[("CONFIRMED", "BUYER")]["buyer"]


# --- 14: response shape --------------------------------------------------------------------

def test_existing_fields_unchanged_plus_actions(world):
    c, session = world["c"], world["session"]
    bh, buyer_id = world["buyer"]
    oh, owner_id, lid = world["owner"]
    vid = _legacy(session, lid, owner_id, buyer_id, "REQUESTED", "BUYER")
    for side, h in (("buyer", bh), ("owner", oh)):
        body = _detail(c, side, h, vid)
        assert set(body) == VISIT_KEYS
        assert set(body["actions"]) == set(FLAGS) and all(isinstance(v, bool) for v in body["actions"].values())
        assert body["status"] == "REQUESTED" and body["initiator"] == "BUYER"
        assert body["scheduled_at"].endswith("Z") and body["listing"]["title"]


# --- 18: batched closed-lead lookup --------------------------------------------------------

def _enquiry_queries(c, h, url):
    seen = []

    def _count(conn, cursor, statement, params, context, executemany):
        if "FROM enquiries" in statement:
            seen.append(statement)

    event.listen(db_module.engine, "before_cursor_execute", _count)
    try:
        assert c.get(url, params={"page_size": 100}, headers=h).status_code == 200
    finally:
        event.remove(db_module.engine, "before_cursor_execute", _count)
    return len(seen)


def test_list_closed_lead_lookup_is_batched_not_per_visit(world):
    c, session = world["c"], world["session"]
    oh, owner_id, lid = world["owner"]
    buyers = [_buyer(c) for _ in range(3)]
    for _bh, bid in buyers:
        _legacy(session, lid, owner_id, bid, "PENDING", "BUYER")
    few = _enquiry_queries(c, oh, "/owner/visits")
    for _bh, bid in buyers:
        for status in ("REQUESTED", "PROPOSED", "CONFIRMED"):
            _legacy(session, lid, owner_id, bid, status, "BUYER")
    many = _enquiry_queries(c, oh, "/owner/visits")
    assert few == many == 1, (few, many)  # one enquiries query per page, regardless of visit count


# --- helper invariants ---------------------------------------------------------------------

def test_visit_actions_uses_the_same_authority_as_patch():
    for (status, to_status), _who in visit_svc._ALLOWED.items():
        for viewer in ("buyer", "owner"):
            flags = visit_svc.visit_actions(status, viewer, lead_closed=False)
            flag = next((f for f, t in visit_svc._ACTION_TARGETS if t == to_status), None)
            if flag is not None and to_status != status:
                assert flags[flag] == visit_svc._side_may(status, to_status, viewer), (status, to_status, viewer)
        assert visit_svc.visit_actions(status, "owner", lead_closed=True) == NONE
    with pytest.raises(ValueError):
        visit_svc.visit_actions("PENDING", "admin", lead_closed=False)
