"""Real DB vertical slice through the authenticated HTTP boundary."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mimit.api import create_app
from mimit.clock import FrozenClock
from mimit.config import Settings
from mimit.db.models import (
    Consumable,
    NotificationOutbox,
    OfferSource,
    Purchase,
    TelegramUpdateReceipt,
)
from mimit.db.session import get_session_factory

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
URL = "https://www.zooplus.pl/shop/koty/2333304?activeVariant=2333304.0"
SECRET = "local_test_webhook_secret"


def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL=database_url,
        TELEGRAM_BOT_TOKEN="123456:local_test_token",
        TELEGRAM_WEBHOOK_SECRET=SECRET,
        PUBLIC_BASE_URL="https://mimit.example.com",
        TELEGRAM_ALLOWED_USER_ID=101,
        TELEGRAM_ALLOWED_CHAT_ID=101,
    )


def payload(update_id: int, message: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1790942400,
            "from": {"id": 101, "is_bot": False, "first_name": "Test"},
            "chat": {"id": 101, "type": "private"},
            "text": message,
        },
    }


def client(engine: AsyncEngine, database_url: str, now: datetime = NOW) -> httpx.AsyncClient:
    app = create_app(
        settings=settings(database_url),
        clock=FrozenClock(now),
        sessions=get_session_factory(engine),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://mimit.test",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )


async def onboard(http: httpx.AsyncClient) -> None:
    for update_id, message in enumerate([URL, "Schesir", "12", "pouch", "2", "3", "yes"], start=1):
        response = await http.post("/telegram/webhook", json=payload(update_id, message))
        assert response.status_code == 200, response.text


async def test_inventory_loop_retries_and_restart(engine: AsyncEngine, database_url: str) -> None:
    # Separate app instances prove conversation state survives an API restart.
    async with client(engine, database_url) as http:
        assert (await http.post("/telegram/webhook", json=payload(1, URL))).status_code == 200
        assert (await http.post("/telegram/webhook", json=payload(2, "Schesir"))).status_code == 200
    async with client(engine, database_url) as http:
        for update_id, message in enumerate(["12", "pouch", "2", "3"], start=3):
            assert (
                await http.post("/telegram/webhook", json=payload(update_id, message))
            ).status_code == 200
        responses = await asyncio.gather(
            *[http.post("/telegram/webhook", json=payload(7, "yes")) for _ in range(4)]
        )
        assert all(r.status_code == 200 for r in responses)
    sessions = get_session_factory(engine)
    async with sessions() as session:
        item: Consumable | None = (await session.scalars(select(Consumable))).one()
        assert item is not None
        item_id = item.id
        assert item.stock_quantity == Decimal("12")
        assert item.daily_consumption == Decimal("2")
        assert (await session.scalars(select(OfferSource.url))).one() == URL
        assert (await session.scalars(select(OfferSource.variant))).one() == "2333304.0"
        assert await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 7
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 7
    async with client(engine, database_url, NOW + timedelta(days=2)) as http:
        responses = await asyncio.gather(
            *[
                http.post("/telegram/webhook", json=payload(8, f"/bought {item_id} 6"))
                for _ in range(4)
            ]
        )
        assert all(r.status_code == 200 for r in responses)
        assert (await http.post("/telegram/webhook", json=payload(9, "/stock"))).status_code == 200
    async with sessions() as session:
        item = await session.get(Consumable, item_id)
        assert item is not None
        assert item.stock_quantity == Decimal("14")  # 12 - 2*2 + 6
        assert item.stock_updated_at == NOW + timedelta(days=2)
        purchases = (await session.scalars(select(Purchase))).all()
        assert len(purchases) == 1 and purchases[0].quantity == Decimal("6")
        replies = (await session.scalars(select(NotificationOutbox))).all()
        stock_reply = next(
            row.payload["text"] for row in replies if row.dedupe_key.startswith("telegram:9:")
        )
        assert "Schesir" in stock_reply and "14" in stock_reply and "7 days" in stock_reply
    async with client(engine, database_url, NOW + timedelta(days=3)) as http:
        for _ in range(2):
            assert (
                await http.post("/telegram/webhook", json=payload(10, f"/setstock {item_id} 5"))
            ).status_code == 200
    async with sessions() as session:
        item = await session.get(Consumable, item_id)
        assert item is not None and item.stock_quantity == Decimal("5")
        assert await session.scalar(select(func.count()).select_from(Purchase)) == 1


async def test_two_distinct_purchases_do_not_lose_stock(
    engine: AsyncEngine, database_url: str
) -> None:
    async with client(engine, database_url) as http:
        await onboard(http)
        async with get_session_factory(engine)() as session:
            item_id = (await session.scalars(select(Consumable.id))).one()
        responses = await asyncio.gather(
            *[
                http.post("/telegram/webhook", json=payload(update_id, f"/bought {item_id} 1"))
                for update_id in [8, 9]
            ]
        )
        assert all(r.status_code == 200 for r in responses)
    async with get_session_factory(engine)() as session:
        assert (await session.scalars(select(Consumable.stock_quantity))).one() == Decimal("14")
        assert await session.scalar(select(func.count()).select_from(Purchase)) == 2


async def test_authorization_precedes_mutation(engine: AsyncEngine, database_url: str) -> None:
    async with client(engine, database_url) as http:
        response = await http.post(
            "/telegram/webhook",
            json=payload(1, URL),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert response.status_code in {401, 403}
        bad = payload(1, URL)
        bad["message"] = {"from": {"id": 202}, "chat": {"id": 101, "type": "private"}, "text": URL}
        response = await http.post("/telegram/webhook", json=bad)
        assert response.status_code == 403
    async with get_session_factory(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


async def test_database_failure_rolls_back_receipt_and_retries(
    engine: AsyncEngine, database_url: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            CREATE FUNCTION test_reject_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected outbox failure'; END $$
        """)
        )
        await connection.execute(
            text("""
            CREATE TRIGGER test_reject_outbox BEFORE INSERT ON notification_outbox
            FOR EACH ROW EXECUTE FUNCTION test_reject_outbox()
        """)
        )
    try:
        async with client(engine, database_url) as http:
            response = await http.post("/telegram/webhook", json=payload(1, URL))
            assert response.status_code == 503
            assert "injected outbox failure" not in response.text
        async with get_session_factory(engine)() as session:
            assert (
                await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 0
            )
            assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TRIGGER test_reject_outbox ON notification_outbox"))
            await connection.execute(text("DROP FUNCTION test_reject_outbox()"))
    async with client(engine, database_url) as http:
        assert (await http.post("/telegram/webhook", json=payload(1, URL))).status_code == 200
    async with get_session_factory(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 1


async def test_unknown_item_and_bad_quantity_leave_stock_unchanged(
    engine: AsyncEngine, database_url: str
) -> None:
    async with client(engine, database_url) as http:
        await onboard(http)
        async with get_session_factory(engine)() as session:
            item_id = (await session.scalars(select(Consumable.id))).one()
        for update_id, command in enumerate(
            [
                f"/bought {item_id} NaN",
                f"/bought {item_id} -1",
                f"/bought {uuid4()} 2",
                f"/setstock {item_id} Infinity",
            ],
            start=8,
        ):
            assert (
                await http.post("/telegram/webhook", json=payload(update_id, command))
            ).status_code == 200
    async with get_session_factory(engine)() as session:
        assert (await session.scalars(select(Consumable.stock_quantity))).one() == Decimal("12")
        assert await session.scalar(select(func.count()).select_from(Purchase)) == 0


async def test_stock_pages_are_bounded_standalone_replies(
    engine: AsyncEngine, database_url: str
) -> None:
    async with client(engine, database_url) as http:
        await onboard(http)
        async with get_session_factory(engine).begin() as session:
            first = (await session.scalars(select(Consumable))).one()
            first.name = "🐱" * 120
            first.canonical_unit = "🐾" * 40
            first.stock_quantity = Decimal("999999999999.999999")
            first.daily_consumption = Decimal("0.000001")
            for _ in range(5):
                session.add(
                    Consumable(
                        household_id=first.household_id,
                        name=first.name,
                        canonical_unit=first.canonical_unit,
                        stock_quantity=first.stock_quantity,
                        daily_consumption=first.daily_consumption,
                        reserve_days=2147483647,
                        stock_updated_at=NOW,
                        created_at=NOW,
                    )
                )
        for update_id, command in enumerate(
            ["/stock", "/stock 2", "/stock 3", "/stock -1"], start=8
        ):
            assert (
                await http.post("/telegram/webhook", json=payload(update_id, command))
            ).status_code == 200
    async with get_session_factory(engine)() as session:
        pages = [
            (
                await session.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.dedupe_key == f"telegram:{update_id}:0"
                    )
                )
            )
            .one()
            .payload["text"]
            for update_id in [8, 9, 10, 11]
        ]
        assert "page 1/2" in pages[0] and pages[0].count("ID:") == 5
        assert "Next page: /stock 2" in pages[0]
        assert "page 2/2" in pages[1] and pages[1].count("ID:") == 1
        assert "2 stock pages" in pages[2]
        assert "positive whole number" in pages[3]
        assert all(len(page.encode("utf-16-le")) // 2 <= 4096 for page in pages)
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 11
