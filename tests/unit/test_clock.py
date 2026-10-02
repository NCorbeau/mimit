from datetime import UTC, datetime, timedelta, timezone

import pytest

from mimit.clock import Clock, FrozenClock, SystemClock


def test_system_clock_returns_current_aware_utc() -> None:
    before = datetime.now(UTC)
    clock: Clock = SystemClock()
    actual = clock.now()
    after = datetime.now(UTC)
    assert before <= actual <= after
    assert actual.tzinfo is UTC


def test_frozen_clock_repeats_the_same_utc_instant() -> None:
    instant = datetime(2026, 10, 2, 12, 34, tzinfo=timezone(timedelta(hours=2)))
    clock: Clock = FrozenClock(instant)
    assert clock.now() == datetime(2026, 10, 2, 10, 34, tzinfo=UTC)
    assert clock.now() is clock.now()
    assert clock.now().tzinfo is UTC


def test_frozen_clock_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 10, 2, 12, 34))
