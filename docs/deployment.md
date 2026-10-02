# Railway deployment

Work tracked by [MAC-59](https://linear.app/mglownia/issue/MAC-59/deploy-api-worker-and-postgresql-to-railway)
and [MAC-60](https://linear.app/mglownia/issue/MAC-60/configure-secrets-production-webhook-and-health-checks).
The parent production acceptance gate remains separate: recurring price work and
real household acceptance are still required.

## Production topology

Use the existing [mimit project](https://railway.com/project/e6cd1cc9-9bae-4a13-9e13-3b471e9f5504)
and its `production` environment. `.railway/railway.ts` preserves its existing
region and 5 GB PostgreSQL volume. Never recreate the project or database to deploy code.

| Service | Runtime | Networking |
| --- | --- | --- |
| `mimit` | FastAPI/Uvicorn, one replica | `https://mimit-production.up.railway.app`, port 8000 |
| `worker` | `python -m mimit.telegram.sender`, one replica | Private; no public domain or TCP proxy |
| `Postgres` | Railway PostgreSQL 18 with persistent volume | Private; no public domain or TCP proxy |

Both application services use `Dockerfile`: Python 3.12, uv 0.12.22, locked
production dependencies, and an unprivileged runtime user. The Docker context
allowlist excludes `.env`, Git metadata, tests, and local caches. The current
worker delivers interactive replies; scheduled prices and recommendations are
not implemented yet.

`DATABASE_URL` references `Postgres.DATABASE_URL`, whose host is private.
The API runs `alembic upgrade head` as a pre-deploy command. A failed migration
stops that deployment. Only the API migrates: deploy it successfully before
starting or redeploying the worker for schema-changing releases.

`/healthz` checks process liveness. `/readyz` reads a nonempty Alembic revision
from PostgreSQL with a five-second deadline and returns generic 503 on failure.
The deployment healthcheck uses `/readyz` with a 120-second startup window.
It does not monitor the worker or verify the complete schema continuously.

## Infrastructure and release commands

Railway's current IaC is `.railway/railway.ts`, evaluated by the CLI. The small
Node package in `.railway/` is only tooling; it is not part of the application image.

```sh
npm ci --prefix .railway
railway link --project e6cd1cc9-9bae-4a13-9e13-3b471e9f5504 --environment production --service mimit
railway config plan
railway config apply --yes
```

Review the plan before applying. Removing resources from the configuration can
delete them. Do not accept volume deletion, detachment, or placement changes as
a routine release step. Secrets are configured separately in Railway variables;
the infrastructure file contains references and nonsecret values only.

The API and worker deployments track `main` with CI checks enabled. Infrastructure
changes require a reviewed `railway config plan` and `railway config apply`;
Railway does not evaluate `.railway/railway.ts` on every source deployment.
For an explicit release:

```sh
railway up --service mimit --detach
railway deployment list --service mimit --limit 3 --json
curl --fail https://mimit-production.up.railway.app/readyz
railway up --service worker --detach
railway service list --json
```

Do not upload until tests pass, and do not deploy the worker until migrations and
Telegram configuration are ready. `railway up` excludes ignored local secrets.
No PostgreSQL integration tests should run against the production account.

## Telegram activation

Configure all five settings on the API together. The worker references the API's
values so both processes use the same credentials and allowed identity:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET` (1–256 letters, digits, `_` or `-`)
- `PUBLIC_BASE_URL=https://mimit-production.up.railway.app`
- `TELEGRAM_ALLOWED_USER_ID`
- `TELEGRAM_ALLOWED_CHAT_ID`

Partial Telegram configuration fails startup. The infrastructure file uses
`preserve()` for existing secrets and allowed IDs; configure them in Railway
before applying it to another environment. Before credentials are available,
the API can run readiness/liveness only and the worker should remain undeployed.
Set secrets through Railway's Variables UI or stdin (`railway variable set KEY
--stdin --skip-deploys`), never as plaintext command arguments. Variable-list JSON
contains credentials: do not paste or log its raw output.

Deploy the API, verify `/readyz`, deploy the worker, then register the webhook
using the included script with Railway's variables (the setup command does not
connect to PostgreSQL, so it can run locally):

```sh
railway run --no-local --service mimit -- uv run --locked python scripts/configure_telegram.py
railway run --no-local --service mimit -- uv run --locked python scripts/configure_telegram.py --check
```

Registration enables only message updates with one delivery connection and does
not discard pending updates. See [Telegram setup](telegram.md) for household
commands and delivery guarantees. Confirm a real `/start` and inventory flow
before treating the real-bot acceptance gate as passed.

For rollback, restore a previously verified application deployment with Railway's
deployment controls. Application rollback does not undo migrations or household
data. Prefer backward-compatible migrations; review any schema rollback separately.
