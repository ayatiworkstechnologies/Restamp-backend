"""Phase 5 test support — a frontend-shaped API client (test-only, not app code).

Mirrors exactly what the future Expo/mobile/web frontend must implement:
- base URL from environment (RESTAMP_API_BASE_URL)
- Bearer token storage after login, attached to every authenticated call
- logout = discard the token client-side (server tokens are stateless JWT)
- backend error mapping: 401 -> 'login', 403 -> 'denied', 404 -> 'missing',
  422/400 -> 'validation', 5xx -> 'retry'
"""
import os

DEFAULT_BASE_URL = "http://testserver"


class ApiError(Exception):
    def __init__(self, kind: str, status: int, detail: str):
        super().__init__(f"{kind} ({status}): {detail}")
        self.kind = kind
        self.status = status
        self.detail = detail


def map_error(status: int, body) -> ApiError:
    detail = body.get("detail", "error") if isinstance(body, dict) else "error"
    if status == 401:
        kind = "login"
    elif status == 403:
        kind = "denied"
    elif status == 404:
        kind = "missing"
    elif status in (400, 422):
        kind = "validation"
    elif status == 429:
        kind = "retry_later"
    else:
        kind = "retry"
    return ApiError(kind, status, detail)


class FrontendClient:
    """Simulates the frontend integration point against a TestClient transport."""

    def __init__(self, transport, base_url: str | None = None):
        self.transport = transport
        self.base_url = base_url or os.environ.get("RESTAMP_API_BASE_URL", DEFAULT_BASE_URL)
        self.token: str | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _call(self, method: str, path: str, **kwargs):
        kwargs.setdefault("headers", {}).update(self._headers())
        resp = getattr(self.transport, method)(path, **kwargs)
        if resp.status_code >= 400:
            raise map_error(resp.status_code, resp.json())
        return resp.json()

    # -- foundation integration -------------------------------------------------
    def request_otp(self, phone: str) -> dict:
        return self._call("post", "/auth/otp/request", json={"phone": phone})

    def login_phone(self, phone: str, code: str) -> dict:
        body = self._call("post", "/auth/otp/verify", json={"phone": phone, "code": code})
        self.token = body["access_token"]  # token storage
        return body

    def login_google(self, id_token: str) -> dict:
        body = self._call("post", "/auth/google", json={"id_token": id_token})
        self.token = body["access_token"]
        return body

    def logout(self) -> None:
        self.token = None  # stateless JWT: logout is client-side discard

    def get_me(self) -> dict:
        return self._call("get", "/auth/me")

    def get_role(self) -> dict:
        return self._call("get", "/users/me/role")

    def set_role(self, role: str) -> dict:
        return self._call("post", "/users/me/role", json={"role": role})

    def get(self, path: str) -> dict:
        return self._call("get", path)
