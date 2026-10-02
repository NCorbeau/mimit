from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mimit.db.models import Consumable
from mimit.inventory import parse_quantity, stock_at

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0.0000001", "1000000000000"])
def test_rejects_quantities_database_cannot_represent(value: str) -> None:
    with pytest.raises(ValueError):
        parse_quantity(value)


def test_positive_daily_rate_required() -> None:
    with pytest.raises(ValueError):
        parse_quantity("0", positive=True)
    assert parse_quantity("0") == Decimal("0")
    assert parse_quantity("2.125", positive=True) == Decimal("2.125")


def test_depletion_uses_elapsed_fractional_days_and_clamps_at_zero() -> None:
    item = Consumable(
        stock_quantity=Decimal("10"), daily_consumption=Decimal("2"), stock_updated_at=NOW
    )
    assert stock_at(item, NOW + timedelta(hours=6)) == Decimal("9.5")
    assert stock_at(item, NOW + timedelta(days=100)) == Decimal("0")
    assert stock_at(item, NOW - timedelta(days=1)) == Decimal("10")
