"""One-shot product checks: external I/O outside short database transactions."""

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock
from mimit.db.models import Consumable, OfferSource, PriceObservation
from mimit.observability import elapsed_ms, log_event
from mimit.products.types import (
    ErrorCode,
    ExtractedProduct,
    ProductCheckError,
    ProductExtractor,
    ProductFetcher,
)

_METADATA_KEYS = {"source", "schema_type", "offer_type", "product_id", "product_nodes"}
_MAX_MONEY = Decimal("999999999999.999999")


class PriceCheckServiceError(ValueError):
    """Safe precondition/persistence failure, independent of merchant availability."""

    def __init__(
        self, code: Literal["not_found", "forbidden", "source_changed", "persistence_error"]
    ):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PriceCheckResult:
    observation_id: UUID
    consumable_id: UUID
    offer_source_id: UUID
    observed_at: datetime
    outcome: Literal["success", "failed"]
    product_name: str | None
    variant: str | None
    price: Decimal | None
    currency: str | None
    unit_price: Decimal | None
    unit: str | None
    availability: Literal["available", "unavailable", "unknown"]
    error_code: ErrorCode | None


@dataclass(frozen=True)
class _SourceSnapshot:
    source_id: UUID
    consumable_id: UUID
    household_id: UUID
    url: str
    variant: str | None


def _safe_text(value: str, limit: int) -> bool:
    return (
        bool(value.strip())
        and len(value) <= limit
        and all(not unicodedata.category(char).startswith("C") for char in value)
    )


def _validate(product: ExtractedProduct, source: _SourceSnapshot) -> None:
    if not isinstance(product.name, str) or not _safe_text(product.name, 512):
        raise ProductCheckError(ErrorCode.INVALID_PRODUCT)
    if not isinstance(product.metadata, dict):
        raise ProductCheckError(ErrorCode.INVALID_PRODUCT)
    if product.variant is not None and (
        not isinstance(product.variant, str) or not _safe_text(product.variant, 1024)
    ):
        raise ProductCheckError(ErrorCode.INVALID_PRODUCT)
    if source.variant is not None and product.variant != source.variant:
        raise ProductCheckError(ErrorCode.IDENTITY_MISMATCH)
    if product.availability not in {"available", "unavailable", "unknown"}:
        raise ProductCheckError(ErrorCode.INVALID_PRODUCT)
    for value in [product.price, product.unit_price]:
        if value is not None and (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or not 0 <= value <= _MAX_MONEY
            or value != value.quantize(Decimal("0.000001"))
        ):
            raise ProductCheckError(ErrorCode.INVALID_PRICE)
    if product.currency is not None and (
        not isinstance(product.currency, str) or re.fullmatch(r"[A-Z]{3}", product.currency) is None
    ):
        raise ProductCheckError(ErrorCode.INVALID_PRICE)
    if (product.price is not None or product.unit_price is not None) and product.currency is None:
        raise ProductCheckError(ErrorCode.INVALID_PRICE)
    if product.availability == "available" and product.price is None:
        raise ProductCheckError(ErrorCode.INVALID_PRICE)
    if product.unit is not None and (
        not isinstance(product.unit, str) or not _safe_text(product.unit, 128)
    ):
        raise ProductCheckError(ErrorCode.INVALID_PRICE)
    if product.unit_price is not None and product.unit is None:
        raise ProductCheckError(ErrorCode.INVALID_PRICE)


def _metadata(product: ExtractedProduct) -> dict[str, str | int | bool | None]:
    # Never persist arbitrary extractor text, URLs, response bodies or exception messages.
    safe: dict[str, str | int | bool | None] = {}
    for key in _METADATA_KEYS:
        if key not in product.metadata:
            continue
        value = product.metadata[key]
        if isinstance(value, str) and re.fullmatch(r"[\w .:/-]{1,128}", value) is not None:
            # Metadata identifiers cannot carry URL credentials or query parameters.
            if "://" not in value:
                safe[key] = value
        elif value is None or isinstance(value, (int, bool)):
            if not isinstance(value, int) or abs(value) <= 1_000_000:
                safe[key] = value
    return safe


class ProductCheckService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        fetcher: ProductFetcher,
        extractor: ProductExtractor,
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.fetcher = fetcher
        self.extractor = extractor

    async def _snapshot(self, consumable_id: UUID, household_id: UUID | None) -> _SourceSnapshot:
        async with self.sessions() as session:
            item = await session.get(Consumable, consumable_id)
            if item is None:
                raise PriceCheckServiceError("not_found")
            if household_id is not None and item.household_id != household_id:
                raise PriceCheckServiceError("forbidden")
            source = await session.scalar(
                select(OfferSource).where(OfferSource.consumable_id == consumable_id)
            )
            if source is None:
                raise PriceCheckServiceError("not_found")
            return _SourceSnapshot(
                source.id, item.id, item.household_id, source.url, source.variant
            )

    async def check(
        self, consumable_id: UUID, *, household_id: UUID | None = None
    ) -> PriceCheckResult:
        try:
            source = await self._snapshot(consumable_id, household_id)
        except SQLAlchemyError:
            raise PriceCheckServiceError("persistence_error") from None
        # The read session has closed before external code is called. Cancellation is
        # allowed to propagate at either external boundary without an ambiguous record.
        product: ExtractedProduct | None = None
        error: ProductCheckError | None = None
        stage = "fetch"
        started = perf_counter()
        log_event(
            "product_fetch_started",
            consumable_id=consumable_id,
            offer_source_id=source.source_id,
            outcome="started",
        )
        try:
            page = await self.fetcher.fetch(source.url)
            log_event(
                "product_fetch_result",
                consumable_id=consumable_id,
                offer_source_id=source.source_id,
                outcome="success",
                http_status=page.status_code,
                body_bytes=page.body_bytes,
                duration_ms=elapsed_ms(started),
            )
            stage = "extract"
            started = perf_counter()
            product = self.extractor.extract(page.html, source.url, source.variant)
            _validate(product, source)
            log_event(
                "product_extraction_result",
                consumable_id=consumable_id,
                offer_source_id=source.source_id,
                outcome="success",
                duration_ms=elapsed_ms(started),
            )
        except asyncio.CancelledError:
            log_event(
                "product_fetch_result" if stage == "fetch" else "product_extraction_result",
                consumable_id=consumable_id,
                offer_source_id=source.source_id,
                outcome="cancelled",
                duration_ms=elapsed_ms(started),
            )
            raise
        except ProductCheckError as exc:
            error = exc
            product = None
        except Exception:
            error = ProductCheckError(
                ErrorCode.TRANSPORT_ERROR if stage == "fetch" else ErrorCode.INVALID_PRODUCT
            )
            product = None
        if error is not None:
            log_event(
                "product_fetch_result" if stage == "fetch" else "product_extraction_result",
                consumable_id=consumable_id,
                offer_source_id=source.source_id,
                outcome="failed",
                error_code=error.code,
                http_status=error.http_status,
                duration_ms=elapsed_ms(started),
            )
        instant = self.clock.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware timestamp")
        instant = instant.astimezone(UTC)
        safe_metadata = _metadata(product) if product is not None else {}
        if error is not None and error.http_status is not None and 100 <= error.http_status <= 599:
            safe_metadata["http_status"] = error.http_status
        variant_snapshot = product.variant if product is not None else source.variant
        if (
            product is None
            and variant_snapshot is not None
            and not _safe_text(variant_snapshot, 1024)
        ):
            # Existing source input remains exact; malformed/oversized identities
            # must not prevent appending the classified failed attempt.
            variant_snapshot = None
        result = PriceCheckResult(
            observation_id=uuid4(),
            consumable_id=source.consumable_id,
            offer_source_id=source.source_id,
            observed_at=instant,
            outcome="success" if product is not None else "failed",
            product_name=product.name if product is not None else None,
            variant=variant_snapshot,
            price=product.price if product is not None else None,
            currency=product.currency if product is not None else None,
            unit_price=product.unit_price if product is not None else None,
            unit=product.unit if product is not None else None,
            availability=product.availability if product is not None else "unknown",
            error_code=error.code if error is not None else None,
        )
        try:
            async with self.sessions.begin() as session:
                # Lock only during append; freeze source and ownership until commit.
                current = (
                    await session.execute(
                        select(OfferSource, Consumable)
                        .join(Consumable, Consumable.id == OfferSource.consumable_id)
                        .where(OfferSource.id == source.source_id)
                        .with_for_update(of=(OfferSource, Consumable))
                    )
                ).one_or_none()
                if current is None:
                    raise PriceCheckServiceError("source_changed")
                current_source, current_item = current
                if (
                    current_source.url != source.url
                    or current_source.variant != source.variant
                    or current_source.consumable_id != source.consumable_id
                    or current_item.household_id != source.household_id
                ):
                    raise PriceCheckServiceError("source_changed")
                session.add(
                    PriceObservation(
                        id=result.observation_id,
                        offer_source_id=result.offer_source_id,
                        observed_at=result.observed_at,
                        outcome=result.outcome,
                        error_code=result.error_code.value
                        if result.error_code is not None
                        else None,
                        product_name=result.product_name,
                        variant_snapshot=result.variant,
                        price=result.price,
                        currency=result.currency,
                        unit_price=result.unit_price,
                        unit=result.unit,
                        availability=result.availability,
                        extraction_metadata=safe_metadata,
                    )
                )
        except SQLAlchemyError:
            raise PriceCheckServiceError("persistence_error") from None
        log_event(
            "product_observation_committed",
            consumable_id=consumable_id,
            offer_source_id=source.source_id,
            observation_id=result.observation_id,
            outcome=result.outcome,
            error_code=result.error_code,
        )
        return result


async def check_product(
    sessions: async_sessionmaker[AsyncSession],
    consumable_id: UUID,
    fetcher: ProductFetcher,
    extractor: ProductExtractor,
    clock: Clock,
    *,
    household_id: UUID | None = None,
) -> PriceCheckResult:
    return await ProductCheckService(sessions, clock, fetcher, extractor).check(
        consumable_id, household_id=household_id
    )
