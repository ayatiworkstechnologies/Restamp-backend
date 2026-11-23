"""M8: database-backed communication rate limits (services.rate_limit).

Covers the approved policy table, per-user / per-IP / per-endpoint buckets,
the fixed 429 + Retry-After (longest wait when several windows are full),
refused and failed requests counting, no business write on 429, concurrency
and persistence on real MySQL, HMAC + IPv4 / IPv6-/64 keys, forwarded-header
trust and its RESTAMP_TRUST_PROXY / RESTAMP_CHAT_TRUST_PROXY alias, fail-closed
behaviour, production startup checks and the bounded cleanup.

The general suite disables comm limits (one shared client IP; see
conftest). Each test here re-enables them with the `limits_on` fixture.
Rollback-isolated (`client` fixture) except the concurrency / persistence /
cleanup tests, which use their own committed rows and delete them.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import config as config_module
from app import db as db_module
from app.services import rate_limit as rl_svc
from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

DETAIL = "Too many requests. Please try again soon."
# A fixed instant 100 s into a 10-minute window (and into the day / hour).
T0 = datetime(2031, 3, 10, 12, 1, 40)


@pytest.fixture()
def limits_on(monkeypatch):
    monkeypatch.setattr(config_module.settings, "COMM_RATE_LIMITS_DISABLED", False)
    monkeypatch.setattr(config_module.settings, "FORWARDED_IP_TRUSTED", False)
    clock = {"now": T0}
    monkeypatch.setattr(rl_svc, "_now", lambda: clock["now"])
    return clock


def _buyer(c):
    tok = fresh_buyer(c)
    return auth_header(tok["access_token"]), tok["user_id"]


def _owner(c, session):
    lid, _, phone = seed_owner_listing(session)
    h, uid = owner_header(c, phone)
    return h, uid, lid


def _tiny(monkeypatch, endpoint, user=1, ip=1000):
    """Shrink ONE endpoint's policy for mechanism tests (numbers asserted separately)."""
    monkeypatch.setitem(
        rl_svc.COMM_POLICY, endpoint,
        ((rl_svc.Limit("user:10m", user, 600),), (rl_svc.Limit("ip:10m", ip, 600),)),
    )


def _bad_visit(c, h, xff=None):
    headers = dict(h)
    if xff:
        headers["X-Forwarded-For"] = xff
    # A listing that does not exist: the request FAILS (404) but still counts.
    return c.post("/buyer/visits", json={"property_listing_id": 999_999_999, "scheduled_at": "2031-04-01T10:00:00Z"}, headers=headers)


# --- 1. the approved policy, exactly ---------------------------------------------

def test_policy_table_is_exactly_the_approved_numbers():
    def spec(endpoint):
        u, i = rl_svc.COMM_POLICY[endpoint]
        return [(l.count, l.seconds) for l in u], [(l.count, l.seconds) for l in i]
    assert spec("enquiry_create") == ([(20, 600), (100, 86400)], [(60, 600)])
    assert spec("lead_message") == ([(60, 600), (300, 86400)], [(120, 600)])
    assert spec("buyer_visit_create") == ([(5, 600), (20, 86400)], [(30, 600)])
    assert spec("owner_visit_create") == ([(20, 600), (100, 86400)], [(60, 600)])
    assert spec("buyer_visit_status") == ([(60, 600)], [(120, 600)])
    assert spec("owner_visit_status") == ([(60, 600)], [(120, 600)])
    assert spec("listing_contact") == ([(30, 600)], [(60, 600)])
    assert spec("lead_contact") == ([(60, 600)], [(120, 600)])
    assert len(rl_svc.COMM_POLICY) == 8


# --- 2. below / at / N+1, fixed message, Retry-After, failed requests count -------

def test_buyer_visit_create_trips_at_request_6_with_fixed_429(client, limits_on):
    c, _ = client
    h, _ = _buyer(c)
    for _ in range(5):  # below and exactly at the limit: the request's own (failed) answer
        assert _bad_visit(c, h).status_code == 404
    r = _bad_visit(c, h)
    assert r.status_code == 429
    assert r.json() == {"detail": DETAIL}
    assert r.headers["Retry-After"] == "500"  # 600 s window, 100 s in
    limits_on["now"] = T0 + timedelta(seconds=500)  # next 10-minute window
    assert _bad_visit(c, h).status_code == 404  # expired: allowed again


def test_refused_requests_keep_counting(client, limits_on, monkeypatch):
    c, session = client
    _tiny(monkeypatch, "buyer_visit_create", user=2)
    h, uid = _buyer(c)
    for _ in range(6):
        _bad_visit(c, h)
    count = session.execute(
        text("SELECT hit_count FROM rate_limit_windows WHERE bucket_key = :k"),
        {"k": f"comm:buyer_visit_create:user:{uid}"},
    ).scalar()
    assert count == 6  # 2 allowed + 4 refused, all recorded


# --- 3. isolation: user, IP, endpoint -------------------------------------------

def test_per_user_buckets_are_isolated_on_one_ip(client, limits_on, monkeypatch):
    c, _ = client
    _tiny(monkeypatch, "buyer_visit_create", user=1)
    (ha, _), (hb, _) = _buyer(c), _buyer(c)
    assert _bad_visit(c, ha).status_code == 404
    assert _bad_visit(c, ha).status_code == 429
    assert _bad_visit(c, hb).status_code == 404  # another user, same IP: unaffected


def test_per_ip_limit_spans_users_and_other_ips_are_unaffected(client, limits_on, monkeypatch):
    c, _ = client
    monkeypatch.setattr(config_module.settings, "FORWARDED_IP_TRUSTED", True)
    _tiny(monkeypatch, "buyer_visit_create", user=100, ip=3)
    users = [_buyer(c)[0] for _ in range(4)]
    for h in users[:3]:
        assert _bad_visit(c, h, xff="198.51.100.7").status_code == 404
    assert _bad_visit(c, users[3], xff="198.51.100.7").status_code == 429  # 4th user, same IP
    assert _bad_visit(c, users[3], xff="198.51.100.8").status_code == 404  # another IP


def test_endpoint_groups_are_isolated(client, limits_on, monkeypatch):
    c, _ = client
    _tiny(monkeypatch, "buyer_visit_create", user=1)
    h, _ = _buyer(c)
    _bad_visit(c, h)
    assert _bad_visit(c, h).status_code == 429
    # Same user, another group: visit status changes are still allowed.
    assert c.patch("/buyer/visits/999999999", json={"status": "CANCELLED"}, headers=h).status_code == 404


# --- 4. Retry-After: longest wait across exceeded windows ------------------------

def test_retry_after_is_the_longest_wait_and_holds_until_all_windows_expire(client, limits_on):
    _, session = client
    key = "comm:test:retry"
    limits = (rl_svc.Limit("m", 1, 60), rl_svc.Limit("h", 1, 3600))
    limits_on["now"] = datetime(2031, 3, 10, 12, 1, 40)  # 40 s into the minute, 100 s into the hour
    rl_svc.check_and_hit(session, key, limits)
    with pytest.raises(HTTPException) as exc:
        rl_svc.check_and_hit(session, key, limits)
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"] == "3500"  # not the 20 s minute wait
    # After the SHORTER wait the longer window still blocks.
    limits_on["now"] = datetime(2031, 3, 10, 12, 2, 5)
    with pytest.raises(HTTPException) as exc:
        rl_svc.check_and_hit(session, key, limits)
    assert exc.value.headers["Retry-After"] == "3475"
    # One exceeded window only: its own wait (600 s window, 125 s in at 12:02:05).
    one = (rl_svc.Limit("x", 1, 600),)
    rl_svc.check_and_hit(session, "comm:test:one", one)
    with pytest.raises(HTTPException) as exc:
        rl_svc.check_and_hit(session, "comm:test:one", one)
    assert exc.value.headers["Retry-After"] == "475"
    # After every window has expired: allowed.
    limits_on["now"] = datetime(2031, 3, 10, 13, 0, 1)
    rl_svc.check_and_hit(session, key, limits)


def test_comm_limit_returns_longest_wait_across_user_and_ip_buckets(client, limits_on, monkeypatch):
    c, _ = client
    monkeypatch.setitem(
        rl_svc.COMM_POLICY, "buyer_visit_create",
        ((rl_svc.Limit("user:day", 1, 86400),), (rl_svc.Limit("ip:10m", 1, 600),)),
    )
    h, _ = _buyer(c)
    _bad_visit(c, h)
    r = _bad_visit(c, h)
    assert r.status_code == 429
    # Both full: the day window (43,300 s left at 12:01:40) wins over the 500 s IP window.
    assert r.headers["Retry-After"] == str(86400 - (12 * 3600 + 100))


# --- 5. no business write on 429 -------------------------------------------------

def test_enquiry_refused_with_429_writes_nothing(client, limits_on, monkeypatch):
    c, session = client
    _tiny(monkeypatch, "enquiry_create", user=2)
    _, _, lid = _owner(c, session)
    h, _ = _buyer(c)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "one"}, headers=h).status_code == 201
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "two"}, headers=h).status_code == 200
    before = session.execute(text("SELECT COUNT(*) FROM enquiry_messages")).scalar()
    leads = session.execute(text("SELECT COUNT(*) FROM enquiries")).scalar()
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "three"}, headers=h)
    assert r.status_code == 429
    assert session.execute(text("SELECT COUNT(*) FROM enquiry_messages")).scalar() == before
    assert session.execute(text("SELECT COUNT(*) FROM enquiries")).scalar() == leads


def test_visit_refused_with_429_writes_nothing(client, limits_on, monkeypatch):
    c, session = client
    _tiny(monkeypatch, "buyer_visit_create", user=1)
    _, _, lid = _owner(c, session)
    h, _ = _buyer(c)
    _bad_visit(c, h)  # uses the single allowed request
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": "2031-04-01T10:00:00Z"}, headers=h)
    assert r.status_code == 429
    assert session.execute(text("SELECT COUNT(*) FROM property_visits WHERE property_listing_id=:l"), {"l": lid}).scalar() == 0


# --- 6. every one of the eight endpoint groups is protected ----------------------

@pytest.mark.parametrize("endpoint", list(rl_svc.COMM_POLICY))
def test_each_endpoint_group_is_protected(client, limits_on, monkeypatch, endpoint):
    c, session = client
    _tiny(monkeypatch, endpoint, user=1)
    bh, _ = _buyer(c)
    oh, _, _ = _owner(c, session)
    calls = {
        "enquiry_create": lambda: c.post("/buyer/enquiries", json={"property_listing_id": 999_999_999, "message": "x"}, headers=bh),
        "buyer_visit_create": lambda: _bad_visit(c, bh),
        "buyer_visit_status": lambda: c.patch("/buyer/visits/999999999", json={"status": "CANCELLED"}, headers=bh),
        "listing_contact": lambda: c.post("/buyer/listings/999999999/contact", headers=bh),
        "lead_message": lambda: c.post("/owner/leads/999999999/messages", json={"message": "x"}, headers=oh),
        "lead_contact": lambda: c.post("/owner/leads/999999999/contact", headers=oh),
        "owner_visit_create": lambda: c.post("/owner/visits", json={"property_listing_id": 999_999_999, "buyer_user_id": 1, "scheduled_at": "2031-04-01T10:00:00Z"}, headers=oh),
        "owner_visit_status": lambda: c.patch("/owner/visits/999999999", json={"status": "CANCELLED"}, headers=oh),
    }
    first = calls[endpoint]()
    assert first.status_code != 429, first.text
    second = calls[endpoint]()
    assert second.status_code == 429 and second.json() == {"detail": DETAIL}
    assert int(second.headers["Retry-After"]) >= 1


def test_contact_rate_limit_does_not_consume_credits(client, limits_on, monkeypatch):
    c, session = client
    _tiny(monkeypatch, "listing_contact", user=1)
    _, _, lid = _owner(c, session)
    h, uid = _buyer(c)
    _bad = c.post("/buyer/listings/999999999/contact", headers=h)
    assert _bad.status_code != 429
    before = session.execute(text("SELECT COUNT(*) FROM contact_reveal_audits WHERE user_id=:u"), {"u": uid}).scalar()
    r = c.post(f"/buyer/listings/{lid}/contact", headers=h)
    assert r.status_code == 429
    assert session.execute(text("SELECT COUNT(*) FROM contact_reveal_audits WHERE user_id=:u"), {"u": uid}).scalar() == before


# --- 7. keys: HMAC, IPv4, IPv6 /64, no plaintext ---------------------------------

class _Req:
    def __init__(self, host, xff=None):
        self.client = type("C", (), {"host": host})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_normalization_ipv4_ipv6_64_and_opaque():
    assert rl_svc.normalize_network("203.0.113.9") == "v4:203.0.113.9"
    assert rl_svc.normalize_network("203.0.113.10") != rl_svc.normalize_network("203.0.113.9")
    a = rl_svc.normalize_network("2001:db8:abcd:12:1::1")
    b = rl_svc.normalize_network("2001:db8:abcd:12:ffff:ffff:ffff:ffff")
    assert a == b == "v6:2001:db8:abcd:12::/64"
    assert rl_svc.normalize_network("2001:db8:abcd:13::1") != a  # another /64
    assert rl_svc.normalize_network("::ffff:203.0.113.9") == "v4:203.0.113.9"
    assert rl_svc.normalize_network("testclient") == "raw:testclient"


def test_ip_key_is_a_stable_hmac_never_plaintext(monkeypatch):
    monkeypatch.setattr(config_module.settings, "FORWARDED_IP_TRUSTED", False)
    monkeypatch.setattr(config_module.settings, "RATE_LIMIT_KEY", "k" * 40)
    one = rl_svc.ip_bucket_id(_Req("203.0.113.9"))
    assert one == rl_svc.ip_bucket_id(_Req("203.0.113.9"))  # stable (any worker, same key)
    assert one == hmac.new(b"k" * 40, b"v4:203.0.113.9", hashlib.sha256).hexdigest()
    assert "203.0.113.9" not in one and len(one) == 64
    assert one != hashlib.sha256(b"v4:203.0.113.9").hexdigest()  # keyed, not a plain hash
    monkeypatch.setattr(config_module.settings, "RATE_LIMIT_KEY", "j" * 40)
    assert rl_svc.ip_bucket_id(_Req("203.0.113.9")) != one  # depends on the secret


def test_generated_comm_keys_contain_no_plaintext_ip(client, limits_on, monkeypatch):
    c, session = client
    monkeypatch.setattr(config_module.settings, "FORWARDED_IP_TRUSTED", True)
    h, _ = _buyer(c)
    _bad_visit(c, h, xff="198.51.100.77")
    keys = [r[0] for r in session.execute(text("SELECT bucket_key FROM rate_limit_windows WHERE bucket_key LIKE 'comm:%'"))]
    assert keys and all("198.51.100.77" not in k for k in keys)


def test_ipv6_rotation_within_a_64_shares_one_bucket(client, limits_on, monkeypatch):
    c, _ = client
    monkeypatch.setattr(config_module.settings, "FORWARDED_IP_TRUSTED", True)
    _tiny(monkeypatch, "buyer_visit_create", user=100, ip=2)
    h, _ = _buyer(c)
    assert _bad_visit(c, h, xff="2001:db8:1:2::1").status_code == 404
    assert _bad_visit(c, h, xff="2001:db8:1:2::abcd").status_code == 404
    assert _bad_visit(c, h, xff="2001:db8:1:2:ffff::9").status_code == 429  # same /64
    assert _bad_visit(c, h, xff="2001:db8:1:3::1").status_code == 404  # next /64


def test_production_never_falls_back_to_plaintext_or_dev_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "RATE_LIMIT_KEY", "")
    with pytest.raises(RuntimeError):
        rl_svc.ip_bucket_id(_Req("203.0.113.9"))


# --- 8. forwarded-header trust and the alias ------------------------------------

def test_forwarded_header_ignored_unless_trusted(client, limits_on, monkeypatch):
    c, _ = client
    _tiny(monkeypatch, "buyer_visit_create", user=100, ip=2)
    h, _ = _buyer(c)
    # Trust OFF: rotating X-Forwarded-For values cannot create new buckets.
    for i in range(2):
        assert _bad_visit(c, h, xff=f"203.0.113.{i}").status_code == 404
    assert _bad_visit(c, h, xff="203.0.113.99").status_code == 429


def test_spoofed_leftmost_entries_cannot_bypass_when_trusted(client, limits_on, monkeypatch):
    c, _ = client
    monkeypatch.setattr(config_module.settings, "FORWARDED_IP_TRUSTED", True)
    _tiny(monkeypatch, "buyer_visit_create", user=100, ip=2)
    h, _ = _buyer(c)
    # The proxy appends the real address LAST; client-supplied entries before it are ignored.
    assert _bad_visit(c, h, xff="1.1.1.1, 198.51.100.5").status_code == 404
    assert _bad_visit(c, h, xff="2.2.2.2, 198.51.100.5").status_code == 404
    assert _bad_visit(c, h, xff="3.3.3.3, 198.51.100.5").status_code == 429


@pytest.mark.parametrize("canonical,legacy,expected", [
    (None, None, (False, False)),
    ("1", None, (True, False)),
    (None, "1", (True, False)),          # old deployments keep working
    ("0", None, (False, False)),
    ("1", "1", (True, False)),
    ("0", "1", (False, True)),           # conflict: fail safe (not trusted)
    ("1", "0", (False, True)),
    ("true", None, (False, False)),      # only the exact value "1" trusts
])
def test_proxy_setting_alias_and_conflict(monkeypatch, canonical, legacy, expected):
    for name, value in (("RESTAMP_TRUST_PROXY", canonical), ("RESTAMP_CHAT_TRUST_PROXY", legacy)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert config_module._resolve_proxy_trust() == expected


def test_removed_trust_proxy_attribute_stays_removed():
    # The OTP clean-up lock (test_otp_core_contract) forbids settings.TRUST_PROXY.
    assert not hasattr(config_module.settings, "TRUST_PROXY")


# --- 9. production startup checks -----------------------------------------------

def _prod(monkeypatch, **overrides):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "DATABASE_URL", "mysql+pymysql://u:p@db.example.com/restamp")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "RATE_LIMIT_KEY", "r" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "https://restamp.in")
    monkeypatch.setattr(config_module.settings, "COMM_RATE_LIMITS_DISABLED", False)
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY_CONFLICT", False)
    for k, v in overrides.items():
        monkeypatch.setattr(config_module.settings, k, v)


def test_production_startup_accepts_a_strong_dedicated_key(monkeypatch):
    _prod(monkeypatch)
    config_module.validate_security_config()


@pytest.mark.parametrize("overrides,needle", [
    ({"RATE_LIMIT_KEY": ""}, "RESTAMP_RATE_LIMIT_KEY"),
    ({"RATE_LIMIT_KEY": "short"}, "RESTAMP_RATE_LIMIT_KEY"),
    ({"RATE_LIMIT_KEY": "z" * 40}, "different from RESTAMP_JWT_SECRET"),
    ({"TRUST_PROXY_CONFLICT": True}, "disagree"),
])
def test_production_startup_refuses_unsafe_m8_config(monkeypatch, overrides, needle):
    _prod(monkeypatch, **overrides)
    with pytest.raises(RuntimeError) as exc:
        config_module.validate_security_config()
    assert needle in str(exc.value)
    assert "r" * 40 not in str(exc.value) and "z" * 40 not in str(exc.value)  # never the value


def test_production_ignores_the_test_disable_switch(monkeypatch, caplog):
    _prod(monkeypatch, COMM_RATE_LIMITS_DISABLED=True)
    with caplog.at_level("WARNING", logger="restamp.config"):
        config_module.validate_security_config()  # boots, but says the switch is ignored
    assert "ignored in production" in caplog.text
    calls = []
    monkeypatch.setattr(rl_svc, "check_and_hit", lambda db, key, limits: calls.append(key))
    rl_svc.comm_limit(None, "enquiry_create", 5, _Req("203.0.113.9"))
    assert [k.split(":")[2] for k in calls] == ["user", "ip"]  # enforced despite the switch


# --- 10. fail closed on database failure -----------------------------------------

def test_database_failure_fails_closed_and_writes_nothing(client, limits_on, monkeypatch):
    c, session = client
    _, _, lid = _owner(c, session)
    h, _ = _buyer(c)

    def boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(rl_svc, "check_and_hit", boom)
    quiet = TestClient(c.app, raise_server_exceptions=False)
    r = quiet.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=h)
    assert r.status_code == 500 and r.json() == {"detail": "Internal error"}
    assert session.execute(text("SELECT COUNT(*) FROM enquiries WHERE property_listing_id=:l"), {"l": lid}).scalar() == 0


# --- 11. real MySQL: concurrency, persistence across sessions --------------------

def _committed_cleanup(prefix):
    with db_module.engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limit_windows WHERE bucket_key LIKE :p"), {"p": f"{prefix}%"})


def test_concurrent_requests_never_exceed_the_limit(monkeypatch):
    monkeypatch.setattr(rl_svc, "_now", lambda: T0)
    prefix = "m8test:concurrency:"
    key = prefix + "k"
    limits = (rl_svc.Limit("x", 5, 600),)
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker():
        db = db_module.SessionLocal()
        try:
            barrier.wait()
            rl_svc.check_and_hit(db, key, limits)
            outcome = 200
        except HTTPException as exc:
            outcome = exc.status_code
        finally:
            db.close()
        with lock:
            results.append(outcome)

    try:
        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [200] * 5 + [429] * 7
        with db_module.engine.connect() as conn:
            assert conn.execute(text("SELECT hit_count FROM rate_limit_windows WHERE bucket_key=:k"), {"k": key}).scalar() == 12
    finally:
        _committed_cleanup(prefix)


def test_counts_persist_across_separate_sessions(monkeypatch):
    monkeypatch.setattr(rl_svc, "_now", lambda: T0)
    prefix = "m8test:persist:"
    limits = (rl_svc.Limit("x", 2, 600),)
    try:
        for _ in range(2):  # two "processes" / restarts: a new session each time
            s = db_module.SessionLocal()
            rl_svc.check_and_hit(s, prefix + "k", limits)
            s.close()
        s = db_module.SessionLocal()
        with pytest.raises(HTTPException) as exc:
            rl_svc.check_and_hit(s, prefix + "k", limits)
        s.close()
        assert exc.value.status_code == 429
    finally:
        _committed_cleanup(prefix)


# --- 12. bounded cleanup ---------------------------------------------------------

def _seed_windows(prefix, starts):
    with db_module.engine.begin() as conn:
        for i, start in enumerate(starts):
            conn.execute(
                text("INSERT INTO rate_limit_windows (bucket_key, window_start, hit_count) VALUES (:k, :s, 1)"),
                {"k": f"{prefix}{i}", "s": start},
            )


def _remaining(prefix):
    with db_module.engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM rate_limit_windows WHERE bucket_key LIKE :p"), {"p": f"{prefix}%"}).scalar()


def test_cleanup_deletes_only_expired_rows_in_bounded_batches():
    prefix = "m8test:cleanup:"
    now = datetime(2031, 3, 10, 12, 0, 0)
    old = [now - timedelta(days=1, seconds=5 + i) for i in range(5)]
    recent = [now - timedelta(hours=23), now - timedelta(minutes=1)]
    _seed_windows(prefix + "old", old)
    _seed_windows(prefix + "new", recent)
    try:
        with db_module.engine.connect() as conn:
            assert rl_svc.cleanup_expired_windows(conn, batch_size=2, max_batches=1, now=now) == 2  # one batch only
        assert _remaining(prefix + "old") == 3
        with db_module.engine.connect() as conn:
            assert rl_svc.cleanup_expired_windows(conn, batch_size=2, max_batches=10, now=now) == 3
        assert _remaining(prefix + "old") == 0
        assert _remaining(prefix + "new") == 2  # within the one-day retention: kept
    finally:
        _committed_cleanup(prefix)


def test_cleanup_does_not_overlap_another_run():
    prefix = "m8test:overlap:"
    now = datetime(2031, 3, 10, 12, 0, 0)
    _seed_windows(prefix, [now - timedelta(days=3)])
    try:
        with db_module.engine.connect() as holder, db_module.engine.connect() as conn:
            assert holder.execute(text("SELECT GET_LOCK('restamp_rate_limit_cleanup', 0)")).scalar() == 1
            assert rl_svc.cleanup_expired_windows(conn, now=now) == 0  # another run holds the lock
            holder.execute(text("SELECT RELEASE_LOCK('restamp_rate_limit_cleanup')"))
        assert _remaining(prefix) == 1
    finally:
        _committed_cleanup(prefix)


# --- 13. OTP stays stateless -------------------------------------------------------

def test_otp_endpoints_are_not_rate_limited_and_store_nothing(client, limits_on):
    c, session = client
    before = session.execute(text("SELECT COUNT(*) FROM rate_limit_windows")).scalar()
    for _ in range(40):
        assert c.post("/auth/otp/verify", json={"phone": "+919000000001", "code": "000000"}).status_code == 401
    assert session.execute(text("SELECT COUNT(*) FROM rate_limit_windows")).scalar() == before
