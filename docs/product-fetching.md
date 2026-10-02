# Safe product fetching and extraction (MAC-31)

`SafeProductFetcher` is the production network boundary; the older Zooplus spike
is developer evidence only. The fetcher accepts HTTP/HTTPS on ports 80/443, with
no credentials, proxies, cookie jar, authentication, or environment TLS trust.
TLS verifies the original hostname against the explicit certifi trust bundle.

DNS is checked at the TCP connection boundary. Every returned IPv4/IPv6 address
must be public; private, loopback, link-local, reserved, multicast, scoped and
transition addresses are rejected. The socket backend receives only a validated
numeric address, while HTTP Host and TLS SNI keep the original hostname.
Each redirect is validated again and each new connection resolves again.

Default limits are three redirects, 20 seconds overall (including DNS, queueing
and body reads), five seconds per connect/read, 2 MiB wire and 4 MiB decoded HTML.
Only HTML/XHTML and identity/gzip/deflate are accepted; decompression is bounded
before allocation. Requests identify themselves as Mimit. An instance allows eight
concurrent checks, one request per hostname, and a one-second cooldown. Limits
are process-local; operators should avoid concurrent one-shot processes.
Cancellation closes resources. Errors expose fixed codes, never raw URLs or bodies.

`JsonLdExtractor` works offline. It reads bounded Product/Offer JSON-LD, including
ProductGroup variants and document-local references; it never loads contexts or
remote references. Compatible repeated definitions are supported. Conflicting
identities, ambiguous products/offers, aggregate offers and conditional selling
offers fail closed. Generic source URLs are compared conservatively.

For Zooplus, the original product path and `activeVariant` must match the exact
Product SKU and both Product/Offer URLs. The canonical unconditional `Offer.price`
is the one-time price; member, subscription and conditional specifications never
replace it. Optional unit prices retain their merchant unit (for example PLN/kg).
They do not convert household stock from pouches to kilograms.

Parsing limits are 4 MiB HTML characters, 512 KiB total JSON-LD characters,
64 scripts, 10,000 tree entries and depth 40. Money uses bounded Decimal values
compatible with PostgreSQL NUMERIC(18,6). There is no DOM scraper, fallback
adapter, recurring scheduler or recommendation logic in this layer.

## Verification — 2 October 2026

The production fetcher and generic extractor returned HTTP 200 for the submitted
Schesir `2333304.0` URL. The extracted one-time offer was 42.96 PLN, its optional
unit price was 84.24 PLN/kg, and availability was in stock. These values are
observations of that check, not hard-coded prices or a promise of future pricing.
Compatible repeated membership-tier descriptions required generic JSON-LD
reconciliation; no merchant-specific DOM fallback was necessary (MAC-47).
Synthetic fixtures keep membership/subscription prices distinct from one-time
pricing and exercise mismatched variants and ambiguous data.

Independent reviews found and verified fixes for environment TLS trust/key logging,
extreme JSON numeric exponents, mixed aggregate/offer types and repeated local IDs.
The network tests intercept the actual numeric socket dial, prohibit a second DNS
lookup, and check verified original-host SNI. The fast suite includes cancellation,
redirect, response-size, decompression, concurrency and metadata ambiguity checks.
