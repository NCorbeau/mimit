"""Deterministic recommendations and atomic transition notification intents.

Callers own the transaction and acquire the household lock before this service.
No network I/O occurs here. The outbox sender owns delivery and bounded retries.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from statistics import median
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mimit.db.models import (
    Consumable,
    NotificationOutbox,
    OfferSource,
    PriceObservation,
    RecommendationState,
)
from mimit.inventory import exact_days_remaining


class State(StrEnum):
    OK = "OK"
    BUY_SOON = "BUY SOON"
    BUY_NOW = "BUY NOW"


@dataclass(frozen=True)
class RecommendationConfig:
    allowed_chat_id: int | None = None
    history_days: int = 30
    minimum_observations: int = 3
    maximum_price_age_hours: int = 48
    discount_ratio: Decimal = Decimal("0.90")

    def __post_init__(self) -> None:
        if (
            self.history_days <= 0
            or self.minimum_observations < 3
            or self.maximum_price_age_hours <= 0
            or not self.discount_ratio.is_finite()
            or not Decimal(0) < self.discount_ratio <= Decimal(1)
        ):
            raise ValueError("Invalid recommendation configuration")


DEFAULT_CONFIG = RecommendationConfig()


@dataclass(frozen=True)
class PriceEvidence:
    current: Decimal | None = None
    median: Decimal | None = None
    fallback: str | None = None


@dataclass(frozen=True)
class Decision:
    state: State
    reason: str
    days_remaining: Fraction

    @property
    def summary(self) -> str:
        return f"{self.state}: {self.reason}"


def decide(
    days_remaining: Fraction,
    reserve_days: int,
    evidence: PriceEvidence,
    config: RecommendationConfig = DEFAULT_CONFIG,
) -> Decision:
    """Compare exact depletion and Decimal prices before any presentation rounding."""
    if days_remaining <= reserve_days:
        state = State.BUY_NOW
        reason = "Stock is at or below your reserve."
    elif days_remaining <= 2 * reserve_days:
        if (
            evidence.current is not None
            and evidence.median is not None
            and evidence.fallback is None
            and evidence.current <= config.discount_ratio * evidence.median
        ):
            state = State.BUY_NOW
            percent = format((1 - config.discount_ratio) * 100, "f")
            reason = f"Low stock; unit price is at least {percent}% below the recent median."
        else:
            state = State.BUY_SOON
            reason = "Stock is within twice your reserve."
    else:
        state = State.OK
        reason = "Stock is above twice your reserve."
    if evidence.fallback:
        reason += f" Stock only: {evidence.fallback}."
    return Decision(state, reason, days_remaining)


def price_evidence(
    observations: list[PriceObservation],
    variant: str | None,
    now: datetime,
    config: RecommendationConfig = DEFAULT_CONFIG,
) -> PriceEvidence:
    """Use the latest attempt and preceding comparable, successful observations."""
    eligible = [row for row in observations if row.observed_at <= now]
    if not eligible:
        return PriceEvidence(fallback="no price checks yet")
    latest = max(eligible, key=lambda row: (row.observed_at, row.id))
    if latest.outcome != "success":
        return PriceEvidence(fallback="latest price check failed")
    if latest.variant_snapshot != variant:
        return PriceEvidence(fallback="price variant does not match")
    if latest.unit_price is None or latest.currency is None or latest.unit is None:
        return PriceEvidence(fallback="unit price unavailable")
    if now - latest.observed_at > timedelta(hours=config.maximum_price_age_hours):
        return PriceEvidence(fallback="price is stale")
    cutoff = now - timedelta(days=config.history_days)
    preceding = [
        row.unit_price
        for row in eligible
        if row.id != latest.id
        and cutoff <= row.observed_at < latest.observed_at
        and row.outcome == "success"
        and row.variant_snapshot == latest.variant_snapshot
        and row.currency == latest.currency
        and row.unit == latest.unit
        and row.unit_price is not None
    ]
    if len(preceding) < config.minimum_observations:
        return PriceEvidence(fallback="too little comparable price history")
    baseline = median(preceding)
    return PriceEvidence(current=latest.unit_price, median=baseline)


async def recommendation_for_item(
    session: AsyncSession,
    item: Consumable,
    now: datetime,
    config: RecommendationConfig = DEFAULT_CONFIG,
) -> Decision:
    """Read a recommendation without changing state or queuing notifications."""
    source = await session.scalar(select(OfferSource).where(OfferSource.consumable_id == item.id))
    evidence = PriceEvidence(fallback="no tracked price source")
    if source is not None:
        # Include the most recent old attempt so stale and never-checked differ.
        latest_id = (
            select(PriceObservation.id)
            .where(
                PriceObservation.offer_source_id == source.id, PriceObservation.observed_at <= now
            )
            .order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        rows = list(
            await session.scalars(
                select(PriceObservation).where(
                    PriceObservation.offer_source_id == source.id,
                    PriceObservation.observed_at <= now,
                    (PriceObservation.observed_at >= now - timedelta(days=config.history_days))
                    | (PriceObservation.id == latest_id),
                )
            )
        )
        evidence = price_evidence(rows, source.variant, now, config)
    return decide(exact_days_remaining(item, now), item.reserve_days, evidence, config)


def _bounded_text(value: str, limit: int = 512) -> str:
    """Bound Unicode text by Telegram UTF-16 units, preserving valid characters."""
    result: list[str] = []
    units = 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit - 1:
            return "".join(result) + "…"
        result.append(char)
        units += width
    return "".join(result)


async def evaluate_item(
    session: AsyncSession,
    item: Consumable,
    now: datetime,
    config: RecommendationConfig = DEFAULT_CONFIG,
) -> Decision:
    """Persist decision and intent atomically under the caller's household lock.

    Flush first so just-appended observations and stock changes participate.
    An initial actionable state counts as entering that state. Unchanged states
    and recovery to OK remain silent. Superseded undelivered intents are fenced;
    an already in-flight Telegram request cannot be retracted.
    """
    await session.flush()
    decision = await recommendation_for_item(session, item, now, config)
    stored = await session.get(RecommendationState, item.id)
    previous = stored.state if stored is not None else None
    if stored is None:
        stored = RecommendationState(
            consumable_id=item.id,
            state=decision.state.value,
            reason=decision.reason,
            generation=0,
            evaluated_at=now,
            changed_at=now,
        )
        session.add(stored)
    stored.reason = decision.reason
    stored.evaluated_at = now
    if previous == decision.state:
        # Keep unsent rationale current without creating another alert. A request
        # already copied by a sender can still complete with its previous text.
        await session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.dedupe_key == f"recommendation:{item.id}:{stored.generation}",
                NotificationOutbox.state == "pending",
            )
            .values(
                payload=NotificationOutbox.payload.op("||")(
                    {
                        "text": f"{_bounded_text(item.name)} — {decision.summary}\nItem: {item.id}",
                    }
                )
            )
        )
        return decision
    stored.state = decision.state.value
    stored.generation += 1
    stored.changed_at = now
    await session.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.dedupe_key.startswith(f"recommendation:{item.id}:"),
            NotificationOutbox.state.in_(["pending", "sending"]),
        )
        .values(
            state="cancelled",
            last_error="recommendation_superseded",
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if decision.state is not State.OK and config.allowed_chat_id is not None:
        expected_household = uuid5(NAMESPACE_URL, f"mimit:telegram:chat:{config.allowed_chat_id}")
        if item.household_id == expected_household:
            session.add(
                NotificationOutbox(
                    dedupe_key=f"recommendation:{item.id}:{stored.generation}",
                    payload={
                        "chat_id": config.allowed_chat_id,
                        "text": f"{_bounded_text(item.name)} — {decision.summary}\nItem: {item.id}",
                    },
                    run_at=now,
                    created_at=now,
                )
            )
    await session.flush()
    return decision
