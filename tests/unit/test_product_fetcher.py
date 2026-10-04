"""Synthetic HTTP wire fixtures through the production transport boundary."""

import asyncio
import gzip
import hashlib
import time
import zlib

import httpcore
import pytest
from test_product_transport import RecordingBackend, public_resolver, response_chunks

from mimit.products.fetcher import SafeProductFetcher, validate_url
from mimit.products.types import ErrorCode, ProductCheckError


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://shop.example/item",
        "http:///item",
        "/relative",
        "https://user:secret@shop.example/item",
        "https://@shop.example/item",
        "http://shop.example:8080/item",
        "https://shop.example:80/item",
        " https://shop.example/item",
        "https://shop.example/\nitem",
        "https://shop.example\\@127.0.0.1/item",
        "http://[invalid",
        "http://127.0.0.1/item",
        "http://[::1]/item",
        "http://[::ffff:127.0.0.1]/item",
        "http://localhost/item",
        "http://localhost./item",
        "http://api.railway.internal/item",
        "http://shop.local/item",
    ],
)
async def test_unsafe_url_fails_before_resolution_or_dial(url: str) -> None:
    async def unexpected_resolver(host: str, port: int) -> tuple[str, ...]:
        pytest.fail("Unsafe URL must fail before resolution")

    raw = RecordingBackend([])
    async with SafeProductFetcher(resolver=unexpected_resolver, network_backend=raw) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch(url)
    assert error.value.code in (ErrorCode.UNSAFE_URL, ErrorCode.UNSAFE_ADDRESS)
    assert "secret" not in str(error.value)
    assert raw.dials == []


def test_exact_query_identity_is_preserved_and_fragment_is_not_sent() -> None:
    url = "https://shop.example/item?activeVariant=2333304.0&other=a%2Fb#client"
    assert str(validate_url(url)) == url.split("#")[0]


async def test_success_has_bounded_body_hash_and_user_agent() -> None:
    body = "<html>kot żółty</html>".encode()
    raw = RecordingBackend([response_chunks(body)])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        result = await fetcher.fetch("https://shop.example/item?activeVariant=2333304.0")
    assert result.html == body.decode()
    assert result.body_bytes == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.status_code == 200
    assert result.final_url == "https://shop.example/item?activeVariant=2333304.0"
    request = b"".join(raw.streams[0].writes)
    assert b"User-Agent: Mimit/0.1" in request
    assert b"Accept-Encoding: gzip, deflate" in request


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://api.railway.internal/admin",
        "https://user:secret@shop.example/admin",
        "file:///etc/passwd",
    ],
)
async def test_every_redirect_is_validated_before_dial(location: str) -> None:
    raw = RecordingBackend([response_chunks(status=302, headers=(("Location", location),))])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        with pytest.raises(ProductCheckError):
            await fetcher.fetch("https://shop.example/item")
    assert len(raw.dials) == 1
    assert raw.streams[0].closed


async def test_redirect_hostname_private_dns_is_rejected_by_real_transport_boundary() -> None:
    resolved: list[str] = []

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        resolved.append(host)
        return ("93.184.216.34",) if host == "shop.example" else ("10.2.0.4",)

    raw = RecordingBackend(
        [response_chunks(status=302, headers=(("Location", "https://rebound.example/item"),))]
    )
    async with SafeProductFetcher(resolver=resolver, network_backend=raw) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("https://shop.example/item")
    assert error.value.code == ErrorCode.UNSAFE_ADDRESS
    assert resolved == ["shop.example", "rebound.example"]
    assert len(raw.dials) == 1


async def test_relative_redirect_and_cookie_auth_proxy_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@127.0.0.1:9999")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9998")
    monkeypatch.setenv("SSL_CERT_FILE", "/does/not/exist")
    raw = RecordingBackend(
        [
            response_chunks(
                status=302,
                headers=(("Location", "/final?variant=exact"), ("Set-Cookie", "session=private")),
            ),
            response_chunks(headers=(("Set-Cookie", "session=private"),)),
            response_chunks(),
        ]
    )
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, domain_cooldown=0
    ) as fetcher:
        page = await fetcher.fetch("https://shop.example/start")
        await fetcher.fetch("https://shop.example/again")
    assert page.final_url == "https://shop.example/final?variant=exact"
    assert len(raw.dials) == 3
    for stream in raw.streams:
        request = b"".join(stream.writes).lower()
        assert b"cookie:" not in request
        assert b"authorization:" not in request
        assert b"proxy-authorization:" not in request
        assert b"referer:" not in request


async def test_redirect_loop_is_bounded_and_connections_closed() -> None:
    raw = RecordingBackend(
        [response_chunks(status=302, headers=(("Location", "/again"),)) for _ in range(3)]
    )
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, max_redirects=2, domain_cooldown=0
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/start")
    assert error.value.code == ErrorCode.REDIRECT_LIMIT
    assert len(raw.dials) == 3
    assert all(stream.closed for stream in raw.streams)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity"])
async def test_supported_content_decoding_is_streamed_and_hashed(encoding: str) -> None:
    body = b"<html>test</html>" * 50
    encoded = (
        gzip.compress(body)
        if encoding == "gzip"
        else zlib.compress(body)
        if encoding == "deflate"
        else body
    )
    chunks = response_chunks(encoded, headers=(("Content-Encoding", encoding),))
    chunks = [chunks[0], *[encoded[index : index + 3] for index in range(0, len(encoded), 3)]]
    raw = RecordingBackend([chunks])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        page = await fetcher.fetch("http://shop.example/item")
    assert page.html == body.decode()
    assert page.sha256 == hashlib.sha256(body).hexdigest()
    assert page.body_bytes == len(body)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity"])
async def test_decoded_size_limit_rejects_compression_bombs(encoding: str) -> None:
    body = b"x" * 100_000
    encoded = (
        gzip.compress(body)
        if encoding == "gzip"
        else zlib.compress(body)
        if encoding == "deflate"
        else body
    )
    raw = RecordingBackend([response_chunks(encoded, headers=(("Content-Encoding", encoding),))])
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, max_decoded_bytes=128
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item")
    assert error.value.code == ErrorCode.BODY_TOO_LARGE
    assert raw.streams[0].closed


async def test_wire_content_length_limit_rejects_before_body_read() -> None:
    raw = RecordingBackend([response_chunks(b"x" * 1000)])
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, max_wire_bytes=128
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item")
    assert error.value.code == ErrorCode.BODY_TOO_LARGE
    assert raw.streams[0].closed


async def test_chunked_transfer_wire_limit_cannot_be_bypassed_by_missing_length() -> None:
    chunks = [
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"80\r\n" + b"a" * 128 + b"\r\n",
        b"80\r\n" + b"b" * 128 + b"\r\n",
        b"0\r\n\r\n",
    ]
    raw = RecordingBackend([chunks])
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, max_wire_bytes=200
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item")
    assert error.value.code == ErrorCode.BODY_TOO_LARGE
    assert raw.streams[0].closed


@pytest.mark.parametrize(
    "chunks,expected",
    [
        (response_chunks(content_type="application/json"), ErrorCode.UNSUPPORTED_CONTENT),
        (response_chunks(headers=(("Content-Encoding", "br"),)), ErrorCode.UNSUPPORTED_CONTENT),
        (response_chunks(status=403), ErrorCode.HTTP_ERROR),
        (response_chunks(status=429), ErrorCode.RATE_LIMITED),
        (response_chunks(b"\xff"), ErrorCode.INVALID_ENCODING),
        (
            response_chunks(content_type="text/html; charset=not-a-charset"),
            ErrorCode.INVALID_ENCODING,
        ),
        (
            response_chunks(b"bad gzip", headers=(("Content-Encoding", "gzip"),)),
            ErrorCode.INVALID_ENCODING,
        ),
        (
            response_chunks(gzip.compress(b"hello")[:-2], headers=(("Content-Encoding", "gzip"),)),
            ErrorCode.INVALID_ENCODING,
        ),
    ],
)
async def test_failures_are_safe_codes_and_close_response(
    chunks: list[bytes], expected: ErrorCode
) -> None:
    raw = RecordingBackend([chunks])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item?token=secret")
    assert error.value.code == expected
    assert str(error.value) == expected.value
    assert raw.streams[0].closed


async def test_declared_text_charset_is_used() -> None:
    body = "żółty".encode("iso-8859-2")
    raw = RecordingBackend([response_chunks(body, content_type="text/html; charset=iso-8859-2")])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        assert (await fetcher.fetch("http://shop.example/item")).html == "żółty"


async def test_total_deadline_includes_dns_resolution() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return ()

    raw = RecordingBackend([])
    async with SafeProductFetcher(
        resolver=resolver, network_backend=raw, total_timeout=0.02
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("https://shop.example/item")
    assert error.value.code == ErrorCode.TIMEOUT
    assert started.is_set() and cancelled.is_set()
    assert raw.dials == []


async def test_total_deadline_closes_stalled_response_connection() -> None:
    raw = RecordingBackend([response_chunks()])
    raw.read_release = asyncio.Event()
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, total_timeout=0.02
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item")
    assert error.value.code == ErrorCode.TIMEOUT
    assert raw.streams[0].closed


async def test_cancellation_propagates_closes_socket_and_releases_domain_gate() -> None:
    raw = RecordingBackend([response_chunks(), response_chunks()])
    raw.read_release = asyncio.Event()
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, domain_cooldown=0
    ) as fetcher:
        task = asyncio.create_task(fetcher.fetch("http://shop.example/item"))
        await asyncio.wait_for(raw.dialed.wait(), 1)
        await asyncio.wait_for(raw.streams[0].read_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert raw.streams[0].closed
        raw.read_release = None
        assert (await fetcher.fetch("http://shop.example/item")).html == "<html>ok</html>"


async def test_same_domain_serializes_and_waits_cooldown() -> None:
    raw = RecordingBackend([response_chunks(), response_chunks()])
    release = asyncio.Event()
    raw.read_release = release
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, domain_cooldown=0.03
    ) as fetcher:
        first = asyncio.create_task(fetcher.fetch("http://shop.example/a"))
        await asyncio.wait_for(raw.dialed.wait(), 1)
        second = asyncio.create_task(fetcher.fetch("https://shop.example/b"))
        await asyncio.sleep(0)
        assert len(raw.dials) == 1
        release.set()
        await first
        completed = time.monotonic()
        await second
        assert time.monotonic() - completed >= 0.02
    assert len(raw.dials) == 2


async def test_different_domains_can_fetch_concurrently() -> None:
    raw = RecordingBackend([response_chunks(), response_chunks()])
    release = asyncio.Event()
    raw.read_release = release
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        first = asyncio.create_task(fetcher.fetch("http://first.example/a"))
        await asyncio.wait_for(raw.dialed.wait(), 1)
        raw.dialed.clear()
        second = asyncio.create_task(fetcher.fetch("https://second.example/b"))
        await asyncio.wait_for(raw.dialed.wait(), 1)
        assert len(raw.dials) == 2
        release.set()
        await asyncio.gather(first, second)


async def test_closed_fetcher_cannot_be_reused() -> None:
    async with SafeProductFetcher() as fetcher:
        pass
    with pytest.raises(RuntimeError, match="closed"):
        await fetcher.fetch("https://shop.example/item")


async def test_resolver_os_error_is_safe_transport_error() -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        raise OSError("private OS error including secret")

    async with SafeProductFetcher(
        resolver=resolver, network_backend=RecordingBackend([])
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item")
    assert error.value.code == ErrorCode.TRANSPORT_ERROR
    assert "secret" not in str(error.value)


async def test_read_timeout_maps_to_safe_timeout() -> None:
    class TimeoutBackend(RecordingBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109 - required backend interface
            local_address: str | None = None,
            socket_options: object = None,
        ) -> httpcore.AsyncNetworkStream:
            raise httpcore.ConnectTimeout("private transport diagnostics")

    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=TimeoutBackend([])
    ) as fetcher:
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/item")
    assert error.value.code == ErrorCode.TIMEOUT


async def test_domain_cache_is_bounded_and_idle_gates_are_evictable() -> None:
    raw = RecordingBackend([response_chunks() for _ in range(260)])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        for index in range(260):
            await fetcher.fetch(f"http://shop{index}.example/item")
        assert len(fetcher._domains) == 256
        assert "shop0.example" not in fetcher._domains
        assert all(gate.users == 0 for gate in fetcher._domains.values())


async def test_deadline_includes_waiting_for_domain_cooldown() -> None:
    raw = RecordingBackend([response_chunks(), response_chunks()])
    async with SafeProductFetcher(
        resolver=public_resolver,
        network_backend=raw,
        total_timeout=0.02,
        domain_cooldown=1,
    ) as fetcher:
        await fetcher.fetch("http://shop.example/first")
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/second")
        assert all(gate.users == 0 for gate in fetcher._domains.values())
    assert error.value.code == ErrorCode.TIMEOUT
    assert len(raw.dials) == 1


async def test_global_concurrency_limit_bounds_distinct_host_requests() -> None:
    raw = RecordingBackend([response_chunks(), response_chunks()])
    release = asyncio.Event()
    raw.read_release = release
    async with SafeProductFetcher(
        resolver=public_resolver, network_backend=raw, max_connections=1
    ) as fetcher:
        first = asyncio.create_task(fetcher.fetch("http://first.example/a"))
        await asyncio.wait_for(raw.dialed.wait(), 1)
        second = asyncio.create_task(fetcher.fetch("http://second.example/b"))
        await asyncio.sleep(0)
        assert len(raw.dials) == 1
        release.set()
        await asyncio.gather(first, second)
    assert len(raw.dials) == 2
