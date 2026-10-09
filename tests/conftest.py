"""Phase 4 test isolation: every test runs inside a rolled-back transaction on the
LOCAL restamp_dev database, so no test rows ever persist. Requires env:
RESTAMP_DATABASE_URL (unix-socket URL), RESTAMP_JWT_SECRET, RESTAMP_OTP_DEBUG=1,
RESTAMP_GOOGLE_DEV_MODE=1.
"""
import os

os.environ.setdefault(
    "RESTAMP_DATABASE_URL",
    "mysql+pymysql://restamp_dev:Restamp%40123@localhost/restamp_dev?unix_socket=/tmp/mysql.sock",
)
os.environ["RESTAMP_JWT_SECRET"] = "phase4-test-secret"
os.environ["RESTAMP_OTP_DEBUG"] = "1"
os.environ["RESTAMP_GOOGLE_DEV_MODE"] = "1"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import engine
from app.main import create_app
from app.services.otp import otp_service


@pytest.fixture()
def client():
    otp_service.reset()
    conn = engine.connect()
    trans = conn.begin()
    TestSession = db_module.sessionmaker(bind=conn)
    session: Session = TestSession()

    def _override():
        try:
            yield session
        finally:
            pass

    app = create_app()
    app.dependency_overrides[db_module.get_db] = _override
    with TestClient(app) as c:
        yield c, session
    session.close()
    trans.rollback()
    conn.close()
    otp_service.reset()


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_phone(client, phone: str) -> dict:
    """Full OTP request+verify cycle; returns the TokenOut body."""
    r = client.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = r.json()["debug_code"]
    r = client.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def make_postcode(session, tag: str = "") -> int:
    """Insert the location chain down to a postcode; returns the postcode id."""
    from sqlalchemy import text as _text

    session.execute(_text("INSERT INTO cities (name) VALUES (:n)"), {"n": f"Testville{tag}"})
    city = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO districts (city_id, name) VALUES (:c, :n)"), {"c": city, "n": f"D1{tag}"})
    dist = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO taluks (district_id, name) VALUES (:d, :n)"), {"d": dist, "n": f"T1{tag}"})
    taluk = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO villages (taluk_id, name) VALUES (:t, :n)"), {"t": taluk, "n": f"V1{tag}"})
    vil = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO localities (village_id, name) VALUES (:v, :n)"), {"v": vil, "n": f"L1{tag}"})
    loc = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        _text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"), {"l": loc, "p": f"600{tag or '001'}"[:10]}
    )
    session.flush()
    return session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
