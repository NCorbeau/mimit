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

## Daily worker, explained advice and release review — 4 October 2026

Verified locally at 21:52 UTC with Python 3.12.15 and PostgreSQL 17:

- `make check`: Ruff lint/format, strict mypy (54 source files), **375 fast tests passed**.
- `TEST_DATABASE_URL=… make test-integration`: **107 real-PostgreSQL tests passed**.
  The harness creates and drops a randomly named local database; it never runs
  concurrency or destructive schema tests against production.
- Migration `0004_recommendations`: zero-to-head, metadata drift, downgrade and
  re-upgrade checks passed as part of the PostgreSQL suite.
- Persistence checks cover concurrent claims, lease expiry/reclaim, bounded
  retries and final-attempt crashes, restart recovery, stable initial/daily job
  identities, missed-slot coalescing, and atomic observation/recommendation/
  notification/job settlement. Lease expiry and evaluation failure roll back
  settlement writes. Merchant HTTP runs outside database transactions.
- Recommendation checks cover exact depletion boundaries, the 30-day median,
  at least three earlier comparable observations, 48-hour freshness, stock-only
  fallback, actionable transitions, silent unchanged/recovery states, and
  cancellation of superseded advice. Telegram onboarding commits its initial
  job and recommendation with the receipt; purchases/corrections reevaluate
  atomically. Duplicate update IDs preserve the existing idempotency guarantee.
- Delivery checks cover current-time stock/price reevaluation before HTTP,
  refreshed explanations, cancellation after claim, stale-lease fencing after
  a real PostgreSQL lock wait, and rollback of all preflight writes when the
  lease expires during reevaluation. Tests use HTTP mocks and do not send
  real Telegram messages. Five maximum-size Unicode priced items still fit
  Telegram's UTF-16 message limit.
- Structured-log tests cover receipt, fetch/extraction, job, recommendation and
  notification events, with strict scalar fields and suppression of bodies,
  arbitrary URLs, exception details, bot tokens and database credentials.
- Independent reviews covered the worker, recommendations, delivery lock order,
  leases, transaction boundaries and published guarantees. Review fixes include
  delivery-time price freshness, post-lock acknowledgement time, malformed
  outbox payloads and bounded discount precision. No material findings remained
  after independent re-review; rollback regressions verify the preflight fix.
- Gitleaks 8.30.1 was downloaded from its official release and checksum-verified.
  A synthetic credential sentinel verified detection. Redacted scans of all
  local Git refs and an export of 89 non-ignored repository files reported no
  leaks. Ignored credential files and caches were excluded from the export.
  This records the scan's scope and result, not a guarantee of exhaustive detection.
- A read-only Railway preview proposes eleven recommendation/worker variables
  and the unified worker start command: zero resources added or destroyed.
  The existing database volume, region and private/public service boundaries
  are preserved. Production health/readiness and the current Telegram webhook
  were checked; these checks precede deployment of this change.

The daily cadence, three-prior-observation minimum, 48-hour freshness, quiet
actionable notifications, queued-advice cancellation and stock-only unavailable
offer behavior were explicitly confirmed by the user. Existing locked product
and architecture choices are preserved.

This completes repository implementation and review for
[MAC-32](https://linear.app/mglownia/issue/MAC-32/build-durable-background-tracking),
[MAC-33](https://linear.app/mglownia/issue/MAC-33/deliver-actionable-recommendations),
[MAC-61](https://linear.app/mglownia/issue/MAC-61/add-structured-operational-logging)
and [MAC-64](https://linear.app/mglownia/issue/MAC-64/perform-final-secret-and-reliability-review).
At this repository-review checkpoint, the production release, a later observation
from the next genuine daily run, the deployed explained stock reply, and a real
purchase update remained required
for [MAC-62](https://linear.app/mglownia/issue/MAC-62/run-real-zooplus-production-acceptance-flow).
[MAC-34](https://linear.app/mglownia/issue/MAC-34/deploy-and-prove-production-acceptance)
and its production milestone remain open until every required sub-issue and that
household exit gate pass. No accelerated checks or fabricated purchases are
counted as that evidence.

## First production worker run — 5 October 2026 (Warsaw)

[PR #8](https://github.com/NCorbeau/mimit/pull/8) passed hosted CI and merged as
`a53f5beea31c14efd0b170b107b31d984e85634a`. The pinned Railway configuration
plan changed eleven variables and the private worker start command, with zero
resources added or destroyed; it was applied. Railway reported SUCCESS for API
deployment `7430c937-8617-4e15-8796-4c4e491a0ffc` and unified worker deployment
`49abdbf4-73b1-445e-a922-d77ca71c23d1`, both on the merged commit. A subsequent
configuration preview reported no pending changes. The API `/healthz` and `/readyz`
returned HTTP 200. The Telegram setup check found the expected webhook, zero pending
updates and no delivery error.

At **2026-10-04 22:06:29–22:06:30 UTC** (00:06 on 5 October in Warsaw), safe
worker events for the already tracked Schesir item recorded a job claim, successful
fetch and extraction, an **OK** recommendation transition, and acknowledged
successful job settlement with an observation ID. This is evidence of the first
scheduled production check. The logs do not expose the extracted price or prove
what `/stock` displays. Direct production-database inspection was not performed;
the required temporary Railway SSH-key registration was rejected by automatic
approval review as an account-access change.

[MAC-62](https://linear.app/mglownia/issue/MAC-62/run-real-zooplus-production-acceptance-flow)
at that checkpoint still needed the deployed stock/price explanation, an observation
from the **next genuine daily run**, and a real purchase with its stock update. The 2 October test
purchase does not satisfy that gate. Keep
[MAC-34](https://linear.app/mglownia/issue/MAC-34/deploy-and-prove-production-acceptance)
and its production milestone open until those facts are verified.

## Household production acceptance — 7 October 2026 (Warsaw)

The user confirmed that the deployed `/stock` flow works correctly and supplied two
Telegram screenshots for the existing Schesir Complete Prawn item. The earlier
onboarding record identifies the exact Zooplus variant as `2333304.0`.

- Before the purchase, `/stock` displayed **14.844137 pouches**, estimated **14.84
  days remaining**, consumption **1 pouch/day** and reserve **3 days**.
- The stored offer was **42.96 PLN**, **84.24 PLN/kg**, **in stock**, with price and
  availability observed **2026-10-07 00:06 CEST** (6 October 22:06 UTC). These are
  timestamped observed values, not fixed expectations for future checks.
- The explained recommendation was **OK: Stock is above twice your reserve. Stock
  only: too little comparable price history.** This is consistent with the configured
  reserve and the three-earlier-observation minimum for discount evidence.
- The displayed observation is later than the first recorded production check at
  **5 October 00:06 CEST** and matches the unchanged 24-hour anchor. Together with
  the recorded deployed worker, it satisfies the later-daily-observation gate through
  the live Telegram display. No accelerated or operator check was run in this session.
- In response to the real-purchase question, the user confirmed it works and supplied
  `/bought <existing-item-id> 5` evidence at **21:26**. The bot acknowledged
  **19.842744 pouches**; the subsequent `/stock` showed **19.842677 pouches** and
  **19.84 days remaining**, with consumption and reserve unchanged. The small
  differences from the earlier quantity plus five are consistent with continuous
  depletion. Price, availability, observation time and the explained OK state persisted.

This completes the first household acceptance for
[MAC-62](https://linear.app/mglownia/issue/MAC-62/run-real-zooplus-production-acceptance-flow)
and the production exit gate for
[MAC-34](https://linear.app/mglownia/issue/MAC-34/deploy-and-prove-production-acceptance),
whose other required sub-issues were already Done. The screenshots and user
confirmation are acceptance evidence; no purchase was fabricated or sent by an agent.
They do not establish every intervening daily run, direct database inspection or
delivery of an actionable BUY SOON / BUY NOW alert. Automated contention, restart,
retry and idempotency evidence remains the previously dated real-PostgreSQL record.
This documentation update does not deploy code or rerun those production checks.

For this documentation change, `make check` passed Ruff lint/format, strict mypy
(54 source files) and **375 fast tests**; `git diff --check` passed. No persistence
code changed, so the 107 real-PostgreSQL tests remain the earlier dated evidence.
