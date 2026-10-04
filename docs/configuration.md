# Configuration

Copy `.env.example` to `.env` for local development. Process environment variables
override `.env` values. Imports do not load or validate configuration: the app factory
and worker entry points, operator command and Alembic call `get_settings()` when they
need it. Settings are cached per process; restart after configuration changes. Tests can
call `get_settings.cache_clear()`.

| Variable | Requirement | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | Required | PostgreSQL host and database URL; `postgres://` and `postgresql://` normalize to `postgresql+asyncpg://`. URL-encode credentials containing reserved characters. |
| `HOUSEHOLD_TIMEZONE` | Optional; defaults to `Europe/Warsaw` | Valid IANA timezone used for household display; daily scheduling uses elapsed UTC time. |
| `TELEGRAM_BOT_TOKEN` | Optional until Telegram is enabled | Bot credential. |
| `TELEGRAM_WEBHOOK_SECRET` | Optional until Telegram is enabled | Webhook authentication secret. |
| `PUBLIC_BASE_URL` | Optional until Telegram is enabled | Absolute HTTPS origin/base path without credentials, query, or fragment. |
| `TELEGRAM_ALLOWED_USER_ID` | Optional until Telegram is enabled | One positive Telegram user ID. |
| `TELEGRAM_ALLOWED_CHAT_ID` | Optional until Telegram is enabled | One private-chat Telegram ID (group chats are rejected by the webhook). |

When optional values are configured, they are validated immediately. Setting any
Telegram variable enables the bot and requires all five values; partial configuration
fails startup. The secret must contain 1–256 ASCII letters, digits, underscores or
hyphens. The webhook denies access unless both the user and private chat match. With all
Telegram values omitted, the app exposes the liveness and readiness endpoints. The
private worker requires complete Telegram configuration. See [Telegram
setup](telegram.md) for webhook registration and the worker.

## Worker and recommendation settings

| Variable | Default | Accepted values and meaning |
| --- | --- | --- |
| `WORKER_POLL_SECONDS` | `1` | Finite number greater than 0 and at most 60; pause between iterations in each lane. |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | Finite number greater than 0 and at most 120; drain active work before cancellation on SIGINT/SIGTERM. |
| `WORKER_PRICE_CONCURRENCY` | `2` | Integer 1–8; concurrent price lanes in one worker. |
| `RECOMMENDATION_HISTORY_DAYS` | `30` | Integer 1–365; preceding price-history window. |
| `RECOMMENDATION_DISCOUNT_FRACTION` | `0.10` | Decimal strictly between 0 and 1; current unit price must be at most `(1 − fraction) × median` for discount advice. |
| `RECOMMENDATION_MIN_PRIOR_OBSERVATIONS` | `3` | Integer 3–365; minimum earlier comparable successful observations, excluding the current observation. |
| `RECOMMENDATION_PRICE_MAX_AGE_HOURS` | `48` | Integer 1–720; maximum age of the latest successful available unit-price evidence. |

Use the same recommendation settings in API and worker so command summaries, stock
changes and scheduled checks use the same rules. Settings do not enable Telegram by
themselves. The check cadence is fixed at 24 hours; there are no per-item cadence
overrides. Job leases are 120 seconds and notification leases are 60 seconds. Both
permit at most five attempts. These bounds are code contracts, separate from the
worker's polling and shutdown settings.

## Secrets and diagnostics

Database credentials and Telegram secrets are masked in settings representations and
validation errors, including structured error inputs. `settings.database_url` explicitly
reveals the normalized URL for the database connection boundary; never log that property
or dump settings as application telemetry.

Runtime entry points emit JSON operational events with identifiers, outcomes and
durations. They omit arbitrary request/page bodies and raw exception messages.
Dependency logging redacts Telegram credential URLs, token-shaped strings, database URLs
and configured secrets. Library imports do not install logging handlers.

Run the health-only API with `uv run uvicorn mimit.api:create_app --factory`. `GET
/healthz` returns `{"status":"ok"}`; it does not test database connectivity. `GET
/readyz` checks database connectivity and reads a nonempty Alembic revision with a
five-second deadline. It returns a generic 503 when unavailable. Railway uses this
endpoint before directing traffic to a new deployment. It does not prove that the
private worker is healthy or that every schema object is correct. The factory accepts
injected settings and a clock. Domain code should accept the `Clock` protocol instead of
reading wall time directly. `SystemClock` returns UTC; `FrozenClock` rejects naive
datetimes and normalizes aware instants to UTC. Persist instants in UTC and convert them
to the household timezone at presentation boundaries.
