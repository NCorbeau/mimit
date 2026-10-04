"""Real PostgreSQL proof of daily job contention, fencing, recurrence and rollback."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from mimit.clock import FrozenClock
from mimit.db.models import Consumable, Household, NotificationOutbox, OfferSource, ScheduledJob
from mimit.db.session import get_session_factory
from mimit.jobs.service import (
    DAILY_INTERVAL,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    claim_batch,
    ensure_initial_check,
    reconcile_initial_checks,
    run_once,
    settle,
)
from mimit.products.types import ErrorCode, ProductCheckError

from .test_products import NOW, Extractor, Fetcher, observations, seed, service


async def jobs(engine: AsyncEngine) -> list[ScheduledJob]:
    async with get_session_factory(engine)() as session:
        return list(await session.scalars(select(ScheduledJob).order_by(ScheduledJob.run_at)))


async def initial(engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    ids = await seed(engine)
    await reconcile_initial_checks(get_session_factory(engine), FrozenClock(NOW))
    return ids


async def test_concurrent_initial_reconciliation_and_onboarding_are_idempotent(
    engine: AsyncEngine,
) -> None:
    _, item_id, _ = await seed(engine)
    sessions = get_session_factory(engine)
    results = await asyncio.gather(
        reconcile_initial_checks(sessions, FrozenClock(NOW)),
        reconcile_initial_checks(sessions, FrozenClock(NOW + timedelta(seconds=1))),
    )
    assert sum(results) == 1
    async with sessions.begin() as session:
        assert not await ensure_initial_check(session, item_id, NOW)
    assert len(await jobs(engine)) == 1
    assert await reconcile_initial_checks(sessions, FrozenClock(NOW)) == 0


async def test_two_workers_never_share_valid_lease(engine: AsyncEngine) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    results = await asyncio.gather(
        claim_batch(sessions, FrozenClock(NOW)), claim_batch(sessions, FrozenClock(NOW))
    )
    winners = [claim for batch in results for claim in batch]
    assert len(winners) == 1
    (row,) = await jobs(engine)
    assert row.state == "running" and row.attempts == 1
    assert row.lease_owner == winners[0].owner
    assert row.lease_expires_at == NOW + timedelta(seconds=LEASE_SECONDS)


async def test_skip_locked_claim_never_waits_on_busy_job(engine: AsyncEngine) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    async with sessions.begin() as session:
        await session.scalar(select(ScheduledJob).with_for_update())
        assert await asyncio.wait_for(claim_batch(sessions, FrozenClock(NOW)), 1) == []
    assert len(await claim_batch(sessions, FrozenClock(NOW))) == 1


async def test_claim_batch_is_bounded_and_claims_other_due_work(engine: AsyncEngine) -> None:
    for _ in range(3):
        await seed(engine)
    sessions = get_session_factory(engine)
    assert await reconcile_initial_checks(sessions, FrozenClock(NOW), limit=2) == 2
    assert await reconcile_initial_checks(sessions, FrozenClock(NOW)) == 1
    claims = await claim_batch(sessions, FrozenClock(NOW), limit=2)
    assert len(claims) == 2
    assert len(await claim_batch(sessions, FrozenClock(NOW), limit=2)) == 1


async def test_restart_reclaims_expired_job_and_fences_stale_observation(
    engine: AsyncEngine,
) -> None:
    _, item_id, _ = await initial(engine)
    sessions = get_session_factory(engine)
    (old,) = await claim_batch(sessions, FrozenClock(NOW))
    prepared = await service(engine).prepare(item_id)
    later = FrozenClock(NOW + timedelta(seconds=LEASE_SECONDS))
    assert not await settle(sessions, later, old, prepared)
    assert await observations(engine) == []
    (new,) = await claim_batch(get_session_factory(engine), later)
    assert new.owner != old.owner and new.attempts == 2
    assert not await settle(sessions, later, old, prepared)
    assert await settle(sessions, later, new, prepared)
    assert not await settle(sessions, later, new, prepared)
    assert len(await observations(engine)) == 1
    stored = await jobs(engine)
    assert [row.state for row in stored] == ["succeeded", "pending"]
    assert stored[1].run_at == NOW + DAILY_INTERVAL


async def test_fetch_runs_after_claim_commit_without_inventory_locks(engine: AsyncEngine) -> None:
    household_id, item_id, source_id = await initial(engine)
    sessions = get_session_factory(engine)

    async def during_fetch() -> None:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'idle in transaction'"
                    )
                )
                == 0
            )
        async with sessions.begin() as session:
            locks: list[tuple[type[Household] | type[Consumable] | type[OfferSource], UUID]] = [
                (Household, household_id),
                (Consumable, item_id),
                (OfferSource, source_id),
            ]
            for model, row_id in locks:
                assert (
                    await session.scalar(
                        select(model).where(model.id == row_id).with_for_update(nowait=True)
                    )
                    is not None
                )
            (row,) = await jobs(engine)
            assert row.state == "running" and row.attempts == 1

    assert await run_once(sessions, FrozenClock(NOW), service(engine, Fetcher(hook=during_fetch)))
    assert len(await observations(engine)) == 1


async def test_transient_failure_retries_same_slot_then_success_once(engine: AsyncEngine) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    fetcher = Fetcher(error=ProductCheckError(ErrorCode.HTTP_ERROR, http_status=503))
    await run_once(sessions, FrozenClock(NOW), service(engine, fetcher), jitter=lambda a, b: b)
    (retry,) = await jobs(engine)
    assert retry.state == "pending" and retry.attempts == 1 and retry.last_error == "http_error"
    assert retry.run_at == NOW + timedelta(seconds=10)
    assert retry.payload["slot_at"] == NOW.isoformat()
    assert await observations(engine) == []
    assert not await run_once(sessions, FrozenClock(NOW + timedelta(seconds=9)), service(engine))
    assert await run_once(sessions, FrozenClock(retry.run_at), service(engine))
    rows = await jobs(engine)
    assert rows[0].id == retry.id and rows[0].state == "succeeded" and rows[0].attempts == 2
    assert rows[1].run_at == NOW + DAILY_INTERVAL
    assert len(await observations(engine)) == 1


@pytest.mark.parametrize(
    "code,status",
    [(ErrorCode.UNSAFE_URL, None), (ErrorCode.HTTP_ERROR, 404), (ErrorCode.INVALID_PRICE, None)],
)
async def test_permanent_failure_records_result_and_preserves_future_checks(
    engine: AsyncEngine, code: ErrorCode, status: int | None
) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    await run_once(
        sessions,
        FrozenClock(NOW),
        service(engine, Fetcher(error=ProductCheckError(code, http_status=status))),
    )
    rows = await jobs(engine)
    assert [row.state for row in rows] == ["failed", "pending"]
    assert rows[0].attempts == 1 and rows[0].last_error == code.value
    assert rows[1].run_at == NOW + DAILY_INTERVAL
    (observation,) = await observations(engine)
    assert observation.outcome == "failed" and observation.error_code == code.value


async def test_exhausted_transient_failure_is_terminal_with_one_observation(
    engine: AsyncEngine,
) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    checker = service(engine, Fetcher(error=ProductCheckError(ErrorCode.TIMEOUT)))
    now = NOW
    for attempt in range(MAX_ATTEMPTS):
        await run_once(sessions, FrozenClock(now), checker, jitter=lambda a, b: 0)
        rows = await jobs(engine)
        if attempt < MAX_ATTEMPTS - 1:
            assert await observations(engine) == []
            now = rows[0].run_at
    assert len(await observations(engine)) == 1
    assert [row.state for row in rows] == ["failed", "pending"]
    assert rows[0].attempts == MAX_ATTEMPTS and rows[0].last_error == "timeout"


async def test_final_attempt_crash_settles_without_more_http_and_continues_daily(
    engine: AsyncEngine,
) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    for attempt in range(MAX_ATTEMPTS):
        claims = await claim_batch(
            sessions, FrozenClock(NOW + timedelta(seconds=attempt * LEASE_SECONDS))
        )
        assert len(claims) == 1 and not claims[0].exhausted
    now = NOW + timedelta(seconds=MAX_ATTEMPTS * LEASE_SECONDS)
    fetcher = Fetcher()
    await run_once(sessions, FrozenClock(now), service(engine, fetcher))
    rows = await jobs(engine)
    assert [row.state for row in rows] == ["failed", "pending"]
    assert rows[0].last_error == "attempts_exhausted" and rows[0].attempts == MAX_ATTEMPTS
    assert fetcher.urls == [] and await observations(engine) == []
    assert rows[1].run_at == NOW + DAILY_INTERVAL


async def test_observation_evaluation_ack_and_next_slot_rollback_together(
    engine: AsyncEngine,
) -> None:
    _, item_id, _ = await initial(engine)
    sessions = get_session_factory(engine)
    (claim,) = await claim_batch(sessions, FrozenClock(NOW))
    prepared = await service(engine).prepare(item_id)

    async def fail_evaluation(session: AsyncSession, item: Consumable, now: object) -> None:
        assert len(await observations(engine)) == 0  # Uncommitted result cannot leak.
        session.add(
            NotificationOutbox(dedupe_key="test:atomic", payload={}, run_at=NOW, created_at=NOW)
        )
        await session.flush()
        raise RuntimeError("simulate failure after outbox write")

    with pytest.raises(RuntimeError):
        await settle(sessions, FrozenClock(NOW), claim, prepared, evaluator=fail_evaluation)
    assert await observations(engine) == []
    (row,) = await jobs(engine)
    assert row.state == "running" and row.lease_owner == claim.owner
    async with sessions() as session:
        assert list(await session.scalars(select(NotificationOutbox))) == []
    assert await settle(sessions, FrozenClock(NOW), claim, prepared)
    assert len(await jobs(engine)) == 2 and len(await observations(engine)) == 1


async def test_downtime_coalesces_past_days_without_drifting_daily_anchor(
    engine: AsyncEngine,
) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    later = NOW + timedelta(days=5, hours=3)
    checker = service(engine)
    checker.clock = FrozenClock(later)
    await run_once(sessions, FrozenClock(later), checker)
    rows = await jobs(engine)
    assert len(rows) == 2 and rows[1].run_at == NOW + timedelta(days=6)
    (observation,) = await observations(engine)
    assert observation.observed_at == later
    assert not await run_once(sessions, FrozenClock(later), checker)


async def test_cancelled_fetch_leaves_recoverable_lease_and_no_partial_result(
    engine: AsyncEngine,
) -> None:
    await initial(engine)
    sessions = get_session_factory(engine)
    started = asyncio.Event()

    async def blocked() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        run_once(sessions, FrozenClock(NOW), service(engine, Fetcher(hook=blocked)))
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await observations(engine) == []
    (row,) = await jobs(engine)
    assert row.state == "running"
    later = FrozenClock(NOW + timedelta(seconds=LEASE_SECONDS))
    assert await run_once(get_session_factory(engine), later, service(engine))
    assert len(await observations(engine)) == 1


async def test_source_change_fences_result_and_preserves_next_daily_work(
    engine: AsyncEngine,
) -> None:
    _, _, source_id = await initial(engine)
    sessions = get_session_factory(engine)

    async def change_source() -> None:
        async with sessions.begin() as session:
            source = await session.get(OfferSource, source_id)
            assert source is not None
            source.variant = "different"

    await run_once(
        sessions, FrozenClock(NOW), service(engine, Fetcher(hook=change_source), Extractor())
    )
    assert await observations(engine) == []
    rows = await jobs(engine)
    assert [row.state for row in rows] == ["failed", "pending"]
    assert rows[0].last_error == "source_changed"


async def test_real_recommendation_commits_with_observation_and_daily_success(
    engine: AsyncEngine,
) -> None:
    from datetime import datetime
    from decimal import Decimal
    from uuid import NAMESPACE_URL, uuid5

    from mimit.db.models import RecommendationState
    from mimit.recommendations import RecommendationConfig, evaluate_item

    _, item_id, _ = await initial(engine)
    sessions = get_session_factory(engine)
    household_id = uuid5(NAMESPACE_URL, "mimit:telegram:chat:17")
    async with sessions.begin() as session:
        session.add(Household(id=household_id, name="Telegram", created_at=NOW))
        await session.flush()
        item = await session.get(Consumable, item_id)
        assert item is not None
        item.household_id = household_id
        item.stock_quantity = Decimal(0)

    async def evaluate(session: AsyncSession, item: Consumable, now: datetime) -> None:
        await evaluate_item(session, item, now, RecommendationConfig(allowed_chat_id=17))

    await run_once(sessions, FrozenClock(NOW), service(engine), evaluator=evaluate)
    assert len(await observations(engine)) == 1
    assert [row.state for row in await jobs(engine)] == ["succeeded", "pending"]
    async with sessions() as session:
        decision = await session.get(RecommendationState, item_id)
        assert decision is not None and decision.state == "BUY NOW"
        intents = list(await session.scalars(select(NotificationOutbox)))
        assert len(intents) == 1 and intents[0].payload["chat_id"] == 17


async def test_lease_expiring_during_evaluation_rolls_back_all_writes(engine: AsyncEngine) -> None:
    from dataclasses import dataclass
    from datetime import datetime

    @dataclass
    class MutableClock:
        instant: datetime = NOW

        def now(self) -> datetime:
            return self.instant

    clock = MutableClock()
    _, item_id, _ = await initial(engine)
    sessions = get_session_factory(engine)
    (claim,) = await claim_batch(sessions, clock)
    prepared = await service(engine).prepare(item_id)

    async def evaluate(session: AsyncSession, item: Consumable, now: datetime) -> None:
        item.name = "must roll back"
        await session.flush()
        clock.instant = NOW + timedelta(seconds=LEASE_SECONDS)

    assert not await settle(sessions, clock, claim, prepared, evaluator=evaluate)
    assert await observations(engine) == []
    (row,) = await jobs(engine)
    assert row.state == "running"
    async with sessions() as session:
        item = await session.get(Consumable, item_id)
        assert item is not None and item.name == "Cat food"


async def test_initial_job_rolls_back_with_onboarding_transaction(engine: AsyncEngine) -> None:
    _, item_id, _ = await seed(engine)
    sessions = get_session_factory(engine)
    with pytest.raises(RuntimeError):
        async with sessions.begin() as session:
            assert await ensure_initial_check(session, item_id, NOW)
            raise RuntimeError("onboarding transaction failed")
    assert await jobs(engine) == []
    assert await reconcile_initial_checks(sessions, FrozenClock(NOW)) == 1
