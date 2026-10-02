"""Synthetic fixtures only; these tests never contact Zooplus."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from scripts import zooplus_spike as spike

URL = (
    "https://www.zooplus.pl/shop/koty/karma_dla_kota_mokra/"
    "schesir/karma_schesir/2333304?activeVariant=2333304.0"
)


def product() -> dict[str, Any]:
    return {
        "@type": "Product",
        "sku": "2333304.0",
        "url": URL,
        "name": "Synthetic test product 6 x 85 g",
        "offers": {
            "@type": "Offer",
            "url": URL,
            "price": "42.96",
            "priceCurrency": "PLN",
            "availability": "https://schema.org/InStock",
            "priceSpecification": [
                {
                    "@type": "UnitPriceSpecification",
                    "price": "36.52",
                    "priceCurrency": "PLN",
                    "validForMemberTier": {"name": "autoshipment"},
                },
                {
                    "@type": "UnitPriceSpecification",
                    "priceType": "https://schema.org/UnitPrice",
                    "price": "84.24",
                    "priceCurrency": "PLN",
                    "referenceQuantity": {"value": 1, "unitCode": "KGM"},
                    "unitText": "kg",
                },
            ],
        },
    }


def html(data: Any) -> str:
    return '<script type="application/ld+json">' + json.dumps(data) + "</script>"


def test_exact_nested_variant_ignores_subscription_and_other_flavour() -> None:
    other = product()
    other["sku"] = "2333304.1"
    other["offers"]["price"] = "1.00"
    observation = spike.extract_observation(
        html({"@graph": [{"@type": "ProductGroup", "hasVariant": [other, product()]}]}), URL
    )
    assert observation["variant_sku"] == "2333304.0"
    assert observation["normal_one_time_price"] == "42.96"
    assert observation["unit_price"] == "84.24"
    assert observation["conditional_price_specifications_ignored"] == 1


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("www.zooplus.pl", "127.0.0.1"),
        URL.replace("www.zooplus.pl", "www.zooplus.pl.evil.test"),
        URL.replace("www.zooplus.pl", "user:password@www.zooplus.pl"),
        URL.replace("https:", "http:"),
        URL.replace("/shop/koty/", "/admin/"),
        URL.replace("2333304.0", "999.0"),
        URL.split("?")[0],
        URL + "&activeVariant=2333304.1",
        URL + "&token=secret",
        URL + "#fragment",
        "\n" + URL,
        "https://[invalid",
    ],
)
async def test_disallowed_url_fails_before_request(url: str) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid URL must fail before network I/O")

    with pytest.raises(spike.SpikeError):
        await spike.fetch_html(url, transport=httpx.MockTransport(unexpected_request))


@pytest.mark.parametrize("products", [[], [product(), product()]])
def test_missing_or_duplicate_exact_variant_is_inconclusive(products: list[Any]) -> None:
    with pytest.raises(spike.SpikeError, match="exactly one"):
        spike.extract_observation(html(products), URL)


@pytest.mark.parametrize("field", ["price", "priceCurrency", "availability", "priceSpecification"])
def test_missing_required_field_is_inconclusive(field: str) -> None:
    data = product()
    del data["offers"][field]
    with pytest.raises(spike.SpikeError):
        spike.extract_observation(html(data), URL)


def test_member_only_offer_is_inconclusive() -> None:
    data = product()
    data["offers"]["validForMemberTier"] = {"name": "autoshipment"}
    with pytest.raises(spike.SpikeError, match="unconditional"):
        spike.extract_observation(html(data), URL)


def test_malformed_availability_is_inconclusive() -> None:
    data = product()
    data["offers"]["availability"] = ["https://schema.org/InStock"]
    with pytest.raises(spike.SpikeError, match="availability"):
        spike.extract_observation(html(data), URL)


def test_offer_url_must_match_exact_variant() -> None:
    data = product()
    data["offers"]["url"] = URL.replace("2333304.0", "2333304.1")
    with pytest.raises(spike.SpikeError, match="disagree"):
        spike.extract_observation(html(data), URL)


async def test_redirect_is_never_followed_or_retried() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["user-agent"] == spike.USER_AGENT
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    with pytest.raises(spike.SpikeError, match="HTTP 302"):
        await spike.fetch_html(URL, transport=httpx.MockTransport(respond))
    assert len(requests) == 1


async def test_decoded_body_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spike, "MAX_BODY_BYTES", 32)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"x" * 33
        )
    )
    with pytest.raises(spike.SpikeError, match="body limit"):
        await spike.fetch_html(URL, transport=transport)


async def test_total_deadline_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spike, "TOTAL_TIMEOUT_SECONDS", 0.001)

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, headers={"content-type": "text/html"})

    with pytest.raises(TimeoutError):
        await spike.fetch_html(URL, transport=httpx.MockTransport(respond))
