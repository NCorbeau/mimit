"""Bounded, offline Product/Offer JSON-LD extraction (MAC-46).

This deliberately does not implement JSON-LD context expansion or remote references.
Only an explicit, unconditional Offer.price can supply the one-time pack price.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

from mimit.products.types import ErrorCode, ExtractedProduct, ProductCheckError

MAX_HTML_CHARS = 4 * 1024 * 1024
MAX_JSONLD_CHARS = 512 * 1024
MAX_SCRIPTS = 64
MAX_NODES = 10_000
MAX_DEPTH = 40
MAX_MONEY = Decimal("999999999999.999999")
QUANTUM = Decimal("0.000001")
CONDITIONAL_FIELDS = frozenset(
    {
        "validForMemberTier",
        "eligibleCustomerType",
        "eligibleQuantity",
        "eligibleTransactionVolume",
        "billingDuration",
        "billingIncrement",
        "billingStart",
        "priceComponentType",
        "eligibleDuration",
    }
)
UNIT_CODES = {"KGM": "kg", "GRM": "g", "LTR": "L", "MLT": "ml", "C62": "pcs"}
UNIT_ALIASES = {"l": "L", "liter": "L", "piece": "pcs", "pieces": "pcs"}
ZOOPLUS_PATH = re.compile(r"/shop/koty/karma_dla_kota_(?:mokra|sucha)/(?:[a-zA-Z0-9_-]+/)*([0-9]+)")
Availability = Literal["available", "unavailable", "unknown"]


def _error(code: ErrorCode) -> ProductCheckError:
    return ProductCheckError(code)


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.parts: list[str] | None = None
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mime = dict(attrs).get("type") or ""
        if tag == "script" and mime.lower().split(";", 1)[0].strip() == "application/ld+json":
            if len(self.scripts) >= MAX_SCRIPTS:
                raise _error(ErrorCode.INVALID_JSONLD)
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.parts is not None:
            self.size += len(data)
            if self.size > MAX_JSONLD_CHARS:
                raise _error(ErrorCode.INVALID_JSONLD)
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.parts is not None:
            self.scripts.append("".join(self.parts))
            self.parts = None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    node: dict[str, Any] = {}
    for key, value in pairs:
        if key in node:
            raise _error(ErrorCode.INVALID_JSONLD)
        node[key] = value
    return node


def _constant(value: str) -> Any:
    raise _error(ErrorCode.INVALID_JSONLD)


def _term(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    for prefix in ("https://schema.org/", "http://schema.org/"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value if re.fullmatch(r"[A-Za-z]+", value) else None


def _type(node: dict[str, Any], kind: str) -> bool:
    types = node.get("@type", [])
    return any(_term(value) == kind for value in (types if isinstance(types, list) else [types]))


def _money(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise _error(ErrorCode.INVALID_PRICE)
    raw = str(value)
    if len(raw) > 128 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", raw):
        raise _error(ErrorCode.INVALID_PRICE)
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount > MAX_MONEY or (positive and amount == 0):
            raise _error(ErrorCode.INVALID_PRICE)
        with localcontext() as context:
            context.prec = 40
            if amount != amount.quantize(QUANTUM):
                raise _error(ErrorCode.INVALID_PRICE)
    except InvalidOperation:
        raise _error(ErrorCode.INVALID_PRICE) from None
    return amount


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise _error(ErrorCode.INVALID_PRICE)
    return value


def _identity(value: Any, base: str) -> tuple[str, str, str, str]:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise _error(ErrorCode.IDENTITY_MISMATCH)
    try:
        parts = urlsplit(urljoin(base, value))
        if parts.username or parts.password or parts.scheme not in {"https", "http"}:
            raise _error(ErrorCode.IDENTITY_MISMATCH)
        if not parts.hostname or parts.fragment:
            raise _error(ErrorCode.IDENTITY_MISMATCH)
        return parts.scheme, parts.netloc.lower(), parts.path, parts.query
    except ValueError as exc:
        raise _error(ErrorCode.IDENTITY_MISMATCH) from exc


class _Document:
    def __init__(self, html: str) -> None:
        if len(html) > MAX_HTML_CHARS:
            raise _error(ErrorCode.INVALID_JSONLD)
        parser = _Scripts()
        parser.feed(html)
        if parser.parts is not None:
            raise _error(ErrorCode.INVALID_JSONLD)
        self.nodes: list[dict[str, Any]] = []
        self.ids: dict[str, dict[str, Any]] = {}
        count = 0
        for script in parser.scripts:
            try:
                tree = json.loads(
                    script, parse_float=Decimal, object_pairs_hook=_pairs, parse_constant=_constant
                )
            except (ValueError, RecursionError, InvalidOperation) as exc:
                if isinstance(exc, ProductCheckError):
                    raise
                raise _error(ErrorCode.INVALID_JSONLD) from None
            pending = [(tree, 0)]
            while pending:
                node, depth = pending.pop()
                count += 1
                if count > MAX_NODES or depth > MAX_DEPTH:
                    raise _error(ErrorCode.INVALID_JSONLD)
                if isinstance(node, dict):
                    self.nodes.append(node)
                    identifier = node.get("@id")
                    if identifier is not None and not isinstance(identifier, str):
                        raise _error(ErrorCode.INVALID_JSONLD)
                    pending.extend((child, depth + 1) for child in node.values())
                elif isinstance(node, list):
                    pending.extend((child, depth + 1) for child in node)

        # Reconcile only after validating every original tree's resource bounds.
        # Repeated descriptions of one @id may add properties; a conflicting
        # shared property is ambiguous. Copies become the canonical local nodes.
        for node in self.nodes:
            identifier = node.get("@id")
            if identifier is not None and len(node) > 1:
                canonical = self.ids.setdefault(identifier, {})
                for key, value in node.items():
                    if key in canonical and canonical[key] != value:
                        raise _error(ErrorCode.INVALID_JSONLD)
                    canonical[key] = value
        canonical_nodes: list[dict[str, Any]] = []
        seen: set[int] = set()
        for node in self.nodes:
            identifier = node.get("@id")
            if isinstance(identifier, str):
                node = self.ids.get(identifier, node)
            if id(node) not in seen:
                canonical_nodes.append(node)
                seen.add(id(node))
        self.nodes = canonical_nodes

    def resolve(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise _error(ErrorCode.INVALID_JSONLD)
        if set(value) == {"@id"}:
            identifier = value["@id"]
            if not isinstance(identifier, str) or identifier not in self.ids:
                raise _error(ErrorCode.INVALID_JSONLD)
            return self.ids[identifier]
        identifier = value.get("@id")
        return self.ids.get(identifier, value) if isinstance(identifier, str) else value

    def many(self, value: Any) -> list[dict[str, Any]]:
        return [self.resolve(child) for child in (value if isinstance(value, list) else [value])]


def _availability(value: Any) -> Availability:
    term = _term(value)
    if term in {"InStock", "LimitedAvailability", "OnlineOnly"}:
        return "available"
    if term in {"OutOfStock", "Discontinued", "SoldOut"}:
        return "unavailable"
    return "unknown"


def _conditional(node: dict[str, Any]) -> bool:
    return bool(CONDITIONAL_FIELDS.intersection(node))


def _unit_label(node: dict[str, Any]) -> str | None:
    code, text = node.get("unitCode"), node.get("unitText")
    if code is not None and (not isinstance(code, str) or not code.strip() or len(code) > 64):
        raise _error(ErrorCode.INVALID_PRICE)
    if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 64):
        raise _error(ErrorCode.INVALID_PRICE)
    normalized = UNIT_CODES.get(code, code) if code is not None else None
    text = UNIT_ALIASES.get(text, text) if text is not None else None
    if normalized is not None and text is not None and normalized != text:
        raise _error(ErrorCode.INVALID_PRICE)
    return normalized or text


def _unit_price(
    document: _Document, offer: dict[str, Any], currency: str | None
) -> tuple[Decimal | None, str | None, int]:
    units: list[tuple[Decimal, str]] = []
    ignored = 0
    specifications = (
        document.many(offer["priceSpecification"]) if "priceSpecification" in offer else []
    )
    for specification in specifications:
        if _conditional(specification):
            ignored += 1
            continue
        price_type = _term(specification.get("priceType"))
        implicit_unit = (
            "priceType" not in specification
            and _type(specification, "UnitPriceSpecification")
            and any(key in specification for key in ("referenceQuantity", "unitCode", "unitText"))
        )
        if price_type != "UnitPrice" and not implicit_unit:
            # Explicit alternative price types are never candidates for the pack price.
            if price_type is None and "price" in specification:
                if _money(specification["price"]) != _money(offer.get("price")):
                    raise _error(ErrorCode.INVALID_PRICE)
                if (
                    "priceCurrency" in specification
                    and _currency(specification["priceCurrency"]) != currency
                ):
                    raise _error(ErrorCode.INVALID_PRICE)
            continue
        if not _type(specification, "UnitPriceSpecification"):
            raise _error(ErrorCode.INVALID_PRICE)
        if currency is None or _currency(specification.get("priceCurrency")) != currency:
            raise _error(ErrorCode.INVALID_PRICE)
        amount = _money(specification.get("price"))
        label = _unit_label(specification)
        if "referenceQuantity" in specification:
            quantity = document.resolve(specification["referenceQuantity"])
            reference_label = _unit_label(quantity)
            if label is not None and reference_label is not None and label != reference_label:
                raise _error(ErrorCode.INVALID_PRICE)
            label = reference_label or label
            divisor = _money(quantity.get("value"), positive=True)
            try:
                with localcontext() as context:
                    context.prec = 40
                    amount = (amount / divisor).quantize(QUANTUM)
            except InvalidOperation:
                raise _error(ErrorCode.INVALID_PRICE) from None
            amount = _money(amount)
        if label is None:
            raise _error(ErrorCode.INVALID_PRICE)
        units.append((amount, label))
    if len(units) > 1:
        raise _error(ErrorCode.INVALID_PRICE)
    return (*units[0], ignored) if units else (None, None, ignored)


class JsonLdExtractor:
    """Extract one exactly identified product and one unconditional selling offer."""

    def extract(self, html: str, url: str, variant: str | None = None) -> ExtractedProduct:
        requested = _identity(url, url)
        query = parse_qs(requested[3], keep_blank_values=True)
        url_variants = query.get("activeVariant", [])
        if url_variants:
            if len(url_variants) != 1 or not url_variants[0]:
                raise _error(ErrorCode.IDENTITY_MISMATCH)
            if variant is not None and variant != url_variants[0]:
                raise _error(ErrorCode.IDENTITY_MISMATCH)
            variant = url_variants[0]
        if variant is not None and (not variant.strip() or len(variant) > 256):
            raise _error(ErrorCode.IDENTITY_MISMATCH)
        zooplus = requested[1] == "www.zooplus.pl"
        if zooplus:
            product_path = ZOOPLUS_PATH.fullmatch(requested[2])
            if (
                product_path is None
                or not url_variants
                or variant is None
                or not re.fullmatch(rf"{product_path.group(1)}\.[0-9]+", variant)
            ):
                raise _error(ErrorCode.IDENTITY_MISMATCH)
        document = _Document(html)
        products = [node for node in document.nodes if _type(node, "Product")]
        if variant is not None:
            products = [node for node in products if node.get("sku") == variant]
        else:
            products = [
                node
                for node in products
                if "url" in node and _identity(node["url"], url) == requested
            ]
        if not products:
            raise _error(ErrorCode.NO_PRODUCT)
        if len(products) != 1:
            raise _error(ErrorCode.AMBIGUOUS_PRODUCT)
        product = products[0]
        name = product.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise _error(ErrorCode.INVALID_PRODUCT)
        offers = document.many(product.get("offers", []))
        if len(offers) != 1:
            raise _error(ErrorCode.AMBIGUOUS_OFFER)
        offer = offers[0]
        if (
            not _type(offer, "Offer")
            or _type(offer, "AggregateOffer")
            or _conditional(offer)
            or "offerCount" in offer
            or "offers" in offer
        ):
            raise _error(ErrorCode.AMBIGUOUS_OFFER)
        if "priceType" in offer or "lowPrice" in offer or "highPrice" in offer:
            raise _error(ErrorCode.INVALID_PRICE)
        function = offer.get("businessFunction")
        if function is not None and function not in {
            "Sell",
            "http://purl.org/goodrelations/v1#Sell",
            "https://purl.org/goodrelations/v1#Sell",
        }:
            raise _error(ErrorCode.AMBIGUOUS_OFFER)
        if "sku" in offer and variant is not None and offer["sku"] != variant:
            raise _error(ErrorCode.IDENTITY_MISMATCH)
        if "itemOffered" in offer:
            offered_product = document.resolve(offer["itemOffered"])
            if offered_product is not product:
                raise _error(ErrorCode.IDENTITY_MISMATCH)
        if zooplus and ("url" not in product or "url" not in offer):
            raise _error(ErrorCode.IDENTITY_MISMATCH)
        identities = [node["url"] for node in (product, offer) if "url" in node]
        if not identities or any(_identity(value, url) != requested for value in identities):
            raise _error(ErrorCode.IDENTITY_MISMATCH)
        availability = _availability(offer.get("availability"))
        price = _money(offer["price"]) if offer.get("price") is not None else None
        currency = _currency(offer["priceCurrency"]) if "priceCurrency" in offer else None
        if (price is not None and currency is None) or (
            price is None and availability != "unavailable"
        ):
            raise _error(ErrorCode.INVALID_PRICE)
        unit_price, unit, _ignored = _unit_price(document, offer, currency)
        return ExtractedProduct(
            name=name.strip(),
            variant=variant,
            price=price,
            currency=currency,
            unit_price=unit_price,
            unit=unit,
            availability=availability,
            metadata={
                "source": "json_ld",
                "schema_type": "Product",
                "offer_type": "one_time",
                "product_nodes": sum(_type(node, "Product") for node in document.nodes),
            },
        )
