"""PostgreSQL jobs: one observation per logical slot, recoverable HTTP attempts.

Fetching can repeat after a crash. Only a current lease may append its result,
settle the job, evaluate recommendations and enqueue the next daily slot. Those
writes share one transaction. Retries keep the original slot identity.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import Text, and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock
from mimit.db.models import Consumable, Household, OfferSource, ScheduledJob
from mimit.observability import elapsed_ms, log_event
from mimit.products.service import (
    PreparedCheck,
    PriceCheckServiceError,
    ProductCheckService,
    append_observation,
    lock_source,
)
from mimit.products.types import ErrorCode

JOB_TYPE = "price_check"
DAILY_INTERVAL = timedelta(days=1)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 120
RECONCILE_BATCH = 100
Evaluator = Callable[[AsyncSession, Consumable, datetime], Awaitable[None]]


@dataclass(frozen=True)
class JobPayload:
    consumable_id: UUID
    slot_at: datetime

    def as_dict(self) -> dict[str, str]:
        return {"consumable_id": str(self.consumable_id), "slot_at": self.slot_at.isoformat()}


@dataclass(frozen=True)
class Claim:
    id: UUID
    owner: str
    attempts: int
    expires_at: datetime
    payload: dict[str, Any] = field(repr=False)
    exhausted: bool = False


class _ExpiredLease(Exception):
    pass


def parse_payload(payload: dict[str, Any]) -> JobPayload | None:
    if set(payload) != {"consumable_id", "slot_at"}:
        return None
    try:
        item_id = UUID(payload["consumable_id"])
        slot = datetime.fromisoformat(payload["slot_at"])
    except (ValueError, TypeError, AttributeError):
        return None
    if slot.tzinfo is None or slot.utcoffset() is None:
        return None
    return JobPayload(item_id, slot.astimezone(UTC))


def next_slot(slot: datetime, now: datetime) -> datetime:
    """Preserve the daily anchor, coalescing missed slots into one current check."""
    return slot + DAILY_INTERVAL * max(1, (now - slot) // DAILY_INTERVAL + 1)


async def _enqueue(session: AsyncSession, payload: JobPayload, key: str, now: datetime) -> bool:
    row_id = await session.scalar(
        insert(ScheduledJob)
        .values(
            id=uuid4(),
            dedupe_key=key,
            job_type=JOB_TYPE,
            payload=payload.as_dict(),
            run_at=payload.slot_at,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[ScheduledJob.dedupe_key])
        .returning(ScheduledJob.id)
    )
    return row_id is not None


async def ensure_initial_check(session: AsyncSession, consumable_id: UUID, now: datetime) -> bool:
    """Caller owns commit; use in onboarding to commit the first job with the item."""
    return await _enqueue(
        session,
        JobPayload(consumable_id, now.astimezone(UTC)),
        f"price:{consumable_id}:initial",
        now,
    )


async def reconcile_initial_checks(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, *, limit: int = RECONCILE_BATCH
) -> int:
    """Backfill existing items in bounded batches; concurrent scans are idempotent."""
    if not 1 <= limit <= RECONCILE_BATCH:
        raise ValueError("invalid reconciliation batch size")
    now = clock.now()
    async with sessions.begin() as session:
        ids = await session.scalars(
            select(Consumable.id)
            .join(OfferSource, OfferSource.consumable_id == Consumable.id)
            .where(
                ~exists().where(
                    ScheduledJob.dedupe_key == "price:" + Consumable.id.cast(Text) + ":initial"
                )
            )
            .order_by(Consumable.created_at, Consumable.id)
            .limit(limit)
        )
        count = 0
        for item_id in ids:
            count += await ensure_initial_check(session, item_id, now)
    return count


async def claim_batch(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, *, limit: int = 1
) -> list[Claim]:
    """Commit SKIP LOCKED claims before any external work or inventory locks."""
    if not 1 <= limit <= 8:
        raise ValueError("invalid claim batch size")
    async with sessions.begin() as session:
        now = clock.now()
        rows = await session.scalars(
            select(ScheduledJob)
            .where(
                ScheduledJob.job_type == JOB_TYPE,
                or_(
                    and_(ScheduledJob.state == "pending", ScheduledJob.run_at <= now),
                    and_(ScheduledJob.state == "running", ScheduledJob.lease_expires_at <= now),
                ),
            )
            .order_by(ScheduledJob.run_at, ScheduledJob.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        claims = []
        for row in rows:
            exhausted = row.attempts >= MAX_ATTEMPTS
            row.state = "running"
            row.lease_owner = str(uuid4())
            row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            if not exhausted:
                row.attempts += 1
            claims.append(
                Claim(
                    row.id,
                    row.lease_owner,
                    row.attempts,
                    row.lease_expires_at,
                    dict(row.payload) if isinstance(row.payload, dict) else {},
                    exhausted,
                )
            )
    for claim in claims:
        log_event("jobs.claimed", job_id=claim.id, lease_owner=claim.owner, attempts=claim.attempts)
    return claims


def is_transient(prepared: PreparedCheck) -> bool:
    error = prepared.result.error_code
    if error in {ErrorCode.TIMEOUT, ErrorCode.TRANSPORT_ERROR, ErrorCode.RATE_LIMITED}:
        return True
    if error == ErrorCode.HTTP_ERROR:
        status = prepared.metadata.get("http_status")
        return isinstance(status, int) and (status in {408, 429} or 500 <= status <= 599)
    return False


async def _locked_job(session: AsyncSession, clock: Clock, claim: Claim) -> ScheduledJob:
    row = await session.scalar(
        select(ScheduledJob).where(ScheduledJob.id == claim.id).with_for_update()
    )
    if (
        row is None
        or row.state != "running"
        or row.lease_owner != claim.owner
        or row.lease_expires_at is None
        or row.lease_expires_at <= clock.now()
    ):
        raise _ExpiredLease
    return row


async def _lock_item(session: AsyncSession, item_id: UUID) -> Consumable | None:
    household_id = await session.scalar(
        select(Consumable.household_id).where(Consumable.id == item_id)
    )
    if household_id is None:
        return None
    await session.scalar(select(Household).where(Household.id == household_id).with_for_update())
    item = await session.scalar(
        select(Consumable).where(Consumable.id == item_id).with_for_update()
    )
    if item is None or item.household_id != household_id:
        raise PriceCheckServiceError("source_changed")
    return item


async def settle(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    claim: Claim,
    prepared: PreparedCheck | None,
    *,
    error_code: str | None = None,
    evaluator: Evaluator | None = None,
    jitter: Callable[[float, float], float] = random.uniform,
) -> bool:
    """Atomic fenced result/recommendation/ack/recurrence; stale leases write nothing."""
    started = perf_counter()
    payload = parse_payload(claim.payload)
    try:
        async with sessions.begin() as session:
            # Claim transactions hold only job locks. Domain work always takes household,
            # item and source locks before its job lock; no network occurs here.
            item = None
            if prepared is not None:
                if payload is None or prepared.source.consumable_id != payload.consumable_id:
                    raise ValueError("prepared check does not match job")
                item = await lock_source(session, prepared.source)
            elif payload is not None:
                item = await _lock_item(session, payload.consumable_id)
            row = await _locked_job(session, clock, claim)
            now = clock.now()
            retry = (
                prepared is not None and is_transient(prepared) and claim.attempts < MAX_ATTEMPTS
            )
            if prepared is not None:
                error_code = (
                    prepared.result.error_code.value
                    if prepared.result.error_code is not None
                    else None
                )
            if retry:
                base = min(5 * 2 ** (claim.attempts - 1), 300)
                delay = min(base + max(0, jitter(0, base)), 300)
                state = "pending"
                row.run_at = now + timedelta(seconds=delay)
            else:
                if prepared is not None:
                    # Deterministic observation identity is an additional uniqueness fence.
                    prepared = replace(
                        prepared,
                        result=replace(prepared.result, observation_id=uuid5(claim.id, "result")),
                    )
                    append_observation(session, prepared)
                    await session.flush()
                state = "succeeded" if prepared is not None and error_code is None else "failed"
                if item is not None:
                    if evaluator is not None:
                        await evaluator(session, item, now)
                    assert payload is not None
                    future = next_slot(payload.slot_at, now)
                    await _enqueue(
                        session,
                        JobPayload(item.id, future),
                        f"price:{item.id}:{future.isoformat()}",
                        now,
                    )
            if clock.now() >= claim.expires_at:
                raise _ExpiredLease
            row.state = state
            row.last_error = error_code
            row.lease_owner = None
            row.lease_expires_at = None
    except _ExpiredLease:
        log_event(
            "jobs.settled",
            job_id=claim.id,
            acknowledged=False,
            outcome="stale_lease",
            duration_ms=elapsed_ms(started),
        )
        return False
    log_event(
        "jobs.settled",
        job_id=claim.id,
        acknowledged=True,
        outcome=row.state,
        consumable_id=payload.consumable_id if payload is not None else None,
        observation_id=prepared.result.observation_id
        if prepared is not None and not retry
        else None,
        attempts=claim.attempts,
        error_code=error_code,
        duration_ms=elapsed_ms(started),
    )
    return True


async def run_once(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    checker: ProductCheckService,
    *,
    evaluator: Evaluator | None = None,
    jitter: Callable[[float, float], float] = random.uniform,
) -> bool:
    claims = await claim_batch(sessions, clock)
    if not claims:
        return False
    (claim,) = claims
    if clock.now() >= claim.expires_at:
        return True
    payload = parse_payload(claim.payload)
    prepared = None
    error = None
    if payload is None:
        error = "invalid_payload"
    elif claim.exhausted:
        error = "attempts_exhausted"
    else:
        try:
            prepared = await checker.prepare(payload.consumable_id)
        except PriceCheckServiceError as exc:
            if exc.code == "persistence_error":
                raise
            error = exc.code
    try:
        await settle(
            sessions, clock, claim, prepared, error_code=error, evaluator=evaluator, jitter=jitter
        )
    except PriceCheckServiceError as exc:
        if exc.code != "source_changed":
            raise
        # A changed source cannot inherit the fetched result. Record the safe failure
        # and still preserve tomorrow's check for the current item/source.
        await settle(
            sessions, clock, claim, None, error_code=exc.code, evaluator=evaluator, jitter=jitter
        )
    return True
