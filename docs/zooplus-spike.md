# MAC-44: Zooplus feasibility spike

## Outcome

**Go for the supplied exact variant using public HTML JSON-LD.** On 2026-10-02,
an ordinary `httpx.AsyncClient` request retrieved the required product identity,
normal one-time price, currency, explicit unit price, and availability. A
Zooplus-specific hydration/DOM adapter is unnecessary for this observed page.
This is a developer feasibility probe; the production scraper remains M2 work.

The user supplied this authoritative [product and variant](https://www.zooplus.pl/shop/koty/karma_dla_kota_mokra/schesir/karma_schesir/2333304?activeVariant=2333304.0):

```text
https://www.zooplus.pl/shop/koty/karma_dla_kota_mokra/schesir/karma_schesir/2333304?activeVariant=2333304.0
```

An initial Animonda page was explored while the exact URL was pending. Its
findings were provisional and are not the acceptance evidence below. No pending
URL or variant clarification remains.

## Reproduce

From the repository root, after `uv sync --locked`:

```sh
uv run python scripts/zooplus_spike.py \
  'https://www.zooplus.pl/shop/koty/karma_dla_kota_mokra/schesir/karma_schesir/2333304?activeVariant=2333304.0'

uv run pytest tests/unit/test_zooplus_spike.py
```

No default product is silently selected. URLs without `activeVariant` fail
before network I/O. Use `--provisional` when deliberately testing a representative
page rather than the agreed target. The command makes one request, prints a small
JSON observation, and exits `2` when evidence is unavailable or ambiguous.
Live values and HTML hashes can change; do not make the recorded price a fixture.

## Recorded evidence

The first exact-target run at `2026-10-02T13:50:28.108149+00:00` returned HTTP 200,
`text/html`, and 1,071,573 decoded bytes. SHA-256 of that response was
`dd35b4b1baf006633e27e53f9adc6cd45d907ecc17fd426f0bd0c10fd7f7acbb`.
The body stayed in memory and was discarded; no full merchant HTML, images,
reviews, account data, cookies, or tokens are stored in the repository.

| Required field | Observed value | Source |
| --- | --- | --- |
| Exact variant | `2333304.0` | `Product.sku`, matching Product and Offer URL |
| Product/pack | Schesir Complete Nutrition filet w galarecie, 6 × 85 g w saszetce | `Product.name` |
| Flavour | Tuńczyk z krewetkami | `Product.name`, selected page variant label |
| Normal one-time price | `42.96` | `Offer.price` |
| Currency | `PLN` | `Offer.priceCurrency` |
| Unit price | `84.24 PLN/kg` | `UnitPriceSpecification`, `priceType=UnitPrice`, quantity `1 KGM` |
| Availability | `https://schema.org/InStock` | `Offer.availability` |

The JSON-LD contains 10 Product objects, nested within ProductGroup variant data.
The script selects exactly one matching SKU and checks both URLs against the
requested path and variant. It never chooses the first, cheapest, or default
variant. It rejects missing/duplicate products and aggregate or conditional offers.

Two member-specific price specifications are ignored, including the autoshipment
price. The one-time observation comes from `Offer.price`, not the minimum price
or member pricing. A second manual inspection of the same public response at
`2026-10-02T13:50:58.544750+00:00` confirmed visible variant `2333304.0`, flavour,
`42,96 zł`, and `84,24 zł / kg`; the separate subscription value was `36,52 zł`.
That response had 1,071,391 decoded bytes and SHA-256
`726ca47f7e4c27186976339d34b63faeadbb3b39bf35aa4be66537b6d0a2b03a`.
Visible-text inspection is evidence for this spike, not a fallback implemented
in the script. `InStock` is the merchant's claim, not a checkout guarantee.

## Bounds and failure behavior

- HTTPS only; exact host `www.zooplus.pl`; cat-food product paths under the
  explicit wet/dry-food allowlist; one `activeVariant` matching the numeric
  product ID. Credentials, other parameters, fragments, and control characters
  are rejected before requests.
- Explicit `MimitFeasibilitySpike/0.1` user agent; default TLS certificate
  verification; no authentication, supplied cookies, environment proxies, or
  browser impersonation. A fresh client makes only one request.
- Redirects and retries are disabled. Any non-200 response, including 403/429,
  stops the probe; no challenge bypass or browser evasion is attempted.
- Connect timeout 5 seconds, HTTP operation timeout 10 seconds, overall request
  deadline 30 seconds, and decoded streamed body limit 4 MiB.
- JSON-LD only. Unsupported/missing unit metadata, currency, availability,
  variant, offer shape, or malformed JSON-LD yields an inconclusive result.

Offline tests use small synthetic JSON-LD and `httpx.MockTransport`; they cover
variant selection versus cheaper alternatives, subscription exclusion, rejected
URLs before I/O, redirects without a second request, missing fields, and bounded
body/deadline behavior. They never contact the retailer or copy its page.

## M2 handoff

Start with a narrow JSON-LD adapter that retains exact SKU/URL matching and
unconditional price selection. Preserve failure as an unavailable observation;
do not overwrite a prior good price with a guessed price. M2 still needs
persistence, scheduling/rate limits, observability, transport policy across
repeated requests, out-of-stock behavior, and regression coverage of changed
merchant markup. This spike proves the observed target is accessible with an
ordinary bounded HTTP request; it does not establish long-term site stability
or production scraping support.

References: [merchant target](https://www.zooplus.pl/shop/koty/karma_dla_kota_mokra/schesir/karma_schesir/2333304?activeVariant=2333304.0),
[schema.org Offer](https://schema.org/Offer),
[schema.org UnitPriceSpecification](https://schema.org/UnitPriceSpecification),
[HTTPX TLS verification](https://www.python-httpx.org/advanced/ssl/).
