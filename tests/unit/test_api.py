from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mimit import api
from mimit.api import create_app
from mimit.clock import FrozenClock
from mimit.config import Settings


async def test_liveness_and_injected_dependencies_without_connecting_to_database() -> None:
    settings = Settings(
        _env_file=None, DATABASE_URL="postgresql://user:password@invalid.example/db"
    )
    clock = FrozenClock(datetime(2026, 10, 2, tzinfo=UTC))
    app = create_app(settings=settings, clock=clock)
    assert app.state.settings is settings
    assert app.state.clock is clock
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert (await client.get("/webhook")).status_code == 404
        assert (await client.get("/telegram/webhook")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


def test_partial_telegram_configuration_prevents_startup() -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://user:password@localhost/db",
        TELEGRAM_BOT_TOKEN="123456:test-token",
    )
    with pytest.raises(ValueError, match="Missing Telegram configuration"):
        create_app(settings=settings)


async def test_owned_engine_is_disposed_on_lifespan_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://user:password@localhost/db",
        TELEGRAM_BOT_TOKEN="123456:test-token",
        TELEGRAM_WEBHOOK_SECRET="webhook-secret",
        PUBLIC_BASE_URL="https://example.com",
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_ALLOWED_CHAT_ID=456,
    )
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker()
    monkeypatch.setattr(api, "create_engine", lambda url: engine)
    monkeypatch.setattr(api, "get_session_factory", lambda active: sessions)
    app = create_app(settings=settings)
    assert app.state.sessions is sessions
    async with app.router.lifespan_context(app):
        engine.dispose.assert_not_awaited()
    engine.dispose.assert_awaited_once()
