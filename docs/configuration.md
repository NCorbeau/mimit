# Configuration

Copy `.env.example` to `.env` for local development. Process environment variables
override `.env` values. Imports do not load or validate configuration: the app factory
and Alembic call `get_settings()` when they need it. Settings are cached per process;
restart after configuration changes. Tests can call `get_settings.cache_clear()`.

| Variable | Foundation requirement | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | Required | PostgreSQL host and database URL; `postgres://` and `postgresql://` normalize to `postgresql+asyncpg://`. URL-encode credentials containing reserved characters. |
| `HOUSEHOLD_TIMEZONE` | Optional; defaults to `Europe/Warsaw` | Valid IANA timezone used for household display and future reminders. |
| `TELEGRAM_BOT_TOKEN` | Optional until Telegram is enabled | Bot credential. |
| `TELEGRAM_WEBHOOK_SECRET` | Optional until Telegram is enabled | Webhook authentication secret. |
| `PUBLIC_BASE_URL` | Optional until Telegram is enabled | Absolute HTTPS origin/base path without credentials, query, or fragment. |
| `TELEGRAM_ALLOWED_USER_ID` | Optional until Telegram is enabled | One positive Telegram user ID. |
| `TELEGRAM_ALLOWED_CHAT_ID` | Optional until Telegram is enabled | One nonzero Telegram chat ID; negative group IDs are allowed. |

When optional values are configured, they are validated immediately. Future Telegram
startup must call `settings.require_telegram_configuration()`, which requires all five
Telegram values. Identity checks deny access unless both the user and chat match.
No Telegram polling, webhook registration, webhook route, or product feature is part
of this foundation. The M0 product gate must pass before implementing those features.

Database credentials and Telegram secrets are masked in settings representations
and validation errors, including structured error inputs. `settings.database_url`
explicitly reveals the normalized URL for the database connection boundary; never
log that property or dump settings as application telemetry.

Run the liveness-only API with `uv run uvicorn mimit.api:create_app --factory`.
`GET /healthz` returns `{"status":"ok"}`; it does not test database connectivity.
The factory accepts injected settings and a clock. Domain code should accept the
`Clock` protocol instead of reading wall time directly. `SystemClock` returns UTC;
`FrozenClock` rejects naive datetimes and normalizes aware instants to UTC. Persist
instants in UTC and convert them to the household timezone at presentation boundaries.
