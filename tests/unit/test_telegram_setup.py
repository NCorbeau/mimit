import json
import logging

import httpx
import pytest

from mimit.config import Settings
from scripts.configure_telegram import configure

TOKEN = "123456:fake_token_for_local_tests"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql://local:local@localhost/test",
        TELEGRAM_BOT_TOKEN=TOKEN,
        TELEGRAM_WEBHOOK_SECRET="local_secret",
        TELEGRAM_ALLOWED_USER_ID=101,
        TELEGRAM_ALLOWED_CHAT_ID=101,
        PUBLIC_BASE_URL="https://mimit.example.com",
    )


async def test_setup_uses_secret_and_serial_delivery_without_logging_token(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    caplog.set_level(logging.DEBUG)
    result = await configure(settings())
    assert "registered" in result
    sent = json.loads(requests[0].content)
    assert sent == {
        "url": "https://mimit.example.com/telegram/webhook",
        "secret_token": "local_secret",
        "allowed_updates": ["message"],
        "max_connections": 1,
    }
    assert TOKEN not in caplog.text
    assert "bot<redacted>" in caplog.text


async def test_setup_failure_does_not_echo_telegram_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(401, text=TOKEN)),
            **kwargs,
        ),
    )
    with pytest.raises(RuntimeError) as caught:
        await configure(settings())
    assert TOKEN not in str(caught.value)
