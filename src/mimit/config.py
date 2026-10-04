"""Lazy, validated runtime configuration; importing this module reads no environment."""

from decimal import Decimal
from functools import lru_cache
from re import fullmatch
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    database_url_secret: SecretStr = Field(alias="DATABASE_URL")
    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_webhook_secret: SecretStr | None = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    household_timezone: str = Field(default="Europe/Warsaw", alias="HOUSEHOLD_TIMEZONE")
    telegram_allowed_user_id: int | None = Field(
        default=None, alias="TELEGRAM_ALLOWED_USER_ID", gt=0
    )
    telegram_allowed_chat_id: int | None = Field(default=None, alias="TELEGRAM_ALLOWED_CHAT_ID")

    worker_poll_seconds: float = Field(
        default=1.0, alias="WORKER_POLL_SECONDS", gt=0, le=60, allow_inf_nan=False
    )
    worker_shutdown_grace_seconds: float = Field(
        default=30.0, alias="WORKER_SHUTDOWN_GRACE_SECONDS", gt=0, le=120, allow_inf_nan=False
    )
    worker_price_concurrency: int = Field(default=2, alias="WORKER_PRICE_CONCURRENCY", ge=1, le=8)
    recommendation_history_days: int = Field(
        default=30, alias="RECOMMENDATION_HISTORY_DAYS", ge=1, le=365
    )
    recommendation_discount_fraction: Decimal = Field(
        default=Decimal("0.10"),
        alias="RECOMMENDATION_DISCOUNT_FRACTION",
        gt=0,
        lt=1,
        allow_inf_nan=False,
        decimal_places=6,
    )
    recommendation_min_prior_observations: int = Field(
        default=3, alias="RECOMMENDATION_MIN_PRIOR_OBSERVATIONS", ge=3, le=365
    )
    recommendation_price_max_age_hours: int = Field(
        default=48, alias="RECOMMENDATION_PRICE_MAX_AGE_HOURS", ge=1, le=720
    )

    @model_validator(mode="wrap")
    @classmethod
    def redact_validation_inputs(cls, value: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Keep credentials out of both rendered and structured validation errors."""
        try:
            return handler(value)
        except ValidationError as exc:
            errors: list[InitErrorDetails] = []
            for error in exc.errors(include_url=False):
                sanitized: InitErrorDetails = {
                    "type": error["type"],
                    "loc": error["loc"],
                    "input": "<redacted>",
                }
                if "ctx" in error:
                    sanitized["ctx"] = error["ctx"]
                errors.append(sanitized)
            raise ValidationError.from_exception_data(
                cls.__name__, errors, hide_input=True
            ) from None

    @field_validator("database_url_secret")
    @classmethod
    def normalize_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        try:
            parsed = urlsplit(raw)
            valid = (
                parsed.scheme in {"postgres", "postgresql", "postgresql+asyncpg"}
                and parsed.hostname is not None
                and parsed.port != 0
                and bool(parsed.path.strip("/"))
                and not parsed.fragment
                and not any(character.isspace() for character in raw)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("DATABASE_URL must be a PostgreSQL URL with a host and database")
        return SecretStr(urlunsplit(parsed._replace(scheme="postgresql+asyncpg")))

    @field_validator("telegram_bot_token", "telegram_webhook_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if not raw or any(character.isspace() for character in raw):
                raise ValueError("configured secrets must be nonempty and contain no whitespace")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_bot_token(cls, value: SecretStr | None) -> SecretStr | None:
        if (
            value is not None
            and fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", value.get_secret_value()) is None
        ):
            raise ValueError("TELEGRAM_BOT_TOKEN must have the token format supplied by BotFather")
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if (
            value is not None
            and fullmatch(r"[A-Za-z0-9_-]{1,256}", value.get_secret_value()) is None
        ):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must contain 1-256 letters, digits, _ or -")
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("PUBLIC_BASE_URL must be an absolute HTTPS URL without credentials")
        return value.rstrip("/")

    @field_validator("household_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("HOUSEHOLD_TIMEZONE must be an IANA timezone name") from None
        return value

    @field_validator("telegram_allowed_chat_id")
    @classmethod
    def validate_chat_id(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_ID must be a nonzero integer")
        return value

    @property
    def database_url(self) -> str:
        """Reveal the normalized connection URL only at the connection boundary."""
        return self.database_url_secret.get_secret_value()

    def require_telegram_configuration(self) -> None:
        """Fail closed before the Telegram runtime is enabled."""
        required = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_WEBHOOK_SECRET": self.telegram_webhook_secret,
            "PUBLIC_BASE_URL": self.public_base_url,
            "TELEGRAM_ALLOWED_USER_ID": self.telegram_allowed_user_id,
            "TELEGRAM_ALLOWED_CHAT_ID": self.telegram_allowed_chat_id,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError("Missing Telegram configuration: " + ", ".join(missing))

    @property
    def telegram_configured(self) -> bool:
        """Any Telegram setting attempts to enable the complete runtime contract."""
        return any(
            value is not None
            for value in (
                self.telegram_bot_token,
                self.telegram_webhook_secret,
                self.public_base_url,
                self.telegram_allowed_user_id,
                self.telegram_allowed_chat_id,
            )
        )

    def allows_telegram_identity(self, *, user_id: int, chat_id: int) -> bool:
        """Both identifiers must match the configured single-user household."""
        return (
            self.telegram_allowed_user_id is not None
            and self.telegram_allowed_chat_id is not None
            and user_id == self.telegram_allowed_user_id
            and chat_id == self.telegram_allowed_chat_id
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
