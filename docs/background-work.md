# Background checks and recommendations

Updated 7 October 2026. These are repository runtime contracts. The unified worker
release and first household acceptance are recorded separately in the dated
[verification record](verification.md). Work is tracked by
[MAC-32](https://linear.app/mglownia/issue/MAC-32/build-durable-background-tracking),
[MAC-33](https://linear.app/mglownia/issue/MAC-33/deliver-actionable-recommendations)
and
[MAC-61](https://linear.app/mglownia/issue/MAC-61/add-structured-operational-logging).

## Run the private worker

Apply migrations first, configure the same database and Telegram identity as the API,
then start one private process:

```sh
uv run python -m mimit.worker
```

The worker runs independent scheduling, price-checking and outbox lanes. A slow merchant
request does not block reply delivery. Price concurrency defaults to two, and the safe
fetcher also applies its per-domain limits. Each lane polls once per second by default.
See [configuration](configuration.md) for bounded tuning. The API creates no background
tasks and performs no merchant fetch during a webhook.

```sh
uv run python -m mimit.worker --once
```

`--once` performs one scheduling pass, then at most one due price job and one eligible
outbox send concurrently. It does not drain the queue. A notification created by that
price job may need a later iteration. The standalone `python -m mimit.telegram.sender
--once` remains an outbox-only operator command; run the unified worker for recurring
checks. The [one-shot product command](product-checks.md) remains available for
deliberate operator observations.

## Recurrence and recovery

Onboarding commits an immediately due initial job with the item. The scheduling lane
reconciles existing items with sources in batches of at most 100, using the same unique
initial identity so concurrent scans cannot create another initial job. Daily recurrence
uses elapsed 24-hour UTC slots from that first enqueue time; it is not a household-local
wall-clock schedule. There are no per-item overrides.

Claims use PostgreSQL `FOR UPDATE SKIP LOCKED`, commit before HTTP, and have a
120-second lease. A successful or final failed result is settled by the current owner in
one transaction with any recommendation change, notification intent, job acknowledgement
and next daily slot. Source identity and ownership are rechecked before appending. Only
one persisted result is accepted per logical job; a crashed fetch can be repeated
externally after lease recovery.

Timeouts, transport/rate-limit failures and HTTP 408/429/5xx retry with exponential
delay and jitter, starting at five seconds and capped at 300 seconds. Other fetch or
extraction failures are terminal for that slot. Each slot permits at most five fetch
attempts; intermediate retries do not append observations. Final success or fetch
failure appends one immutable observation with the actual check time. A fifth-attempt
crash settles as `attempts_exhausted` after recovery without an invented observation or
a sixth fetch.

Terminal slots remain visible while the next daily check continues. A changed source
discards the fetched result and schedules a later check against current state; there is
no source-editing workflow. A missing source fails honestly, while a missing consumable
ends recurrence. Downtime results in one overdue catch-up check, then the first future
anchored slot; the worker does not replay every missed day or fabricate historical
observations.

SIGINT/SIGTERM stops new claims. The default shutdown grace is 30 seconds, after which
remaining work is cancelled. Interrupted leases remain recoverable; they are not marked
successful. A process crash or forced shutdown has the same external ambiguity.
Notification delivery has a separate 60-second lease and bounded retry policy described
in [Telegram delivery](telegram.md).

## Explainable purchase advice

The rules compare unrounded days remaining and Decimal unit prices:

| State | Rule |
| --- | --- |
| BUY NOW | Days remaining are at or below reserve, or within twice reserve and the eligible current unit price is at most 90% of its recent median. |
| BUY SOON | Days remaining are above reserve and at or below twice reserve, without qualifying discount advice. |
| OK | Days remaining are above twice reserve. |

The discount default is 10%, with a 30-day baseline. The latest attempt must be
successful, for the current variant, available, at most 48 hours old, and contain unit
price, currency and unit. Its baseline requires at least three earlier successful
observations in the history window with exactly the same variant, currency and merchant
unit. The current observation and same-timestamp rows are excluded. A zero median cannot
establish a discount. No currency or unit conversion is inferred; merchant unit prices
do not change canonical household stock units.

Failed, stale, unavailable/unknown or insufficient/comparability-mismatched price
evidence falls back to stock-only advice with an explanation. Low-stock reminders
therefore continue even if a merchant cannot be checked or its offer is unavailable.
Recommendations do not promise that buying is possible or make purchases.

Scheduled settlement evaluates the item after final success/failure, while onboarding,
purchases and corrections evaluate within their existing household transaction. `/stock`
computes current advice from stored history without fetching or queuing an alert.
Time-only notification transitions are found on daily checks, not at an exact
reserve-crossing instant; delayed work can delay reminders.

Only entry into BUY SOON or BUY NOW creates a notification, including an initial
actionable state. Unchanged states and recovery to OK stay quiet. State changes and
their generation-based outbox identity commit together. A new state cancels superseded
pending/sending advice and clears its lease; an old sender cannot acknowledge that
cancelled row. A same-state rationale can refresh a pending message without creating
another alert. Before HTTP, delivery reevaluates current stock and price freshness under
the household lock and a live lease. A changed state supersedes the claimed advice and
applies the same transition rules; an unchanged state refreshes the sending payload.
The preflight transaction closes before HTTP. Cancellation cannot retract a Telegram
request already in flight, and crash ambiguity can still cause duplicate delivery.

## Operator diagnostics

Runtime JSON events contain UTC timestamps, event names, identifiers, outcomes, error
codes and operation durations where available. They cover webhook receipt,
fetch/extraction, job claim/settlement, recommendation transitions and notification
delivery. They omit message/page bodies, arbitrary URLs and raw exception details.
Imports do not configure logging. Credentials belong in ignored environment files or
deployment variables, never command arguments, commits or pasted logs.

Inspect durable state without dumping payloads or credentials:

```sql
SELECT id, state, attempts, run_at, last_error
FROM scheduled_job
WHERE job_type = 'price_check' AND state = 'failed'
ORDER BY run_at DESC;

SELECT id, state, attempts, run_at, last_error
FROM notification_outbox
WHERE state IN ('failed', 'cancelled')
ORDER BY run_at DESC;
```

Failed notifications require operator attention; there is no automatic replay command or
guarantee of eventual delivery. Failed daily jobs normally leave a future slot
scheduled. Do not treat an API `/readyz` success as worker health: check worker runtime
events and durable due/failed work separately. Test against disposable PostgreSQL
databases, never the production account. See [verification](verification.md) for
recorded checks and [deployment](deployment.md) for release sequencing and remaining
production acceptance.
