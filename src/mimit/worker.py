"""Private worker: independently poll daily checks, scheduling and Telegram outbox."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from mimit.clock import SystemClock
from mimit.config import get_settings
from mimit.db.models import Consumable
from mimit.db.session import create_engine, get_session_factory
from mimit.jobs.service import reconcile_initial_checks, run_once
from mimit.observability import configure_logging, log_event
from mimit.products.extractor import JsonLdExtractor
from mimit.products.fetcher import SafeProductFetcher
from mimit.products.service import ProductCheckService
from mimit.recommendations import RecommendationConfig, evaluate_item
from mimit.telegram.sender import HTTP_TIMEOUT, TelegramSender, send_once

Operation = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class WorkerConfig:
    poll_seconds: float = 1.0
    shutdown_grace_seconds: float = 30.0
    price_concurrency: int = 2

    def __post_init__(self) -> None:
        if not 0 < self.poll_seconds <= 60:
            raise ValueError("worker poll must be between zero and 60 seconds")
        if not 0 < self.shutdown_grace_seconds <= 120:
            raise ValueError("worker grace must be between zero and 120 seconds")
        if not 1 <= self.price_concurrency <= 8:
            raise ValueError("worker price concurrency must be between 1 and 8")


async def _poll(operation: Operation, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            await operation()
        except Exception:
            # Raw exceptions may include credentials, SQL parameters or merchant URLs.
            task = asyncio.current_task()
            log_event(
                "worker.iteration_failed",
                level=logging.ERROR,
                error_code="iteration_failed",
                worker=task.get_name() if task is not None else "unknown",
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def run_worker(
    *,
    schedule: Operation,
    check: Operation,
    send: Operation,
    stop: asyncio.Event,
    config: WorkerConfig | None = None,
) -> None:
    """Stop claiming on shutdown; drain active work, then cancel after bounded grace."""
    config = config or WorkerConfig()
    log_event("worker.started")
    tasks = [
        asyncio.create_task(_poll(schedule, stop, config.poll_seconds), name="schedule"),
        asyncio.create_task(_poll(send, stop, config.poll_seconds), name="outbox"),
        *(
            asyncio.create_task(_poll(check, stop, config.poll_seconds), name=f"price-{index}")
            for index in range(config.price_concurrency)
        ),
    ]
    try:
        await stop.wait()
    finally:
        stop.set()
        _, pending = await asyncio.wait(tasks, timeout=config.shutdown_grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log_event("worker.stopped")


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    settings.require_telegram_configuration()
    assert settings.telegram_bot_token is not None
    assert settings.telegram_allowed_chat_id is not None
    configure_logging(
        secrets=[
            settings.database_url,
            settings.telegram_bot_token.get_secret_value(),
            settings.telegram_webhook_secret.get_secret_value()
            if settings.telegram_webhook_secret
            else "",
        ]
    )
    worker_config = WorkerConfig(
        poll_seconds=settings.worker_poll_seconds,
        shutdown_grace_seconds=settings.worker_shutdown_grace_seconds,
        price_concurrency=settings.worker_price_concurrency,
    )
    recommendation_config = RecommendationConfig.from_settings(settings)
    engine = create_engine(settings.database_url)
    clock = SystemClock()
    sessions = get_session_factory(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with (
            httpx.AsyncClient(verify=True, trust_env=False, timeout=HTTP_TIMEOUT) as client,
            SafeProductFetcher() as fetcher,
        ):
            checker = ProductCheckService(sessions, clock, fetcher, JsonLdExtractor())
            sender = TelegramSender(
                token=settings.telegram_bot_token,
                allowed_chat_id=settings.telegram_allowed_chat_id,
                client=client,
            )

            async def schedule() -> object:
                return await reconcile_initial_checks(sessions, clock)

            async def evaluate(session: AsyncSession, item: Consumable, now: datetime) -> None:
                await evaluate_item(session, item, now, recommendation_config)

            async def check() -> object:
                return await run_once(sessions, clock, checker, evaluator=evaluate)

            async def send() -> object:
                return await send_once(
                    sessions, clock, sender, recommendation_config=recommendation_config
                )

            if once:
                await schedule()
                await asyncio.gather(check(), send())
            else:
                await run_worker(
                    schedule=schedule, check=check, send=send, stop=stop, config=worker_config
                )
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one due price/outbox iteration")
    args = parser.parse_args()
    try:
        asyncio.run(run(once=args.once))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception:
        raise SystemExit("Worker failed; check configuration and database availability") from None


if __name__ == "__main__":
    main()
