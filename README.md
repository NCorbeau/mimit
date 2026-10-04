# Mimit

Mimit is a Telegram-first household replenishment assistant. Track cat food, coffee,
dishwasher tablets and other consumables, estimate when they will run out, and receive a
quiet reminder when stock or stored price history makes buying useful.

Send a product URL, then enter a name, current stock, canonical unit (for example,
“pouch”), daily consumption, and reserve days. Confirm with `yes`. `/stock [page]` shows
estimated stock, days remaining, stored prices and an explained **OK / BUY SOON / BUY
NOW** recommendation (five items per page). `/bought <item-id> 6` records a purchase;
`/setstock <item-id> 12` corrects the count. Quantities always use the item's canonical
unit; pack conversion is manual.

Onboarding saves the exact offer URL and schedules the first price check. The private
worker fetches outside the webhook and repeats checks every 24 hours. Prices are normal
one-time offer prices; subscription pricing is excluded. The supported first target is
one exact Zooplus product variant. Notifications are sent when an item enters BUY SOON
or BUY NOW; unchanged states and recovery to OK stay silent. See [Telegram
use](docs/telegram.md), [product checks](docs/product-checks.md) and [background
work](docs/background-work.md).

Railway deployment and live inventory acceptance are recorded. Recurring checks,
recommendations and structured logging are implemented in the repository; their full
production acceptance remains a separate gate. See the dated [verification
record](docs/verification.md) for evidence and its limits.

## Architecture

Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, asyncio, and httpx. One
codebase serves a public API and a private worker, with PostgreSQL holding both product
state and durable work. [Railway deployment](docs/deployment.md) uses the same
Dockerfile for both processes; no cloud resources are required for local development.

- A household owns consumables, which hold stock in a user-chosen canonical unit.
- Each consumable has one offer source in v1; its exact submitted URL and variant
  identity remain separate from the consumable.
- Price observations and purchases have their own history tables.
- PostgreSQL jobs retain recurring checks, retries and leases; recommendation
  transitions and notification intents commit together.
- Telegram update receipts prevent repeating committed inbound mutations.
- Domain timestamps are supplied explicitly through an injected clock and stored
  as timezone-aware PostgreSQL timestamps.

One private `python -m mimit.worker` process runs independent scheduling, price checking
and outbox delivery lanes. Claims commit before HTTP; lease fencing protects persisted
results. Both replies and recommendation notifications use bounded retries. See [the
invariants](docs/invariants.md), [configuration contract](docs/configuration.md), and
[Telegram setup and use](docs/telegram.md).

## Local setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker with
Compose. uv downloads Python 3.12 when needed. The committed lockfile fixes the resolved
dependency versions.

```sh
git clone https://github.com/NCorbeau/mimit.git
cd mimit
uv sync --locked
cp .env.example .env
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn mimit.api:create_app --factory --reload
```

`GET http://127.0.0.1:8000/healthz` is a process health check, not a database readiness
check. With complete Telegram configuration, `/telegram/webhook` is enabled.

PostgreSQL is bound only to `127.0.0.1:55439`. Its `mimit` username/password are local
development values only. If that port is occupied, set `MIMIT_POSTGRES_PORT` for Compose
and update `DATABASE_URL` in `.env` to match. `docker compose down` stops the database
and retains its named volume; add `--volumes` only to deliberately delete local data.

No Telegram credentials are needed for tests or the health-only application. For the
bot, follow [Telegram setup](docs/telegram.md), including the separate `uv run python -m
mimit.worker` process. `.env` is ignored by Git. Runtime configuration uses environment
variables and may load `.env` for local convenience. Production credentials belong in
deployment variables.

## Verification

```sh
make check                  # Ruff lint/format, strict mypy, fast unit tests
export TEST_DATABASE_URL='postgresql+asyncpg://mimit:mimit@localhost:55439/mimit'
make test-integration       # actual PostgreSQL; creates disposable databases
```

The integration role needs `CREATEDB`. The harness creates a uniquely named
`mimit_test_<uuid>` database, migrates it from zero, and drops only that database on
completion. It never truncates the database named in `TEST_DATABASE_URL`. Do not use a
production database account for tests. Explicit integration runs fail if
`TEST_DATABASE_URL` is missing or PostgreSQL is unavailable; they do not silently
substitute SQLite or skip verification. Fast tests need no database.

Individual commands:

```sh
uv run pytest -m 'not integration'
uv run pytest -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic check        # compare the current migrated DB with model metadata
```

CI runs the same checks against PostgreSQL 17. `make format` applies Ruff fixes and
formatting. To change the schema, generate and inspect an Alembic revision; committed
migrations must describe their own schema rather than importing the current application
metadata to create tables.

## Guarantees and current limits

Database constraints enforce the modeled quantity, relationship, identity, and state
invariants. Integration tests verify migrations from zero, round trips, constraint
failures, rollback, and concurrent uniqueness at the database boundary. The HTTP
integration tests exercise the authenticated webhook, persistent onboarding, concurrent
duplicate deliveries, stock changes, and rollback after a database failure. Worker and
outbox tests use real PostgreSQL, deterministic clocks and simulated external responses.
No automated test sends messages to a real Telegram account.

Receipt, domain mutation, conversation state, and reply intent commit together. A
household row lock serializes inventory updates. Duplicate deliveries with the same
`update_id` cannot repeat a committed mutation. Different updates are processed in
arrival/lock order; the API does not reorder them by Telegram ID. Webhook setup requests
one delivery connection to reduce out-of-order conversations.

Scheduled checks can repeat external HTTP after a crash, but only the current lease can
commit a logical job result, recommendation changes and the next daily slot. A failed
daily slot remains inspectable while future checks continue. Downtime produces one
catch-up check, then resumes the daily anchor.

Outbound delivery has bounded at-least-once attempt semantics and a visible failed
state. A crash after Telegram accepts a message can cause a duplicate notification.
Superseded queued recommendations are cancelled; cancellation cannot retract an HTTP
request already in flight. Mimit does not claim exactly-once external effects or
guaranteed eventual delivery.

The [fetcher and extractor](docs/product-fetching.md) validate DNS at the actual
connection boundary and extract exact-variant JSON-LD. The older [Zooplus
spike](docs/zooplus-spike.md) remains historical developer evidence. Discount advice
needs fresh, available unit-price evidence and at least three earlier comparable
observations; otherwise recommendations use stock alone. The stock model depends on
manual corrections and an approximate consumption rate.

There is no web frontend, queue broker, automatic purchasing, automatic pack conversion,
source-editing workflow or general store scraper framework. API readiness does not prove
worker health. Production price-history, recurrence and recommendation acceptance
remains unverified until recorded separately.
