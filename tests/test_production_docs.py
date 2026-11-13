"""Production Swagger docs behind the explicit RESTAMP_DOCS_ENABLED opt-in.

- Production + RESTAMP_DOCS_ENABLED=1: /docs, /redoc and /openapi.json are
  served (standard FastAPI docs); the schema lists the API routes.
- Production without the opt-in: all three stay 404 (previous behaviour).
- Enabling docs changes no access rule: protected endpoints still answer
  401/403 without valid credentials, OTP still never returns a code in
  production (the request endpoint fails closed 503), and the schema
  contains no secret.
No database writes (requests that reach the DB use the rollback client).
"""
import pytest
from fastapi.testclient import TestClient

from app import config as config_module
from tests.conftest import auth_header, register_phone

STRONG = "z" * 40
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _production(monkeypatch, docs: bool, **overrides) -> None:
    s = config_module.settings
    values = dict(ENV="production", JWT_SECRET=STRONG, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="", CORS_ORIGINS="https://restamp.in",
                  DOCS_ENABLED=docs)
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


def _app():
    import app.main as main_module
    return main_module.create_app()


def test_docs_disabled_in_production_by_default(monkeypatch):
    _production(monkeypatch, docs=False)
    tc = TestClient(_app())
    for path in DOC_PATHS:
        assert tc.get(path).status_code == 404, path


def test_docs_enabled_in_production_with_opt_in(monkeypatch):
    _production(monkeypatch, docs=True)
    tc = TestClient(_app())
    docs = tc.get("/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text.lower() and "/openapi.json" in docs.text
    assert tc.get("/redoc").status_code == 200
    schema = tc.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/auth/otp/request" in paths and "/owner/listings" in paths and "/admin/ping" in paths


def test_schema_contains_no_secrets(monkeypatch):
    _production(monkeypatch, docs=True)
    text = TestClient(_app()).get("/openapi.json").text
    for secret in (STRONG, config_module.settings.DATABASE_URL, "restamp-local-dev-secret-change-me"):
        assert secret not in text


def test_startup_warns_when_docs_public_in_production(monkeypatch, caplog):
    """Real startup with the opt-in: boots (production gate still passes) and warns."""
    _production(monkeypatch, docs=True)
    with caplog.at_level("WARNING", logger="restamp"):
        with TestClient(_app()) as tc:
            assert tc.get("/health").status_code == 200
            assert tc.get("/docs").status_code == 200
    assert "RESTAMP_DOCS_ENABLED=1" in caplog.text


@pytest.mark.parametrize("method,path", [
    ("GET", "/auth/me"), ("GET", "/admin/ping"), ("GET", "/admin/users"), ("GET", "/owner/listings"),
    ("GET", "/owner/leads"), ("GET", "/buyer/saved"), ("GET", "/buyer/enquiries"), ("GET", "/broker/plan"),
    ("POST", "/buyer/enquiries"), ("POST", "/owner/listings"), ("GET", "/users/me/role"),
])
def test_protected_endpoints_still_require_authentication(monkeypatch, method, path):
    _production(monkeypatch, docs=True)
    tc = TestClient(_app())
    assert tc.request(method, path, json={}).status_code == 401, path
    bad = tc.request(method, path, json={}, headers={"Authorization": "Bearer not-a-valid-token"})
    assert bad.status_code == 401, path


def test_role_rules_unchanged_with_docs_enabled(client, monkeypatch):
    """A signed-in buyer is still refused owner/admin endpoints.

    The session is issued while the process is local (phone OTP login is
    fail-closed 503 in production, so it can never mint a token there) and
    stays valid after the production flip because the JWT secret is the
    same the whole test: role authorization is unchanged by docs or env.
    """
    c, _ = client
    h = auth_header(register_phone(c, "+919876543290")["access_token"])
    # Same JWT secret throughout, so the local-issued token stays valid
    # after the production flip (only the docs/env behaviour changes).
    _production(monkeypatch, docs=True, JWT_SECRET="phase4-test-secret")
    assert c.get("/owner/listings", headers=h).status_code == 403
    assert c.get("/admin/ping", headers=h).status_code in (403, 404)
    assert c.get("/buyer/saved", headers=h).status_code == 200


def test_otp_still_never_returns_a_code_in_production(client, monkeypatch):
    c, _ = client
    _production(monkeypatch, docs=True)
    r = c.post("/auth/otp/request", json={"phone": "+919876543291"})
    assert r.status_code == 503 and "debug_code" not in r.text and "349086" not in r.text


def test_local_development_docs_unchanged(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(config_module.settings, "DOCS_ENABLED", False)
    tc = TestClient(_app())
    assert tc.get("/docs").status_code == 200 and tc.get("/openapi.json").status_code == 200
