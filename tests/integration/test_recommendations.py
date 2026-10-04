import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mimit.db.models import (
    Consumable,
    Household,
    NotificationOutbox,
    OfferSource,
    PriceObservation,
    RecommendationState,
)
from mimit.db.session import get_session_factory
from mimit.recommendations import (
    RecommendationConfig,
    State,
    evaluate_item,
    recommendation_for_item,
)
from mimit.telegram.service import household_id_for_chat

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
CONFIG = RecommendationConfig(allowed_chat_id=17)


async def seed(sessions: async_sessionmaker[AsyncSession], *, stock: str = "4") -> UUID:
    item_id = uuid4()
    async with sessions.begin() as session:
        session.add(Household(id=household_id_for_chat(17), name="Home", created_at=NOW))
        await session.flush()
        session.add(
            Consumable(
                id=item_id,
                household_id=household_id_for_chat(17),
                name="Food",
                stock_quantity=Decimal(stock),
                canonical_unit="pouch",
                daily_consumption=Decimal(1),
                reserve_days=3,
                stock_updated_at=NOW,
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            OfferSource(
                id=uuid4(),
                consumable_id=item_id,
                url="https://example.com/food",
                variant="one",
                created_at=NOW,
            )
        )
    return item_id


async def locked_item(session: AsyncSession, item_id: UUID) -> Consumable:
    await session.scalar(
        select(Household).where(Household.id == household_id_for_chat(17)).with_for_update()
    )
    item = await session.get(Consumable, item_id)
    assert item is not None
    return item


async def test_quiet_transitions_reentry_and_supersession(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        assert (await evaluate_item(session, item, NOW, CONFIG)).state is State.BUY_SOON
        assert (await evaluate_item(session, item, NOW, CONFIG)).state is State.BUY_SOON
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        assert (
            await evaluate_item(session, item, NOW + timedelta(days=1), CONFIG)
        ).state is State.BUY_NOW
        item.stock_quantity = Decimal(100)
        item.stock_updated_at = NOW + timedelta(days=1)
        assert (
            await evaluate_item(session, item, NOW + timedelta(days=1), CONFIG)
        ).state is State.OK
        item.stock_quantity = Decimal(2)
        assert (
            await evaluate_item(session, item, NOW + timedelta(days=1), CONFIG)
        ).state is State.BUY_NOW
    async with sessions() as session:
        rows = list(
            await session.scalars(
                select(NotificationOutbox).order_by(NotificationOutbox.dedupe_key)
            )
        )
        assert len(rows) == 3
        assert [row.state for row in rows] == ["cancelled", "cancelled", "pending"]
        assert len({row.dedupe_key for row in rows}) == 3
        state = await session.get(RecommendationState, item_id)
        assert state is not None and state.generation == 4


async def test_observation_decision_and_outbox_rollback_together(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)
    with pytest.raises(RuntimeError, match="abort"):
        async with sessions.begin() as session:
            item = await locked_item(session, item_id)
            source = await session.scalar(
                select(OfferSource).where(OfferSource.consumable_id == item_id)
            )
            assert source is not None
            session.add(
                PriceObservation(
                    offer_source_id=source.id,
                    observed_at=NOW,
                    availability="unknown",
                    outcome="failed",
                    error_code="timeout",
                )
            )
            await evaluate_item(session, item, NOW, CONFIG)
            raise RuntimeError("abort")
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RecommendationState)) == 0
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 0


async def test_concurrent_evaluations_create_one_intent(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)

    async def evaluate() -> None:
        async with sessions.begin() as session:
            item = await locked_item(session, item_id)
            await evaluate_item(session, item, NOW, CONFIG)

    await asyncio.gather(evaluate(), evaluate())
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 1


async def test_just_appended_price_participates_and_failure_preserves_history(
    engine: AsyncEngine,
) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        source = await session.scalar(
            select(OfferSource).where(OfferSource.consumable_id == item_id)
        )
        assert source is not None
        for day, price in [(3, "100"), (2, "100"), (1, "100"), (0, "90")]:
            session.add(
                PriceObservation(
                    offer_source_id=source.id,
                    observed_at=NOW - timedelta(days=day),
                    unit_price=Decimal(price),
                    currency="PLN",
                    unit="kg",
                    variant_snapshot="one",
                    availability="available",
                )
            )
        assert (await evaluate_item(session, item, NOW, CONFIG)).state is State.BUY_NOW
        session.add(
            PriceObservation(
                offer_source_id=source.id,
                observed_at=NOW + timedelta(seconds=1),
                availability="unknown",
                outcome="failed",
                error_code="timeout",
                variant_snapshot="one",
            )
        )
        decision = await evaluate_item(session, item, NOW + timedelta(seconds=1), CONFIG)
        assert decision.state is State.BUY_SOON
        assert "latest price check failed" in decision.reason
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 5


@pytest.mark.parametrize("chat", [None, 999])
async def test_no_delivery_to_unconfigured_or_other_household(
    engine: AsyncEngine, chat: int | None
) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        await evaluate_item(session, item, NOW, RecommendationConfig(allowed_chat_id=chat))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


async def test_read_only_display_does_not_enqueue_and_old_price_is_stale(
    engine: AsyncEngine,
) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)
    async with sessions.begin() as session:
        source = await session.scalar(
            select(OfferSource).where(OfferSource.consumable_id == item_id)
        )
        assert source is not None
        session.add(
            PriceObservation(
                offer_source_id=source.id,
                observed_at=NOW - timedelta(days=31),
                unit_price=Decimal(90),
                currency="PLN",
                unit="kg",
                variant_snapshot="one",
                availability="available",
            )
        )
    async with sessions() as session:
        item = await session.get(Consumable, item_id)
        assert item is not None
        decision = await recommendation_for_item(session, item, NOW, CONFIG)
        assert "price is stale" in decision.reason
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert await session.get(RecommendationState, item_id) is None


async def test_unicode_message_bounded_and_superseded_lease_fenced(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions)
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        item.name = "🐈" * 10000
        await evaluate_item(session, item, NOW, CONFIG)
        row = await session.scalar(select(NotificationOutbox))
        assert row is not None
        assert len(row.payload["text"].encode("utf-16-le")) // 2 <= 4096
        row.state, row.lease_owner, row.lease_expires_at = (
            "sending",
            "old",
            NOW + timedelta(seconds=60),
        )
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        item.stock_quantity = Decimal(100)
        await evaluate_item(session, item, NOW, CONFIG)
    async with sessions() as session:
        row = await session.scalar(select(NotificationOutbox))
        assert row is not None and row.state == "cancelled"
        assert row.lease_owner is None and row.lease_expires_at is None


async def test_unchanged_state_refreshes_pending_reason_without_new_alert(
    engine: AsyncEngine,
) -> None:
    sessions = get_session_factory(engine)
    item_id = await seed(sessions, stock="2")
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        await evaluate_item(session, item, NOW, CONFIG)
    async with sessions.begin() as session:
        item = await locked_item(session, item_id)
        source = await session.scalar(
            select(OfferSource).where(OfferSource.consumable_id == item_id)
        )
        assert source is not None
        session.add(
            PriceObservation(
                offer_source_id=source.id,
                observed_at=NOW,
                outcome="failed",
                error_code="timeout",
                availability="unknown",
            )
        )
        await evaluate_item(session, item, NOW, CONFIG)
    async with sessions() as session:
        rows = list(await session.scalars(select(NotificationOutbox)))
        assert len(rows) == 1
        assert "latest price check failed" in rows[0].payload["text"]
