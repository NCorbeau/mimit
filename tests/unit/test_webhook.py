import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.api import create_app
from mimit.clock import FrozenClock
from mimit.config import Settings
from mimit.recommendations import RecommendationConfig
from mimit.telegram import webhook


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql://user:password@invalid.example/db",
        TELEGRAM_BOT_TOKEN="123456:test-token",
        TELEGRAM_WEBHOOK_SECRET="test-webhook-secret",
        PUBLIC_BASE_URL="https://example.com",
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_ALLOWED_CHAT_ID=456,
    )


@pytest.fixture
def sessions() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker()


@pytest.fixture
def processor(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(webhook, "process_message", mock)
    return mock


@pytest.fixture
async def client(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        settings=settings,
        clock=FrozenClock(datetime(2026, 10, 2, tzinfo=UTC)),
        sessions=sessions,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as active:
        yield active


def update() -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": 123},
            "chat": {"id": 456, "type": "private"},
            "text": "/stock",
        },
    }


HEADERS = {webhook.SECRET_HEADER: "test-webhook-secret"}


@pytest.mark.parametrize("is_new", [True, False])
async def test_new_and_duplicate_updates_acknowledge_after_processing(
    client: httpx.AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    processor: AsyncMock,
    is_new: bool,
) -> None:
    processor.return_value = is_new
    payload = update()
    payload["update_id"] = 0
    response = await client.post("/telegram/webhook", json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    processor.assert_awaited_once_with(
        sessions,
        update_id=0,
        user_id=123,
        chat_id=456,
        text="/stock",
        clock=FrozenClock(datetime(2026, 10, 2, tzinfo=UTC)),
        timezone="Europe/Warsaw",
        recommendation_config=RecommendationConfig(allowed_chat_id=456),
    )


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(webhook.SECRET_HEADER, "wrong-secret")],
        [(webhook.SECRET_HEADER, "test-webhook-secret"), (webhook.SECRET_HEADER, "wrong")],
    ],
)
async def test_secret_header_must_be_present_unique_and_exact(
    client: httpx.AsyncClient, processor: AsyncMock, headers: list[tuple[str, str]]
) -> None:
    response = await client.post(
        "/telegram/webhook", content=b"sensitive-invalid-json", headers=headers
    )
    assert response.status_code == 403
    assert "sensitive" not in response.text
    assert "secret" not in response.text
    processor.assert_not_awaited()


@pytest.mark.parametrize("field", ["user", "chat", "chat_type"])
async def test_both_allowlisted_ids_and_private_chat_are_required(
    client: httpx.AsyncClient, processor: AsyncMock, field: str
) -> None:
    payload = update()
    if field == "user":
        payload["message"]["from"]["id"] = 999
    elif field == "chat":
        payload["message"]["chat"]["id"] = 999
    else:
        payload["message"]["chat"]["type"] = "group"
    response = await client.post("/telegram/webhook", json=payload, headers=HEADERS)
    assert response.status_code == 403
    processor.assert_not_awaited()


@pytest.mark.parametrize("field", ["update", "user", "chat"])
@pytest.mark.parametrize("identifier", [True, "123", 1.0, None, 2**64])
async def test_identifiers_are_strict_bounded_json_integers(
    client: httpx.AsyncClient, processor: AsyncMock, field: str, identifier: object
) -> None:
    payload = update()
    if field == "update":
        payload["update_id"] = identifier
    elif field == "user":
        payload["message"]["from"]["id"] = identifier
    else:
        payload["message"]["chat"]["id"] = identifier
    response = await client.post("/telegram/webhook", json=payload, headers=HEADERS)
    assert response.status_code == 422
    processor.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        b"private-invalid-body",
        b"\xff",
        b'{"update_id":1,"update_id":2}',
        b'{"update_id":NaN}',
        b"[1,2]",
        b"null",
    ],
)
async def test_malformed_json_does_not_expose_inputs(
    client: httpx.AsyncClient, processor: AsyncMock, body: bytes
) -> None:
    response = await client.post("/telegram/webhook", content=body, headers=HEADERS)
    assert response.status_code in {400, 422}
    assert "private-invalid-body" not in response.text
    processor.assert_not_awaited()


async def test_streaming_body_limit_does_not_depend_on_content_length(
    client: httpx.AsyncClient, processor: AsyncMock
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b" " * webhook.MAX_BODY_BYTES
        yield b" "

    response = await client.post("/telegram/webhook", content=chunks(), headers=HEADERS)
    assert response.status_code == 413
    processor.assert_not_awaited()


@pytest.mark.parametrize("kind", ["edited_message", "callback_query", "photo"])
async def test_unrelated_updates_are_acknowledged_without_mutation(
    client: httpx.AsyncClient, processor: AsyncMock, kind: str
) -> None:
    if kind == "photo":
        payload = update()
        del payload["message"]["text"]
        payload["message"]["photo"] = []
    else:
        payload = {"update_id": 5, kind: {}}
    response = await client.post("/telegram/webhook", json=payload, headers=HEADERS)
    assert response.status_code == 200
    processor.assert_not_awaited()


@pytest.mark.parametrize("text", [None, "", 123, "a" * 4097])
async def test_invalid_text_is_rejected(
    client: httpx.AsyncClient, processor: AsyncMock, text: object
) -> None:
    payload = update()
    payload["message"]["text"] = text
    response = await client.post("/telegram/webhook", json=payload, headers=HEADERS)
    assert response.status_code == 422
    processor.assert_not_awaited()


async def test_transaction_failure_is_not_acknowledged(
    client: httpx.AsyncClient, processor: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    processor.side_effect = RuntimeError("database failed: private-user-text token-secret")
    response = await client.post("/telegram/webhook", json=update(), headers=HEADERS)
    assert response.status_code == 503
    assert "telegram_processing_failed" in caplog.text
    for secret in ("private-user-text", "token-secret"):
        assert secret not in response.text + caplog.text


@pytest.mark.parametrize("secret", ["contains.dot", "not-ascii-ą", "a" * 257])
def test_webhook_secret_api_format_is_validated_without_leaking(secret: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:password@localhost/db",
            TELEGRAM_WEBHOOK_SECRET=secret,
        )
    assert secret not in str(caught.value) + repr(caught.value.errors()) + caught.value.json()


@pytest.mark.parametrize("invalid_text", ["https://example.com/\ud800", "https://example.com/\x00"])
async def test_unstorable_text_is_rejected_before_database(
    client: httpx.AsyncClient,
    processor: AsyncMock,
    invalid_text: str,
) -> None:
    payload = update()
    payload["message"]["text"] = invalid_text
    response = await client.post(
        "/telegram/webhook",
        content=json.dumps(payload).encode("ascii"),
        headers=HEADERS,
    )
    assert response.status_code == 422
    processor.assert_not_awaited()
