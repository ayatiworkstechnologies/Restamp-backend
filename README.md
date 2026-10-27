# Restamp-backend

RESTAMP Phase 4 — FastAPI + SQLAlchemy + MySQL backend.

## Local development OTP testing (restricted display mode)

Real SMS delivery is **not connected yet**: `OTPService.delivery`
(`app/services/otp.py`) ships unconnected, and with no provider configured
every `POST /auth/otp/request` fails closed with a generic 503. This is
deliberate — production phone-OTP login stays unavailable until a real SMS
provider is integrated in code.

For **local / internal testing only**, the codebase ships a restricted
display mode: when `RESTAMP_OTP_DISPLAY_MODE=1` in a **non-production-classified
** process, `/auth/otp/request` returns the backend-selected code as
`debug_code` in the API response so testers can log in without SMS. Nothing
is sent and the delivery seam is bypassed while active. Safeguards:

- The flag alone is never enough: `is_production()` is re-checked on every
  request, and it fail-closes (unknown/malformed DATABASE_URL counts as
  production).
- In production the startup gate (`validate_security_config`,
  `app/config.py:191–239`) **refuses to boot** with the flag enabled.
- Display mode substitutes delivery only — every lifecycle rule is
  unchanged: per-phone (5/hr) and per-IP (30/hr) request caps, 5 wrong-code
  attempts, 300s expiry, single-use verification, 900s blocks.

### Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Local MySQL dev database (defaults to restamp_dev over
# /tmp/mysql.sock); provision it and migrate to head ONCE:
export RESTAMP_DATABASE_URL="mysql+pymysql://USER:PASSWORD@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
alembic upgrade head
```

### Run the backend with restricted display mode

```bash
export RESTAMP_DATABASE_URL="mysql+pymysql://USER:PASSWORD@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
export RESTAMP_JWT_SECRET="pick-a-strong-random-secret-32-chars-min"
export RESTAMP_OTP_DISPLAY_MODE=1   # LOCAL/INTERNAL TESTING ONLY — never production
uvicorn app.main:app --reload --port 8000
```

### Test OTP login locally (curl)

```bash
# 1. Request an OTP (Indian mobiles only: +91 + 10 digits starting 6-9).
curl -s -X POST http://localhost:8000/auth/otp/request \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919876543210"}'
# => {"message":"If the number is valid, an OTP was sent.","debug_code":"<6 digits>"}

# 2. Verify with the displayed code (first verification registers the phone
#    as a new BUYER user).
curl -s -X POST http://localhost:8000/auth/otp/verify \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919876543210","code":"<debug_code>"}'
# => {"access_token":"...","token_type":"bearer","expires_in":1800,
#     "user_id":1,"role":"BUYER"}

# 3. Replay the same code is rejected (single use):
# => 401 {"detail":"Invalid or expired code"}
```

Notes:

- Repeating the request more than 5 times in an hour for the same phone
  returns 429 (per-phone cap). Use a different number between tests or
  tune `RESTAMP_OTP_MAX_REQUESTS_PER_HOUR` locally only.
- Wrong codes: 5 attempts then the phone is blocked for 900s.
- With display mode off in a local process, requests fail closed with the
  same generic 503 as production.

### Production posture

Explicitly supported production testing window — restricted test-only login:

- `POST /auth/test-login` — body: same shape as `/auth/otp/request`
  (Indian mobile); header: `X-Test-Login-Key: <RESTAMP_TEST_LOGIN_KEY>` value.
  When `RESTAMP_TEST_LOGIN_ENABLED="1"` and the key are configured, it
  returns the SAME `TokenOut` as `/auth/otp/verify` — **only for the
  phones explicitly listed in `RESTAMP_TEST_LOGIN_ALLOWED_PHONES`**
  (comma-separated `+91...` Indian mobiles). **NO OTP is ever generated,
  stored, returned, or logged.** Guards:
  - allowlist **fails closed**: an empty list rejects EVERY phone (403
    "Forbidden") even with a valid key;
  - any phone outside the list is rejected (403) even with a valid key,
    and no account is created for it — the list is the ONLY permission
    to use the endpoint at all;
  - account creation is a SEPARATE flag: with
    `RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION=0` (the default), existing
    allowlisted accounts log in but an unknown phone gets the same
    generic 401 as a wrong key (no account is created, no oracle); with
    `"1"`, unknown allowlisted phones register as BUYER with
    `verified_by="test_login"`;
  - disabled by default: with the flag off (or no key), it answers the
    same 404 body as an unknown route;
  - request shape is guarded too: DISABLED, every body (even malformed,
    unparseable ones) gets that exact 404 — the body is never even read;
    ACTIVE, malformed bodies get the same generic 401 as a wrong key
    (never a 422), and malformed garbage never consumes rate budget;
  - production startup REFUSES to boot if enabled with a missing/weak
    key (< 32 chars) or with malformed allowlist entries;
  - attempts are rate limited per IP (10/hr) and per phone (20/hr)
    before the key check;
  - authorization MODE (`RESTAMP_TEST_LOGIN_MODE`, default `"allowlist"`):
    in **`"existing"` mode** the allowlist is ignored and ANY valid Indian
    mobile may log in ONLY when a Restamp account already exists for it —
    an unknown phone gets the explicit 404 "No account found for this
    phone number." (key-gated: checked after the key, so it is never an
    open account-existence oracle), ZERO rows are written, and
    registration is impossible (`RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION`
    must stay `"0"` — production refuses otherwise). Production refuses
    boot for an unknown mode value; at request time an unknown mode keeps
    the endpoint disabled (exact 404).
  - in **`"suffix"` mode** (presenter convenience for a bounded demo
    window ONLY) the sole authorization is the LAST SIX DIGITS of the
    entered phone, sent in a `{phone, code}` body with NO key header
    (`X-Test-Login-Key` is ignored). **The value is predictable — the
    client literally just typed the phone — so it is NOT a secret and
    never proof of ownership**; the mode is therefore guarded: active
    ONLY inside `RESTAMP_TEST_LOGIN_SUFFIX_UNTIL` (ISO-8601 UTC deadline,
    required in production, future and at most 24h ahead at boot, and
    re-checked on EVERY request — once it passes the endpoint
    self-disarms to the exact inactive 404 with no redeploy), sharing the
    per-IP (10/hr) and per-phone (20/hr) limits, existing accounts only
    (unknown phones → the explicit 404, zero rows), registration
    structurally impossible (production refuses suffix + registration),
    and a wrong code is the byte-identical generic 401 for phones with
    and without accounts (no account-existence oracle). Recommended for
    a single presentation only; switch `RESTAMP_TEST_LOGIN_MODE` back to
    `"existing"` (or let the deadline pass) before leaving the window
    open.
  Enable only for bounded test windows, then set the flag back to `"0"`
  and/or rotate the key. Separate from the display mode: enabling one
  never affects the other.

Everything else unchanged:

- `RESTAMP_OTP_DISPLAY_MODE` must stay `0` in production (startup refuses
  `1` outright); production API responses and logs can never contain a code.
- With no SMS provider attached, public `POST /auth/otp/request` returns a
  generic 503 until real SMS delivery is implemented and wired behind
  `OTPService.delivery` (`render.yaml` notes: a code change is required —
  there is no dashboard-only toggle).
- `RESTAMP_ENV=demo` is no longer a supported environment anywhere; startup
  refuses it in every classification.
- Auth tests: `pytest tests/test_otp_delivery.py tests/test_otp_display_mode.py
  tests/test_otp_demo_mode.py tests/test_concurrency.py tests/test_auth_production.py
  tests/test_phase1_security.py tests/test_test_login.py` (requires the local
  DB at Alembic head).
