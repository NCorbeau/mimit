"""Real PostgreSQL only: a randomly named database, migrated from zero."""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

ROOT = Path(__file__).resolve().parents[2]


def migrate(database_url: str, *args: str) -> None:
    subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.fail(
            "Set TEST_DATABASE_URL to a PostgreSQL role with CREATEDB for integration tests"
        )
    url = make_url(admin_url)
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("TEST_DATABASE_URL must use postgresql+asyncpg")
    database_name = f"mimit_test_{uuid4().hex}"

    async def manage_database(*, create: bool) -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                command = (
                    f'CREATE DATABASE "{database_name}"'
                    if create
                    else f'DROP DATABASE "{database_name}" WITH (FORCE)'
                )
                await connection.execute(text(command))
        finally:
            await engine.dispose()

    asyncio.run(manage_database(create=True))
    test_url = url.set(database=database_name).render_as_string(hide_password=False)
    try:
        migrate(test_url, "upgrade", "head")
        yield test_url
    finally:
        asyncio.run(manage_database(create=False))


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            tables = await connection.scalars(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename != 'alembic_version'"
                )
            )
            names = ", ".join(f'"{name}"' for name in tables)
            if names:
                await connection.execute(text(f"TRUNCATE {names} CASCADE"))
        yield engine
    finally:
        await engine.dispose()
