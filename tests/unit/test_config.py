import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from mimit.config import Settings, get_settings

DATABASE_URL = "postgresql://mimit:database-password@localhost:55439/mimit"
ENVIRONMENT_KEYS = (
    "DATABASE_URL",
    "HOUSEHOLD_TIMEZONE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "PUBLIC_BASE_URL",
    "TELEGRAM_ALLOWED_USER_ID",
    "TELEGRAM_ALLOWED_CHAT_ID",
)


@pytest.fixture(autouse=True)
def isolated_environment(
    pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    for key in ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(pytestconfig.rootpath / "tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_settings(**values: Any) -> Settings:
    return Settings(_env_file=None, DATABASE_URL=DATABASE_URL, **values)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_postgresql_url_normalizes_to_async_driver(scheme: str) -> None:
    settings = Settings(_env_file=None, DATABASE_URL=f"{scheme}://user:password@localhost/db")
    assert settings.database_url == "postgresql+asyncpg://user:password@localhost/db"
    assert settings.household_timezone == "Europe/Warsaw"


def test_configuration_is_lazy_and_database_is_required() -> None:
    import mimit.api
    import mimit.config

    importlib.reload(mimit.api)
    importlib.reload(mimit.config)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///database-password.db",
        "postgresql://user:database-password@localhost",
        "postgresql://user:database-password@localhost:bad/db",
        "postgresql://user:database-password@localhost/db#fragment",
        "postgresql://user:database-password@/db",
    ],
)
def test_invalid_database_url_errors_do_not_leak_secrets(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, DATABASE_URL=url)
    rendered = (
        str(caught.value) + repr(caught.value) + repr(caught.value.errors()) + caught.value.json()
    )
    assert "database-password" not in rendered


def test_settings_representation_masks_credentials() -> None:
    settings = make_settings(
        TELEGRAM_BOT_TOKEN="123456:token-secret", TELEGRAM_WEBHOOK_SECRET="webhook-secret"
    )
    rendered = str(settings) + repr(settings) + settings.model_dump_json()
    for secret in ("database-password", "token-secret", "webhook-secret"):
        assert secret not in rendered


@pytest.mark.parametrize(
    "values",
    [
        {"HOUSEHOLD_TIMEZONE": "Not/A_Timezone"},
        {"PUBLIC_BASE_URL": "http://example.com"},
        {"PUBLIC_BASE_URL": "https://user:private-secret@example.com"},
        {"PUBLIC_BASE_URL": "https://example.com/?token=private-secret"},
        {"TELEGRAM_BOT_TOKEN": "private-secret with-spaces"},
        {"TELEGRAM_WEBHOOK_SECRET": ""},
        {"TELEGRAM_ALLOWED_USER_ID": 0},
        {"TELEGRAM_ALLOWED_CHAT_ID": 0},
    ],
)
def test_bad_optional_configuration_is_rejected_without_exposing_inputs(
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(**values)
    rendered = str(caught.value) + repr(caught.value.errors())
    assert "private-secret" not in rendered
    assert "database-password" not in rendered


def test_telegram_gate_and_allowlist_fail_closed() -> None:
    settings = make_settings()
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        settings.require_telegram_configuration()
    assert not settings.allows_telegram_identity(user_id=123, chat_id=456)
    partial = make_settings(TELEGRAM_ALLOWED_USER_ID=123)
    assert not partial.allows_telegram_identity(user_id=123, chat_id=456)


def test_complete_telegram_configuration_requires_both_identity_matches() -> None:
    settings = make_settings(
        TELEGRAM_BOT_TOKEN="123456:token-secret",
        TELEGRAM_WEBHOOK_SECRET="webhook-secret",
        PUBLIC_BASE_URL="https://example.com/mimit/",
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_ALLOWED_CHAT_ID=-456,
    )
    settings.require_telegram_configuration()
    assert settings.public_base_url == "https://example.com/mimit"
    assert settings.allows_telegram_identity(user_id=123, chat_id=-456)
    assert not settings.allows_telegram_identity(user_id=999, chat_id=-456)
    assert not settings.allows_telegram_identity(user_id=123, chat_id=999)


def test_environment_configuration_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    first = get_settings()
    monkeypatch.setenv("HOUSEHOLD_TIMEZONE", "UTC")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().household_timezone == "UTC"
