"""Foundation app factory; product routes wait for the M0 gate."""

from fastapi import FastAPI

from mimit.clock import Clock, SystemClock
from mimit.config import Settings, get_settings


def create_app(*, settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    app = FastAPI(title="Mimit", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings if settings is not None else get_settings()
    app.state.clock = clock if clock is not None else SystemClock()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Process liveness only; this does not claim database readiness."""
        return {"status": "ok"}

    return app
