# One-shot product checks

After applying migrations, run one check against an existing item UUID from `/stock`.
The command uses `DATABASE_URL` from settings; Telegram credentials are not required.
Use the same application version for API, private worker and command.

```sh
uv run alembic upgrade head
uv run python -m mimit.products.check ITEM_UUID
uv run python -m mimit.products.check ITEM_UUID --household-id HOUSEHOLD_UUID
```

This operator command accesses the configured database. The optional household argument
restricts ownership; it is not a separate authentication mechanism. A successful check
appends an immutable observation and emits JSON with the observed UTC time, selected
variant, one-time price, merchant unit price and availability. Exit status is 0 on
success, 2 on a failed check or precondition, and 130 on interruption. Failures use
fixed error codes without merchant bodies, URLs, database credentials or exception
messages. The command only appends an observation: it does not retry, create scheduled jobs,
persist a recommendation transition or enqueue a recommendation. Repeat deliberately and
respect the merchant's request limits.

`/stock` shows the last known price with its own timestamp, the latest successful
availability, and any newer failed attempt, plus a recommendation computed from stored
history. A failed fetch leaves previous good prices intact. An out-of-stock observation
can have no price and still succeed. No observations means `Price: not checked yet.`
Stored prices are per merchant offer; canonical household stock units remain unchanged.
Long merchant unit labels are shortened for Telegram display while preserving the stored
value.

The supported acceptance target is the submitted Schesir variant `2333304.0`. JSON-LD
extraction must prove exact identity and unconditional one-time pricing. If a merchant
changes its structured data, the check can fail safely instead of using another variant
or subscription price. [Fetching/extraction boundaries](product-fetching.md) describe
the network and parsing limits.

## Scheduled checks

The private `python -m mimit.worker` process handles initial and daily checks. Operator
checks append independent observations; they do not acknowledge or replace a scheduled
job, and deliberately repeated commands can append more than one observation. Scheduled
retries keep their logical job identity and append only the final accepted result under
an unexpired lease. Intermediate transient attempts remain visible in job diagnostics
and operational events.

A changed source cannot receive the old fetched result. External HTTP may repeat after a
crash, while a logical scheduled result is fenced at persistence. Final settlement
includes recommendation evaluation and the next daily slot in the same transaction. See
[background work](background-work.md) for cadence, retries, price-history eligibility
and quiet notifications.
