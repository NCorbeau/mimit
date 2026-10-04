"""Minimal M1 interactive-reply outbox delivery, not a scheduled-job worker.

Claims commit before HTTP; acknowledgements require the same unexpired lease.
Cancellation/crash leaves a lease for recovery. Telegram sendMessage has no
idempotency key: an accepted send followed by a crash/lost response can be sent
again after lease expiry. This is bounded at-least-once intent processing, not an
exactly-once external delivery guarantee. M4 recommendation delivery is deferred.

Bot API contract: https://core.telegram.org/bots/api#sendmessage and
https://core.telegram.org/bots/api#responseparameters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock, SystemClock
from mimit.config import get_settings
from mimit.db.models import NotificationOutbox
from mimit.db.session import create_engine, get_session_factory
from mimit.observability import configure_logging, elapsed_ms, log_event
from mimit.observability import install_log_redaction as install_log_redaction

MAX_ATTEMPTS = 5
LEASE_SECONDS = 60
SEND_DEADLINE_SECONDS = 20
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RETRY_AFTER_SECONDS = 86400
HTTP_TIMEOUT = httpx.Timeout(10, connect=5, pool=5)


class Outcome(StrEnum):
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"


class ErrorCode(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    TRANSPORT = "transport_error"
    RATE_LIMIT = "telegram_rate_limit"
    SERVER = "telegram_server_error"
    PERMANENT = "telegram_permanent_error"
    INVALID_RESPONSE = "telegram_invalid_response"
    RETRY_AFTER_TOO_LONG = "retry_after_too_long"
    EXHAUSTED = "attempts_exhausted"


@dataclass(frozen=True)
class SendResult:
    outcome: Outcome
    error: ErrorCode | None = None
    retry_after: int = 0


@dataclass(frozen=True)
class Claim:
    id: UUID
    owner: str
    attempts: int
    expires_at: datetime
    payload: dict[str, Any] = field(repr=False)


class TelegramSender:
    def __init__(
        self, *, token: SecretStr, allowed_chat_id: int, client: httpx.AsyncClient
    ) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token.get_secret_value()):
            raise ValueError("Invalid Telegram bot token format")
        if type(allowed_chat_id) is not int or allowed_chat_id == 0:
            raise ValueError("A nonzero allowed Telegram chat ID is required")
        self._token = token
        self._allowed_chat_id = allowed_chat_id
        self._client = client
        install_log_redaction()

    async def send(self, payload: dict[str, Any]) -> SendResult:
        chat_id, text = payload.get("chat_id"), payload.get("text")
        if (
            set(payload) != {"chat_id", "text"}
            or type(chat_id) is not int
            or chat_id != self._allowed_chat_id
            or not isinstance(text, str)
            or not 1 <= len(text) <= 4096
        ):
            return SendResult(Outcome.FAILED, ErrorCode.INVALID_PAYLOAD)
        endpoint = f"https://api.telegram.org/bot{self._token.get_secret_value()}/sendMessage"
        try:
            async with asyncio.timeout(SEND_DEADLINE_SECONDS):
                async with self._client.stream(
                    "POST",
                    endpoint,
                    json={"chat_id": chat_id, "text": text},
                    timeout=HTTP_TIMEOUT,
                    follow_redirects=False,
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            return SendResult(Outcome.RETRY, ErrorCode.INVALID_RESPONSE)
                        body.extend(chunk)
                    try:
                        data = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        data = None
                    return self._interpret(response.status_code, data, chat_id)
        except (httpx.HTTPError, TimeoutError):
            # Never persist/log exception strings: their request URL contains the token.
            return SendResult(Outcome.RETRY, ErrorCode.TRANSPORT)

    @staticmethod
    def _interpret(status: int, data: Any, chat_id: int) -> SendResult:
        if status == 429 or (
            isinstance(data, dict) and data.get("ok") is False and data.get("error_code") == 429
        ):
            parameters = data.get("parameters") if isinstance(data, dict) else None
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else 0
            if type(retry_after) is not int or retry_after < 0:
                retry_after = 0
            if retry_after > MAX_RETRY_AFTER_SECONDS:
                return SendResult(Outcome.FAILED, ErrorCode.RETRY_AFTER_TOO_LONG)
            return SendResult(Outcome.RETRY, ErrorCode.RATE_LIMIT, retry_after)
        if status >= 500 or status == 408:
            return SendResult(Outcome.RETRY, ErrorCode.SERVER)
        if 400 <= status < 500:
            return SendResult(Outcome.FAILED, ErrorCode.PERMANENT)
        if not isinstance(data, dict):
            return SendResult(Outcome.RETRY, ErrorCode.INVALID_RESPONSE)
        if status == 200 and data.get("ok") is True:
            message = data.get("result")
            if isinstance(message, dict):
                chat = message.get("chat")
                if (
                    type(message.get("message_id")) is int
                    and message["message_id"] > 0
                    and isinstance(chat, dict)
                    and chat.get("id") == chat_id
                ):
                    return SendResult(Outcome.SENT)
            return SendResult(Outcome.RETRY, ErrorCode.INVALID_RESPONSE)
        code = data.get("error_code")
        if data.get("ok") is False and type(code) is int:
            if 400 <= code < 500:
                return SendResult(Outcome.FAILED, ErrorCode.PERMANENT)
            if code >= 500:
                return SendResult(Outcome.RETRY, ErrorCode.SERVER)
        return SendResult(Outcome.RETRY, ErrorCode.INVALID_RESPONSE)


async def claim_next(sessions: async_sessionmaker[AsyncSession], clock: Clock) -> Claim | None:
    now = clock.now()
    due = or_(
        and_(NotificationOutbox.state == "pending", NotificationOutbox.run_at <= now),
        and_(NotificationOutbox.state == "sending", NotificationOutbox.lease_expires_at <= now),
    )
    # This sender handles only interactive replies. Recommendation intents remain M4.
    scope = NotificationOutbox.dedupe_key.startswith("telegram:")
    async with sessions.begin() as session:
        row = await session.scalar(
            select(NotificationOutbox)
            .where(scope, due)
            .order_by(NotificationOutbox.run_at, NotificationOutbox.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        if row.attempts >= MAX_ATTEMPTS:
            # Final-attempt crashes settle under the same nonblocking row lock.
            row.state = "failed"
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error = ErrorCode.EXHAUSTED.value
            return None
        row.state = "sending"
        row.lease_owner = str(uuid4())
        row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        row.attempts += 1
        claim = Claim(
            row.id, row.lease_owner, row.attempts, row.lease_expires_at, dict(row.payload)
        )
    return claim


async def acknowledge(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    claim: Claim,
    result: SendResult,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
) -> bool:
    now = clock.now()
    if result.outcome is Outcome.SENT:
        values: dict[str, Any] = {"state": "sent", "delivered_at": now, "last_error": None}
    elif result.outcome is Outcome.FAILED or claim.attempts >= MAX_ATTEMPTS:
        error = ErrorCode.EXHAUSTED if result.outcome is Outcome.RETRY else result.error
        values = {"state": "failed", "last_error": error.value if error else None}
    else:
        base_delay = min(5 * 2 ** (claim.attempts - 1), 300)
        delay = max(min(base_delay + jitter(0, base_delay), 300), result.retry_after)
        values = {
            "state": "pending",
            "run_at": now + timedelta(seconds=delay),
            "last_error": result.error.value if result.error else None,
        }
    async with sessions.begin() as session:
        row_id = await session.scalar(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == claim.id,
                NotificationOutbox.state == "sending",
                NotificationOutbox.lease_owner == claim.owner,
                NotificationOutbox.lease_expires_at > now,
            )
            .values(**values, lease_owner=None, lease_expires_at=None)
            .returning(NotificationOutbox.id)
        )
    log_event(
        "notification_acknowledged",
        notification_id=claim.id,
        attempt=claim.attempts,
        outcome=values["state"] if row_id is not None else "stale",
        error_code=values.get("last_error"),
        acknowledged=row_id is not None,
    )
    return row_id is not None


async def send_once(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    sender: TelegramSender,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
) -> bool:
    claim = await claim_next(sessions, clock)
    if claim is None:
        return False
    log_event(
        "notification_claimed", notification_id=claim.id, attempt=claim.attempts, outcome="claimed"
    )
    if clock.now() >= claim.expires_at:
        return True  # Lease expired before I/O; recovery belongs to a later claim.
    started = perf_counter()
    try:
        result = await sender.send(claim.payload)
    except asyncio.CancelledError:
        log_event(
            "notification_send_result",
            notification_id=claim.id,
            attempt=claim.attempts,
            outcome="cancelled",
            duration_ms=elapsed_ms(started),
        )
        raise
    acknowledged = await acknowledge(sessions, clock, claim, result, jitter=jitter)
    log_event(
        "notification_send_result",
        notification_id=claim.id,
        attempt=claim.attempts,
        outcome=result.outcome,
        error_code=result.error,
        retry_after=result.retry_after,
        acknowledged=acknowledged,
        duration_ms=elapsed_ms(started),
    )
    return True


async def run(*, once: bool) -> None:
    settings = get_settings()
    settings.require_telegram_configuration()
    configure_logging(
        secrets=[
            settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else "",
            settings.telegram_webhook_secret.get_secret_value()
            if settings.telegram_webhook_secret
            else "",
        ]
    )
    assert settings.telegram_bot_token is not None
    assert settings.telegram_allowed_chat_id is not None
    # SIGTERM cancels even in-flight HTTP; its durable lease remains recoverable.
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    engine = create_engine(settings.database_url)
    try:
        async with httpx.AsyncClient(verify=True, trust_env=False, timeout=HTTP_TIMEOUT) as client:
            sender = TelegramSender(
                token=settings.telegram_bot_token,
                allowed_chat_id=settings.telegram_allowed_chat_id,
                client=client,
            )
            sessions, clock = get_session_factory(engine), SystemClock()
            while True:
                try:
                    worked = await send_once(sessions, clock, sender)
                except Exception:
                    # DB errors may include payload/credentials. Retry without rendering details.
                    log_event(
                        "telegram_sender_failed", level=logging.ERROR, error_code="runtime_error"
                    )
                    if once:
                        raise RuntimeError("Outbox sender iteration failed") from None
                    worked = False
                if once:
                    return
                await asyncio.sleep(0.1 if worked else 1)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver pending M1 Telegram interactive replies")
    parser.add_argument("--once", action="store_true", help="Process at most one available reply")
    args = parser.parse_args()
    try:
        asyncio.run(run(once=args.once))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception:
        raise SystemExit(
            "Outbox sender failed; check configuration and database availability"
        ) from None


if __name__ == "__main__":
    main()
