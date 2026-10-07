# Mimit

Keep everyday supplies topped up, with quiet reminders in Telegram.

Mimit tracks things like cat food, coffee and dishwasher tablets. Tell it how much
you have and how quickly you use it; it estimates when you'll run low and helps
you decide when to buy more.

## What it does

- **Tracks your stock.** Estimates quantities and days remaining from your daily usage.
- **Checks prices daily.** Stores price and availability history for the exact offer you add.
- **Explains its advice.** Shows **OK**, **BUY SOON** or **BUY NOW**, with a reason.
- **Keeps reminders quiet.** Alerts when an item enters BUY SOON or BUY NOW; unchanged
  states and recovery to OK stay silent.

Price-based advice needs enough recent, comparable history. Until then, Mimit uses
stock alone. It tracks normal one-time prices, excluding subscription pricing.

The current version is for one allowed user in one private Telegram chat, with one
offer per item. Initial merchant support focuses on one exact Zooplus product variant.
Stock estimates depend on your usage rate and manual corrections. Pack conversion is
manual; Mimit does not place orders or provide a web frontend.

## Using the bot

1. Send a product URL.
2. Enter its name, current stock, unit (such as `pouch`), daily usage and reserve days.
3. Confirm with `yes`. Mimit saves the item and schedules its first price check.

| Command | What it does |
| --- | --- |
| `/stock [page]` | Show stock, days remaining, stored prices and buying advice. |
| `/bought <item-id> 6` | Record a purchase of six units. |
| `/setstock <item-id> 12` | Correct the current stock to twelve units. |
| `/cancel` | Cancel unfinished setup for an item. |
| `/help` | Show the available commands. |

Copy the item ID from `/stock`. All quantities use the unit you chose for that item.
See [Telegram setup and use](docs/telegram.md) to connect your own bot.

## Run locally

You'll need [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker
with Compose. uv downloads Python 3.12 when needed.

```sh
git clone https://github.com/NCorbeau/mimit.git
cd mimit
uv sync --locked
cp .env.example .env
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn mimit.api:create_app --factory --reload
```

Open [the health endpoint](http://127.0.0.1:8000/healthz) to check that the API is
running. `/readyz` checks database connectivity and the presence of a migration
revision; neither endpoint checks worker health.

No Telegram credentials are needed to run the API locally or run tests. To use the
bot, complete [Telegram setup](docs/telegram.md) and start the worker in another terminal:

```sh
uv run python -m mimit.worker
```

The worker checks prices and sends replies and reminders. Keep credentials in the
ignored `.env` file locally and in deployment variables in production.

Local PostgreSQL uses port `55439`. If it's occupied, change `MIMIT_POSTGRES_PORT`
for Compose and update `DATABASE_URL` in `.env`. `docker compose down` stops the
database and keeps your data; adding `--volumes` deletes it.

## Development

```sh
make check                  # Lint, formatting, strict type checks and fast tests
export TEST_DATABASE_URL='postgresql+asyncpg://mimit:mimit@localhost:55439/mimit'
make test-integration       # Real PostgreSQL tests in disposable databases
```

Fast tests need no database. Integration tests require a local PostgreSQL role with
`CREATEDB`; they create and remove a unique test database without truncating the database
in `TEST_DATABASE_URL`. Use a local test account. CI runs both suites against PostgreSQL 17.

## How it's built

Python 3.12+, FastAPI, SQLAlchemy, Alembic and PostgreSQL. One codebase runs a public
API and a private worker, with PostgreSQL storing inventory, history and durable work.
Both processes use the same Dockerfile for [Railway deployment](docs/deployment.md).

Repeated Telegram updates cannot repeat a committed stock change. Work and replies
survive restarts, with bounded retries. A crash after Telegram accepts a message can
still cause duplicate delivery; failed messages may need operator attention.

## Further reading

- [Telegram setup and use](docs/telegram.md) — connect a bot and manage your supplies.
- [Configuration](docs/configuration.md) — environment variables and worker settings.
- [Deployment](docs/deployment.md) — run the API, worker and PostgreSQL on Railway.
- [Background work](docs/background-work.md) — daily checks, buying advice and delivery rules.
- [Product checks](docs/product-checks.md) and [fetching](docs/product-fetching.md) — price
  observations, operator tools and safe fetching.
- [Persistence invariants](docs/invariants.md) — transaction and data guarantees.
- [Verification record](docs/verification.md) — dated test results and production evidence.
