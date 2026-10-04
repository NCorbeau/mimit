import asyncio

import pytest

from mimit.worker import WorkerConfig, run_worker


async def test_slow_prices_do_not_block_outbox_and_shutdown_drains_active_work() -> None:
    stop, started, release, sent = (asyncio.Event() for _ in range(4))
    checks = 0
    finished = False

    async def check() -> None:
        nonlocal checks, finished
        checks += 1
        started.set()
        await release.wait()
        finished = True

    async def send() -> None:
        sent.set()

    async def schedule() -> None:
        pass

    task = asyncio.create_task(
        run_worker(
            schedule=schedule,
            check=check,
            send=send,
            stop=stop,
            config=WorkerConfig(0.01, 1, 1),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(sent.wait(), 1)
    stop.set()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 1)
    assert checks == 1 and finished


async def test_shutdown_cancels_stuck_work_after_grace_and_stops_claiming() -> None:
    stop, started, cancelled = (asyncio.Event() for _ in range(3))
    active = 0
    maximum = 0

    async def check() -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            cancelled.set()

    async def noop() -> None:
        pass

    task = asyncio.create_task(
        run_worker(
            schedule=noop,
            send=noop,
            check=check,
            stop=stop,
            config=WorkerConfig(0.01, 0.01, 2),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set() and active == 0 and 1 <= maximum <= 2


async def test_worker_recovers_iteration_failure_without_logging_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    attempts = 0

    async def check() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("PRIVATE URL AND DATABASE CREDENTIALS")
        stop.set()

    async def noop() -> None:
        pass

    await asyncio.wait_for(
        run_worker(
            schedule=noop,
            send=noop,
            check=check,
            stop=stop,
            config=WorkerConfig(0.001, 0.01, 1),
        ),
        1,
    )
    assert attempts == 2
    assert "PRIVATE" not in caplog.text


async def test_already_stopped_worker_does_not_claim() -> None:
    stop = asyncio.Event()
    stop.set()

    async def unexpected() -> None:
        pytest.fail("new operation after shutdown")

    await run_worker(schedule=unexpected, send=unexpected, check=unexpected, stop=stop)
