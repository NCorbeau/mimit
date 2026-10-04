"""Explicit application factory and database engine lifecycle."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock, SystemClock
from mimit.config import Settings, get_settings
from mimit.db.session import create_engine, get_session_factory
from mimit.observability import configure_logging, elapsed_ms, log_event


def create_app(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging(
        secrets=[
            resolved_settings.telegram_bot_token.get_secret_value()
            if resolved_settings.telegram_bot_token
            else "",
            resolved_settings.telegram_webhook_secret.get_secret_value()
            if resolved_settings.telegram_webhook_secret
            else "",
        ]
    )
    resolved_clock = clock if clock is not None else SystemClock()
    enabled = resolved_settings.telegram_configured
    if enabled:
        resolved_settings.require_telegram_configuration()
    engine = create_engine(resolved_settings.database_url) if sessions is None else None
    resolved_sessions = get_session_factory(engine) if engine is not None else sessions

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log_event("api_started", outcome="started")
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()
            log_event("api_stopped", outcome="stopped")

    app = FastAPI(title="Mimit", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.clock = resolved_clock
    app.state.sessions = resolved_sessions
    if enabled:
        from mimit.telegram.webhook import create_webhook_router

        assert resolved_sessions is not None
        app.include_router(
            create_webhook_router(resolved_settings, resolved_clock, resolved_sessions)
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Process liveness only; this does not claim database readiness."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Bounded database/schema connectivity check; no claim of worker readiness."""
        assert resolved_sessions is not None
        started = perf_counter()
        try:
            async with asyncio.timeout(5), resolved_sessions() as session:
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
                if revision is None:
                    log_event(
                        "api_readiness",
                        outcome="unavailable",
                        error_code="missing_revision",
                        duration_ms=elapsed_ms(started),
                    )
                    return JSONResponse({"status": "unavailable"}, status_code=503)
        except Exception:
            log_event(
                "api_readiness",
                outcome="unavailable",
                error_code="database_unavailable",
                duration_ms=elapsed_ms(started),
            )
            # Database exceptions can contain credentials; do not render or log them.
            return JSONResponse({"status": "unavailable"}, status_code=503)
        log_event("api_readiness", outcome="ready", duration_ms=elapsed_ms(started))
        return JSONResponse({"status": "ok"})

    return app
