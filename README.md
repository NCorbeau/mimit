# Mimit

Mimit is a Telegram-first household replenishment assistant. It is being built to
track everyday consumables—cat food, coffee, dishwasher tablets—and estimate when
they will run out. Price history will help it explain when buying makes sense.

The local implementation includes Telegram onboarding, stock summaries, purchases,
stock corrections, durable replies, PostgreSQL migrations, and separate fast and
real PostgreSQL tests. Price monitoring and recommendations are later milestones.
The foundation gate has passed. Railway hosts the API and interactive reply
worker; full real-household acceptance still requires exercising the deployed flow.

Send a product URL, then enter a name, current stock, canonical unit (for example,
“pouch”), daily consumption, and reserve days. Confirm with `yes`. `/stock [page]` shows
estimated stock and days remaining (five items per page); `/bought <item-id> 6` records a purchase and
`/setstock <item-id> 12` corrects the stock count. Quantities always use the item's
canonical unit, including purchases; pack conversion is manual for now.

URLs are saved without fetching in this milestone. The exact Zooplus acceptance
variant has been checked in a developer spike; production price tracking will
use its regular one-time price. Subscription pricing remains a separate offer type.

## Architecture

Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, asyncio, and httpx.
One codebase serves a public API and a private worker, with PostgreSQL holding
both product state and durable work. [Railway deployment](docs/deployment.md) uses
the same Dockerfile for both processes; no cloud resources are required for local development.

- A household owns consumables, which hold stock in a user-chosen canonical unit.
- Each consumable has one offer source in v1; its exact submitted URL and variant
  identity remain separate from the consumable.
- Price observations and purchases have their own history tables.
- Scheduled jobs, notification outbox rows, and Telegram update receipts establish
  persistence boundaries for later processing.
- Domain timestamps are supplied explicitly through an injected clock and stored
  as timezone-aware PostgreSQL timestamps.

Interactive replies use a small outbox sender with leases and bounded retries.
Scheduled price jobs and recommendation notifications are not implemented. See
[the invariants](docs/invariants.md), [configuration contract](docs/configuration.md),
and [Telegram setup and use](docs/telegram.md).

## Local setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker with
Compose. uv downloads Python 3.12 when needed. The committed lockfile fixes the
resolved dependency versions.

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

PostgreSQL is bound only to `127.0.0.1:55439`. Its `mimit` username/password are
local development values only. If that port is occupied, set `MIMIT_POSTGRES_PORT`
for Compose and update `DATABASE_URL` in `.env` to match. `docker compose down`
stops the database and retains its named volume; add `--volumes` only to deliberately
delete local data.

No Telegram credentials are needed for tests or the health-only application.
For the bot, follow [Telegram setup](docs/telegram.md), including the separate
`uv run python -m mimit.telegram.sender` reply process. `.env` is ignored by Git. Runtime configuration uses environment variables and may load `.env` for
local convenience. Production credentials belong in deployment variables.

## Verification

```sh
make check                  # Ruff lint/format, strict mypy, fast unit tests
export TEST_DATABASE_URL='postgresql+asyncpg://mimit:mimit@localhost:55439/mimit'
make test-integration       # actual PostgreSQL; creates disposable databases
```

The integration role needs `CREATEDB`. The harness creates a uniquely named
`mimit_test_<uuid>` database, migrates it from zero, and drops only that database on
completion. It never truncates the database named in `TEST_DATABASE_URL`. Do not
use a production database account for tests. Explicit integration runs fail if
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

CI runs the same checks against PostgreSQL 17. `make format` applies Ruff fixes
and formatting. To change the schema, generate and inspect an Alembic revision;
committed migrations must describe their own schema rather than importing the
current application metadata to create tables.

## Guarantees and current limits

Database constraints enforce the modeled quantity, relationship, identity, and
state invariants. Integration tests verify migrations from zero, round trips,
constraint failures, rollback, and concurrent uniqueness at the database boundary.
The HTTP integration tests exercise the authenticated webhook, persistent onboarding,
concurrent duplicate deliveries, stock changes, and rollback after a database failure.
Reply-sender tests use real PostgreSQL and simulated Telegram responses. No automated
test sends messages to a real Telegram account.

Receipt, domain mutation, conversation state, and reply intent commit together.
A household row lock serializes inventory updates. Duplicate deliveries with the
same `update_id` cannot repeat a committed mutation. Different updates are processed
in arrival/lock order; the API does not reorder them by Telegram ID. Webhook setup
requests one delivery connection to reduce out-of-order conversations.

Outbound delivery has at-least-once attempt semantics with bounded retries and a
visible failed state. A crash after a Telegram send can leave delivery ambiguous
and cause a duplicate reply. Mimit does not claim exactly-once external delivery.

The developer-only [Zooplus spike](docs/zooplus-spike.md) investigates structured
data via httpx. It is not the production URL-fetching boundary. Production SSRF
protection, recurring price jobs, and recommendations are later
milestones. The reply sender is limited to interactive Telegram messages.
There is no web frontend, queue broker, automatic purchasing, or general store
scraper framework.
