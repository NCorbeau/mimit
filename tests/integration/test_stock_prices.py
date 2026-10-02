"""Stored prices remain honest and household-scoped in real Telegram replies."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Result, select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import Executable

from mimit.db.models import (
    Consumable,
    Household,
    NotificationOutbox,
    OfferSource,
    PriceObservation,
)
from mimit.db.session import get_session_factory
from mimit.products.display import stock_price_summaries

from .test_telegram import NOW, client, onboard, payload


async def test_stock_retains_last_price_after_failed_check(
    engine: AsyncEngine, database_url: str
) -> None:
    async with client(engine, database_url) as http:
        await onboard(http)
        assert (await http.post("/telegram/webhook", json=payload(8, "/stock"))).status_code == 200
    sessions = get_session_factory(engine)
    async with sessions.begin() as session:
        source = (await session.scalars(select(OfferSource))).one()
        session.add(
            PriceObservation(
                offer_source_id=source.id,
                observed_at=NOW + timedelta(hours=1),
                price=Decimal("42.96"),
                currency="PLN",
                unit_price=Decimal("84.24"),
                unit="kg",
                availability="available",
                outcome="success",
                product_name="Schesir 6 x 85g",
                variant_snapshot=source.variant,
            )
        )
        session.add(
            PriceObservation(
                offer_source_id=source.id,
                observed_at=NOW + timedelta(hours=2),
                availability="unknown",
                outcome="failed",
                error_code="timeout",
                variant_snapshot=source.variant,
            )
        )
    async with client(engine, database_url, NOW + timedelta(hours=3)) as http:
        assert (await http.post("/telegram/webhook", json=payload(9, "/stock"))).status_code == 200
    async with sessions() as session:
        before = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.dedupe_key == "telegram:8:0")
        )
        after = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.dedupe_key == "telegram:9:0")
        )
        assert before is not None and "Price: not checked yet." in before.payload["text"]
        assert after is not None
        message = after.payload["text"]
        assert "42.96 PLN per offer; 84.24 PLN/kg" in message
        assert "observed 2026-10-02 15:00 CEST" in message
        assert "Availability: in stock; checked 2026-10-02 15:00 CEST" in message
        assert "Latest check failed (timeout); checked 2026-10-02 16:00 CEST" in message
        assert "PLN/pouch" not in message


async def test_stock_shows_latest_availability_without_losing_prior_price(
    engine: AsyncEngine, database_url: str
) -> None:
    async with client(engine, database_url) as http:
        await onboard(http)
    sessions = get_session_factory(engine)
    async with sessions.begin() as session:
        source = (await session.scalars(select(OfferSource))).one()
        session.add_all(
            [
                PriceObservation(
                    offer_source_id=source.id,
                    observed_at=NOW,
                    price=Decimal("42.96"),
                    currency="PLN",
                    availability="available",
                    outcome="success",
                ),
                PriceObservation(
                    offer_source_id=source.id,
                    observed_at=NOW + timedelta(hours=1),
                    availability="unavailable",
                    outcome="success",
                    product_name="Schesir",
                    variant_snapshot=source.variant,
                ),
            ]
        )
        other_household, other_item, other_source = uuid4(), uuid4(), uuid4()
        session.add(Household(id=other_household, name="Other", created_at=NOW))
        await session.flush()
        session.add(
            Consumable(
                id=other_item,
                household_id=other_household,
                name="Other secret item",
                stock_quantity=Decimal(1),
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
                id=other_source, consumable_id=other_item, url="https://example.com", created_at=NOW
            )
        )
        await session.flush()
        session.add(
            PriceObservation(
                offer_source_id=other_source,
                observed_at=NOW,
                price=Decimal("999"),
                currency="USD",
                availability="available",
            )
        )
    async with client(engine, database_url, NOW + timedelta(hours=2)) as http:
        assert (await http.post("/telegram/webhook", json=payload(8, "/stock"))).status_code == 200
    async with sessions() as session:
        reply = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.dedupe_key == "telegram:8:0")
        )
        assert reply is not None
        message = reply.payload["text"]
        assert "42.96 PLN per offer" in message
        assert "Availability: out of stock; checked 2026-10-02 15:00 CEST" in message
        assert "Latest check failed" not in message
        assert "Other secret item" not in message and "USD" not in message


async def test_priced_unicode_stock_page_stays_within_telegram_limit(
    engine: AsyncEngine,
    database_url: str,
) -> None:
    sessions = get_session_factory(engine)
    merchant_unit = "🐾" * 64
    async with client(engine, database_url) as http:
        await onboard(http)
    async with sessions.begin() as session:
        item = (await session.scalars(select(Consumable))).one()
        item.name = "🐱" * 120
        item.canonical_unit = "🐾" * 40
        item.stock_quantity = Decimal("999999999999.999999")
        item.daily_consumption = Decimal("0.000001")
        item.reserve_days = 2147483647
        source = (await session.scalars(select(OfferSource))).one()
        sources = [source]
        for _ in range(4):
            extra = Consumable(
                household_id=item.household_id,
                name=item.name,
                canonical_unit=item.canonical_unit,
                stock_quantity=item.stock_quantity,
                daily_consumption=item.daily_consumption,
                reserve_days=item.reserve_days,
                stock_updated_at=NOW,
                created_at=NOW,
            )
            session.add(extra)
            await session.flush()
            extra_source = OfferSource(
                consumable_id=extra.id,
                url="https://example.com",
                created_at=NOW,
            )
            session.add(extra_source)
            await session.flush()
            sources.append(extra_source)
        for source in sources:
            session.add_all(
                [
                    PriceObservation(
                        offer_source_id=source.id,
                        observed_at=NOW,
                        price=Decimal("999999999999.999999"),
                        currency="PLN",
                        unit_price=Decimal("999999999999.999999"),
                        unit=merchant_unit,
                        availability="unavailable",
                        outcome="success",
                    ),
                    PriceObservation(
                        offer_source_id=source.id,
                        observed_at=NOW + timedelta(hours=1),
                        availability="unknown",
                        outcome="failed",
                        error_code="unsupported_content",
                    ),
                ]
            )
    async with client(engine, database_url, NOW + timedelta(hours=2)) as http:
        assert (await http.post("/telegram/webhook", json=payload(8, "/stock"))).status_code == 200
    async with sessions() as session:
        reply = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.dedupe_key == "telegram:8:0")
        )
        assert reply is not None
        message = reply.payload["text"]
        assert message.count("ID:") == 5 and message.count("Last price:") == 5
        assert message.count("Latest check failed (unsupported_content)") == 5
        assert len(message.encode("utf-16-le")) // 2 <= 4096
        assert "🐾" * 15 + "…" in message and merchant_unit not in message
        units = await session.scalars(
            select(PriceObservation.unit).where(PriceObservation.outcome == "success")
        )
        assert list(units) == [merchant_unit] * 5


async def test_stock_history_uses_one_bounded_statement_snapshot(
    engine: AsyncEngine,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with client(engine, database_url) as http:
        await onboard(http)
    sessions = get_session_factory(engine)
    async with sessions.begin() as session:
        source = (await session.scalars(select(OfferSource))).one()
        source_id, item_id = source.id, source.consumable_id
        for hour in range(12):
            session.add(
                PriceObservation(
                    offer_source_id=source_id,
                    observed_at=NOW - timedelta(hours=hour),
                    price=Decimal("42.96"),
                    currency="PLN",
                    availability="available",
                    outcome="success",
                )
            )
        session.add(
            PriceObservation(
                offer_source_id=source_id,
                observed_at=NOW + timedelta(hours=1),
                availability="unknown",
                outcome="failed",
                error_code="timeout",
            )
        )
    async with sessions() as session:
        original_execute = session.execute
        reads = 0
        returned_rows: list[int] = []

        async def interleaved_execute(
            statement: Executable,
            *args: Any,
            **kwargs: Any,
        ) -> Result[Any]:
            nonlocal reads
            result = await original_execute(statement, *args, **kwargs)
            if "price_observation" in str(statement):
                reads += 1
                frozen = result.freeze()
                returned_rows.append(len(frozen().all()))
                result = frozen()
                if reads == 1:
                    # Commit a new successful check after the first actual DB read,
                    # exactly where the old multi-query implementation mixed history.
                    async with sessions.begin() as writer:
                        writer.add(
                            PriceObservation(
                                offer_source_id=source_id,
                                observed_at=NOW + timedelta(hours=2),
                                price=Decimal("33"),
                                currency="PLN",
                                availability="unavailable",
                                outcome="success",
                            )
                        )
            return result

        monkeypatch.setattr(session, "execute", interleaved_execute)
        summary = (await stock_price_summaries(session, [item_id], "Europe/Warsaw"))[item_id]
        assert reads == 1 and returned_rows == [3]
        assert "42.96 PLN per offer" in summary
        assert "Availability: in stock; checked 2026-10-02 14:00 CEST" in summary
        assert "Latest check failed (timeout); checked 2026-10-02 15:00 CEST" in summary
        assert "33 PLN" not in summary and "out of stock" not in summary
    async with sessions() as session:
        summary = (await stock_price_summaries(session, [item_id], "Europe/Warsaw"))[item_id]
        assert "33 PLN per offer" in summary and "Availability: out of stock" in summary
        assert "Latest check failed" not in summary
