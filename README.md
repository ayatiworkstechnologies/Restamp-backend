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
  tests/test_phase1_security.py` (requires the local
  DB at Alembic head).

## Running the test suite (local `restamp_test`)

Run the full suite against the dedicated local `restamp_test` database, not
`restamp_dev`: existing dev data (e.g. phone identities) collides with test
fixtures. Every test runs in a rolled-back transaction, so the suite leaves
`restamp_test` unchanged.

```bash
# In the same shell that runs pytest/alembic (pytest does not read .env):
export RESTAMP_DATABASE_URL="<local restamp_test URL>"
export RESTAMP_TEST_REQUIRE_DISPOSABLE=1   # disposable-database guard
alembic upgrade head                       # only if restamp_test is behind head
pytest
```

- `<local restamp_test URL>` is your `restamp_dev` URL from Local setup with
  the database name changed to `restamp_test` (same user, same
  `unix_socket=/tmp/mysql.sock`). Never commit it or put it in tracked files.
- `tests/conftest.py` has no fallback URL: if `RESTAMP_DATABASE_URL` is unset
  or not local (unix socket or loopback host), pytest stops before connecting.
- `RESTAMP_TEST_REQUIRE_DISPOSABLE=1` refuses `restamp_dev` (plus any names in
  `RESTAMP_TEST_BLOCKED_DBS`) and URLs without a database name.
- The session aborts unless the database is at the single Alembic head.

## Production demo override (displayed OTP without SMS)

Until an SMS provider is connected, a production deployment can return the
generated code exactly like local display mode, but only with **both**:

```env
RESTAMP_OTP_DISPLAY_MODE=1
RESTAMP_OTP_DISPLAY_IN_PRODUCTION=1
```

- `RESTAMP_OTP_DISPLAY_MODE=1` alone still refuses production startup.
- Startup logs a warning while the override is active.
- Codes stay random, single use, expiring and rate limited, and are verified
  by the normal `POST /auth/otp/verify`.
- **Anyone who can reach the API can sign in as any phone number,
  including existing accounts.** Set `RESTAMP_OTP_DISPLAY_IN_PRODUCTION=0`
  (or `RESTAMP_OTP_DISPLAY_MODE=0`) to restore the fail-closed 503.
- Tests: `pytest tests/test_otp_production_display.py`.
