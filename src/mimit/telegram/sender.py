"""Interactive replies and recommendation outbox delivery.

Claims commit before HTTP; acknowledgements require the same unexpired lease.
Cancellation/crash leaves a lease for recovery. Telegram sendMessage has no
idempotency key: an accepted send followed by a crash/lost response can be sent
again after lease expiry. This is bounded at-least-once intent processing, not an
exactly-once external delivery guarantee.

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
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from pydantic import SecretStr
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock, SystemClock
from mimit.config import get_settings
from mimit.db.models import Consumable, Household, NotificationOutbox, RecommendationState
from mimit.db.session import create_engine, get_session_factory
from mimit.observability import configure_logging, elapsed_ms, log_event
from mimit.observability import install_log_redaction as install_log_redaction
from mimit.recommendations import RecommendationConfig, State, evaluate_item, notification_text
from mimit.telegram.text import valid_message

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
    dedupe_key: str = ""


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

    @property
    def allowed_chat_id(self) -> int:
        return self._allowed_chat_id

    async def send(self, payload: dict[str, Any]) -> SendResult:
        chat_id, text = payload.get("chat_id"), payload.get("text")
        if (
            set(payload) != {"chat_id", "text"}
            or type(chat_id) is not int
            or chat_id != self._allowed_chat_id
            or not valid_message(text)
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
    scope = or_(
        NotificationOutbox.dedupe_key.startswith("telegram:"),
        NotificationOutbox.dedupe_key.startswith("recommendation:"),
    )
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
            row.id,
            row.lease_owner,
            row.attempts,
            row.lease_expires_at,
            dict(row.payload) if isinstance(row.payload, dict) else {},
            row.dedupe_key,
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
    values: dict[str, Any] = {}
    async with sessions.begin() as session:
        row = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.id == claim.id).with_for_update()
        )
        # Read time after acquiring the lock: waiting cannot preserve an expired lease.
        now = clock.now()
        accepted = (
            row is not None
            and row.state == "sending"
            and row.lease_owner == claim.owner
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
        )
        if accepted:
            assert row is not None
            if result.outcome is Outcome.SENT:
                values = {"state": "sent", "delivered_at": now, "last_error": None}
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
            for key, value in values.items():
                setattr(row, key, value)
            row.lease_owner = None
            row.lease_expires_at = None
    log_event(
        "notification_acknowledged",
        notification_id=claim.id,
        attempt=claim.attempts,
        outcome=values["state"] if accepted else "stale",
        error_code=values.get("last_error"),
        acknowledged=accepted,
    )
    return accepted


class _ExpiredPreflight(Exception):
    pass


def _owns_live_claim(row: NotificationOutbox | None, claim: Claim, now: datetime) -> bool:
    return (
        row is not None
        and row.state == "sending"
        and row.lease_owner == claim.owner
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    )


async def preflight(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    claim: Claim,
    config: RecommendationConfig | None = None,
) -> dict[str, Any] | None:
    """Refresh actionable advice and fence cancellation before releasing locks for HTTP.

    Empty payloads become terminal invalid_payload failures. None means the claim
    was cancelled or expired. The household lock uses the same order as inventory
    and scheduled checks, so every current recommendation sees committed inputs.
    """
    recommendation = claim.dedupe_key.startswith("recommendation:")
    item_id: UUID | None = None
    generation: int | None = None
    if recommendation:
        parts = claim.dedupe_key.split(":")
        if len(parts) == 3 and re.fullmatch(r"[1-9][0-9]{0,9}", parts[2]):
            try:
                item_id, generation = UUID(parts[1]), int(parts[2])
            except ValueError:
                pass
        if item_id is not None and claim.dedupe_key != f"recommendation:{item_id}:{generation}":
            item_id = None
    try:
        async with sessions.begin() as session:
            item = None
            if recommendation and item_id is not None and config is not None:
                if config.allowed_chat_id is not None:
                    household_id = uuid5(
                        NAMESPACE_URL, f"mimit:telegram:chat:{config.allowed_chat_id}"
                    )
                    await session.scalar(
                        select(Household).where(Household.id == household_id).with_for_update()
                    )
                    item = await session.scalar(
                        select(Consumable)
                        .where(Consumable.id == item_id, Consumable.household_id == household_id)
                        .with_for_update()
                    )
            row = await session.scalar(
                select(NotificationOutbox)
                .where(NotificationOutbox.id == claim.id)
                .with_for_update()
            )
            if not _owns_live_claim(row, claim, clock.now()):
                return None
            assert row is not None
            payload = dict(row.payload) if isinstance(row.payload, dict) else {}
            if recommendation:
                if (
                    item is None
                    or config is None
                    or row.dedupe_key != claim.dedupe_key
                    or set(payload) != {"chat_id", "text"}
                    or type(payload.get("chat_id")) is not int
                    or payload["chat_id"] != config.allowed_chat_id
                ):
                    return {}
                if await session.get(RecommendationState, item.id) is None:
                    return {}
                decision = await evaluate_item(session, item, clock.now(), config)
                if clock.now() >= claim.expires_at:
                    raise _ExpiredPreflight
                current = await session.get(RecommendationState, item.id)
                assert current is not None
                if current.generation != generation or decision.state is State.OK:
                    row.state = "cancelled"
                    row.last_error = "recommendation_superseded"
                    row.lease_owner = None
                    row.lease_expires_at = None
                    return None
                payload = {
                    "chat_id": config.allowed_chat_id,
                    "text": notification_text(item, decision),
                }
                row.payload = payload
            # Roll back refreshes too if the lease expires during database work.
            if not _owns_live_claim(row, claim, clock.now()):
                raise _ExpiredPreflight
        return payload
    except _ExpiredPreflight:
        return None


async def send_once(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    sender: TelegramSender,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
    recommendation_config: RecommendationConfig | None = None,
) -> bool:
    claim = await claim_next(sessions, clock)
    if claim is None:
        return False
    log_event(
        "notification_claimed", notification_id=claim.id, attempt=claim.attempts, outcome="claimed"
    )
    if clock.now() >= claim.expires_at:
        return True  # Lease expired before I/O; recovery belongs to a later claim.
    if claim.dedupe_key.startswith("recommendation:") and recommendation_config is None:
        recommendation_config = RecommendationConfig(allowed_chat_id=sender.allowed_chat_id)
    payload = await preflight(sessions, clock, claim, recommendation_config)
    if payload is None:
        log_event("notification_send_result", notification_id=claim.id, outcome="superseded")
        return True
    if clock.now() >= claim.expires_at:
        return True
    started = perf_counter()
    try:
        result = await sender.send(payload)
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
                    worked = await send_once(
                        sessions,
                        clock,
                        sender,
                        recommendation_config=RecommendationConfig.from_settings(settings),
                    )
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
    parser = argparse.ArgumentParser(description="Deliver pending Telegram replies and advice")
    parser.add_argument("--once", action="store_true", help="Process at most one available message")
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
