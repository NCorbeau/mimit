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

## Safe one-shot product tracking — 2 October 2026

MAC-31 implementation was reviewed in an isolated worktree and split into two
stacked changes: fetching/extraction, then observation storage/command/stock display.

- `make check`: Ruff lint/format, strict mypy (42 source files), **297 fast tests passed**.
- `TEST_DATABASE_URL=… make test-integration`: **64 PostgreSQL 17 tests passed**.
- Migration `0003_product_observations`: zero-to-head, metadata drift, downgrade
  and re-upgrade passed; existing immutable rows migrate without UPDATE.
- Fetcher tests check the actual numeric socket boundary, mixed DNS answers,
  rebinding, unsafe redirects, verified hostname/SNI, valid environment CA and
  key-log isolation, deadlines, decompression limits, cancellation and concurrency.
- Parser fixtures cover generic graphs, local references, compatible repeated IDs,
  selected variants, canonical one-time pricing, excluded member/subscription
  prices, aggregate offers, malformed numeric literals and bounded parsing.
- PostgreSQL scenarios cover independent concurrent observations, source/ownership
  changes during HTTP, no open transaction or inventory locks during HTTP,
  rollback, cancellation, last-good history, and append-only snapshots.
- Stock display reads bounded history in one SQL snapshot. A concurrent append
  cannot mix old failure with new successful price. Five maximum-size Unicode
  priced items plus failed attempts fit Telegram's UTF-16 message limit.
- Independent reviews found and verified fixes for TLS environment trust/key
  logging, numeric/aggregate parser cases, compatible repeated JSON-LD definitions,
  history snapshot consistency, Telegram message size and bounded failed-variant snapshots. No material findings
  remained after re-review.

At **2026-10-02 16:22:47 UTC**, the production fetcher and generic extractor checked
the original Schesir URL for variant **2333304.0**, appended exactly one successful
observation to a disposable migrated local PostgreSQL database, and read its stock
summary: **42.96 PLN per offer**, **84.24 PLN/kg**, **in stock**, displayed at
18:22 CEST. Canonical stock remained pouches. The disposable database was dropped.
These are timestamped observed prices, not test constants for future live checks.
Generic structured data was sufficient, so MAC-47 requires no fallback adapter.

This proves the local production-code path against the live merchant, not a
Railway deployment or live Telegram price reply. MAC-31 remains open for review
and integration. Recurring execution (MAC-32), recommendations (MAC-33) and full
production acceptance (MAC-34/MAC-62) remain separate gates.

Earlier pending live-inventory statements in this record are historical: user
Telegram screenshots on 2 October verified onboarding, stock, purchase and
correction, completing MAC-39/MAC-30. Deployment setup is recorded in merged
PRs #3/#4; no deployment is performed by this product-tracking change.
