"""Explicit application factory and database engine lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock, SystemClock
from mimit.config import Settings, get_settings
from mimit.db.session import create_engine, get_session_factory


def create_app(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    resolved_clock = clock if clock is not None else SystemClock()
    enabled = resolved_settings.telegram_configured
    if enabled:
        resolved_settings.require_telegram_configuration()
    engine = create_engine(resolved_settings.database_url) if enabled and sessions is None else None
    resolved_sessions = get_session_factory(engine) if engine is not None else sessions

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

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

    return app
