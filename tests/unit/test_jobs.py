from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mimit.jobs.service import JobPayload, next_slot, parse_payload

NOW = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)


def test_payload_preserves_slot_across_serialization() -> None:
    payload = JobPayload(uuid4(), NOW)
    assert parse_payload(payload.as_dict()) == payload
    assert parse_payload({**payload.as_dict(), "slot_at": NOW.isoformat().replace("+00:00", "Z")})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"consumable_id": str(uuid4()), "slot_at": "2026-01-01T00:00:00"},
        {"consumable_id": "bad", "slot_at": NOW.isoformat()},
        {"consumable_id": 17, "slot_at": None},
        {"consumable_id": str(uuid4()), "slot_at": 5},
        {"consumable_id": str(uuid4()), "slot_at": NOW.isoformat(), "extra": "x"},
    ],
)
def test_malformed_payload_rejected(payload: dict[str, object]) -> None:
    assert parse_payload(payload) is None


@pytest.mark.parametrize("elapsed,days", [(0, 1), (86400, 2), (86401, 2), (86400 * 5 + 90, 6)])
def test_next_slot_is_future_and_preserves_original_daily_anchor(elapsed: int, days: int) -> None:
    assert next_slot(NOW, NOW + timedelta(seconds=elapsed)) == NOW + timedelta(days=days)
