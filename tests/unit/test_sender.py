import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from mimit.telegram import sender as delivery

TOKEN = "123456:test_secret"
PAYLOAD = {"chat_id": 17, "text": "Saved <plain text>"}
SUCCESS = {"ok": True, "result": {"message_id": 91, "chat": {"id": 17}}}


async def send_response(status: int, data: Any) -> delivery.SendResult:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=data))
    ) as client:
        sender = delivery.TelegramSender(token=SecretStr(TOKEN), allowed_chat_id=17, client=client)
        return await sender.send(PAYLOAD)


async def test_plain_text_request_and_success_redacts_httpx_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "api.telegram.org"
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        assert json.loads(request.content) == PAYLOAD
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.extensions["timeout"] == {"connect": 5, "read": 10, "write": 10, "pool": 5}
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sender = delivery.TelegramSender(token=SecretStr(TOKEN), allowed_chat_id=17, client=client)
        assert await sender.send(PAYLOAD) == delivery.SendResult(delivery.Outcome.SENT)
    assert TOKEN not in caplog.text
    assert "bot<redacted>" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"chat_id": 18, "text": "saved"},
        {"chat_id": True, "text": "saved"},
        {"chat_id": "17", "text": "saved"},
        {"chat_id": 17, "text": ""},
        {"chat_id": 17, "text": "x" * 4097},
        {"chat_id": 17, "text": "🐱" * 2049},
        {"chat_id": 17, "text": "\ud800"},
        {"chat_id": 17, "text": "text\x00"},
        {"chat_id": 17, "text": 3},
        {"chat_id": 17, "text": "saved", "parse_mode": "HTML"},
        {},
    ],
)
async def test_invalid_payload_cannot_send(payload: dict[str, Any]) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid outbox payload must fail before HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        sender = delivery.TelegramSender(token=SecretStr(TOKEN), allowed_chat_id=17, client=client)
        assert await sender.send(payload) == delivery.SendResult(
            delivery.Outcome.FAILED, delivery.ErrorCode.INVALID_PAYLOAD
        )


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_permanent_http_errors_are_terminal_without_raw_description(status: int) -> None:
    result = await send_response(status, {"ok": False, "description": TOKEN})
    assert result == delivery.SendResult(delivery.Outcome.FAILED, delivery.ErrorCode.PERMANENT)
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("status", [408, 500, 502, 503])
async def test_temporary_http_errors_retry(status: int) -> None:
    assert await send_response(status, {}) == delivery.SendResult(
        delivery.Outcome.RETRY, delivery.ErrorCode.SERVER
    )


async def test_429_honors_retry_after_even_for_api_error_over_http_200() -> None:
    for status in (200, 429):
        result = await send_response(
            status, {"ok": False, "error_code": 429, "parameters": {"retry_after": 123}}
        )
        assert result == delivery.SendResult(
            delivery.Outcome.RETRY, delivery.ErrorCode.RATE_LIMIT, retry_after=123
        )


async def test_extreme_retry_after_settles_without_retrying_earlier_than_requested() -> None:
    result = await send_response(
        429, {"ok": False, "parameters": {"retry_after": delivery.MAX_RETRY_AFTER_SECONDS + 1}}
    )
    assert result == delivery.SendResult(
        delivery.Outcome.FAILED, delivery.ErrorCode.RETRY_AFTER_TOO_LONG
    )


@pytest.mark.parametrize("data", [{"ok": True}, {"ok": False}, [], "invalid"])
async def test_malformed_success_is_not_acknowledged(data: Any) -> None:
    assert await send_response(200, data) == delivery.SendResult(
        delivery.Outcome.RETRY, delivery.ErrorCode.INVALID_RESPONSE
    )


async def test_transport_error_token_url_never_becomes_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"lost response from {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sender = delivery.TelegramSender(token=SecretStr(TOKEN), allowed_chat_id=17, client=client)
        result = await sender.send(PAYLOAD)
    assert result == delivery.SendResult(delivery.Outcome.RETRY, delivery.ErrorCode.TRANSPORT)
    assert TOKEN not in caplog.text + repr(result)


async def test_redirect_never_sends_token_to_another_host() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://other.example/steal"})

    async with httpx.AsyncClient(
        follow_redirects=True, transport=httpx.MockTransport(respond)
    ) as client:
        sender = delivery.TelegramSender(token=SecretStr(TOKEN), allowed_chat_id=17, client=client)
        assert (await sender.send(PAYLOAD)).outcome == delivery.Outcome.RETRY
    assert calls == 1


async def test_response_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "MAX_RESPONSE_BYTES", 8)
    assert (await send_response(200, SUCCESS)).error == delivery.ErrorCode.INVALID_RESPONSE


async def test_send_deadline_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "SEND_DEADLINE_SECONDS", 0.001)

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sender = delivery.TelegramSender(token=SecretStr(TOKEN), allowed_chat_id=17, client=client)
        assert (await sender.send(PAYLOAD)).error == delivery.ErrorCode.TRANSPORT


@pytest.mark.parametrize("token", ["not-a-token", "123:bad/path", "123:bad?query"])
def test_token_cannot_inject_endpoint_path(token: str) -> None:
    with pytest.raises(ValueError, match="token format"):
        delivery.TelegramSender(
            token=SecretStr(token), allowed_chat_id=17, client=httpx.AsyncClient()
        )
