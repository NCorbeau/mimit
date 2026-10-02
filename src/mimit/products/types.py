"""Small contracts shared by fetching, extraction and price-check persistence."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol


class ErrorCode(StrEnum):
    UNSAFE_URL = "unsafe_url"
    UNSAFE_ADDRESS = "unsafe_address"
    REDIRECT_LIMIT = "redirect_limit"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    BODY_TOO_LARGE = "body_too_large"
    UNSUPPORTED_CONTENT = "unsupported_content"
    TRANSPORT_ERROR = "transport_error"
    RATE_LIMITED = "rate_limited"
    INVALID_ENCODING = "invalid_encoding"
    UNSUPPORTED_SOURCE = "unsupported_source"
    INVALID_JSONLD = "invalid_jsonld"
    AMBIGUOUS_PRODUCT = "ambiguous_product"
    AMBIGUOUS_OFFER = "ambiguous_offer"
    INVALID_PRICE = "invalid_price"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_PRODUCT = "invalid_product"
    NO_PRODUCT = "no_product"


class ProductCheckError(ValueError):
    """Safe error identity; never includes a URL, response body or raw exception."""

    def __init__(self, code: ErrorCode, *, http_status: int | None = None) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code.value)


@dataclass(frozen=True)
class FetchedPage:
    html: str
    final_url: str
    status_code: int
    body_bytes: int
    sha256: str


@dataclass(frozen=True)
class ExtractedProduct:
    name: str
    variant: str | None
    price: Decimal | None
    currency: str | None
    unit_price: Decimal | None
    unit: str | None
    availability: Literal["available", "unavailable", "unknown"]
    metadata: dict[str, str | int | bool | None] = field(default_factory=dict)


class ProductFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class ProductExtractor(Protocol):
    def extract(self, html: str, url: str, variant: str | None = None) -> ExtractedProduct: ...
