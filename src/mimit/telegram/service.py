"""Transactional Telegram inventory interactions; no network calls.

The HTTP/polling boundary authenticates user/chat IDs before invoking this module.
Receipt insertion, conversation/stock changes, and reply intents commit together.
Household locks serialize mutations but do not reorder updates by Telegram ID.
"""

import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock
from mimit.db.models import (
    Consumable,
    Household,
    NotificationOutbox,
    OfferSource,
    Purchase,
    TelegramConversation,
    TelegramUpdateReceipt,
)
from mimit.inventory import (
    MAX_QUANTITY,
    days_remaining,
    format_quantity,
    parse_quantity,
    stock_at,
)
from mimit.observability import log_event
from mimit.products.display import stock_price_summaries

HELP = (
    "Send a product URL to add a consumable. I'll ask for its name, stock, unit, "
    "daily consumption, and reserve days. Automatic price checks are pending.\n"
    "/stock [page] — estimated stock, stored prices and item IDs\n"
    "/bought <item-id> <quantity> — record a purchase\n"
    "/setstock <item-id> <quantity> — correct current stock\n"
    "/cancel — cancel onboarding\n/help — show this help"
)


def household_id_for_chat(chat_id: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"mimit:telegram:chat:{chat_id}")


def _url_variant(url: str) -> str | None:
    """Validate storage input only; M2 must validate any eventual fetch separately."""
    if len(url) > 2048 or any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("Send one product URL, without spaces (maximum 2048 characters).")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        # Accessing port catches malformed ports without fetching the URL.
        _ = parsed.port
    except ValueError:
        raise ValueError("Use an http:// or https:// product URL without credentials.") from None
    variants = parse_qs(parsed.query, keep_blank_values=True).get("activeVariant", [])
    if len(variants) > 1 or (variants and not variants[0]):
        raise ValueError("The URL must select one unambiguous activeVariant.")
    return variants[0] if variants else None


async def process_message(
    sessions: async_sessionmaker[AsyncSession],
    *,
    update_id: int,
    user_id: int,
    chat_id: int,
    text: str,
    clock: Clock,
    timezone: str = "Europe/Warsaw",
) -> bool:
    """Process one authenticated message; False means it was already committed."""
    del user_id  # Authorization is deliberately owned by the inbound boundary.
    household_id = household_id_for_chat(chat_id)
    async with sessions() as session, session.begin():
        receipt = await session.scalar(
            insert(TelegramUpdateReceipt)
            .values(update_id=update_id, received_at=clock.now())
            .on_conflict_do_nothing(index_elements=[TelegramUpdateReceipt.update_id])
            .returning(TelegramUpdateReceipt.update_id)
        )
        if receipt is None:
            return False
        await session.execute(
            insert(Household)
            .values(id=household_id, name="Telegram household", created_at=clock.now())
            .on_conflict_do_nothing(index_elements=[Household.id])
        )
        await session.scalar(
            select(Household).where(Household.id == household_id).with_for_update()
        )
        now = clock.now()
        reply = await _handle(session, household_id, text.strip(), now, timezone)
        session.add(
            NotificationOutbox(
                dedupe_key=f"telegram:{update_id}:0",
                payload={"chat_id": chat_id, "text": reply},
                run_at=now,
                created_at=now,
            )
        )
    log_event(
        "telegram_message_committed",
        update_id=update_id,
        household_id=household_id,
        outcome="committed",
    )
    return True


async def _handle(
    session: AsyncSession, household_id: UUID, text: str, now: datetime, timezone: str
) -> str:
    if len(text) > 4096:
        return "That message is too long. Please use at most 4096 characters."
    conversation = await session.get(TelegramConversation, household_id)
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command in {"/start", "/help"}:
        return HELP
    if command == "/cancel":
        if conversation is not None:
            await session.delete(conversation)
        return "Onboarding cancelled. Send a product URL to start again."
    if command == "/stock":
        if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"[1-9][0-9]{0,8}", parts[1])):
            return "Usage: /stock [page]. Page must be a positive whole number."
        page = int(parts[1]) if len(parts) == 2 else 1
        count = await session.scalar(
            select(func.count())
            .select_from(Consumable)
            .where(Consumable.household_id == household_id)
        )
        total_pages = max(1, ((count or 0) + 4) // 5)
        if page > total_pages:
            return f"There are {total_pages} stock pages. Use /stock to start."
        items = (
            await session.scalars(
                select(Consumable)
                .where(Consumable.household_id == household_id)
                .order_by(Consumable.created_at, Consumable.id)
                .offset((page - 1) * 5)
                .limit(5)
            )
        ).all()
        if not items:
            return "No consumables yet. Send a product URL to add one."
        prices = await stock_price_summaries(session, [item.id for item in items], timezone)
        lines = [f"Estimated stock (page {page}/{total_pages}):"]
        for item in items:
            lines.append(
                f"{item.name}: {format_quantity(stock_at(item, now))} {item.canonical_unit}; "
                f"estimated {format_quantity(days_remaining(item, now))} days remaining; "
                f"daily {format_quantity(item.daily_consumption)}; "
                f"reserve {item.reserve_days} days\n"
                f"ID: {item.id}\n{prices[item.id]}"
            )
        if page < total_pages:
            lines.append(f"Next page: /stock {page + 1}")
        lines.append("Use /bought <item-id> <quantity> or /setstock <item-id> <quantity>.")
        return "\n".join(lines)
    if command in {"/bought", "/setstock"}:
        return await _change_stock(session, household_id, command, parts, now)
    if command.startswith("/"):
        return "Unknown command. Use /help for the available commands."
    if text.lower().startswith(("http://", "https://")):
        try:
            variant = _url_variant(text)
        except ValueError as error:
            return str(error)
        data = {"url": text, "variant": variant}
        if conversation is None:
            session.add(
                TelegramConversation(
                    household_id=household_id, step="name", data=data, updated_at=now
                )
            )
        else:
            conversation.step, conversation.data, conversation.updated_at = "name", data, now
        return "What friendly name should I use for this consumable? (Up to 120 characters.)"
    if conversation is not None:
        return await _advance(session, conversation, text, now)
    return "Send an http:// or https:// product URL to begin, or use /help."


async def _change_stock(
    session: AsyncSession, household_id: UUID, command: str, parts: list[str], now: datetime
) -> str:
    if len(parts) != 3:
        return f"Usage: {command} <item-id> <quantity>. Find item IDs with /stock."
    try:
        item_id = UUID(parts[1])
        quantity = parse_quantity(parts[2], positive=command == "/bought")
    except ValueError:
        return "Use a valid item ID and decimal quantity (up to six decimal places)."
    item = await session.scalar(
        select(Consumable).where(Consumable.id == item_id, Consumable.household_id == household_id)
    )
    if item is None:
        return "That item is not in this household. Use /stock to find its ID."
    new_stock = stock_at(item, now) + quantity if command == "/bought" else quantity
    if new_stock > MAX_QUANTITY:
        return "That purchase would exceed the maximum supported stock quantity."
    item.stock_quantity = new_stock
    item.stock_updated_at = max(now, item.stock_updated_at)
    if command == "/bought":
        session.add(Purchase(consumable_id=item.id, quantity=quantity, purchased_at=now))
    action = "Purchase recorded" if command == "/bought" else "Stock corrected"
    return f"{action}. {item.name}: {format_quantity(new_stock)} {item.canonical_unit}."


async def _advance(
    session: AsyncSession, conversation: TelegramConversation, text: str, now: datetime
) -> str:
    data = dict(conversation.data)
    step = conversation.step
    try:
        if step == "name":
            if not text or len(text) > 120 or any(ord(char) < 32 for char in text):
                raise ValueError("Enter a friendly name of 1–120 characters on one line.")
            data["name"] = text
            next_step, reply = (
                "stock",
                "How much stock do you have now? Enter a number, such as 12.",
            )
        elif step == "stock":
            data["stock"] = str(parse_quantity(text))
            next_step, reply = (
                "unit",
                "What unit will you use consistently? For example: cans, g, kg.",
            )
        elif step == "unit":
            if not text or len(text) > 40 or any(ord(char) < 32 for char in text):
                raise ValueError("Enter a unit of 1–40 characters on one line, such as cans or kg.")
            data["unit"] = text
            next_step, reply = (
                "daily",
                f"How many {text} do you use per day? Enter a number above 0.",
            )
        elif step == "daily":
            data["daily"] = str(parse_quantity(text, positive=True))
            next_step, reply = "reserve", "How many reserve days? Enter a whole number, 0 or more."
        elif step == "reserve":
            if not re.fullmatch(r"[0-9]{1,10}", text) or int(text) > 2147483647:
                raise ValueError("Reserve days must be a whole number from 0 to 2147483647.")
            data["reserve"] = int(text)
            next_step = "confirm"
            reply = (
                f"Add {data['name']}?\nStock: {data['stock']} {data['unit']}\n"
                f"Daily consumption: {data['daily']} {data['unit']}\n"
                f"Reserve: {data['reserve']} days\n"
                "Reply yes to save, or no to cancel. Automatic price checks are pending."
            )
        elif step == "confirm":
            if text.lower() in {"no", "n"}:
                await session.delete(conversation)
                return "Cancelled. Send a product URL to start again."
            if text.lower() not in {"yes", "y"}:
                return "Reply yes to save, or no to cancel."
            item = Consumable(
                household_id=conversation.household_id,
                name=data["name"],
                stock_quantity=Decimal(data["stock"]),
                canonical_unit=data["unit"],
                daily_consumption=Decimal(data["daily"]),
                reserve_days=data["reserve"],
                stock_updated_at=now,
                created_at=now,
            )
            session.add(item)
            await session.flush()
            session.add(
                OfferSource(
                    consumable_id=item.id,
                    url=data["url"],
                    variant=data["variant"],
                    created_at=now,
                )
            )
            await session.delete(conversation)
            return (
                f"Added {item.name}. ID: {item.id}\n"
                f"Estimated {format_quantity(days_remaining(item, now))} days remaining.\n"
                "Use /stock to check estimated stock. Automatic price checks are pending."
            )
        else:
            raise RuntimeError("Unexpected persisted conversation step")
    except ValueError as error:
        return str(error)
    conversation.data = data
    conversation.step = next_step
    conversation.updated_at = now
    return reply
