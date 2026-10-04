"""Foundation schema. Business timestamps are supplied by the application clock."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Reject ambiguous naive input and normalize aware values to UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Household(Base):
    __tablename__ = "household"
    __table_args__ = (CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Consumable(Base):
    __tablename__ = "consumable"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("length(trim(canonical_unit)) > 0", name="canonical_unit_nonempty"),
        CheckConstraint(
            "stock_quantity >= 0 AND stock_quantity <> 'NaN'::numeric", name="stock_nonnegative"
        ),
        CheckConstraint(
            "daily_consumption > 0 AND daily_consumption <> 'NaN'::numeric",
            name="daily_consumption_positive",
        ),
        CheckConstraint("reserve_days >= 0", name="reserve_days_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("household.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    stock_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    canonical_unit: Mapped[str] = mapped_column(Text)
    daily_consumption: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    reserve_days: Mapped[int] = mapped_column(Integer)
    stock_updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class OfferSource(Base):
    __tablename__ = "offer_source"
    __table_args__ = (
        UniqueConstraint("consumable_id"),
        CheckConstraint("length(trim(url)) > 0", name="url_nonempty"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    consumable_id: Mapped[UUID] = mapped_column(ForeignKey("consumable.id"))
    url: Mapped[str] = mapped_column(Text)
    variant: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class PriceObservation(Base):
    __tablename__ = "price_observation"
    __table_args__ = (
        CheckConstraint(
            "price IS NULL OR (price >= 0 AND price <> 'NaN'::numeric)", name="price_nonnegative"
        ),
        CheckConstraint(
            "unit_price IS NULL OR (unit_price >= 0 AND unit_price <> 'NaN'::numeric)",
            name="unit_price_nonnegative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("price IS NULL OR currency IS NOT NULL", name="price_currency"),
        CheckConstraint(
            "unit_price IS NULL OR (currency IS NOT NULL AND unit IS NOT NULL "
            "AND length(trim(unit)) > 0)",
            name="unit_price_unit",
        ),
        CheckConstraint(
            "availability IN ('available', 'unavailable', 'unknown')", name="availability"
        ),
        CheckConstraint("outcome IN ('success', 'failed')", name="outcome"),
        CheckConstraint(
            "(outcome = 'success' AND error_code IS NULL) OR "
            "(outcome = 'failed' AND error_code IS NOT NULL AND "
            "price IS NULL AND currency IS NULL AND unit_price IS NULL AND unit IS NULL "
            "AND product_name IS NULL AND availability = 'unknown')",
            name="outcome_shape",
        ),
        CheckConstraint(
            "error_code IN ('unsafe_url', 'unsafe_address', 'redirect_limit', 'timeout', "
            "'http_error', 'body_too_large', 'unsupported_content', 'transport_error', "
            "'rate_limited', 'invalid_encoding', 'unsupported_source', 'invalid_jsonld', "
            "'ambiguous_product', 'ambiguous_offer', 'invalid_price', 'identity_mismatch', "
            "'invalid_product', 'no_product')",
            name="error_code",
        ),
        CheckConstraint(
            "product_name IS NULL OR (length(trim(product_name)) > 0 "
            "AND length(product_name) <= 512)",
            name="product_name",
        ),
        CheckConstraint(
            "variant_snapshot IS NULL OR length(variant_snapshot) <= 1024", name="variant_snapshot"
        ),
        CheckConstraint(
            "jsonb_typeof(extraction_metadata) = 'object' "
            "AND octet_length(extraction_metadata::text) <= 4096",
            name="extraction_metadata",
        ),
        Index("ix_price_observation_source_observed", "offer_source_id", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    offer_source_id: Mapped[UUID] = mapped_column(ForeignKey("offer_source.id"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unit: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[str] = mapped_column(String(16))

    outcome: Mapped[str] = mapped_column(String(16), server_default="success")
    error_code: Mapped[str | None] = mapped_column(String(32))
    product_name: Mapped[str | None] = mapped_column(Text)
    variant_snapshot: Mapped[str | None] = mapped_column(Text)
    extraction_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class Purchase(Base):
    __tablename__ = "purchase"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND quantity <> 'NaN'::numeric", name="quantity_positive"),
        CheckConstraint(
            "price IS NULL OR (price >= 0 AND price <> 'NaN'::numeric)", name="price_nonnegative"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "(price IS NULL AND currency IS NULL) OR (price IS NOT NULL AND currency IS NOT NULL)",
            name="price_currency",
        ),
        Index("ix_purchase_consumable_purchased", "consumable_id", "purchased_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    consumable_id: Mapped[UUID] = mapped_column(ForeignKey("consumable.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    purchased_at: Mapped[datetime] = mapped_column(UTCDateTime())
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    source: Mapped[str | None] = mapped_column(Text)


class ScheduledJob(Base):
    __tablename__ = "scheduled_job"
    __table_args__ = (
        CheckConstraint("length(trim(dedupe_key)) > 0", name="dedupe_key_nonempty"),
        CheckConstraint("length(trim(job_type)) > 0", name="job_type_nonempty"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')", name="state"
        ),
        CheckConstraint(
            "(state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        Index("ix_scheduled_job_state_run_at", "state", "run_at"),
        Index("ix_scheduled_job_lease_expires_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    job_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime())
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint("length(trim(dedupe_key)) > 0", name="dedupe_key_nonempty"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("state IN ('pending', 'sending', 'sent', 'failed')", name="state"),
        CheckConstraint(
            "(state = 'sending' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'sending' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
            "(state = 'sent' AND delivered_at IS NOT NULL) "
            "OR (state <> 'sent' AND delivered_at IS NULL)",
            name="delivery_state",
        ),
        Index("ix_notification_outbox_state_run_at", "state", "run_at"),
        Index("ix_notification_outbox_lease_expires_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime())
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipt"
    __table_args__ = (CheckConstraint("update_id >= 0", name="update_id_nonnegative"),)

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TelegramConversation(Base):
    __tablename__ = "telegram_conversation"
    __table_args__ = (
        CheckConstraint(
            "step IN ('name', 'stock', 'unit', 'daily', 'reserve', 'confirm')", name="step"
        ),
        CheckConstraint("jsonb_typeof(data) = 'object'", name="data_object"),
    )

    household_id: Mapped[UUID] = mapped_column(ForeignKey("household.id"), primary_key=True)
    step: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
