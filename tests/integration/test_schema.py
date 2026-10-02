"""Exercise the migrated PostgreSQL schema, not an in-memory substitute."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine

from mimit.db.models import (
    Base,
    Consumable,
    Household,
    NotificationOutbox,
    OfferSource,
    PriceObservation,
    Purchase,
    ScheduledJob,
    TelegramUpdateReceipt,
)
from mimit.db.session import get_session_factory

from .conftest import migrate

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def test_migration_round_trip_and_metadata(database_url: str) -> None:
    migrate(database_url, "check")
    migrate(database_url, "downgrade", "base")
    migrate(database_url, "upgrade", "head")
    migrate(database_url, "check")


async def test_all_boundaries_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.scalar(text("SELECT version()"))).startswith("PostgreSQL")
        tables = set(
            await conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        )
        assert set(Base.metadata.tables) <= tables


async def test_receipt_and_domain_intent_rollback_together(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    with pytest.raises(RuntimeError, match="simulated crash"):
        async with sessions.begin() as session:
            session.add(TelegramUpdateReceipt(update_id=77, received_at=NOW))
            session.add(Household(name="Home", created_at=NOW))
            session.add(
                NotificationOutbox(
                    dedupe_key="telegram:77:reply",
                    payload={"text": "saved"},
                    run_at=NOW,
                    created_at=NOW,
                )
            )
            await session.flush()
            raise RuntimeError("simulated crash")
    async with sessions() as session:
        for model in [TelegramUpdateReceipt, Household, NotificationOutbox]:
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    # A retry can commit after rollback; receipt wasn't left behind.
    async with sessions.begin() as session:
        session.add(TelegramUpdateReceipt(update_id=77, received_at=NOW))


async def test_concurrent_receipt_has_one_committed_winner(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def first() -> bool:
        async with sessions.begin() as session:
            session.add(TelegramUpdateReceipt(update_id=91, received_at=NOW))
            await session.flush()
            ready.set()
            await release.wait()
        return True

    async def second() -> bool:
        await ready.wait()
        try:
            async with sessions.begin() as session:
                session.add(TelegramUpdateReceipt(update_id=91, received_at=NOW))
                await session.flush()
            return True
        except IntegrityError:
            return False

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]

    async def wait_for_database_contention() -> None:
        await ready.wait()
        async with engine.connect() as connection:
            while not await connection.scalar(  # noqa: ASYNC110 - observe a PostgreSQL lock, not a task
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query LIKE 'INSERT INTO telegram_update_receipt%')"
                )
            ):
                await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_database_contention(), timeout=10)
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    assert results == [True, False]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 1


async def test_foreign_key_and_naive_time_are_rejected(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                OfferSource(
                    consumable_id=uuid4(),
                    url="https://www.zooplus.pl/example?activeVariant=1.0",
                    created_at=NOW,
                )
            )
    with pytest.raises(StatementError, match="timezone-aware"):
        async with sessions.begin() as session:
            session.add(Household(name="Home", created_at=datetime(2026, 10, 2)))


@pytest.mark.parametrize("model", [ScheduledJob, NotificationOutbox])
async def test_durable_intent_identity_is_unique(engine: AsyncEngine, model: type[Base]) -> None:
    sessions = get_session_factory(engine)
    kwargs = {"job_type": "price_check"} if model is ScheduledJob else {}
    async with sessions.begin() as session:
        session.add(model(dedupe_key="one", payload={}, run_at=NOW, created_at=NOW, **kwargs))
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(model(dedupe_key="one", payload={}, run_at=NOW, created_at=NOW, **kwargs))


async def test_job_lease_state_is_consistent(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                ScheduledJob(
                    dedupe_key="invalid-lease",
                    job_type="price_check",
                    payload={},
                    run_at=NOW,
                    created_at=NOW,
                    state="running",
                )
            )


async def test_inventory_price_and_purchase_round_trip(engine: AsyncEngine) -> None:
    sessions = get_session_factory(engine)
    household_id, consumable_id, source_id = uuid4(), uuid4(), uuid4()
    original_url = "https://www.zooplus.pl/shop/2333304?activeVariant=2333304.0"
    async with sessions.begin() as session:
        session.add(Household(id=household_id, name="Home", created_at=NOW))
        await session.flush()
        session.add(
            Consumable(
                id=consumable_id,
                household_id=household_id,
                name="Cat food",
                stock_quantity=Decimal("12.5"),
                canonical_unit="pouch",
                daily_consumption=Decimal("2"),
                reserve_days=3,
                stock_updated_at=NOW,
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            OfferSource(
                id=source_id,
                consumable_id=consumable_id,
                url=original_url,
                variant="2333304.0",
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            PriceObservation(
                offer_source_id=source_id,
                observed_at=NOW,
                price=Decimal("42.96"),
                currency="PLN",
                unit_price=Decimal("84.24"),
                unit="kg",
                availability="available",
            )
        )
        session.add(Purchase(consumable_id=consumable_id, quantity=Decimal("6"), purchased_at=NOW))
    async with sessions() as session:
        stored = await session.get(Consumable, consumable_id)
        assert stored is not None
        assert stored.stock_quantity == Decimal("12.5")
        assert stored.stock_updated_at == NOW
        source = await session.get(OfferSource, source_id)
        assert source is not None and source.url == original_url and source.variant == "2333304.0"
        observation = (await session.scalars(select(PriceObservation))).one()
        assert observation.price == Decimal("42.96")
        assert observation.observed_at.tzinfo is UTC
        assert (await session.scalars(select(Purchase.quantity))).one() == Decimal("6")
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(OfferSource(consumable_id=consumable_id, url=original_url, created_at=NOW))
    for invalid in [Decimal("-1"), Decimal("NaN")]:
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                item = await session.get(Consumable, consumable_id)
                assert item is not None
                item.stock_quantity = invalid
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                PriceObservation(
                    offer_source_id=source_id,
                    observed_at=NOW,
                    unit_price=Decimal("1"),
                    currency="PLN",
                    unit=None,
                    availability="available",
                )
            )


async def test_price_history_rejects_update_and_delete(engine: AsyncEngine) -> None:
    # Raw SQL demonstrates the guarantee lives in the migrated DB, not only the ORM.
    async with engine.begin() as connection:
        household_id, item_id, source_id = uuid4(), uuid4(), uuid4()
        await connection.execute(
            insert(Household).values(
                id=household_id,
                name="Home",
                created_at=NOW,
            )
        )
        await connection.execute(
            insert(Consumable).values(
                id=item_id,
                household_id=household_id,
                name="Food",
                created_at=NOW,
                stock_quantity=12,
                canonical_unit="pouch",
                daily_consumption=2,
                reserve_days=3,
                stock_updated_at=NOW,
            )
        )
        await connection.execute(
            insert(OfferSource).values(
                id=source_id,
                consumable_id=item_id,
                url="https://example.com",
                created_at=NOW,
            )
        )
        await connection.execute(
            insert(PriceObservation).values(
                offer_source_id=source_id,
                observed_at=NOW,
                availability="unknown",
            )
        )
    for command in [
        "UPDATE price_observation SET availability = 'available'",
        "DELETE FROM price_observation",
    ]:
        with pytest.raises(IntegrityError, match="append-only"):
            async with engine.begin() as connection:
                await connection.execute(text(command))
