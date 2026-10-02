# Persistence invariants

This is the M0 PostgreSQL foundation. It stores the inputs for later domain logic;
it does not execute jobs, send Telegram messages, scrape offers, or calculate stock.

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
  beyond six fractional digits are rounded by PostgreSQL, so future input
  validation must enforce the intended precision. Numeric NaN is rejected;
  the bounded precision also rejects infinity.
- A purchase has a positive quantity and a purchase timestamp. Its optional
  price is the recorded purchase total, paired with a three-letter uppercase
  currency; `source` is optional free text, independent of the tracked offer.
- A price observation is a timestamped snapshot of a source's normal one-time
  offer price. Subscription, coupon, and account-specific prices must not be
  substituted for it by future extraction logic. Unknown prices can be null;
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
  Retries reuse the row and identity. Future producers must derive keys from
  stable domain events, rather than new random IDs on every attempt.
- Job states are `pending`, `running`, `succeeded`, `failed`, and `cancelled`.
  Outbox states are `pending`, `sending`, `sent`, and `failed`. Attempt counters
  cannot be negative. A running job or sending notification must have both a
  lease owner and expiry; other states must clear both. A sent notification
  must have `delivered_at`; other states must leave it null.
- Work due time, payload, retry count, leases, and last error survive process
  restarts. Due-state/time and lease-expiry indexes support future workers.
  There is no claim algorithm, worker, retry policy, or delivery implementation
  in M0. Database checks constrain row shape, not allowed state transitions.
- `telegram_update_receipt.update_id` is a nonnegative bigint primary key. A
  future inbound handler must insert its receipt in the same transaction as
  resulting domain changes and outbox rows. Uniqueness alone does not make a
  handler idempotent if its side effects are committed separately.
- Outbox uniqueness prevents duplicate logical enqueueing; it cannot guarantee
  exactly-once delivery to Telegram across a crash after an external send.

## Schema lifecycle

Alembic revision `0001_foundation` contains explicit frozen DDL and the history
trigger, independent of future ORM changes. Application startup does not create
tables. Run `alembic upgrade head` to migrate and `alembic check` to detect model
drift. A downgrade removes the schema and its stored data and is only intended
for disposable development/test databases at this stage.
