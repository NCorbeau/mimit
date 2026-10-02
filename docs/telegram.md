# Telegram M1 setup and use

M1 supports one allowed user in one private chat, product onboarding, stock listing,
purchase recording, and stock corrections. It saves product URLs without fetching
prices; price tracking and scheduled replenishment recommendations are pending.

## Operator setup

1. Create a bot with [BotFather](https://t.me/BotFather) and store its token in
   `TELEGRAM_BOT_TOKEN` in the local `.env` or deployment secret store.
2. Set `TELEGRAM_WEBHOOK_SECRET` to a random secret containing 1–256 letters,
   digits, underscores, or hyphens. For example, generate one locally with
   `uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
3. Set `TELEGRAM_ALLOWED_USER_ID` and `TELEGRAM_ALLOWED_CHAT_ID` to the intended
   numeric IDs. This implementation accepts private chats only. Verify the IDs
   from a trusted Telegram update before registering the webhook; usernames are
   not identifiers. An unconfigured or mismatched user/chat cannot mutate stock.
4. Set `PUBLIC_BASE_URL` to your externally reachable HTTPS base URL. The webhook
   path is `/telegram/webhook`; TLS termination may forward to the local API.
   Configure `DATABASE_URL`, run `uv run alembic upgrade head`, then start both
   processes with the same settings:

   ```sh
   uv run uvicorn mimit.api:create_app --factory --host 0.0.0.0 --port 8000
   uv run python -m mimit.telegram.sender
   ```

5. Register and inspect the webhook using the local configuration script:

   ```sh
   uv run python scripts/configure_telegram.py
   uv run python scripts/configure_telegram.py --check
   ```

   Registration requests only new message updates and one simultaneous delivery
   connection. The script reads secrets from settings; do not put tokens in
   command arguments or paste them into request URLs in logs.

Telegram sends the configured secret in `X-Telegram-Bot-Api-Secret-Token` and
retries unsuccessful webhook deliveries. Its hosted webhook API requires an
HTTPS URL and supports ports 443, 80, 88, and 8443. The secret character/length
constraints above come from the [official setWebhook contract](https://core.telegram.org/bots/api#setwebhook).

When all five Telegram variables are absent, the app exposes `/healthz` and `/readyz`.
Supplying any one attempts to enable Telegram and requires the complete set.
`/healthz` reports process liveness, not database or sender readiness. The API
creates a lazy database pool and disposes it during lifespan shutdown; injected
session factories remain owned by the caller. Shutdown the API and sender
gracefully so their pools are disposed.

## Household interaction

Send `/start` or `/help` to see the commands. Send a product URL to begin adding a
consumable. The bot asks for:

1. A name, such as `Cat food`.
2. Current stock, such as `12`.
3. A unit used consistently, such as `cans`, `g`, or `kg`.
4. Daily consumption greater than zero, such as `0.5`.
5. Reserve days as a whole number, such as `3`.
6. Final confirmation: `yes`/`y` saves, and `no`/`n` cancels.

Quantities use a decimal dot and up to six meaningful fractional places. Stock
can be zero; daily consumption and purchase quantities must be positive. Units
are labels, and the bot does not convert between them. Sending another URL
restarts onboarding; `/cancel` discards the current onboarding conversation.
`/start` and `/help` leave an existing conversation available to resume.

| Command | Behavior |
| --- | --- |
| `/stock [page]` | Show estimated stock, days remaining, and full item UUIDs; five items per page. |
| `/bought <uuid> <quantity>` | Add a purchase to the current stock and record it. |
| `/setstock <uuid> <quantity>` | Replace current estimated stock with a correction. |
| `/cancel` | Discard unfinished onboarding. |

For example, `/bought 12345678-1234-1234-1234-123456789abc 6` records six units.
Copy the actual UUID from `/stock`; the example UUID has no special meaning.
Estimated stock decreases with elapsed UTC time at the configured daily
consumption rate and never drops below zero. `HOUSEHOLD_TIMEZONE` remains the
household's local display/reminder timezone.

## Delivery behavior

The webhook authenticates before reading the request body, compares the header
secret in constant time, and measures the body while streaming with a 64 KiB
limit. Identifiers must be JSON integers, and both allowed IDs must match.
Other update types and authorized nontext messages are acknowledged without
changing stock. Requests with invalid secrets/identities return 403; malformed
updates return 400/422, oversized bodies return 413, and transaction failures
return a generic 503 so Telegram can retry. Error responses do not
include the supplied secret or request text.

Accepted messages commit the update receipt, state changes, and reply intents in
one database transaction. Repeated `update_id` values are acknowledged without
repeating purchases or replies. Replies are sent by the separate outbox process,
which uses bounded retries and recoverable leases; a webhook response alone does
not mean the reply has reached Telegram. Use `uv run python -m mimit.telegram.sender --once`
to process at most one eligible interactive reply during operator checks.
External sends cannot be guaranteed exactly once: a crash after Telegram accepts
a message but before the sender records success can result in a repeated reply.


Failed replies remain inspectable in PostgreSQL:

```sql
SELECT id, attempts, last_error FROM notification_outbox WHERE state = 'failed';
```

The sender makes at most five attempts, with exponential delay and jitter, and
honors a bounded Telegram retry-after value. A cancelled or crashed sender leaves
its lease recoverable after 60 seconds. Failed replies need operator attention;
there is no automatic replay command or claim of guaranteed eventual delivery.
The API does not reorder distinct updates, and multiple reply senders do not
promise ordering between different updates. Use one reply process for this v1 chat.

## Acceptance status

Local HTTP/PostgreSQL and simulated Telegram delivery checks pass. Railway
deployment and production Telegram configuration are documented in
[deployment setup](deployment.md). Full real-household onboarding, purchase,
and stock-correction acceptance still needs to be recorded before the Milestone 1
real-bot gate can pass.
