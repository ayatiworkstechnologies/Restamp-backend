"""URL normalisation + Aiven SSL handling (placeholder credentials only).

No live database is touched: these tests exercise the pure URL/SSL helpers in
app.db with placeholder hosts/credentials and a dummy CA file.
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.db import build_engine_kwargs, normalize_database_url

# Placeholder stand-ins — never real credentials or the real production host.
PLACEHOLDER_HOST = "placeholder-service.aivencloud.example.com"
PLACEHOLDER_URL = (
    "mysql+pymysql://placeholder_user:placeholder_pass"
    f"@{PLACEHOLDER_HOST}:23400/placeholder_db"
)


def _dummy_ca(tmp_path):
    ca = tmp_path / "aiven-ca.pem"
    ca.write_text("placeholder-ca-content")
    return str(ca)


def test_ssl_mode_hyphen_stripped_and_verified(tmp_path):
    ca = _dummy_ca(tmp_path)
    raw = PLACEHOLDER_URL + "?ssl-mode=REQUIRED"
    clean, args = normalize_database_url(raw, ssl_ca=ca)
    assert "ssl-mode" not in clean and "ssl_mode" not in clean
    assert args == {"ssl": {"ca": ca, "check_hostname": True}}
    # Password/host survive normalisation; ssl-mode never reaches the driver.
    parsed = make_url(clean)
    assert parsed.host == PLACEHOLDER_HOST
    assert parsed.password == "placeholder_pass"
    assert dict(parsed.query) == {}


def test_ssl_mode_underscore_variant(tmp_path):
    ca = _dummy_ca(tmp_path)
    raw = PLACEHOLDER_URL + "?ssl_mode=REQUIRED"
    clean, args = normalize_database_url(raw, ssl_ca=ca)
    assert "ssl_mode" not in clean
    assert args["ssl"]["ca"] == ca


def test_ssl_mode_case_insensitive_value(tmp_path):
    ca = _dummy_ca(tmp_path)
    clean, args = normalize_database_url(
        PLACEHOLDER_URL + "?ssl-mode=required", ssl_ca=ca
    )
    assert args["ssl"]["ca"] == ca
    assert "ssl-mode" not in clean


def test_other_query_params_preserved(tmp_path):
    ca = _dummy_ca(tmp_path)
    raw = PLACEHOLDER_URL + "?ssl-mode=REQUIRED&charset=utf8mb4"
    clean, args = normalize_database_url(raw, ssl_ca=ca)
    assert make_url(clean).query.get("charset") == "utf8mb4"
    assert "ssl-mode" not in clean
    assert args["ssl"]["ca"] == ca


def test_local_unix_socket_needs_no_ssl_or_ca():
    raw = (
        "mysql+pymysql://placeholder_user:placeholder_pass@localhost/"
        "placeholder_db?unix_socket=/tmp/mysql.sock"
    )
    clean, args = normalize_database_url(raw)
    assert args == {}
    assert make_url(clean).query.get("unix_socket") == "/tmp/mysql.sock"


def test_localhost_needs_no_ca():
    clean, args = normalize_database_url(
        "mysql+pymysql://placeholder_user:placeholder_pass@localhost/placeholder_db"
    )
    assert args == {}


def test_remote_without_ssl_mode_still_requires_ssl(tmp_path):
    ca = _dummy_ca(tmp_path)
    clean, args = normalize_database_url(PLACEHOLDER_URL, ssl_ca=ca)
    assert args == {"ssl": {"ca": ca, "check_hostname": True}}
    assert "ssl" not in dict(make_url(clean).query)


def test_remote_without_ca_fails_closed(monkeypatch):
    monkeypatch.delenv("RESTAMP_DB_SSL_CA", raising=False)
    with pytest.raises(RuntimeError, match="CA certificate"):
        normalize_database_url(PLACEHOLDER_URL + "?ssl-mode=REQUIRED")


def test_remote_disabled_ssl_refused(tmp_path):
    ca = _dummy_ca(tmp_path)
    with pytest.raises(RuntimeError, match="must remain enabled"):
        normalize_database_url(
            PLACEHOLDER_URL + "?ssl-mode=DISABLED", ssl_ca=ca
        )


def test_remote_verify_disabled_refused(tmp_path):
    ca = _dummy_ca(tmp_path)
    with pytest.raises(RuntimeError, match="verification must remain enabled"):
        normalize_database_url(
            PLACEHOLDER_URL + "?ssl-mode=REQUIRED&ssl_verify_cert=false",
            ssl_ca=ca,
        )
    with pytest.raises(RuntimeError, match="verification must remain enabled"):
        normalize_database_url(
            PLACEHOLDER_URL + "?ssl-mode=REQUIRED&ssl_verify_identity=0",
            ssl_ca=ca,
        )


def test_missing_ca_file_refused(tmp_path):
    missing = str(tmp_path / "does-not-exist.pem")
    with pytest.raises(RuntimeError, match="not found"):
        normalize_database_url(
            PLACEHOLDER_URL + "?ssl-mode=REQUIRED", ssl_ca=missing
        )


def test_ssl_ca_from_url_query(tmp_path):
    ca = _dummy_ca(tmp_path)
    raw = PLACEHOLDER_URL + f"?ssl-mode=REQUIRED&ssl_ca={ca}"
    clean, args = normalize_database_url(raw)
    # Bare ssl_ca must be translated to the verified dict form, never passed
    # through as a discrete driver arg (which would disable verification).
    assert "ssl_ca" not in clean
    assert args == {"ssl": {"ca": ca, "check_hostname": True}}


def test_ssl_ca_from_env(monkeypatch, tmp_path):
    ca = _dummy_ca(tmp_path)
    monkeypatch.setenv("RESTAMP_DB_SSL_CA", ca)
    clean, args = normalize_database_url(PLACEHOLDER_URL + "?ssl-mode=REQUIRED")
    assert args["ssl"]["ca"] == ca


def test_engine_kwargs_never_contain_ssl_mode(tmp_path):
    ca = _dummy_ca(tmp_path)
    kwargs = build_engine_kwargs(PLACEHOLDER_URL + "?ssl-mode=REQUIRED", ssl_ca=ca)
    assert "ssl-mode" not in kwargs["url"]
    assert kwargs["connect_args"] == {"ssl": {"ca": ca, "check_hostname": True}}
    engine = create_engine(kwargs["url"], connect_args=kwargs["connect_args"])
    assert "ssl-mode" not in dict(engine.url.query)
    assert engine.dialect.dbapi is not None


def test_no_ssl_disabled_or_unverified_connect_args(tmp_path):
    ca = _dummy_ca(tmp_path)
    _, args = normalize_database_url(PLACEHOLDER_URL + "?ssl-mode=REQUIRED", ssl_ca=ca)
    # Dict form (not discrete ssl_ca) so PyMySQL enables CERT_REQUIRED.
    assert "ssl" in args and isinstance(args["ssl"], dict)
    assert args["ssl"]["ca"] == ca
    assert args["ssl"].get("check_hostname") is True
    # No insecure flags anywhere in the connect args structure.
    assert "ssl_disabled" not in args
    assert "ssl_verify_cert" not in args
    assert "ssl_verify_identity" not in args
    assert args["ssl"].get("verify_mode", True) is not False


def test_bare_mysql_scheme_normalised_to_pymysql(tmp_path):
    ca = _dummy_ca(tmp_path)
    raw = (
        "mysql://placeholder_user:placeholder_pass"
        f"@{PLACEHOLDER_HOST}:23400/placeholder_db?ssl-mode=REQUIRED"
    )
    clean, args = normalize_database_url(raw, ssl_ca=ca)
    assert make_url(clean).drivername == "mysql+pymysql"
    assert args["ssl"]["ca"] == ca


def test_env_ca_not_hardcoded_and_not_logged():
    # The resolver must read from the environment, not from a hardcoded path.
    import app.db as db_module
    import inspect

    src = inspect.getsource(db_module._resolve_ssl_ca)
    assert "RESTAMP_DB_SSL_CA" in src
    # Normalised URLs keep the password for the driver but callers must never
    # log it; ensure the helper returns the real password (driver needs it)
    # rather than a masked "***" placeholder.
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False
    ) as f:
        f.write("placeholder")
        ca = f.name
    try:
        clean, _ = normalize_database_url(
            PLACEHOLDER_URL + "?ssl-mode=REQUIRED", ssl_ca=ca
        )
        assert "***" not in clean
        assert "placeholder_pass" in clean
    finally:
        os.unlink(ca)
