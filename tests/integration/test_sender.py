"""Real migrated PostgreSQL for outbox races; every Telegram request is mocked."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mimit.clock import FrozenClock
from mimit.db.models import NotificationOutbox
from mimit.db.session import get_session_factory
from mimit.telegram.sender import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    ErrorCode,
    Outcome,
    SendResult,
    TelegramSender,
    acknowledge,
    claim_next,
    send_once,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
SUCCESS = {"ok": True, "result": {"message_id": 12, "chat": {"id": 17}}}


async def enqueue(sessions: async_sessionmaker[AsyncSession], *, key: str = "telegram:1:0") -> UUID:
    row_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            NotificationOutbox(
                id=row_id,
                dedupe_key=key,
                payload={"chat_id": 17, "text": "Saved"},
                run_at=NOW,
                created_at=NOW,
            )
        )
    return row_id


async def stored(sessions: async_sessionmaker[AsyncSession], row_id: UUID) -> NotificationOutbox:
    async with sessions() as session:
        row = await session.get(NotificationOutbox, row_id)
        assert row is not None
        return row


async def test_two_senders_cannot_claim_same_valid_lease(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    claims = await asyncio.gather(
        claim_next(sessions, FrozenClock(NOW)), claim_next(sessions, FrozenClock(NOW))
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    row = await stored(sessions, row_id)
    assert row.state == "sending" and row.attempts == 1
    assert row.lease_owner == winners[0].owner
    assert row.lease_expires_at == NOW + timedelta(seconds=LEASE_SECONDS)


@pytest.mark.parametrize("attempts", [0, MAX_ATTEMPTS])
async def test_locked_row_is_skipped_without_waiting(engine: AsyncEngine, attempts: int) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    async with sessions.begin() as session:
        row = await session.get(NotificationOutbox, row_id)
        assert row is not None
        row.attempts = attempts
    async with sessions.begin() as session:
        await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.id == row_id).with_for_update()
        )
        assert await asyncio.wait_for(claim_next(sessions, FrozenClock(NOW)), 2) is None
    assert (await stored(sessions, row_id)).state == "pending"


async def test_restart_recovers_expired_lease_and_fences_old_ack(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    original = await claim_next(sessions, FrozenClock(NOW))
    assert original is not None
    assert await claim_next(sessions, FrozenClock(NOW + timedelta(seconds=59))) is None
    # Even before another owner claims it, an expired lease may no longer acknowledge.
    later = FrozenClock(NOW + timedelta(seconds=LEASE_SECONDS))
    assert not await acknowledge(sessions, later, original, SendResult(Outcome.SENT))
    restarted = await claim_next(get_session_factory(engine), later)
    assert restarted is not None and restarted.id == row_id
    assert restarted.owner != original.owner and restarted.attempts == 2
    assert not await acknowledge(sessions, later, original, SendResult(Outcome.SENT))
    assert not await acknowledge(
        sessions, later, original, SendResult(Outcome.RETRY, ErrorCode.TRANSPORT)
    )
    assert (await stored(sessions, row_id)).lease_owner == restarted.owner
    assert await acknowledge(sessions, later, restarted, SendResult(Outcome.SENT))
    row = await stored(sessions, row_id)
    assert row.state == "sent" and row.delivered_at == later.now()
    assert row.lease_owner is None and row.lease_expires_at is None


async def test_http_happens_after_claim_commit_and_retry_survives_restart(
    engine: AsyncEngine,
) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)

    async def rate_limited(request: httpx.Request) -> httpx.Response:
        # A separate connection sees committed state and takes a NOWAIT row lock.
        async with sessions.begin() as session:
            row = await session.scalar(
                select(NotificationOutbox)
                .where(NotificationOutbox.id == row_id)
                .with_for_update(nowait=True)
            )
            assert row is not None and row.state == "sending" and row.attempts == 1
        return httpx.Response(
            429, json={"ok": False, "parameters": {"retry_after": 120}, "description": "secret"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(rate_limited)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(sessions, FrozenClock(NOW), sender)
    row = await stored(sessions, row_id)
    assert row.state == "pending" and row.attempts == 1
    assert row.run_at == NOW + timedelta(seconds=120)
    assert row.last_error == ErrorCode.RATE_LIMIT.value
    assert row.lease_owner is None and row.delivered_at is None
    assert await claim_next(sessions, FrozenClock(NOW + timedelta(seconds=119))) is None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=SUCCESS))
    ) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(
            get_session_factory(engine), FrozenClock(NOW + timedelta(seconds=120)), sender
        )
    row = await stored(sessions, row_id)
    assert row.state == "sent" and row.attempts == 2 and row.last_error is None


@pytest.mark.parametrize("jitter_fraction", [0.0, 1.0])
async def test_retries_are_bounded_and_terminal_rows_cannot_be_reclaimed(
    engine: AsyncEngine,
    jitter_fraction: float,
) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    current = NOW
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"ok": False}))
    ) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            assert await send_once(
                sessions,
                FrozenClock(current),
                sender,
                jitter=lambda low, high: high * jitter_fraction,
            )
            row = await stored(sessions, row_id)
            assert row.attempts == attempt
            if attempt < MAX_ATTEMPTS:
                assert row.state == "pending"
                assert row.run_at == current + timedelta(
                    seconds=5 * 2 ** (attempt - 1) * (1 + jitter_fraction)
                )
                current = row.run_at
            else:
                assert row.state == "failed" and row.last_error == ErrorCode.EXHAUSTED.value
                assert row.delivered_at is None
        assert not await send_once(sessions, FrozenClock(current + timedelta(days=1)), sender)


async def test_permanent_error_is_durably_terminal(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"ok": False}))
    ) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        await send_once(sessions, FrozenClock(NOW), sender)
    row = await stored(sessions, row_id)
    assert row.state == "failed" and row.attempts == 1
    assert row.last_error == ErrorCode.PERMANENT.value
    assert await claim_next(sessions, FrozenClock(NOW + timedelta(days=1))) is None


async def test_cancelled_http_leaves_recoverable_lease(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)

    def cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        with pytest.raises(asyncio.CancelledError):
            await send_once(sessions, FrozenClock(NOW), sender)
    row = await stored(sessions, row_id)
    assert row.state == "sending" and row.attempts == 1 and row.delivered_at is None
    assert (
        await claim_next(sessions, FrozenClock(NOW + timedelta(seconds=LEASE_SECONDS))) is not None
    )


async def test_final_attempt_crash_settles_without_an_extra_send(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    async with sessions.begin() as session:
        row = await session.get(NotificationOutbox, row_id)
        assert row is not None
        row.state, row.attempts = "sending", MAX_ATTEMPTS
        row.lease_owner, row.lease_expires_at = str(uuid4()), NOW
    assert await claim_next(sessions, FrozenClock(NOW)) is None
    row = await stored(sessions, row_id)
    assert row.state == "failed" and row.attempts == MAX_ATTEMPTS
    assert row.last_error == ErrorCode.EXHAUSTED.value and row.lease_owner is None


async def test_recommendation_outbox_is_deferred_to_m4(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions, key="recommendation:one")
    assert await claim_next(sessions, FrozenClock(NOW)) is None
    assert (await stored(sessions, row_id)).state == "pending"
