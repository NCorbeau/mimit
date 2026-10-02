"""Injectable UTC clocks for deterministic domain behavior."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return an aware datetime in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FrozenClock:
    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None or self.instant.utcoffset() is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        object.__setattr__(self, "instant", self.instant.astimezone(UTC))

    def now(self) -> datetime:
        return self.instant
