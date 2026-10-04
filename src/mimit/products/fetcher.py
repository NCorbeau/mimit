"""Bounded, public-network-only HTML fetches for user-supplied product URLs.

DNS is resolved at the httpcore TCP boundary. Only validated, canonical numeric
addresses reach the socket backend; HTTP Host and verified TLS SNI remain the
original hostname. Injected resolvers/backends are test seams, not trust bypasses.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import ipaddress
import socket
import ssl
import time
import zlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import TracebackType
from urllib.parse import urlsplit

import certifi
import httpcore
import httpx

from mimit.products.types import ErrorCode, FetchedPage, ProductCheckError

Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)


async def resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve both IPv4 and IPv6 through the asyncio system resolver."""
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
    )
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


def _public_address(address: str) -> str:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise ProductCheckError(ErrorCode.UNSAFE_ADDRESS) from None
    # Multicast can be is_global; scoped IPv6 and transition mechanisms are not
    # acceptable public HTTP destinations even when the outer prefix is global.
    if (
        not parsed.is_global
        or parsed.is_multicast
        or parsed.is_reserved
        or "%" in address
        or (
            isinstance(parsed, ipaddress.IPv6Address)
            and (
                parsed.is_site_local
                or parsed.ipv4_mapped is not None
                or parsed.sixtofour is not None
                or parsed.teredo
            )
        )
    ):
        raise ProductCheckError(ErrorCode.UNSAFE_ADDRESS)
    return str(parsed)


def validate_url(value: str) -> httpx.URL:
    """Accept absolute HTTP(S), credential-free URLs on their standard ports."""
    if len(value) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ProductCheckError(ErrorCode.UNSAFE_URL)
    try:
        url = httpx.URL(value)
        expected_port = {"http": 80, "https": 443}.get(url.scheme)
        if (
            expected_port is None
            or not url.host
            or url.userinfo
            or "@" in urlsplit(value).netloc
            or url.port not in (None, expected_port)
            or "\\" in value
            or "%" in url.host
        ):
            raise ProductCheckError(ErrorCode.UNSAFE_URL)
        host = url.host.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ProductCheckError(ErrorCode.UNSAFE_ADDRESS)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass  # Hostnames are resolved and checked at the connection boundary.
        else:
            _public_address(host)
        return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError) as exc:
        if isinstance(exc, ProductCheckError):
            raise
        raise ProductCheckError(ErrorCode.UNSAFE_URL) from None


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Pin each TCP connection to an address validated immediately before dial."""

    def __init__(
        self,
        *,
        resolver: Resolver = resolve_addresses,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._backend = backend if backend is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        async with asyncio.timeout(timeout):
            try:
                ipaddress.ip_address(host)
            except ValueError:
                addresses = await self._resolver(host, port)
            else:
                addresses = (host,)
            if not addresses:
                raise httpcore.ConnectError("DNS resolution failed")
            # Reject a mixed public/private answer rather than silently choosing
            # a public address. No second hostname resolution occurs on dial.
            pinned = tuple(dict.fromkeys(_public_address(address) for address in addresses))
            last_error: httpcore.ConnectError | None = None
            for address in pinned:
                try:
                    return await self._backend.connect_tcp(
                        address,
                        port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                except httpcore.ConnectError as exc:
                    last_error = exc
            raise httpcore.ConnectError("Connection failed") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise ProductCheckError(ErrorCode.UNSAFE_ADDRESS)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_stream():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class _PublicTransport(httpx.AsyncBaseTransport):
    """Small public-API HTTPX/httpcore adapter without proxies or client state."""

    def __init__(self, backend: PublicNetworkBackend, max_connections: int) -> None:
        # Do not use create_default_context(): OpenSSL trust environment and
        # SSLKEYLOGFILE would alter the trust store or expose TLS session keys.
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls_context.load_verify_locations(cafile=certifi.where())
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=backend,
            ssl_context=tls_context,
            max_connections=max_connections,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=b"",
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


@dataclass
class _DomainGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    next_request: float = 0.0


class SafeProductFetcher:
    """One GET per domain at a time, with a cooldown and bounded resource use.

    Use ``async with SafeProductFetcher() as fetcher`` or close explicitly via
    ``aclose``. Limits and cooldown apply to this instance/process, not globally.
    Requests have no cookie jar, auth, environment proxy, or environment TLS settings.
    """

    def __init__(
        self,
        *,
        total_timeout: float = 20.0,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
        max_redirects: int = 3,
        max_wire_bytes: int = 2 * 1024 * 1024,
        max_decoded_bytes: int = 4 * 1024 * 1024,
        domain_cooldown: float = 1.0,
        max_connections: int = 8,
        user_agent: str = "Mimit/0.1 (household product price checks)",
        resolver: Resolver = resolve_addresses,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        if (
            min(total_timeout, connect_timeout, read_timeout) <= 0
            or min(max_wire_bytes, max_decoded_bytes, max_connections) <= 0
            or max_redirects < 0
            or domain_cooldown < 0
            or not user_agent.strip()
        ):
            raise ValueError("Invalid fetch limits")
        self._total_timeout = total_timeout
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_redirects = max_redirects
        self._max_wire_bytes = max_wire_bytes
        self._max_decoded_bytes = max_decoded_bytes
        self._domain_cooldown = domain_cooldown
        self._user_agent = user_agent
        self._slots = asyncio.Semaphore(max_connections)
        self._domains: OrderedDict[str, _DomainGate] = OrderedDict()
        self._max_domain_states = max(256, max_connections)
        self._transport = _PublicTransport(
            PublicNetworkBackend(resolver=resolver, backend=network_backend), max_connections
        )
        self._closed = False

    async def __aenter__(self) -> SafeProductFetcher:
        if self._closed:
            raise RuntimeError("Fetcher is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        await self._transport.aclose()

    @asynccontextmanager
    async def _domain_slot(self, host: str) -> AsyncIterator[None]:
        gate = self._domains.get(host)
        if gate is None:
            if len(self._domains) >= self._max_domain_states:
                for key, existing in self._domains.items():
                    if existing.users == 0:
                        del self._domains[key]
                        break
            gate = _DomainGate()
            self._domains[host] = gate
        self._domains.move_to_end(host)
        gate.users += 1
        try:
            async with gate.lock:
                delay = gate.next_request - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    yield
                finally:
                    gate.next_request = time.monotonic() + self._domain_cooldown
        finally:
            gate.users -= 1

    async def fetch(self, url: str) -> FetchedPage:
        if self._closed:
            raise RuntimeError("Fetcher is closed")
        target = validate_url(url)
        try:
            async with asyncio.timeout(self._total_timeout), self._slots:
                return await self._fetch(target)
        except ProductCheckError:
            raise
        except (TimeoutError, httpcore.TimeoutException, httpx.TimeoutException):
            raise ProductCheckError(ErrorCode.TIMEOUT) from None
        except (httpcore.NetworkError, httpcore.ProtocolError, httpx.HTTPError, OSError):
            raise ProductCheckError(ErrorCode.TRANSPORT_ERROR) from None

    async def _fetch(self, target: httpx.URL) -> FetchedPage:
        for redirect_count in range(self._max_redirects + 1):
            target = validate_url(str(target))
            request = httpx.Request(
                "GET",
                target,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html, application/xhtml+xml",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "close",
                },
                extensions={
                    "timeout": {
                        "connect": self._connect_timeout,
                        "read": self._read_timeout,
                        "write": self._connect_timeout,
                        "pool": self._connect_timeout,
                    }
                },
            )
            async with self._domain_slot(target.host.lower().rstrip(".")):
                response = await self._transport.handle_async_request(request)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if redirect_count >= self._max_redirects:
                            raise ProductCheckError(ErrorCode.REDIRECT_LIMIT)
                        location = response.headers.get("location")
                        if not location:
                            raise ProductCheckError(
                                ErrorCode.HTTP_ERROR, http_status=response.status_code
                            )
                        try:
                            target = validate_url(str(target.join(location)))
                        except httpx.InvalidURL:
                            raise ProductCheckError(ErrorCode.UNSAFE_URL) from None
                        continue
                    if response.status_code == 429:
                        raise ProductCheckError(ErrorCode.RATE_LIMITED, http_status=429)
                    if response.status_code != 200:
                        raise ProductCheckError(
                            ErrorCode.HTTP_ERROR, http_status=response.status_code
                        )
                    content_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if content_type not in ("text/html", "application/xhtml+xml"):
                        raise ProductCheckError(ErrorCode.UNSUPPORTED_CONTENT)
                    body = await self._read_body(response)
                    try:
                        encoding = response.charset_encoding or "utf-8"
                        codecs.lookup(encoding)
                        html = body.decode(encoding, errors="strict")
                    except (LookupError, UnicodeError):
                        raise ProductCheckError(ErrorCode.INVALID_ENCODING) from None
                    return FetchedPage(
                        html=html,
                        final_url=str(target),
                        status_code=response.status_code,
                        body_bytes=len(body),
                        sha256=hashlib.sha256(body).hexdigest(),
                    )
                finally:
                    await response.aclose()
        raise ProductCheckError(ErrorCode.REDIRECT_LIMIT)  # Defensive; loop always returns/raises.

    async def _read_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self._max_wire_bytes:
                    raise ProductCheckError(ErrorCode.BODY_TOO_LARGE)
            except ValueError as exc:
                if isinstance(exc, ProductCheckError):
                    raise
                raise ProductCheckError(ErrorCode.TRANSPORT_ERROR) from None
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("identity", "gzip", "deflate"):
            raise ProductCheckError(ErrorCode.UNSUPPORTED_CONTENT)
        decoder = (
            zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            if encoding != "identity"
            else None
        )
        body = bytearray()
        wire_bytes = 0
        try:
            async for chunk in response.aiter_raw():
                wire_bytes += len(chunk)
                if wire_bytes > self._max_wire_bytes:
                    raise ProductCheckError(ErrorCode.BODY_TOO_LARGE)
                if decoder is None:
                    body.extend(chunk)
                else:
                    # max_length prevents allocating a decompression bomb before
                    # checking the decoded limit. Never use unbounded flush().
                    body.extend(decoder.decompress(chunk, self._max_decoded_bytes - len(body) + 1))
                    if decoder.unconsumed_tail:
                        raise ProductCheckError(ErrorCode.BODY_TOO_LARGE)
                    if decoder.unused_data:
                        raise ProductCheckError(ErrorCode.INVALID_ENCODING)
                if len(body) > self._max_decoded_bytes:
                    raise ProductCheckError(ErrorCode.BODY_TOO_LARGE)
            if decoder is not None and not decoder.eof:
                raise ProductCheckError(ErrorCode.INVALID_ENCODING)
        except zlib.error:
            raise ProductCheckError(ErrorCode.INVALID_ENCODING) from None
        return bytes(body)
