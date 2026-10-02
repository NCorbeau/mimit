"""Synthetic offline acceptance and adversarial extraction checks (MAC-46)."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mimit.products.extractor import MAX_JSONLD_CHARS, JsonLdExtractor
from mimit.products.types import ErrorCode, ProductCheckError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "products"
URL = "https://shop.example/product?activeVariant=selected"
ZOOPLUS_URL = (
    "https://www.zooplus.pl/shop/koty/karma_dla_kota_mokra/schesir/karma_schesir/2333304"
    "?activeVariant=2333304.0"
)


def product() -> dict[str, Any]:
    return {
        "@type": "Product",
        "name": "Synthetic selected pack",
        "sku": "selected",
        "url": URL,
        "offers": {
            "@type": "Offer",
            "url": URL,
            "price": "12.30",
            "priceCurrency": "PLN",
            "availability": "https://schema.org/InStock",
        },
    }


def html(value: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(value)}</script>'


def assert_error(
    value: Any, code: ErrorCode, *, url: str = URL, variant: str | None = None
) -> None:
    with pytest.raises(ProductCheckError) as caught:
        JsonLdExtractor().extract(html(value), url, variant)
    assert caught.value.code == code
    assert str(caught.value) == code.value


def test_selects_exact_zooplus_pack_and_ignores_membership_prices() -> None:
    result = JsonLdExtractor().extract((FIXTURES / "zooplus_variant.html").read_text(), ZOOPLUS_URL)
    assert result.variant == "2333304.0"
    assert result.price == Decimal("50.10")
    assert result.unit_price == Decimal("100.20")
    assert result.unit == "kg"
    assert result.availability == "available"
    assert result.metadata == {
        "source": "json_ld",
        "schema_type": "Product",
        "offer_type": "one_time",
        "product_nodes": 2,
    }


def test_resolves_local_graph_and_normalizes_reference_quantity() -> None:
    result = JsonLdExtractor().extract(
        (FIXTURES / "local_graph.html").read_text(),
        "https://shop.example/product?activeVariant=coffee-large",
    )
    assert result.price == Decimal("12.30")
    assert result.unit_price == Decimal("10")
    assert result.unit == "kg"
    assert result.availability == "unavailable"


def test_no_unit_metadata_is_optional() -> None:
    result = JsonLdExtractor().extract(html(product()), URL)
    assert result.unit_price is None and result.unit is None


def test_single_product_without_variant_matches_url() -> None:
    value = product()
    del value["sku"]
    value["url"] = "https://shop.example/product"
    value["offers"]["url"] = value["url"]
    result = JsonLdExtractor().extract(html([value]), value["url"])
    assert result.variant is None


def test_stored_variant_selects_sku_without_url_variant() -> None:
    value = product()
    value["url"] = "https://shop.example/product"
    value["offers"]["url"] = value["url"]
    assert JsonLdExtractor().extract(html(value), value["url"], "selected").variant == "selected"


def test_stored_variant_and_input_variant_cannot_disagree() -> None:
    assert_error(product(), ErrorCode.IDENTITY_MISMATCH, variant="other")


@pytest.mark.parametrize("target", ["product", "offer"])
@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/product?activeVariant=selected",
        "https://shop.example/other?activeVariant=selected",
        "https://shop.example/product?activeVariant=other",
    ],
)
def test_identity_cannot_change(target: str, url: str) -> None:
    value = product()
    (value if target == "product" else value["offers"])["url"] = url
    assert_error(value, ErrorCode.IDENTITY_MISMATCH)


def test_duplicate_products_and_offers_fail_without_cheapest_selection() -> None:
    value = product()
    assert_error([value, deepcopy(value)], ErrorCode.AMBIGUOUS_PRODUCT)
    value["offers"] = [value["offers"], deepcopy(value["offers"])]
    assert_error(value, ErrorCode.AMBIGUOUS_OFFER)


@pytest.mark.parametrize(
    "field",
    [
        "validForMemberTier",
        "eligibleCustomerType",
        "billingDuration",
        "eligibleQuantity",
        "eligibleTransactionVolume",
        "billingIncrement",
        "priceComponentType",
    ],
)
def test_conditional_offers_are_never_normal_prices(field: str) -> None:
    value = product()
    value["offers"][field] = "conditional"
    assert_error(value, ErrorCode.AMBIGUOUS_OFFER)


def test_aggregate_minimum_is_not_a_price() -> None:
    value = product()
    value["offers"] = {"@type": "AggregateOffer", "lowPrice": "1", "priceCurrency": "PLN"}
    assert_error(value, ErrorCode.AMBIGUOUS_OFFER)


@pytest.mark.parametrize(
    "price",
    [
        "NaN",
        "Infinity",
        "-1",
        "12,30",
        "1000000000000",
        "0.0000001",
        True,
        [],
        "1e999999999",
        "1e-999999999",
        "9" * 129,
    ],
)
def test_invalid_or_unstorable_money_fails(price: Any) -> None:
    value = product()
    value["offers"]["price"] = price
    assert_error(value, ErrorCode.INVALID_PRICE)


@pytest.mark.parametrize("price", ["0", "999999999999.999999", "12.3000000"])
def test_storable_decimal_limits(price: str) -> None:
    value = product()
    value["offers"]["price"] = price
    assert JsonLdExtractor().extract(html(value), URL).price == Decimal(price)


def test_out_of_stock_can_have_no_price() -> None:
    value = product()
    del value["offers"]["price"]
    del value["offers"]["priceCurrency"]
    value["offers"]["availability"] = "OutOfStock"
    result = JsonLdExtractor().extract(html(value), URL)
    assert result.price is None and result.currency is None and result.availability == "unavailable"


def test_missing_in_stock_price_does_not_use_subscription_or_sale_specification() -> None:
    value = product()
    del value["offers"]["price"]
    value["offers"]["priceSpecification"] = {
        "@type": "PriceSpecification",
        "price": "1",
        "priceCurrency": "PLN",
        "priceType": "SalePrice",
    }
    assert_error(value, ErrorCode.INVALID_PRICE)


@pytest.mark.parametrize(
    "availability", [None, "PreOrder", "BackOrder", "https://other.test/InStock"]
)
def test_unknown_availability_is_honest(availability: Any) -> None:
    value = product()
    value["offers"]["availability"] = availability
    assert JsonLdExtractor().extract(html(value), URL).availability == "unknown"


def unit_spec() -> dict[str, Any]:
    return {
        "@type": "UnitPriceSpecification",
        "priceType": "UnitPrice",
        "price": "20.00",
        "priceCurrency": "PLN",
        "referenceQuantity": {"value": 2, "unitCode": "KGM"},
    }


@pytest.mark.parametrize(
    "change", ["currency", "zero_quantity", "conflict", "missing_unit", "bad_price"]
)
def test_explicit_malformed_unit_metadata_fails(change: str) -> None:
    value, spec = product(), unit_spec()
    if change == "currency":
        spec["priceCurrency"] = "EUR"
    elif change == "zero_quantity":
        spec["referenceQuantity"]["value"] = 0
    elif change == "conflict":
        spec["unitText"] = "g"
    elif change == "missing_unit":
        del spec["referenceQuantity"]["unitCode"]
    elif change == "bad_price":
        spec["price"] = "NaN"
    value["offers"]["priceSpecification"] = spec
    assert_error(value, ErrorCode.INVALID_PRICE)


def test_duplicate_unit_prices_are_ambiguous() -> None:
    value = product()
    value["offers"]["priceSpecification"] = [unit_spec(), unit_spec()]
    assert_error(value, ErrorCode.INVALID_PRICE)


@pytest.mark.parametrize("offer", [{"@id": "#missing"}, {"@id": "https://remote.example/offer"}])
def test_unresolved_references_are_not_fetched(offer: dict[str, Any]) -> None:
    value = product()
    value["offers"] = offer
    assert_error(value, ErrorCode.INVALID_JSONLD)


@pytest.mark.parametrize(
    "body",
    [
        "{",
        '{"@type":"Product","@type":"Product"}',
        '{"price":NaN}',
        "[" * 1000 + "0" + "]" * 1000,
    ],
)
def test_malformed_jsonld_fails_safely(body: str) -> None:
    with pytest.raises(ProductCheckError) as caught:
        JsonLdExtractor().extract(f'<script type="application/ld+json">{body}</script>', URL)
    assert caught.value.code == ErrorCode.INVALID_JSONLD


def test_bounded_jsonld_payload_and_tree() -> None:
    assert_error({"padding": "x" * MAX_JSONLD_CHARS}, ErrorCode.INVALID_JSONLD)
    assert_error([0] * 10_001, ErrorCode.INVALID_JSONLD)
    value: Any = 0
    for _ in range(42):
        value = [value]
    assert_error(value, ErrorCode.INVALID_JSONLD)


def test_conflicting_local_identifier_fails() -> None:
    assert_error([{"@id": "#x", "name": "a"}, {"@id": "#x", "name": "b"}], ErrorCode.INVALID_JSONLD)


def test_no_products_and_wrong_variant_fail() -> None:
    assert_error({"@type": "WebSite"}, ErrorCode.NO_PRODUCT)
    value = product()
    value["sku"] = "other"
    assert_error(value, ErrorCode.NO_PRODUCT)


def test_zooplus_path_product_and_variant_must_agree() -> None:
    assert_error(
        product(), ErrorCode.IDENTITY_MISMATCH, url=ZOOPLUS_URL.replace("2333304.0", "999.0")
    )
    assert_error(
        product(), ErrorCode.IDENTITY_MISMATCH, url=ZOOPLUS_URL.split("?")[0], variant="2333304.0"
    )


def test_unit_specification_with_direct_unit_code_without_price_type() -> None:
    value = product()
    value["offers"]["priceSpecification"] = {
        "@type": "UnitPriceSpecification",
        "price": "2",
        "priceCurrency": "PLN",
        "unitCode": "LTR",
    }
    result = JsonLdExtractor().extract(html(value), URL)
    assert result.unit_price == Decimal("2") and result.unit == "L"


def test_canonical_price_specification_currency_cannot_conflict() -> None:
    value = product()
    value["offers"]["priceSpecification"] = {
        "@type": "PriceSpecification",
        "price": "12.30",
        "priceCurrency": "EUR",
    }
    assert_error(value, ErrorCode.INVALID_PRICE)


def test_unit_normalization_cannot_overflow_storage() -> None:
    value, spec = product(), unit_spec()
    spec["price"] = "999999999999.999999"
    spec["referenceQuantity"]["value"] = "0.000001"
    value["offers"]["priceSpecification"] = spec
    assert_error(value, ErrorCode.INVALID_PRICE)


def test_offer_item_identity_cannot_contradict_product() -> None:
    value = product()
    value["offers"]["itemOffered"] = {"@type": "Product", "sku": "other"}
    assert_error(value, ErrorCode.IDENTITY_MISMATCH)


def test_zooplus_requires_both_product_and_offer_urls() -> None:
    value = product()
    value["sku"] = "2333304.0"
    value["url"] = ZOOPLUS_URL
    del value["offers"]["url"]
    assert_error(value, ErrorCode.IDENTITY_MISMATCH, url=ZOOPLUS_URL)


def test_normal_one_time_price_does_not_use_promotional_minima() -> None:
    value = product()
    value["offers"]["priceSpecification"] = [
        {
            "@type": "PriceSpecification",
            "priceType": "SalePrice",
            "price": "1",
            "priceCurrency": "PLN",
        },
        {
            "@type": "PriceSpecification",
            "priceType": "ListPrice",
            "price": "20",
            "priceCurrency": "PLN",
        },
    ]
    assert JsonLdExtractor().extract(html(value), URL).price == Decimal("12.30")


def test_repeated_compatible_identifiers_merge_without_duplicate_products() -> None:
    value = product()
    value["@id"] = "#selected"
    value["offers"]["itemOffered"] = {"@id": "#selected"}
    value["offers"]["priceSpecification"] = [
        {
            "@type": "UnitPriceSpecification",
            "price": "1",
            "priceCurrency": "PLN",
            "validForMemberTier": {"@type": "MemberProgramTier", "@id": "#standard"},
        },
        {
            "@type": "UnitPriceSpecification",
            "price": "2",
            "priceCurrency": "PLN",
            "validForMemberTier": {
                "@type": "MemberProgramTier",
                "@id": "#standard",
                "name": "Standard",
                "hasTierBenefit": ["https://schema.org/TierBenefitLoyaltyPoints"],
            },
        },
    ]
    result = JsonLdExtractor().extract(html([value, deepcopy(value)]), URL)
    assert result.price == Decimal("12.30")
    assert result.metadata["product_nodes"] == 1


def test_partial_product_identifier_definition_resolves_to_canonical_identity() -> None:
    value = product()
    value["@id"] = "#selected"
    value["offers"]["itemOffered"] = {"@id": "#selected"}
    partial = {"@type": "Product", "@id": "#selected", "sku": "selected"}
    result = JsonLdExtractor().extract(html([partial, value]), URL)
    assert result.price == Decimal("12.30") and result.metadata["product_nodes"] == 1


def test_conflicting_shared_identifier_property_still_fails() -> None:
    value, conflicting = product(), product()
    value["@id"] = conflicting["@id"] = "#selected"
    conflicting["name"] = "Different product"
    assert_error([value, conflicting], ErrorCode.INVALID_JSONLD)


def test_raw_extreme_numeric_literal_is_safe_jsonld_error() -> None:
    payload = '<script type="application/ld+json">{"price":1e999999999999999999999999}</script>'
    with pytest.raises(ProductCheckError) as caught:
        JsonLdExtractor().extract(payload, URL)
    assert caught.value.code == ErrorCode.INVALID_JSONLD


@pytest.mark.parametrize("aggregate_marker", ["mixed_type", "offer_count", "nested_offers"])
def test_aggregate_shape_cannot_claim_one_time_offer(aggregate_marker: str) -> None:
    value = product()
    if aggregate_marker == "mixed_type":
        value["offers"]["@type"] = ["Offer", "AggregateOffer"]
    elif aggregate_marker == "offer_count":
        value["offers"]["offerCount"] = 2
    else:
        value["offers"]["offers"] = [deepcopy(value["offers"])]
    assert_error(value, ErrorCode.AMBIGUOUS_OFFER)
