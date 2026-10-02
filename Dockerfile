FROM ghcr.io/astral-sh/uv:0.12.22 AS uv
FROM python:3.12-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/configure_telegram.py ./scripts/configure_telegram.py
RUN useradd --create-home --uid 10001 mimit
USER mimit

CMD ["sh", "-c", "exec uvicorn mimit.api:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
