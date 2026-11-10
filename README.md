# Restamp-backend

RESTAMP Phase 4 — FastAPI + SQLAlchemy + MySQL backend.

## Local development OTP testing (restricted display mode)

Real SMS delivery is **not connected yet**: `OTPService.delivery`
(`app/services/otp.py`) ships unconnected, and with no provider configured
every `POST /auth/otp/request` fails closed with a generic 503. This is
deliberate — production phone-OTP login stays unavailable until a real SMS
provider is integrated in code.

For **local development only**, the codebase ships a restricted display
mode: when `RESTAMP_OTP_DISPLAY_MODE=1` and `RESTAMP_ENV` is a development
value (`local`/`development`/`dev`/`test`), `/auth/otp/request` returns the
backend-selected code as `debug_code` so developers can log in without SMS.
Nothing is sent and the delivery seam is bypassed while active. Safeguards:

- The flag alone is never enough: the environment is re-checked on every
  request by the OTP service and again by the auth router, and an unset or
  unknown `RESTAMP_ENV` counts as production (fail closed).
- In production the startup gate (`validate_security_config` in
  `app/config.py`) **refuses to boot** with the flag enabled. There is no
  production override.
- Display mode substitutes delivery only — every lifecycle rule is
  unchanged: per-phone (5/hr) and per-IP (30/hr) request caps, 5 wrong-code
  attempts, 300s expiry, single-use verification, 900s blocks.

### Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Local MySQL dev database (defaults to restamp_dev over
# /tmp/mysql.sock); provision it and migrate to head ONCE:
export RESTAMP_ENV=local   # required: the app has no default environment
export RESTAMP_DATABASE_URL="mysql+pymysql://USER:PASSWORD@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
alembic upgrade head
```

### Run the backend with restricted display mode

```bash
export RESTAMP_ENV=local
export RESTAMP_DATABASE_URL="mysql+pymysql://USER:PASSWORD@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
export RESTAMP_JWT_SECRET="pick-a-strong-random-secret-32-chars-min"
export RESTAMP_OTP_DISPLAY_MODE=1   # LOCAL DEVELOPMENT ONLY — refused in production
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
  refuses it, like any unset or unknown `RESTAMP_ENV`.
- Auth tests: `pytest tests/test_otp_delivery.py tests/test_otp_display_mode.py
  tests/test_otp_demo_mode.py tests/test_concurrency.py tests/test_auth_production.py
  tests/test_phase1_security.py tests/test_production_hardening.py` (requires
  the local DB at Alembic head).

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

## Production configuration

Production is decided by `RESTAMP_ENV` alone (never by the database host), so
a single-server deployment with MySQL on `localhost` is production when it
says so. Startup refuses to boot unless all of these hold:

```env
RESTAMP_ENV=production                      # required; unset/unknown values are refused
RESTAMP_DATABASE_URL=<real URL>             # the built-in placeholder is refused
RESTAMP_JWT_SECRET=<random, 32+ chars>      # weak/placeholder secrets are refused
RESTAMP_OTP_DISPLAY_MODE=0                  # "1" is refused; there is no override
RESTAMP_GOOGLE_DEV_MODE=0                   # "1" is refused
RESTAMP_CORS_ORIGINS=https://restamp.in,https://www.restamp.in,https://restamp-alpha.vercel.app,https://restamp-app.vercel.app
```

- `RESTAMP_CORS_ORIGINS` is the only source of allowed browser origins in
  production: exact `https://host[:port]` origins (lowercase), no path or
  trailing slash, no `*`, no localhost/private-LAN hosts. Listed origins get
  credentialed CORS; every other origin gets no CORS headers.
- `RESTAMP_GOOGLE_ENABLED=1` additionally requires a real
  `RESTAMP_GOOGLE_CLIENT_ID`.
- A development `RESTAMP_ENV` with a non-local database is refused (a
  deployment that forgot `RESTAMP_ENV=production`).
- With no SMS provider connected, `POST /auth/otp/request` returns 503 in
  production; OTP codes are never returned in responses.
- Tests: `pytest tests/test_production_hardening.py`.
