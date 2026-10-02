"""Price checks and immutable outcomes verified against migrated real PostgreSQL."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from mimit.clock import FrozenClock
from mimit.db.models import Consumable, Household, OfferSource, PriceObservation
from mimit.db.session import get_session_factory
from mimit.products.service import PriceCheckServiceError, ProductCheckService
from mimit.products.types import ErrorCode, ExtractedProduct, FetchedPage, ProductCheckError

from .conftest import migrate

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
URL = "https://www.zooplus.pl/shop/cats/food?activeVariant=2333304.0&keep=EXACT#offer"
VARIANT = "2333304.0"
PRODUCT = ExtractedProduct(
    name="Cat food 6 x 85g",
    variant=VARIANT,
    price=Decimal("42.96"),
    currency="PLN",
    unit_price=Decimal("84.24"),
    unit="kg",
    availability="available",
    metadata={"source": "jsonld", "offer_type": "one_time", "raw_body": "SECRET"},
)


class Fetcher:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        hook: Callable[[], Awaitable[None]] | None = None,
        final_url: str | None = None,
    ) -> None:
        self.error = error
        self.final_url = final_url
        self.hook = hook
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        if self.hook is not None:
            await self.hook()
        if self.error is not None:
            raise self.error
        return FetchedPage("<html />", self.final_url or url, 200, 8, "a" * 64)


class Extractor:
    def __init__(self, product: ExtractedProduct = PRODUCT, error: Exception | None = None) -> None:
        self.product = product
        self.error = error
        self.inputs: list[tuple[str, str, str | None]] = []

    def extract(self, html: str, url: str, variant: str | None = None) -> ExtractedProduct:
        self.inputs.append((html, url, variant))
        if self.error is not None:
            raise self.error
        return self.product


async def seed(engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    household_id, item_id, source_id = uuid4(), uuid4(), uuid4()
    sessions = get_session_factory(engine)
    async with sessions.begin() as session:
        session.add(Household(id=household_id, name="Home", created_at=NOW))
        await session.flush()
        session.add(
            Consumable(
                id=item_id,
                household_id=household_id,
                name="Cat food",
                stock_quantity=Decimal("12"),
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
                consumable_id=item_id,
                url=URL,
                variant=VARIANT,
                created_at=NOW,
            )
        )
    return household_id, item_id, source_id


def service(
    engine: AsyncEngine,
    fetcher: Fetcher | None = None,
    extractor: Extractor | None = None,
) -> ProductCheckService:
    return ProductCheckService(
        get_session_factory(engine),
        FrozenClock(NOW),
        fetcher or Fetcher(),
        extractor or Extractor(),
    )


async def observations(engine: AsyncEngine) -> list[PriceObservation]:
    async with get_session_factory(engine)() as session:
        return list(await session.scalars(select(PriceObservation)))


async def test_exact_source_product_money_and_utc_round_trip(engine: AsyncEngine) -> None:
    household_id, item_id, source_id = await seed(engine)
    fetcher, extractor = Fetcher(), Extractor()
    result = await service(engine, fetcher, extractor).check(item_id, household_id=household_id)
    assert fetcher.urls == [URL]
    assert extractor.inputs == [("<html />", URL, VARIANT)]
    assert result.offer_source_id == source_id
    assert result.price == Decimal("42.96") and result.observed_at == NOW
    (row,) = await observations(engine)
    assert row.id == result.observation_id and row.outcome == "success"
    assert row.product_name == PRODUCT.name and row.variant_snapshot == VARIANT
    assert row.unit_price == Decimal("84.24") and row.currency == "PLN"
    assert row.observed_at.tzinfo is UTC
    assert row.extraction_metadata["source"] == "jsonld"
    assert "raw_body" not in row.extraction_metadata
    async with get_session_factory(engine)() as session:
        source = await session.get(OfferSource, source_id)
        assert source is not None and source.url == URL and source.variant == VARIANT


@pytest.mark.parametrize("stage", ["fetch", "extract"])
async def test_failures_are_distinct_and_keep_last_good_price(
    engine: AsyncEngine, stage: str
) -> None:
    _, item_id, _ = await seed(engine)
    good = await service(engine).check(item_id)
    error = ProductCheckError(ErrorCode.HTTP_ERROR, http_status=503)
    bad = await service(
        engine,
        Fetcher(error=error) if stage == "fetch" else Fetcher(),
        Extractor(error=error) if stage == "extract" else Extractor(),
    ).check(item_id)
    assert bad.outcome == "failed" and bad.error_code == ErrorCode.HTTP_ERROR
    assert bad.availability == "unknown" and bad.price is None
    rows = await observations(engine)
    assert len(rows) == 2
    stored_good = next(row for row in rows if row.id == good.observation_id)
    assert stored_good.price == Decimal("42.96") and stored_good.outcome == "success"
    stored_bad = next(row for row in rows if row.id == bad.observation_id)
    assert stored_bad.error_code == "http_error" and stored_bad.extraction_metadata == {
        "http_status": 503,
    }


async def test_unavailable_is_successful_merchant_state(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    product = replace(
        PRODUCT, price=None, currency=None, unit_price=None, unit=None, availability="unavailable"
    )
    result = await service(engine, extractor=Extractor(product)).check(item_id)
    assert result.outcome == "success" and result.availability == "unavailable"
    assert result.error_code is None and result.price is None


@pytest.mark.parametrize(
    "product,code",
    [
        (replace(PRODUCT, variant="different"), ErrorCode.IDENTITY_MISMATCH),
        (replace(PRODUCT, price=Decimal("NaN")), ErrorCode.INVALID_PRICE),
        (replace(PRODUCT, currency="pln"), ErrorCode.INVALID_PRICE),
        (replace(PRODUCT, name="unsafe\nname"), ErrorCode.INVALID_PRODUCT),
        (replace(PRODUCT, price=Decimal("1.0000001")), ErrorCode.INVALID_PRICE),
    ],
)
async def test_injected_extractor_validated_before_append(
    engine: AsyncEngine,
    product: ExtractedProduct,
    code: ErrorCode,
) -> None:
    _, item_id, _ = await seed(engine)
    result = await service(engine, extractor=Extractor(product)).check(item_id)
    assert result.outcome == "failed" and result.error_code == code
    (row,) = await observations(engine)
    assert row.price is None and row.product_name is None


async def test_safe_unexpected_external_error(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    result = await service(engine, Fetcher(error=RuntimeError("SECRET URL credentials"))).check(
        item_id
    )
    assert result.error_code == ErrorCode.TRANSPORT_ERROR
    (row,) = await observations(engine)
    assert "SECRET" not in str(row.extraction_metadata)


async def test_ownership_and_missing_source_prevent_http(engine: AsyncEngine) -> None:
    _, item_id, source_id = await seed(engine)
    fetcher = Fetcher()
    with pytest.raises(PriceCheckServiceError, match="forbidden"):
        await service(engine, fetcher).check(item_id, household_id=uuid4())
    with pytest.raises(PriceCheckServiceError, match="not_found"):
        await service(engine, fetcher).check(uuid4())
    async with get_session_factory(engine).begin() as session:
        source = await session.get(OfferSource, source_id)
        assert source is not None
        await session.delete(source)
    with pytest.raises(PriceCheckServiceError, match="not_found"):
        await service(engine, fetcher).check(item_id)
    assert fetcher.urls == [] and await observations(engine) == []


async def test_http_has_no_transaction_or_inventory_source_locks(engine: AsyncEngine) -> None:
    household_id, item_id, source_id = await seed(engine)

    async def during_fetch() -> None:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'idle in transaction'"
                    )
                )
                == 0
            )
        async with get_session_factory(engine).begin() as session:
            assert (
                await session.scalar(
                    select(Household)
                    .where(Household.id == household_id)
                    .with_for_update(nowait=True)
                )
                is not None
            )
            assert (
                await session.scalar(
                    select(OfferSource)
                    .where(OfferSource.id == source_id)
                    .with_for_update(nowait=True)
                )
                is not None
            )
            item = await session.get(Consumable, item_id)
            assert item is not None
            item.stock_quantity = Decimal("10")

    result = await service(engine, Fetcher(hook=during_fetch)).check(item_id)
    assert result.outcome == "success"


@pytest.mark.parametrize("change", ["url", "variant", "ownership", "deleted"])
async def test_source_identity_rechecked_after_http(engine: AsyncEngine, change: str) -> None:
    _, item_id, source_id = await seed(engine)

    async def during_fetch() -> None:
        async with get_session_factory(engine).begin() as session:
            source = await session.get(OfferSource, source_id)
            item = await session.get(Consumable, item_id)
            assert source is not None and item is not None
            if change == "url":
                source.url = URL + "changed"
            elif change == "variant":
                source.variant = "other"
            elif change == "deleted":
                await session.delete(source)
            else:
                new_household = Household(name="Other", created_at=NOW)
                session.add(new_household)
                await session.flush()
                item.household_id = new_household.id

    with pytest.raises(PriceCheckServiceError, match="source_changed"):
        await service(engine, Fetcher(hook=during_fetch)).check(item_id)
    assert await observations(engine) == []


async def test_cancelled_http_is_not_recorded(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    started = asyncio.Event()

    async def during_fetch() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(service(engine, Fetcher(hook=during_fetch)).check(item_id))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await observations(engine) == []


async def test_concurrent_checks_each_append_without_lost_history(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    ready = asyncio.Event()
    calls = 0

    async def during_fetch() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await ready.wait()

    checker = service(engine, Fetcher(hook=during_fetch))
    results = await asyncio.wait_for(
        asyncio.gather(checker.check(item_id), checker.check(item_id)), 10
    )
    assert len({row.observation_id for row in results}) == 2
    assert len(await observations(engine)) == 2


async def test_failed_append_rolls_back_and_raises_safe_error(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)

    def reject_insert(*args: object) -> None:
        statement = str(args[2])
        if statement.startswith("INSERT INTO price_observation"):
            raise IntegrityError("SECRET DATABASE CREDENTIALS", {}, ValueError("SECRET"))

    event.listen(engine.sync_engine, "before_cursor_execute", reject_insert)
    try:
        with pytest.raises(PriceCheckServiceError, match="^persistence_error$") as caught:
            await service(engine).check(item_id)
        assert caught.value.__suppress_context__
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject_insert)
    assert await observations(engine) == []
    assert (await service(engine).check(item_id)).outcome == "success"


@pytest.mark.parametrize(
    "values",
    [
        {"outcome": "failed", "availability": "unavailable", "error_code": "timeout"},
        {"outcome": "failed", "availability": "unknown", "error_code": None},
        {"outcome": "success", "availability": "available", "error_code": "timeout"},
        {"outcome": "failed", "availability": "unknown", "error_code": "secret message"},
        {"outcome": "success", "availability": "unknown", "extraction_metadata": []},
        {"outcome": "success", "availability": "unknown", "extraction_metadata": {"x": "a" * 4100}},
    ],
)
async def test_database_enforces_outcome_and_metadata_shape(
    engine: AsyncEngine,
    values: dict[str, object],
) -> None:
    _, _, source_id = await seed(engine)
    with pytest.raises(IntegrityError):
        async with get_session_factory(engine).begin() as session:
            session.add(PriceObservation(offer_source_id=source_id, observed_at=NOW, **values))
    assert await observations(engine) == []


async def test_new_observation_fields_are_append_only(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    await service(engine).check(item_id)
    for sql in [
        "UPDATE price_observation SET product_name = 'changed'",
        "UPDATE price_observation SET outcome = 'failed', error_code = 'timeout'",
        "DELETE FROM price_observation",
    ]:
        with pytest.raises(IntegrityError, match="append-only"):
            async with engine.begin() as connection:
                await connection.execute(text(sql))
    assert len(await observations(engine)) == 1


async def test_migration_preserves_existing_price_rows(
    engine: AsyncEngine,
    database_url: str,
) -> None:
    _, _, source_id = await seed(engine)
    migrate(database_url, "downgrade", "0002_telegram_conversation")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO price_observation (id,offer_source_id,observed_at,price,currency,"
                    "availability) VALUES (:id,:source,:time,42.96,'PLN','available')"
                ),
                {"id": uuid4(), "source": source_id, "time": NOW},
            )
        migrate(database_url, "upgrade", "head")
        migrate(database_url, "check")
        (row,) = await observations(engine)
        assert row.price == Decimal("42.96") and row.outcome == "success"
        assert row.product_name is None and row.variant_snapshot is None
        assert row.error_code is None and row.extraction_metadata == {}
        async with engine.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(PriceObservation)) == 1
    finally:
        migrate(database_url, "upgrade", "head")


async def test_extraction_uses_original_identity_after_redirect(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    extractor = Extractor()
    fetcher = Fetcher(final_url="https://www.zooplus.pl/shop/other?activeVariant=wrong")
    await service(engine, fetcher, extractor).check(item_id)
    assert extractor.inputs == [("<html />", URL, VARIANT)]


@pytest.mark.parametrize("source_variant", ["a" * 1500, "variant\nvalue", "\u202evariant", "   "])
async def test_failed_check_bounds_variant_snapshot_preserving_source(
    engine: AsyncEngine, source_variant: str
) -> None:
    _, item_id, source_id = await seed(engine)
    sessions = get_session_factory(engine)
    async with sessions.begin() as session:
        source = await session.get(OfferSource, source_id)
        assert source is not None
        source.variant = source_variant
    result = await service(
        engine, extractor=Extractor(error=ProductCheckError(ErrorCode.IDENTITY_MISMATCH))
    ).check(item_id)
    assert result.outcome == "failed" and result.error_code == ErrorCode.IDENTITY_MISMATCH
    assert result.variant is None
    (row,) = await observations(engine)
    assert row.outcome == "failed" and row.error_code == "identity_mismatch"
    assert row.variant_snapshot is None and row.price is None and row.availability == "unknown"
    async with sessions() as session:
        source = await session.get(OfferSource, source_id)
        assert source is not None and source.variant == source_variant and source.url == URL
    # A later valid source/check can append successfully without losing the failure.
    async with sessions.begin() as session:
        source = await session.get(OfferSource, source_id)
        assert source is not None
        source.variant = VARIANT
    recovered = await service(engine).check(item_id)
    assert recovered.outcome == "success" and recovered.variant == VARIANT
    rows = await observations(engine)
    assert len(rows) == 2
    assert next(observation for observation in rows if observation.id == row.id).outcome == "failed"
