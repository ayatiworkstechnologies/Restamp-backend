"""Local CORS regression: browser origins receive ACAO headers (rollback-isolated)."""
from fastapi.testclient import TestClient


def test_cors_local_origins(client):
    c, _ = client
    r = c.get("/buyer/listings", headers={"Origin": "http://localhost:8081"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:8081"
