# Persistence invariants

This describes the PostgreSQL foundation, Telegram inventory, product observations,
recurring price jobs and transactional recommendation notifications.

## Ownership and identity

- A consumable belongs to one explicit household. The consumable is the root of
  purchasing and stock decisions; an offer is a source attached to it.
- `offer_source.consumable_id` is unique: v1 supports at most one tracked offer
  per consumable. Store the supplied `url` and optional `variant` exactly, including
  URL query parameters; persistence does not normalize or replace them.
- UUID primary keys are assigned by Python on insert. Foreign keys prevent
  orphaned rows. Deletion does not cascade through household, consumable, source,
  purchase, or price history.

## Quantities, money, and history

- Consumable stock is a nonnegative quantity at `stock_updated_at`, expressed in
  a nonblank `canonical_unit`. `daily_consumption` is positive, and `reserve_days`
  is a nonnegative integer. Persisted quantities and money use `NUMERIC(18, 6)`;
  construct inputs as `Decimal`, never through binary floating point. Values
  beyond six fractional digits are rounded by PostgreSQL; the Telegram boundary
  rejects inputs outside the stored precision before mutation. Numeric NaN is rejected;
  the bounded precision also rejects infinity.
- A purchase has a positive quantity and a purchase timestamp. Its optional
  price is the recorded purchase total, paired with a three-letter uppercase
  currency; `source` is optional free text, independent of the tracked offer.
- A price observation is a timestamped snapshot of a source's normal one-time
  offer price. Subscription, coupon, and account-specific prices must not be
  substituted for it by extraction logic. Unknown prices can be null;
  present prices require currency. Unit price requires currency and a nonblank
  unit. Prices cannot be negative. Availability is `available`, `unavailable`,
  or `unknown`.
- Price history is append-only: a PostgreSQL trigger rejects row UPDATE and
  DELETE, including changes to the observation timestamp. Corrections are new
  observations. Administrative TRUNCATE is outside this application guarantee
  and remains available for isolated test cleanup. The source row itself does
  not yet have a URL-edit workflow; future edits must explicitly preserve the
  historical meaning of existing observations.

## Time and transactions

- All timestamp columns are PostgreSQL `TIMESTAMP WITH TIME ZONE`. The ORM
  `UTCDateTime` adapter rejects naive datetimes and normalizes aware inputs and
  outputs to UTC. Raw SQL callers must independently supply aware timestamps;
  PostgreSQL itself interprets naive literals using its session timezone.
- Every business timestamp is provided by the caller's injected clock. There
  are no model `datetime.now()` or database `now()` defaults. Stock, purchase,
  observation, receipt, creation, delivery, and scheduling times are explicit.
- `create_engine(database_url)` creates a lazy async engine. Its owner must call
  `await engine.dispose()` on shutdown. `get_session_factory(engine)` creates
  independent sessions with autoflush disabled and no implicit commit. Callers
  explicitly flush when needed and own transaction commit/rollback boundaries.
  Never share one session between concurrent tasks.

## Durable work and idempotency

- `scheduled_job.dedupe_key` is a stable unique identity for one logical job;
  `notification_outbox.dedupe_key` similarly identifies one logical notification.
  Retries reuse the row and identity. Initial price jobs dedupe by consumable;
  subsequent jobs include the anchored UTC slot. Recommendation intents use
  the consumable and transition generation.
- Job states are `pending`, `running`, `succeeded`, `failed`, and `cancelled`.
  Outbox states are `pending`, `sending`, `sent`, `failed`, and `cancelled`. Attempt counters
  cannot be negative. A running job or sending notification must have both a
  lease owner and expiry; other states must clear both. A sent notification
  must have `delivered_at`; other states must leave it null.
- Work due time, payload, retry count, leases, and last error survive process
  restarts. Due-state/time and lease-expiry indexes support the private worker.
  Price jobs and Telegram reply/recommendation outbox rows are claimed with
  SKIP LOCKED and committed before external calls. Price leases last 120 seconds;
  notification leases last 60 seconds. Completion requires the current owner
  and an unexpired lease. Database checks constrain row shape, not allowed transitions.
- `telegram_update_receipt.update_id` is a nonnegative bigint primary key. The
  inbound handler inserts its receipt in the same transaction as domain changes,
  persistent conversation state, and reply outbox rows. A household row lock
  serializes mutations; duplicate receipts return without another mutation.
- Outbox uniqueness prevents duplicate logical enqueueing; it cannot guarantee
  exactly-once delivery to Telegram across a crash after an external send.

## Schema lifecycle

Alembic revision `0001_foundation` contains explicit frozen DDL and the history trigger,
independent of future ORM changes. Application startup does not create tables. Run
`alembic upgrade head` to migrate and `alembic check` to detect model drift. A downgrade
removes the schema and its stored data and is only intended for disposable
development/test databases at this stage.

## Telegram inventory transactions

The single allowed private chat maps to a stable household UUID. A persisted
conversation holds the current onboarding step; each accepted update advances it and
queues a reply atomically. Confirmed items preserve the submitted URL and explicit
variant query value and enqueue the initial check in the same transaction.
Recommendation decisions after onboarding, purchases and corrections share the
receipt/domain transaction and household lock. No HTTP request is made to that URL by
the webhook.

Stock is an anchored quantity. Elapsed days times daily consumption reduce the estimate,
clamped at zero. Purchases add to that estimate and reset the anchor; manual corrections
set a new anchor without fabricating a purchase. Both operations are serialized by the
household lock and protected against duplicate update IDs. Different update IDs are
processed in lock order, not sorted by their numeric ID.

## Product observation transactions

A check reads the source identity and closes its session before network I/O. It extracts
against the original submitted URL, including the selected variant, then uses a short
transaction to lock and recheck source URL, variant, item and household ownership before
appending. Changed sources cannot receive stale results. Independent operator checks may
each append. Scheduled checks use job identity and lease fencing to commit at most one
result for a logical daily slot.

Observations distinguish `success` from `failed`. Out-of-stock with no price is a
successful availability observation; transport/extraction failure has only a safe error
code and unknown availability. Failures never overwrite good history. Name, variant and
bounded extraction metadata are immutable snapshots. A failed attempt with an
invalid/oversized source variant stores a null variant snapshot while preserving the
original source exactly. Old observations remain successful after migration without
UPDATE. `/stock` reads one database snapshot for latest attempt, availability and last
known price, and displays their observation times. It never fetches a merchant URL.

## Scheduled check settlement

A price job snapshots its source and closes the session before HTTP. Settlement locks
household, item and source before the job, rechecks ownership/source identity, and
verifies the current lease. A completed fetch result, recommendation state, notification
intent, job acknowledgement and next daily slot commit together. An expired/stale owner
writes none of those results. No domain lock is held during HTTP. Scheduled observation
IDs derive from the job ID; immutable history provides an additional uniqueness
boundary. External HTTP itself can repeat after a crash.

Transient attempts retain the same job and schedule a bounded retry without appending
observations or evaluating recommendations. Success or final fetch failure appends one
observation. Recovery after a fifth-attempt crash marks the job failed without inventing
a fetch observation or making a sixth HTTP call. Source changes discard the fetched
result. A terminal slot still evaluates current stock and schedules the next slot when
the item exists; a missing item ends recurrence. After downtime, one overdue check runs
and the next job uses the first future 24-hour slot from the original anchor.
Observation time is actual check time.

## Recommendation and notification transactions

Recommendation state is one row per consumable, with an incrementing generation for
state changes. Evaluation uses exact depletion before presentation rounding. BUY NOW
applies at/below reserve; BUY SOON applies within twice reserve unless eligible discount
evidence promotes it to BUY NOW; above twice reserve is OK.

Discount evidence uses the latest attempt only: it must be successful, available, fresh,
have a unit price, and match the current variant. The baseline uses at least three
earlier successful observations in the configured history window with the same variant,
currency and unit. The current observation is excluded, and a zero median disables
discount advice. Failed, stale, unavailable/unknown or incomparable evidence falls back
to stock-only advice.

A household lock serializes recommendation evaluation with stock updates. The state
change and outbox intent commit together. Initial entry into an actionable state counts
as a transition. Unchanged states and recovery to OK create no new notification. A new
transition cancels superseded pending/sending recommendation rows and clears their
leases; cancelled rows cannot be reclaimed or acknowledged by an old owner. A request
already in flight can still arrive at Telegram. Updating the rationale within the same
state can refresh a pending message without creating another notification. Outbox dedupe
prevents duplicate logical intents, not duplicate external delivery across a crash.
