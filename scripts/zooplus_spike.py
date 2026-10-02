"""Developer-only, one-request Zooplus PL feasibility probe (MAC-44).

No application scraper or scheduled task is installed by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

MAX_BODY_BYTES = 4 * 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 30
USER_AGENT = "MimitFeasibilitySpike/0.1 (developer-only; public product feasibility)"
PRODUCT_PATH = re.compile(r"/shop/koty/karma_dla_kota_(?:mokra|sucha)/(?:[a-zA-Z0-9_-]+/)*([0-9]+)")


class SpikeError(ValueError):
    """Evidence cannot support an unambiguous exact-variant observation."""


def validate_url(url: str) -> str:
    """Allow only public HTTPS Zooplus PL cat-food product URLs with exact SKU."""
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise SpikeError("URL must not contain whitespace or control characters.")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SpikeError("Malformed URL.") from exc
    path = PRODUCT_PATH.fullmatch(parts.path)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (
        parts.scheme != "https"
        or parts.netloc != "www.zooplus.pl"
        or parts.fragment
        or path is None
        or set(query) != {"activeVariant"}
        or len(query["activeVariant"]) != 1
    ):
        raise SpikeError("Require an allowlisted Zooplus PL cat-food URL and activeVariant.")
    variant = query["activeVariant"][0]
    if not re.fullmatch(rf"{path.group(1)}\.[0-9]+", variant):
        raise SpikeError("activeVariant must identify a variant of the product in the path.")
    return variant


class JsonLdScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def nodes(value: Any) -> list[dict[str, Any]]:
    """Find inline objects in @graph, ProductGroup.hasVariant, or plain JSON-LD."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(nodes(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(nodes(child))
    return found


def is_type(node: dict[str, Any], kind: str) -> bool:
    value = node.get("@type")
    return value == kind or (isinstance(value, list) and kind in value)


def money(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise SpikeError("Missing or invalid price.") from exc
    if not amount.is_finite() or amount < 0:
        raise SpikeError("Price must be finite and non-negative.")
    return str(amount)


def extract_observation(html: str, url: str) -> dict[str, Any]:
    variant = validate_url(url)
    parser = JsonLdScripts()
    parser.feed(html)
    objects: list[dict[str, Any]] = []
    for script in parser.scripts:
        try:
            objects.extend(nodes(json.loads(script, parse_float=Decimal)))
        except (json.JSONDecodeError, RecursionError) as exc:
            raise SpikeError("Malformed or excessively nested JSON-LD.") from exc
    products = [p for p in objects if is_type(p, "Product") and p.get("sku") == variant]
    if len(products) != 1:
        raise SpikeError("Exact variant requires exactly one inline JSON-LD Product by SKU.")
    product = products[0]
    if not isinstance(product.get("name"), str) or not product["name"].strip():
        raise SpikeError("Missing product name/pack identity.")
    if (
        validate_url(str(product.get("url", ""))) != variant
        or urlsplit(product["url"]).path != urlsplit(url).path
    ):
        raise SpikeError("Product URL and SKU disagree.")
    offer = product.get("offers")
    if (
        not isinstance(offer, dict)
        or not is_type(offer, "Offer")
        or "validForMemberTier" in offer
        or "eligibleCustomerType" in offer
    ):
        raise SpikeError("Require a single unconditional Offer, without aggregate/member fallback.")
    if (
        validate_url(str(offer.get("url", ""))) != variant
        or urlsplit(offer["url"]).path != urlsplit(url).path
    ):
        raise SpikeError("Offer URL and SKU disagree.")
    currency = offer.get("priceCurrency")
    availability = offer.get("availability")
    if (
        currency != "PLN"
        or not isinstance(availability, str)
        or availability
        not in {
            "https://schema.org/InStock",
            "https://schema.org/OutOfStock",
            "https://schema.org/PreOrder",
            "https://schema.org/BackOrder",
            "https://schema.org/Discontinued",
        }
    ):
        raise SpikeError("Missing/unsupported currency or availability.")
    specifications = offer.get("priceSpecification", [])
    if not isinstance(specifications, list):
        raise SpikeError("Unsupported priceSpecification shape.")
    units = [
        s
        for s in specifications
        if isinstance(s, dict)
        and is_type(s, "UnitPriceSpecification")
        and s.get("priceType") == "https://schema.org/UnitPrice"
        and "validForMemberTier" not in s
        and "eligibleCustomerType" not in s
    ]
    if len(units) != 1 or units[0].get("priceCurrency") != currency:
        raise SpikeError("Require exactly one unconditional unit price in the same currency.")
    unit = units[0]
    quantity = unit.get("referenceQuantity")
    if (
        not isinstance(quantity, dict)
        or quantity.get("value") != 1
        or quantity.get("unitCode") != "KGM"
        or unit.get("unitText") != "kg"
    ):
        raise SpikeError("Probe only supports an explicit price per 1 kg.")
    return {
        "source": "JSON-LD Product/Offer",
        "variant_sku": variant,
        "name": product.get("name"),
        "normal_one_time_price": money(offer.get("price")),
        "currency": currency,
        "unit_price": money(unit.get("price")),
        "unit": "kg",
        "availability": availability,
        "product_nodes": sum(is_type(p, "Product") for p in objects),
        "conditional_price_specifications_ignored": sum(
            isinstance(s, dict) and "validForMemberTier" in s for s in specifications
        ),
    }


async def fetch_html(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[str, dict[str, Any]]:
    validate_url(url)  # Validate before opening a client or performing any network I/O.
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=httpx.Timeout(10, connect=5),
        follow_redirects=False,
        verify=True,
        trust_env=False,
        transport=transport,
    ) as client:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise SpikeError(
                        f"HTTP {response.status_code}; no retry or redirect attempted."
                    )
                if response.headers.get("content-type", "").split(";")[0] != "text/html":
                    raise SpikeError("Expected HTML response.")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise SpikeError("Response exceeded the 4 MiB decoded body limit.")
                    body.extend(chunk)
                return body.decode("utf-8"), {
                    "http_status": response.status_code,
                    "decoded_body_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "observed_at": datetime.now(UTC).isoformat(),
                }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Zooplus PL cat-food product URL with activeVariant")
    parser.add_argument(
        "--provisional",
        action="store_true",
        help="Mark a representative URL, not the acceptance target",
    )
    args = parser.parse_args()
    try:
        html, evidence = asyncio.run(fetch_html(args.url))
        result = extract_observation(html, args.url)
    except (SpikeError, httpx.HTTPError, TimeoutError, UnicodeDecodeError) as exc:
        message = str(exc) if isinstance(exc, SpikeError) else type(exc).__name__
        print(json.dumps({"status": "inconclusive", "error": message}))
        return 2
    print(
        json.dumps(
            {
                "status": "provisional" if args.provisional else "observation",
                "requested_url": args.url,
                **evidence,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
