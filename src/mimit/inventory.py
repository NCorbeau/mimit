"""Deterministic stock arithmetic and input precision boundaries."""

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from mimit.db.models import Consumable

QUANTUM = Decimal("0.000001")
MAX_QUANTITY = Decimal("999999999999.999999")


def parse_quantity(value: str, *, positive: bool = False) -> Decimal:
    """Accept finite base-10 input representable exactly in NUMERIC(18, 6)."""
    if not re.fullmatch(r"\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value.strip()):
        raise ValueError("Enter a decimal number, using a dot for fractions.")
    result = Decimal(value.strip())
    if not result.is_finite() or result < 0 or result > MAX_QUANTITY:
        raise ValueError("Quantity must be between 0 and 999999999999.999999.")
    if positive and result == 0:
        raise ValueError("Quantity must be greater than zero.")
    if result != result.quantize(QUANTUM):
        raise ValueError("Use at most six decimal places.")
    return result


def stock_at(item: Consumable, now: datetime) -> Decimal:
    """Estimate stock at an explicit time, clamping depletion at zero."""
    if now.utcoffset() is None or item.stock_updated_at.utcoffset() is None:
        raise ValueError("Stock timestamps must be timezone-aware.")
    elapsed = max(now - item.stock_updated_at, now - now)
    seconds = Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(
        elapsed.microseconds
    ) / Decimal(1000000)
    remaining = max(
        Decimal(0), item.stock_quantity - item.daily_consumption * seconds / Decimal(86400)
    )
    return remaining.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def format_quantity(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def days_remaining(item: Consumable, now: datetime) -> Decimal:
    """Estimated remaining days, rounded to two decimal places for display."""
    return (stock_at(item, now) / item.daily_consumption).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
