import asyncio
import io
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit import observability
from mimit.clock import FrozenClock
from mimit.observability import JsonFormatter, configure_logging, log_event
from mimit.telegram import sender as delivery


def formatted_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(JsonFormatter().format(record))
        for record in caplog.records
        if record.name == "mimit.events"
    ]


def test_import_does_not_configure_logging() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; before = list(logging.getLogger().handlers); "
            "import mimit.observability; assert logging.getLogger().handlers == before",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_json_events_are_correlated_and_reject_untrusted_fields(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observability, "_secrets", {"known-webhook-secret"})
    caplog.set_level(logging.INFO, logger="mimit.events")
    item_id = uuid4()
    log_event(
        "product_fetch_result",
        consumable_id=item_id,
        update_id=7,
        outcome="failed",
        error_code="http_error",
        duration_ms=12.34567,
        http_status=500,
        body="private message and full merchant page",
        url="https://api.telegram.org/bot123:secret/sendMessage",
        token="123:secret",
        payload={"text": "private text"},
        stage="known-webhook-secret",
        household_id="private-household-name",
    )
    [event] = formatted_events(caplog)
    assert event["event"] == "product_fetch_result"
    assert event["consumable_id"] == str(item_id)
    assert event["update_id"] == 7
    assert event["duration_ms"] == 12.346
    assert event["http_status"] == 500
    assert event["error_code"] == "http_error"
    assert event["level"] == "INFO"
    assert datetime.fromisoformat(str(event["timestamp"])).utcoffset() == timedelta(0)
    for secret in ("private", "merchant", "123:secret", "known-webhook-secret"):
        assert secret not in json.dumps(event) + caplog.text


@pytest.mark.parametrize(
    "unsafe",
    [
        "postgresql://user:password@host/db",
        "123:bot_secret",
        "raw exception text",
        "secret\nnew log",
        "x" * 1000,
    ],
)
def test_code_fields_cannot_carry_urls_tokens_or_free_text(
    unsafe: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="mimit.events")
    log_event("job_result", error_code=unsafe, outcome=unsafe, worker=unsafe)
    [event] = formatted_events(caplog)
    assert "error_code" not in event
    assert "outcome" not in event
    assert "worker" not in event
    assert unsafe not in caplog.text


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1.0, True])
def test_duration_is_finite_nonnegative_and_not_boolean(
    duration: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="mimit.events")
    log_event("job_result", duration_ms=duration)
    assert "duration_ms" not in formatted_events(caplog)[0]


def test_runtime_formatter_omits_arbitrary_messages_and_exception_bodies() -> None:
    try:
        raise RuntimeError("private message postgres://user:password@host/db 123:secret")
    except RuntimeError:
        record = logging.LogRecord(
            "httpx",
            logging.ERROR,
            __file__,
            1,
            "merchant page body %s",
            ("private-text",),
            sys.exc_info(),
        )
    result = json.loads(JsonFormatter().format(record))
    assert result["event"] == "runtime_log"
    for secret in ("private", "password", "123:secret", "merchant", "RuntimeError"):
        assert secret not in json.dumps(result)


def test_runtime_setup_is_idempotent_and_preserves_caller_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    previous_handler = logging.NullHandler()
    monkeypatch.setattr(root, "handlers", [previous_handler])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(observability, "_secrets", set())
    configure_logging(secrets=["webhook-secret"])
    configure_logging()
    assert root.handlers[0] is previous_handler
    assert len(root.handlers) == 2
    stream = io.StringIO()
    handler = root.handlers[1]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    log_event("worker_started", outcome="started", worker="tracking")
    event = json.loads(stream.getvalue())
    assert event["event"] == "worker_started"
    assert event["worker"] == "tracking"


def test_dependency_redaction_protects_other_handlers_and_tracebacks(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observability, "_secrets", set())
    observability.install_log_redaction(secrets=["webhook-secret"])
    caplog.set_level(logging.INFO, logger="httpx")
    try:
        raise RuntimeError("raw private exception")
    except RuntimeError:
        logging.getLogger("httpx").exception(
            "request %s db=%s header=%s",
            "https://api.telegram.org/bot123:bot_secret/sendMessage",
            "postgresql+asyncpg://user:db_password@host/db",
            "webhook-secret",
        )
    assert "bot<redacted>" in caplog.text
    for secret in ("123:bot_secret", "db_password", "webhook-secret", "raw private exception"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "outcome,error",
    [
        (delivery.Outcome.SENT, None),
        (delivery.Outcome.RETRY, delivery.ErrorCode.RATE_LIMIT),
        (delivery.Outcome.FAILED, delivery.ErrorCode.PERMANENT),
    ],
)
async def test_notification_result_has_identity_outcome_duration_without_message(
    outcome: delivery.Outcome,
    error: delivery.ErrorCode | None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="mimit.events")
    clock = FrozenClock(datetime(2026, 10, 4, tzinfo=UTC))
    claim = delivery.Claim(
        uuid4(),
        str(uuid4()),
        2,
        clock.now() + timedelta(seconds=60),
        {"chat_id": 17, "text": "private household message"},
    )
    monkeypatch.setattr(delivery, "claim_next", AsyncMock(return_value=claim))
    monkeypatch.setattr(delivery, "preflight", AsyncMock(return_value=claim.payload))
    monkeypatch.setattr(delivery, "acknowledge", AsyncMock(return_value=True))
    sender = AsyncMock(spec=delivery.TelegramSender)
    sender.send.return_value = delivery.SendResult(outcome, error, 12)
    assert await delivery.send_once(async_sessionmaker(), clock, sender)
    [claimed, result] = formatted_events(caplog)
    assert claimed["event"] == "notification_claimed"
    assert result["event"] == "notification_send_result"
    assert result["notification_id"] == str(claim.id)
    assert result["outcome"] == outcome.value
    assert result["error_code"] == (error.value if error else None)
    assert result["acknowledged"] is True
    assert result["duration_ms"] >= 0  # type: ignore[operator]
    assert "private household message" not in caplog.text


async def test_notification_cancel_logs_recoverable_attempt_without_exception(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="mimit.events")
    clock = FrozenClock(datetime(2026, 10, 4, tzinfo=UTC))
    claim = delivery.Claim(uuid4(), str(uuid4()), 1, clock.now() + timedelta(seconds=60), {})
    monkeypatch.setattr(delivery, "claim_next", AsyncMock(return_value=claim))
    monkeypatch.setattr(delivery, "preflight", AsyncMock(return_value=claim.payload))
    sender = AsyncMock(spec=delivery.TelegramSender)
    sender.send.side_effect = asyncio.CancelledError("private exception")
    with pytest.raises(asyncio.CancelledError):
        await delivery.send_once(async_sessionmaker(), clock, sender)
    assert formatted_events(caplog)[-1]["outcome"] == "cancelled"
    assert "private exception" not in caplog.text


async def test_bot_transport_failure_never_exposes_url_or_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    token = "123456:sentinel_bot_secret"

    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"raw URL {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sender = delivery.TelegramSender(token=SecretStr(token), allowed_chat_id=17, client=client)
        result = await sender.send({"chat_id": 17, "text": "private-text"})
    assert result.error is delivery.ErrorCode.TRANSPORT
    assert token not in caplog.text
    assert "private-text" not in caplog.text


@pytest.mark.parametrize("processed", [True, False])
async def test_webhook_receipt_distinguishes_committed_and_duplicate_without_text(
    processed: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from mimit.api import create_app
    from mimit.config import Settings
    from mimit.telegram import webhook

    caplog.set_level(logging.INFO, logger="mimit.events")
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgres://user:password@invalid.example/db",
        TELEGRAM_BOT_TOKEN="123456:test_token",
        TELEGRAM_WEBHOOK_SECRET="secret",
        PUBLIC_BASE_URL="https://example.com",
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_ALLOWED_CHAT_ID=456,
    )
    monkeypatch.setattr(webhook, "process_message", AsyncMock(return_value=processed))
    app = create_app(settings=settings, sessions=async_sessionmaker())
    payload = {
        "update_id": 77,
        "message": {
            "from": {"id": 123},
            "chat": {"id": 456, "type": "private"},
            "text": "private-user-message",
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/telegram/webhook", json=payload, headers={webhook.SECRET_HEADER: "secret"}
        )
    assert response.status_code == 200
    receipt = next(
        event for event in formatted_events(caplog) if event["event"] == "telegram_update_receipt"
    )
    assert receipt["update_id"] == 77
    assert receipt["outcome"] == ("processed" if processed else "duplicate")
    assert "private-user-message" not in caplog.text


@pytest.mark.parametrize("failure_stage", [None, "fetch", "extract", "commit"])
async def test_product_stages_have_safe_ids_and_only_log_commit_after_persistence(
    failure_stage: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from decimal import Decimal
    from unittest.mock import MagicMock

    from sqlalchemy.exc import SQLAlchemyError

    from mimit.db.models import Consumable
    from mimit.products import service as product_service
    from mimit.products.service import PriceCheckServiceError, ProductCheckService, SourceSnapshot
    from mimit.products.types import ErrorCode, ExtractedProduct, FetchedPage, ProductCheckError

    caplog.set_level(logging.INFO, logger="mimit.events")
    item_id, source_id, household_id = uuid4(), uuid4(), uuid4()
    url = "https://merchant.example/product?token=private-url-secret"
    source = SourceSnapshot(source_id, item_id, household_id, url, None)
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    if failure_stage == "commit":
        session.__aexit__.side_effect = SQLAlchemyError(
            "raw database credentials private-db-secret"
        )
    sessions = MagicMock(spec=async_sessionmaker)
    sessions.begin.return_value = session
    fetcher = AsyncMock()
    fetcher.fetch.return_value = FetchedPage("private full page body", url, 200, 22, "digest")
    extractor = MagicMock()
    extractor.extract.return_value = ExtractedProduct(
        "private product name",
        None,
        Decimal("10"),
        "PLN",
        None,
        None,
        "available",
        {"source": "private metadata"},
    )
    if failure_stage == "fetch":
        fetcher.fetch.side_effect = ProductCheckError(ErrorCode.TIMEOUT)
    elif failure_stage == "extract":
        extractor.extract.side_effect = RuntimeError("raw merchant exception private-exception")
    service = ProductCheckService(
        sessions, FrozenClock(datetime(2026, 10, 4, tzinfo=UTC)), fetcher, extractor
    )
    monkeypatch.setattr(service, "snapshot", AsyncMock(return_value=source))
    monkeypatch.setattr(
        product_service,
        "lock_source",
        AsyncMock(return_value=Consumable(id=item_id, household_id=household_id)),
    )
    if failure_stage == "commit":
        with pytest.raises(PriceCheckServiceError, match="persistence_error"):
            await service.check(item_id)
    else:
        await service.check(item_id)
    events = formatted_events(caplog)
    assert events[0]["event"] == "product_fetch_started"
    fetch = next(event for event in events if event["event"] == "product_fetch_result")
    assert fetch["outcome"] == ("failed" if failure_stage == "fetch" else "success")
    assert fetch["consumable_id"] == str(item_id)
    assert fetch["offer_source_id"] == str(source_id)
    if failure_stage != "fetch":
        extraction = next(
            event for event in events if event["event"] == "product_extraction_result"
        )
        assert extraction["outcome"] == ("failed" if failure_stage == "extract" else "success")
    committed = [event for event in events if event["event"] == "product_observation_committed"]
    assert bool(committed) == (failure_stage != "commit")
    assert "private" not in json.dumps(events) + caplog.text
