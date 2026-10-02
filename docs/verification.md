# Verification record

## Foundation — 2 October 2026

Verified locally with Python 3.12.15 and PostgreSQL 17 in Docker:

- `uv sync --locked` in a fresh temporary directory: passed.
- `make check`: Ruff lint/format, strict mypy, 50 fast tests passed.
- `TEST_DATABASE_URL=… make test-integration`: 10 real-PostgreSQL tests passed.
- `alembic upgrade head` against an empty DB: passed.
- Upgrade → metadata drift check → downgrade to base → upgrade → drift check:
  passed in a disposable database.
- Exact Schesir variant `2333304.0` fetched through httpx; see
  [the timestamped spike evidence](zooplus-spike.md).
- Independent review covered architecture, persistence, transactions, Python,
  security, test credibility, and README accuracy; no material findings remained.
- Project files reviewed for credentials; only documented local/test values present.

The concurrency test observes a PostgreSQL lock wait before releasing the winning
transaction. The harness is isolated by random database names. These checks do not
claim production Telegram or worker acceptance.
