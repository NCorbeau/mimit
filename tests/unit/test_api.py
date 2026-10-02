from datetime import UTC, datetime

import httpx

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
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
