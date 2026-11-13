# Restamp-backend

RESTAMP Phase 4 — FastAPI + SQLAlchemy + MySQL backend.

## OTP login (simplified testing OTP)

Phone OTP login uses **one fixed testing code**: `349086`
(`TEST_OTP` in `app/services/otp.py`). The whole random-code pipeline
(generation with `secrets`, SHA-256 storage, expiry, single use, attempt
counters, per-phone/per-IP rate limits and the SMS delivery seam) was
removed — none of it protects a code that is published by design.

Where the fixed code works:

- `local`/`development`/`dev`/`test` (and `staging`, see below):
  `POST /auth/otp/request` returns `{"message": ..., "debug_code": "349086"}`
  and `POST /auth/otp/verify` accepts `349086` for any valid Indian mobile,
  issuing the normal JWT/session response (first verification still
  registers the phone as a new BUYER; roles, account status and account
  isolation are unchanged).
- `production`/`prod` (and any unset or unknown `RESTAMP_ENV`, fail closed):
  the request endpoint returns a generic **503** (no SMS provider exists, so
  nothing can be sent) and **every** code — including `349086` — is rejected
  with 401. No OTP is ever returned in a production response or log.
- `production`/`prod` with the explicit opt-in
  `RESTAMP_PRODUCTION_FIXED_OTP=1` (exact value `"1"`; `true`/`yes` do not
  count): the request endpoint returns the generic **200** message with **no**
  `debug_code`, and `349086` is accepted. **Anyone can then sign in as any
  ordinary (non-admin) phone account.** Restrictions: an existing admin
  account is refused with the same 401 as a wrong code; the token carries
  `amr: "fixed_otp"`, and such a session gets 403 on every admin endpoint
  (even if the account is made admin later), on reading another user's
  record, and on `POST /auth/google/link`. Startup logs a warning. JWT,
  CORS, Google and every other production check are unchanged.

Safeguards:

- The gate is the environment alone (`otp_display_permitted()` in
  `app/config.py`), re-checked on every request by the OTP service and again
  by the auth router. No environment variable can widen display. Acceptance
  (never display) can additionally be enabled in production only through
  `RESTAMP_PRODUCTION_FIXED_OTP=1` (`fixed_otp_permitted()`).
- Production startup (`validate_security_config`) refuses to boot while the
  obsolete `RESTAMP_OTP_DISPLAY_MODE=1` is still set (a leftover testing
  configuration must be cleaned up, not silently ignored).
- Staging keeps every production startup check and additionally refuses
  `RESTAMP_CORS_ORIGINS` listing `https://restamp.in` /
  `https://www.restamp.in` (the testing OTP lets anyone sign in as any phone
  number, so a staging backend must never serve the public site) and logs a
  startup warning that the testing OTP is active.

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

### Run the backend locally

```bash
export RESTAMP_ENV=local
export RESTAMP_DATABASE_URL="mysql+pymysql://USER:PASSWORD@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
export RESTAMP_JWT_SECRET="pick-a-strong-random-secret-32-chars-min"
uvicorn app.main:app --reload --port 8000
```

### Test OTP login locally (curl)

```bash
# 1. Request the OTP (Indian mobiles only: +91 + 10 digits starting 6-9).
curl -s -X POST http://localhost:8000/auth/otp/request \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919876543210"}'
# => {"message":"If the number is valid, an OTP was sent.","debug_code":"349086"}

# 2. Verify with the fixed code (first verification registers the phone
#    as a new BUYER user).
curl -s -X POST http://localhost:8000/auth/otp/verify \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919876543210","code":"349086"}'
# => {"access_token":"...","token_type":"bearer","expires_in":1800,
#     "user_id":1,"role":"BUYER"}

# 3. A wrong code is rejected (and the right one keeps working — no counters):
# => 401 {"detail":"Invalid code"}
```

Notes:

- The only OTP environment variable is the production opt-in
  `RESTAMP_PRODUCTION_FIXED_OTP` (exact `"1"`; see above). The removed
  `RESTAMP_OTP_TTL_SECONDS`, `RESTAMP_OTP_MAX_*`, `RESTAMP_OTP_BLOCK_SECONDS`
  and `RESTAMP_OTP_DISPLAY_MODE` are inert; production refuses the last one.
- `RESTAMP_ENV=demo` is not a supported environment anywhere; startup
  refuses it, like any unset or unknown `RESTAMP_ENV`.
- Auth tests: `pytest tests/test_otp_core_contract.py tests/test_otp_demo_mode.py
  tests/test_auth_production.py tests/test_phase1_security.py
  tests/test_production_hardening.py` (requires the local DB at Alembic head).

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
- No SMS provider is integrated and the fixed testing OTP is refused, so
  `POST /auth/otp/request` returns 503 in production and startup logs a
  warning. OTP codes are never returned in production responses. Phone OTP
  login in production needs a real SMS provider implemented in code first.

### Staging (fixed testing OTP for testers)

A test deployment such as Render behind a Vercel test site uses
`RESTAMP_ENV=staging`. Staging gets every production startup check above,
plus the fixed testing OTP: `/auth/otp/request` returns `349086` as
`debug_code` and the website and app show it, and verification accepts it
for any Indian mobile.

```env
RESTAMP_ENV=staging
RESTAMP_CORS_ORIGINS=https://restamp-alpha.vercel.app
```

- Staging refuses `RESTAMP_CORS_ORIGINS` listing `https://restamp.in` or
  `https://www.restamp.in` at startup, and logs a warning that the testing
  OTP is active. Never point real users at a staging backend: the testing
  OTP lets anyone sign in as any phone number there.
- `RESTAMP_ENV=production`/`prod` reject the fixed code on every request;
  the OTP service and auth router re-check the environment on every request.
- Tests: `pytest tests/test_production_hardening.py
  tests/test_otp_core_contract.py`.
