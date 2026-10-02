# Verification record

## Foundation — 2 October 2026

Verified locally with Python 3.12.15 and PostgreSQL 17 in Docker:

- `uv sync --locked` in a fresh temporary directory: passed.
- `make check`: Ruff lint/format, strict mypy, 50 fast tests passed.
- `TEST_DATABASE_URL=… make test-integration`: 10 real-PostgreSQL tests passed.
- `alembic upgrade head` against an empty DB: passed.
- Upgrade → metadata drift check → downgrade to base → upgrade → drift check:
  passed in a disposable database.
- Exact Schesir variant `2333304.0` fetched through httpx; see
  [the timestamped spike evidence](zooplus-spike.md).
- Independent review covered architecture, persistence, transactions, Python,
  security, test credibility, and README accuracy; no material findings remained.
- Project files reviewed for credentials; only documented local/test values present.

The concurrency test observes a PostgreSQL lock wait before releasing the winning
transaction. The harness is isolated by random database names. These checks do not
claim production Telegram or worker acceptance.

## Telegram inventory implementation — 2 October 2026

- `make check`: Ruff lint/format, strict mypy (29 source files), **134 fast tests passed**.
- `TEST_DATABASE_URL=… make test-integration`: **27 PostgreSQL tests passed**.
- `alembic upgrade head` and `alembic check`: passed through
  `0002_telegram_conversation`, no metadata drift.
- HTTP/PostgreSQL scenarios include onboarding across API restarts, concurrent
  duplicate confirmation and purchase deliveries, distinct concurrent purchases,
  stock depletion/corrections, authorization failures, transaction rollback and
  retry, and bounded standalone stock pages.
- Reply delivery scenarios use real PostgreSQL and an HTTP mock: SKIP LOCKED
  contention, lease recovery, stale acknowledgement fencing, retry/backoff/jitter,
  cancellation, terminal attempts, and commit-before-HTTP behavior.
- Independent review found and verified fixes for stock-page delivery ordering and
  setup-script HTTPX token logging. No material findings remained.
- No real bot token, allowed IDs, or HTTPS preview configuration was provided;
  **real Telegram acceptance has not run**. MAC-39 and the MAC-30 parent remain open.
  No Railway deployment or later milestone work was attempted.

### Tracker mapping

- Foundation: [MAC-29](https://linear.app/mglownia/issue/MAC-29/build-project-foundation),
  with MAC-35, MAC-36, MAC-37, MAC-38, and MAC-44 complete.
- Inventory: [MAC-30](https://linear.app/mglownia/issue/MAC-30/ship-telegram-inventory-loop),
  with verified local behavior for MAC-40 through MAC-43; real-bot acceptance is
  still required for MAC-39 and the milestone gate.
- Review branches: `dev/mac-29-foundation` (base `main`) and
  `dev/mac-30-telegram-inventory` (base `dev/mac-29-foundation`).
  Review and merge the foundation PR first, then retarget the inventory PR to `main`.
