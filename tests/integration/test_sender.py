"""Real migrated PostgreSQL for outbox races; every Telegram request is mocked."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mimit.clock import FrozenClock
from mimit.db.models import (
    Consumable,
    Household,
    NotificationOutbox,
    OfferSource,
    PriceObservation,
    RecommendationState,
)
from mimit.db.session import get_session_factory
from mimit.recommendations import Decision, RecommendationConfig, evaluate_item
from mimit.telegram import sender as delivery
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

from .test_recommendations import NOW as RECOMMENDATION_NOW
from .test_recommendations import locked_item, seed

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


async def test_recommendation_outbox_is_claimed_for_delivery(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions, key="recommendation:one")
    claim = await claim_next(sessions, FrozenClock(NOW))
    assert claim is not None and claim.id == row_id
    assert (await stored(sessions, row_id)).state == "sending"


async def discounted_advice(
    sessions: async_sessionmaker[AsyncSession], *, stock: str, age_hours: int = 47
) -> tuple[UUID, UUID]:
    item_id = await seed(sessions, stock=stock)
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        source = await session.scalar(
            select(OfferSource).where(OfferSource.consumable_id == item_id)
        )
        assert source is not None
        for index in range(4):
            session.add(
                PriceObservation(
                    offer_source_id=source.id,
                    observed_at=RECOMMENDATION_NOW - timedelta(hours=age_hours + index * 24),
                    unit_price=Decimal(90 if index == 0 else 100),
                    currency="PLN",
                    unit="kg",
                    variant_snapshot=source.variant,
                    availability="available",
                )
            )
        await evaluate_item(
            session, item, RECOMMENDATION_NOW, RecommendationConfig(allowed_chat_id=17)
        )
        row = await session.scalar(select(NotificationOutbox))
        assert row is not None
        return item_id, row.id


@pytest.mark.parametrize("maximum_age", [24, 48])
async def test_delayed_discount_advice_is_recomputed_before_delivery(
    engine: AsyncEngine, maximum_age: int
) -> None:
    sessions = get_session_factory(engine)
    item_id, old_id = await discounted_advice(sessions, stock="4", age_hours=maximum_age - 1)
    delivered: list[str] = []
    clock = FrozenClock(RECOMMENDATION_NOW + timedelta(hours=2))
    config = RecommendationConfig(allowed_chat_id=17, maximum_price_age_hours=maximum_age)

    async def receive(request: httpx.Request) -> httpx.Response:
        # Recommendation/domain/outbox locks are all released before Telegram I/O.
        async with sessions.begin() as session:
            assert await session.scalar(select(Household).with_for_update(nowait=True))
            assert await session.scalar(select(Consumable).with_for_update(nowait=True))
            rows = list(
                await session.scalars(select(NotificationOutbox).with_for_update(nowait=True))
            )
            assert [row.state for row in rows].count("sending") == 1
        delivered.append(json.loads(request.content)["text"])
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(sessions, clock, sender, recommendation_config=config)
        assert delivered == []
        assert (await stored(sessions, old_id)).state == "cancelled"
        assert await send_once(sessions, clock, sender, recommendation_config=config)
        assert len(delivered) == 1
        assert "BUY SOON" in delivered[0] and "price is stale" in delivered[0]
        assert "below the recent median" not in delivered[0]
        assert not await send_once(sessions, clock, sender, recommendation_config=config)
    async with sessions() as session:
        state = await session.get(RecommendationState, item_id)
        assert state is not None and state.state == "BUY SOON" and state.generation == 2


async def test_same_state_refreshes_rationale_on_claimed_advice(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    _, row_id = await discounted_advice(sessions, stock="2")
    original = await stored(sessions, row_id)
    assert "Stock only" not in original.payload["text"]
    delivered: list[str] = []

    def receive(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content)["text"])
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(
            sessions, FrozenClock(RECOMMENDATION_NOW + timedelta(hours=2)), sender
        )
    assert len(delivered) == 1 and "BUY NOW" in delivered[0] and "price is stale" in delivered[0]
    assert (await stored(sessions, row_id)).state == "sent"
    async with sessions() as session:
        assert len(list(await session.scalars(select(NotificationOutbox)))) == 1


async def test_stock_recovery_between_claim_and_preflight_cancels_advice(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = get_session_factory(engine)
    item_id, row_id = await discounted_advice(sessions, stock="4")
    original_claim = delivery.claim_next

    async def claim_then_purchase(
        factory: async_sessionmaker[AsyncSession], clock: FrozenClock
    ) -> delivery.Claim | None:
        claim = await original_claim(factory, clock)
        async with factory.begin() as session:
            item = await locked_item(session, item_id)
            item.stock_quantity = Decimal(100)
            await evaluate_item(
                session, item, clock.now(), RecommendationConfig(allowed_chat_id=17)
            )
        return claim

    monkeypatch.setattr(delivery, "claim_next", claim_then_purchase)

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("superseded advice must never reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(sessions, FrozenClock(RECOMMENDATION_NOW), sender)
    assert (await stored(sessions, row_id)).state == "cancelled"


@pytest.mark.parametrize("key", ["recommendation:one", f"recommendation:{uuid4()}:1"])
async def test_malformed_or_missing_recommendation_identity_is_terminal(
    engine: AsyncEngine, key: str
) -> None:
    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions, key=key)

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid recommendation must never reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(sessions, FrozenClock(NOW), sender)
        assert not await send_once(sessions, FrozenClock(NOW + timedelta(days=1)), sender)
    row = await stored(sessions, row_id)
    assert row.state == "failed" and row.last_error == "invalid_payload"


@pytest.mark.parametrize("invalid", ["other_household", "missing_state"])
async def test_recommendation_preflight_requires_ownership_and_persisted_state(
    engine: AsyncEngine, invalid: str
) -> None:
    sessions = get_session_factory(engine)
    item_id, row_id = await discounted_advice(sessions, stock="4")
    async with sessions.begin() as session:
        if invalid == "other_household":
            household_id = uuid5(NAMESPACE_URL, "mimit:telegram:chat:99")
            session.add(Household(id=household_id, name="Other", created_at=RECOMMENDATION_NOW))
            await session.flush()
            item = await session.get(Consumable, item_id)
            assert item is not None
            item.household_id = household_id
        else:
            state = await session.get(RecommendationState, item_id)
            assert state is not None
            await session.delete(state)

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("unowned or orphaned advice must never reach Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(sessions, FrozenClock(RECOMMENDATION_NOW), sender)
    row = await stored(sessions, row_id)
    assert row.state == "failed" and row.last_error == "invalid_payload"


@pytest.mark.parametrize("payload", [[], ["invalid"], None, "invalid"])
async def test_non_object_outbox_payload_fails_without_blocking_other_messages(
    engine: AsyncEngine, payload: object
) -> None:
    sessions = get_session_factory(engine)
    invalid_id = await enqueue(sessions)
    valid_id = await enqueue(sessions, key="telegram:2:0")
    async with sessions.begin() as session:
        await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id == invalid_id)
            .values(payload=payload)
        )
        await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id == valid_id)
            .values(run_at=NOW + timedelta(seconds=1))
        )
    delivered = 0

    def receive(request: httpx.Request) -> httpx.Response:
        nonlocal delivered
        delivered += 1
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        sender = TelegramSender(
            token=SecretStr("123:test_secret"), allowed_chat_id=17, client=client
        )
        assert await send_once(sessions, FrozenClock(NOW + timedelta(seconds=1)), sender)
        assert (await stored(sessions, invalid_id)).last_error == "invalid_payload"
        assert await send_once(sessions, FrozenClock(NOW + timedelta(seconds=1)), sender)
    assert delivered == 1
    assert (await stored(sessions, invalid_id)).state == "failed"
    assert (await stored(sessions, valid_id)).state == "sent"


async def test_acknowledgement_waiting_for_lock_cannot_outlive_lease(engine: AsyncEngine) -> None:
    @dataclass
    class MutableClock:
        instant: datetime = NOW

        def now(self) -> datetime:
            return self.instant

    sessions = get_session_factory(engine)
    row_id = await enqueue(sessions)
    clock = MutableClock()
    claim = await claim_next(sessions, clock)
    assert claim is not None
    async with sessions.begin() as session:
        await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.id == row_id).with_for_update()
        )
        task = asyncio.create_task(acknowledge(sessions, clock, claim, SendResult(Outcome.SENT)))
        async with asyncio.timeout(3):
            async with engine.connect() as connection:
                # PostgreSQL exposes lock waits by polling, not an asyncio event.
                while not await connection.scalar(  # noqa: ASYNC110
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query LIKE '%notification_outbox%')"
                    )
                ):
                    await asyncio.sleep(0.01)
        clock.instant += timedelta(seconds=LEASE_SECONDS)
    assert not await asyncio.wait_for(task, 3)
    row = await stored(sessions, row_id)
    assert row.state == "sending" and row.delivered_at is None


@pytest.mark.parametrize("stock", ["4", "2"])
async def test_lease_expiry_during_preflight_rolls_back_recommendation_writes(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, stock: str
) -> None:
    @dataclass
    class MutableClock:
        instant: datetime = RECOMMENDATION_NOW + timedelta(hours=2)

        def now(self) -> datetime:
            return self.instant

    sessions = get_session_factory(engine)
    item_id, row_id = await discounted_advice(sessions, stock=stock)
    original_row = await stored(sessions, row_id)
    async with sessions() as session:
        original_state = await session.get(RecommendationState, item_id)
        assert original_state is not None
        original_values = (
            original_state.state,
            original_state.generation,
            original_state.reason,
            original_state.evaluated_at,
            original_state.changed_at,
        )
    clock = MutableClock()
    claim = await claim_next(sessions, clock)
    assert claim is not None and claim.id == row_id
    original_evaluate = evaluate_item
    evaluated = False

    async def evaluate_then_expire(
        session: AsyncSession, item: Consumable, now: datetime, config: RecommendationConfig
    ) -> Decision:
        nonlocal evaluated
        decision = await original_evaluate(session, item, now, config)
        # Flush even same-state reason/timestamp changes, proving rollback undoes
        # executed writes rather than only discarding an unchanged ORM object.
        await session.flush()
        current = await session.get(RecommendationState, item.id)
        assert current is not None and "price is stale" in current.reason
        rows = list(await session.scalars(select(NotificationOutbox)))
        if stock == "4":
            assert current.generation == original_values[1] + 1
            assert sorted(row.state for row in rows) == ["cancelled", "pending"]
        else:
            assert current.generation == original_values[1]
            assert len(rows) == 1 and rows[0].state == "sending"
        evaluated = True
        clock.instant = claim.expires_at
        return decision

    monkeypatch.setattr(delivery, "evaluate_item", evaluate_then_expire)
    assert (
        await delivery.preflight(sessions, clock, claim, RecommendationConfig(allowed_chat_id=17))
        is None
    )
    assert evaluated
    async with sessions() as session:
        state = await session.get(RecommendationState, item_id)
        assert state is not None
        assert (
            state.state,
            state.generation,
            state.reason,
            state.evaluated_at,
            state.changed_at,
        ) == original_values
        rows = list(await session.scalars(select(NotificationOutbox)))
        assert len(rows) == 1
        row = rows[0]
        assert row.id == row_id and row.payload == original_row.payload
        assert row.state == "sending" and row.attempts == 1
        assert row.lease_owner == claim.owner and row.lease_expires_at == claim.expires_at
        assert row.last_error is None and row.delivered_at is None
