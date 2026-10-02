# Mimit

Mimit is a Telegram-first household replenishment assistant. It is being built to
track everyday consumables—cat food, coffee, dishwasher tablets—and estimate when
they will run out. Price history will help it explain when buying makes sense.

The foundation currently provides PostgreSQL persistence, migrations, configuration,
a deterministic clock, a FastAPI application shell, and separate fast and real
PostgreSQL tests. **The Telegram bot, inventory interactions, price monitoring,
and recommendations are not implemented yet.**

The intended interaction is simple: send a product URL, enter stock and daily
usage, check `/stock`, and record purchases. The first supported product source
will be one exact Zooplus variant. Mimit will track its regular one-time price;
subscription pricing is a separate offer type.

## Architecture

Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, asyncio, and httpx.
One codebase will serve a public API and a private worker, with PostgreSQL holding
both product state and durable work. Railway deployment is planned; no cloud
resources are required for local development.

- A household owns consumables, which hold stock in a user-chosen canonical unit.
- Each consumable has one offer source in v1; its exact submitted URL and variant
  identity remain separate from the consumable.
- Price observations and purchases have their own history tables.
- Scheduled jobs, notification outbox rows, and Telegram update receipts establish
  persistence boundaries for later processing.
- Domain timestamps are supplied explicitly through an injected clock and stored
  as timezone-aware PostgreSQL timestamps.

The schema is preparation for these behaviors. It does **not** implement job
claiming, retries, inbound event processing, or outbound delivery. See
[the invariants](docs/invariants.md) and [configuration contract](docs/configuration.md).

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
check. There is no webhook endpoint yet.

PostgreSQL is bound only to `127.0.0.1:55439`. Its `mimit` username/password are
local development values only. If that port is occupied, set `MIMIT_POSTGRES_PORT`
for Compose and update `DATABASE_URL` in `.env` to match. `docker compose down`
stops the database and retains its named volume; add `--volumes` only to deliberately
delete local data.

No Telegram credentials are needed for foundation development. `.env` is ignored
by Git. Runtime configuration uses environment variables and may load `.env` for
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
These tests do not yet establish end-to-end Telegram idempotency or worker safety.

The design calls for receipt + domain mutation + outbox intent to commit together.
Future outbound delivery will have at-least-once attempt semantics: a crash after a
Telegram send can leave delivery ambiguous. Mimit will not claim exactly-once
external delivery.

The developer-only [Zooplus spike](docs/zooplus-spike.md) investigates structured
data via httpx. It is not the production URL-fetching boundary. Production SSRF
protection, durable workers, recommendations, and deployment are later milestones.
There is no web frontend, queue broker, automatic purchasing, or general store
scraper framework.
