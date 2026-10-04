from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from uuid import uuid4

import pytest

from mimit.db.models import Consumable, PriceObservation
from mimit.inventory import days_remaining, exact_days_remaining
from mimit.recommendations import (
    PriceEvidence,
    RecommendationConfig,
    State,
    decide,
    price_evidence,
)

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def observation(price: str, days: int = 0, **kwargs: object) -> PriceObservation:
    values: dict[str, object] = {
        "id": uuid4(),
        "observed_at": NOW - timedelta(days=days),
        "unit_price": Decimal(price),
        "currency": "PLN",
        "unit": "kg",
        "variant_snapshot": "one",
        "outcome": "success",
    }
    values.update(kwargs)
    return PriceObservation(**values)


@pytest.mark.parametrize(
    ("days", "discount", "expected"),
    [
        ("0", False, State.BUY_NOW),
        ("3", False, State.BUY_NOW),
        ("3.00000000001", False, State.BUY_SOON),
        ("6", False, State.BUY_SOON),
        ("6.00000000001", True, State.OK),
        ("4", True, State.BUY_NOW),
        ("6", True, State.BUY_NOW),
        ("7", False, State.OK),
    ],
)
def test_exact_rule_boundaries(days: str, discount: bool, expected: State) -> None:
    evidence = PriceEvidence(Decimal("90") if discount else Decimal("90.000001"), Decimal(100))
    assert decide(Fraction(days), 3, evidence).state is expected


def test_microsecond_depletion_does_not_round_into_reserve() -> None:
    item = Consumable(
        stock_quantity=Decimal("3.000001"), daily_consumption=Decimal(1), stock_updated_at=NOW
    )
    assert days_remaining(item, NOW) == 3
    assert decide(exact_days_remaining(item, NOW), 3, PriceEvidence()).state is State.BUY_SOON
    boundary = NOW + timedelta(microseconds=86400)
    assert exact_days_remaining(item, boundary) == 3
    assert decide(exact_days_remaining(item, boundary), 3, PriceEvidence()).state is State.BUY_NOW
    assert exact_days_remaining(item, NOW - timedelta(days=1)) == Fraction("3.000001")
    assert exact_days_remaining(item, NOW + timedelta(days=100)) == 0


def test_zero_reserve_requires_depletion() -> None:
    assert decide(Fraction(0), 0, PriceEvidence()).state is State.BUY_NOW
    assert decide(Fraction(1, 10**12), 0, PriceEvidence(Decimal(0), Decimal(100))).state is State.OK


def test_current_price_excluded_from_odd_and_even_median() -> None:
    rows = [observation("90"), observation("100", 1), observation("100", 2), observation("100", 3)]
    evidence = price_evidence(rows, "one", NOW)
    assert evidence == PriceEvidence(Decimal(90), Decimal(100))
    assert decide(Fraction(4), 3, evidence).state is State.BUY_NOW
    rows.append(observation("200", 4))
    assert price_evidence(rows, "one", NOW).median == Decimal(100)
    rows[-1].unit_price = Decimal("20")
    rows[-2].unit_price = Decimal("20")
    assert price_evidence(rows, "one", NOW).median == Decimal(60)


@pytest.mark.parametrize("mismatch", ["currency", "unit", "variant_snapshot", "outcome"])
def test_excludes_incomparable_history(mismatch: str) -> None:
    rows = [observation("90"), observation("100", 1), observation("100", 2)]
    rows.append(observation("100", 3, **{mismatch: "different"}))
    assert price_evidence(rows, "one", NOW).fallback == "too little comparable price history"


def test_three_earlier_required_not_three_including_current() -> None:
    rows = [observation("90"), observation("100", 1), observation("100", 2)]
    result = decide(Fraction(4), 3, price_evidence(rows, "one", NOW))
    assert result.state is State.BUY_SOON
    assert "Stock only: too little comparable price history" in result.reason


def test_freshness_is_inclusive_and_failure_falls_back() -> None:
    rows = [
        observation("90", 2),
        observation("100", 3),
        observation("100", 4),
        observation("100", 5),
    ]
    assert price_evidence(rows, "one", NOW).fallback is None
    assert price_evidence(rows, "one", NOW + timedelta(microseconds=1)).fallback == "price is stale"
    rows.append(observation("0", 0, outcome="failed", unit_price=None))
    assert price_evidence(rows, "one", NOW).fallback == "latest price check failed"
    assert decide(Fraction(3), 3, price_evidence(rows, "one", NOW)).state is State.BUY_NOW


def test_window_boundary_equal_timestamp_and_future_exclusion() -> None:
    rows = [observation("90"), observation("100", 1), observation("100", 2), observation("100", 30)]
    assert price_evidence(rows, "one", NOW).median == Decimal(100)
    rows[-1].observed_at -= timedelta(microseconds=1)
    rows.extend([observation("1", -1), observation("100", 0)])
    assert price_evidence(rows, "one", NOW).fallback == "too little comparable price history"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"unit_price": None}, "unit price unavailable"),
        ({"variant_snapshot": "other"}, "price variant does not match"),
    ],
)
def test_latest_unusable_price_is_explicit(changes: dict[str, object], reason: str) -> None:
    assert price_evidence([observation("90", 0, **changes)], "one", NOW).fallback == reason


def test_configured_discount_has_explainable_reason() -> None:
    result = decide(
        Fraction(4),
        3,
        PriceEvidence(Decimal(80), Decimal(100)),
        RecommendationConfig(discount_ratio=Decimal("0.8")),
    )
    assert result.state is State.BUY_NOW
    assert "20" in result.reason


def test_no_history_and_zero_price_rule() -> None:
    assert price_evidence([], "one", NOW).fallback == "no price checks yet"
    rows = [observation("0", day) for day in range(4)]
    assert decide(Fraction(4), 3, price_evidence(rows, "one", NOW)).state is State.BUY_NOW
