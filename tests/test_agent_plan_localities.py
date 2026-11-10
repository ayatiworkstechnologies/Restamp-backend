"""Agent plan unused allowance: POST /broker/plan/localities.

An agent who bought a 5-locality plan but chose 3 can add the other 2 later
under the SAME plan: no payment, no new subscription, grants expire with the
plan. The limit is enforced server-side (a 6th is refused and nothing is
written), already-covered localities are never duplicated, and without an
active plan the call is refused (the app then uses the purchase flow).
Rollback-isolated.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone

_n = {"i": 0}


def _agent(c):
    _n["i"] += 1
    tok = register_phone(c, f"+916771{_n['i']:06d}")
    return auth_header(tok["access_token"]), tok["user_id"]


def _places(session, count, names=None):
    """`count` fresh localities (one postcode each) with unique names."""
    run = uuid.uuid4().hex[:6]
    out = []
    for i in range(count):
        pid = make_postcode(session, tag=f"AP{run}{i}")
        name = names[i] if names else f"Zone {run} {i}"
        session.execute(
            text("UPDATE localities SET name=:n WHERE id=(SELECT locality_id FROM postcodes WHERE id=:p)"),
            {"n": name, "p": pid},
        )
        out.append((name, pid))
    session.flush()
    return out


def _count(session, sql, uid):
    return session.execute(text(sql), {"u": uid}).scalar()


PAYMENTS = "SELECT COUNT(*) FROM payments WHERE user_id=:u"
SUBS = "SELECT COUNT(*) FROM subscriptions WHERE user_id=:u"
GRANTS = "SELECT COUNT(*) FROM broker_postcode_access_current WHERE broker_user_id=:u"
HISTORY = "SELECT COUNT(*) FROM broker_postcode_access_history WHERE broker_user_id=:u"


def _starter_with_three(c, session):
    h, uid = _agent(c)
    places = _places(session, 6)
    names = [n for n, _ in places]
    r = c.post("/broker/plans/agent-starter/subscribe", json={"localities": names[:3]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["locality_limit"] == 5 and len(r.json()["localities"]) == 3
    return h, uid, names, places


def test_status_reports_remaining_allowance(client):
    c, session = client
    h, _, names, _ = _starter_with_three(c, session)
    plan = c.get("/broker/plan", headers=h).json()
    assert plan["has_plan"] is True
    assert sorted(plan["localities"]) == sorted(names[:3])
    assert plan["localities_remaining"] == 2


def test_add_fourth_and_fifth_without_payment(client):
    c, session = client
    h, uid, names, _ = _starter_with_three(c, session)
    payments, subs = _count(session, PAYMENTS, uid), _count(session, SUBS, uid)
    end = session.execute(
        text("SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'"), {"u": uid}
    ).scalar()

    fourth = c.post("/broker/plan/localities", json={"localities": [names[3]]}, headers=h)
    assert fourth.status_code == 200, fourth.text
    assert fourth.json()["added_localities"] == [names[3]]
    assert len(fourth.json()["localities"]) == 4 and fourth.json()["localities_remaining"] == 1

    fifth = c.post("/broker/plan/localities", json={"localities": [names[4]]}, headers=h)
    assert fifth.status_code == 200, fifth.text
    assert sorted(fifth.json()["localities"]) == sorted(names[:5])
    assert fifth.json()["localities_remaining"] == 0
    assert fifth.json()["plan_id"] == "agent-starter"

    # Same plan: no new payment, no new subscription, grants expire with it.
    assert _count(session, PAYMENTS, uid) == payments
    assert _count(session, SUBS, uid) == subs
    expiries = session.execute(
        text("SELECT DISTINCT expires_at FROM broker_postcode_access_current WHERE broker_user_id=:u"), {"u": uid}
    ).fetchall()
    assert [e[0] for e in expiries] == [end]
    # The new coverage is live for browsing straight away.
    assert c.get("/broker/locations", headers=h).json()["total"] == 5


def test_sixth_is_refused_and_writes_nothing(client):
    c, session = client
    h, uid, names, _ = _starter_with_three(c, session)
    assert c.post("/broker/plan/localities", json={"localities": names[3:5]}, headers=h).status_code == 200
    grants, history = _count(session, GRANTS, uid), _count(session, HISTORY, uid)

    sixth = c.post("/broker/plan/localities", json={"localities": [names[5]]}, headers=h)
    assert sixth.status_code == 422
    assert sixth.json()["detail"] == "Agent Starter Plan covers up to 5 localities."
    assert _count(session, GRANTS, uid) == grants and _count(session, HISTORY, uid) == history
    assert c.get("/broker/plan", headers=h).json()["localities_remaining"] == 0


def test_batch_over_the_limit_is_refused_whole(client):
    c, session = client
    h, uid, names, _ = _starter_with_three(c, session)
    grants = _count(session, GRANTS, uid)
    r = c.post("/broker/plan/localities", json={"localities": names[3:6]}, headers=h)  # 3 + 3 > 5
    assert r.status_code == 422
    assert _count(session, GRANTS, uid) == grants
    assert len(c.get("/broker/plan", headers=h).json()["localities"]) == 3


def test_existing_localities_are_kept_and_never_duplicated(client):
    c, session = client
    h, uid, names, _ = _starter_with_three(c, session)
    grants, history = _count(session, GRANTS, uid), _count(session, HISTORY, uid)
    # Already covered (any casing): accepted, nothing added, nothing counted.
    again = c.post("/broker/plan/localities", json={"localities": [names[0].upper(), names[1]]}, headers=h)
    assert again.status_code == 200 and again.json()["added_localities"] == []
    assert _count(session, GRANTS, uid) == grants and _count(session, HISTORY, uid) == history
    assert again.json()["localities_remaining"] == 2
    # Mixed: the covered one is skipped, the new one is added once.
    mixed = c.post("/broker/plan/localities", json={"localities": [names[0], names[3], names[3]]}, headers=h)
    assert mixed.status_code == 200 and mixed.json()["added_localities"] == [names[3]]
    assert sorted(mixed.json()["localities"]) == sorted(names[:4])
    assert _count(session, GRANTS, uid) == grants + 1


def test_no_active_plan_is_refused(client):
    c, session = client
    h, uid = _agent(c)
    names = [n for n, _ in _places(session, 1)]
    r = c.post("/broker/plan/localities", json={"localities": names}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "No active agent plan"
    assert _count(session, GRANTS, uid) == 0 and _count(session, PAYMENTS, uid) == 0


def test_expired_plan_is_refused(client):
    c, session = client
    h, uid, names, _ = _starter_with_three(c, session)
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    session.execute(text("UPDATE subscriptions SET end_date=:e WHERE user_id=:u"), {"e": past, "u": uid})
    session.flush()
    r = c.post("/broker/plan/localities", json={"localities": [names[3]]}, headers=h)
    assert r.status_code == 409
    assert c.get("/broker/plan", headers=h).json() == {"has_plan": False}


def test_auth_and_validation(client):
    c, session = client
    h, _, names, _ = _starter_with_three(c, session)
    assert c.post("/broker/plan/localities", json={"localities": [names[3]]}).status_code == 401
    assert c.post("/broker/plan/localities", json={"localities": []}, headers=h).status_code == 422
    assert c.post("/broker/plan/localities", json={"localities": ["  "]}, headers=h).status_code == 422
    unknown = c.post("/broker/plan/localities", json={"localities": [f"Nowhere {uuid.uuid4().hex}"]}, headers=h)
    assert unknown.status_code == 422 and unknown.json()["detail"] == "None of the chosen localities are served yet."


def test_agents_are_isolated(client):
    c, session = client
    h, uid, names, _ = _starter_with_three(c, session)
    other_h, other_uid = _agent(c)
    other_names = [n for n, _ in _places(session, 2)]
    assert c.post("/broker/plans/agent-starter/subscribe", json={"localities": other_names[:1]},
                  headers=other_h).status_code == 200
    before = _count(session, GRANTS, uid)
    assert c.post("/broker/plan/localities", json={"localities": [other_names[1]]}, headers=other_h).status_code == 200
    assert _count(session, GRANTS, uid) == before
    assert sorted(c.get("/broker/plan", headers=h).json()["localities"]) == sorted(names[:3])


def test_spelling_variants_resolve_to_the_same_locality(client):
    c, session = client
    h, uid = _agent(c)
    run = uuid.uuid4().hex[:5].upper()
    places = _places(session, 1, names=[f"Q{run} Nagar"])
    r = c.post("/broker/plans/agent-starter/subscribe", json={"localities": [f"Q{run}. Nagar"]}, headers=h)
    assert r.status_code == 200 and r.json()["localities"] == [places[0][0]]
    # Re-adding it under another spelling is a duplicate, not a new locality.
    again = c.post("/broker/plan/localities", json={"localities": [f"q{run}nagar"]}, headers=h)
    assert again.status_code == 200 and again.json()["added_localities"] == []
    assert again.json()["localities_remaining"] == 4
