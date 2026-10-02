"""Read stored product checks for a bounded stock page; never fetch during a reply."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Select, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from mimit.db.models import OfferSource, PriceObservation
from mimit.inventory import format_quantity


async def stock_price_summaries(
    session: AsyncSession, item_ids: Sequence[UUID], timezone: str
) -> dict[UUID, str]:
    if not item_ids:
        return {}

    def query(
        kind: str, *, priced: bool = False, successful: bool = False
    ) -> Select[tuple[UUID, UUID, str]]:
        statement = (
            select(
                OfferSource.consumable_id,
                PriceObservation.id.label("observation_id"),
                literal(kind).label("kind"),
            )
            .join(PriceObservation, PriceObservation.offer_source_id == OfferSource.id)
            .where(OfferSource.consumable_id.in_(item_ids))
            .distinct(OfferSource.consumable_id)
            .order_by(
                OfferSource.consumable_id,
                PriceObservation.observed_at.desc(),
                PriceObservation.id.desc(),
            )
        )
        if successful:
            statement = statement.where(PriceObservation.outcome == "success")
        if priced:
            statement = statement.where(PriceObservation.price.is_not(None))
        return statement

    # One PostgreSQL statement supplies one READ COMMITTED snapshot. Each tagged
    # branch picks at most one row per page item before loading observation details.
    choices = union_all(
        query("latest"),
        query("success", successful=True),
        query("price", priced=True, successful=True),
    ).subquery()
    rows = await session.execute(
        select(choices.c.consumable_id, choices.c.kind, PriceObservation).join(
            PriceObservation, PriceObservation.id == choices.c.observation_id
        )
    )
    summaries: dict[str, dict[UUID, PriceObservation]] = {
        "latest": {},
        "success": {},
        "price": {},
    }
    for item_id, kind, observation in rows:
        summaries[kind][item_id] = observation
    latest, successes, prices = summaries["latest"], summaries["success"], summaries["price"]
    return {
        item_id: _summary(
            latest.get(item_id), successes.get(item_id), prices.get(item_id), timezone
        )
        for item_id in item_ids
    }


def _time(value: datetime, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


def _merchant_unit(value: str) -> str:
    """Bound presentation to 32 Telegram UTF-16 units, leaving history intact."""
    if len(value.encode("utf-16-le")) // 2 <= 32:
        return value
    visible: list[str] = []
    units = 0
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > 31:  # Reserve one unit for the ellipsis.
            break
        visible.append(character)
        units += width
    return "".join(visible) + "…"


def _summary(
    latest: PriceObservation | None,
    success: PriceObservation | None,
    priced: PriceObservation | None,
    timezone: str,
) -> str:
    if latest is None:
        return "Price: not checked yet."
    lines: list[str] = []
    if priced is not None:
        assert priced.price is not None
        price = f"Last price: {format_quantity(priced.price)} {priced.currency} per offer"
        if priced.unit_price is not None:
            assert priced.unit is not None
            unit = _merchant_unit(priced.unit)
            price += f"; {format_quantity(priced.unit_price)} {priced.currency}/{unit}"
        lines.append(price + f"; observed {_time(priced.observed_at, timezone)}.")
    else:
        lines.append("Price: no successful price observation yet.")
    if success is not None:
        label = {"available": "in stock", "unavailable": "out of stock", "unknown": "unknown"}
        lines.append(
            f"Availability: {label[success.availability]}; "
            f"checked {_time(success.observed_at, timezone)}."
        )
    if latest.outcome == "failed":
        lines.append(
            f"Latest check failed ({latest.error_code}); "
            f"checked {_time(latest.observed_at, timezone)}."
        )
    return "\n".join(lines)
