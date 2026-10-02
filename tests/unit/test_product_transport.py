"""Exercise the actual httpcore network seam; no DNS or Internet access."""

import asyncio
import ssl
from collections.abc import Iterable
from pathlib import Path

import certifi
import httpcore
import pytest

from mimit.products.fetcher import PublicNetworkBackend, SafeProductFetcher, SocketOption
from mimit.products.types import ErrorCode, ProductCheckError

PUBLIC_IP = "93.184.216.34"


async def public_resolver(host: str, port: int) -> tuple[str, ...]:
    return (PUBLIC_IP,)


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__(chunks)
        self.writes: list[bytes] = []
        self.tls_host: str | None = None
        self.tls_context: ssl.SSLContext | None = None
        self.closed = False
        self.read_started = asyncio.Event()
        self.read_release: asyncio.Event | None = None

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,  # noqa: ASYNC109 - required backend interface
    ) -> None:
        self.writes.append(buffer)

    async def read(
        self,
        max_bytes: int,
        timeout: float | None = None,  # noqa: ASYNC109 - required backend interface
    ) -> bytes:
        self.read_started.set()
        if self.read_release is not None:
            await self.read_release.wait()
        return await super().read(max_bytes, timeout=timeout)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - required backend interface
    ) -> httpcore.AsyncNetworkStream:
        self.tls_host = server_hostname
        self.tls_context = ssl_context
        return self

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, responses: list[list[bytes]]) -> None:
        self.responses = responses
        self.dials: list[tuple[str, int]] = []
        self.streams: list[RecordingStream] = []
        self.read_release: asyncio.Event | None = None
        self.dialed = asyncio.Event()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - required backend interface
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dials.append((host, port))
        stream = RecordingStream(self.responses.pop(0))
        stream.read_release = self.read_release
        self.streams.append(stream)
        self.dialed.set()
        return stream


def response_chunks(
    body: bytes = b"<html>ok</html>",
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
    content_type: str = "text/html; charset=utf-8",
) -> list[bytes]:
    lines = [
        f"HTTP/1.1 {status} Test",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{key}: {value}" for key, value in headers),
    ]
    return [("\r\n".join(lines) + "\r\n\r\n").encode(), body]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fc00::1",
        "fec0::1",
        "64:ff9b::7f00:1",
        "64:ff9b:1::a00:1",
        "fe80::1",
        "fe80::1%eth0",
        "2001:db8::1",
        "ff0e::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::1",
        "not-an-ip",
    ],
)
async def test_non_public_dns_answers_never_reach_dial(address: str) -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return (address,)

    raw = RecordingBackend([])
    safe = PublicNetworkBackend(resolver=resolver, backend=raw)
    with pytest.raises(ProductCheckError) as error:
        await safe.connect_tcp("shop.example", 443)
    assert error.value.code == ErrorCode.UNSAFE_ADDRESS
    assert raw.dials == []


async def test_mixed_public_private_answer_fails_before_any_dial() -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return (PUBLIC_IP, "10.5.2.4")

    raw = RecordingBackend([])
    safe = PublicNetworkBackend(resolver=resolver, backend=raw)
    with pytest.raises(ProductCheckError):
        await safe.connect_tcp("shop.example", 443)
    assert raw.dials == []


async def test_dns_rebinding_between_connections_is_rejected_at_dial_boundary() -> None:
    calls = 0

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return (PUBLIC_IP,) if calls == 1 else ("127.0.0.1",)

    raw = RecordingBackend([response_chunks()])
    async with SafeProductFetcher(
        resolver=resolver, network_backend=raw, domain_cooldown=0
    ) as fetcher:
        await fetcher.fetch("http://shop.example/first")
        with pytest.raises(ProductCheckError) as error:
            await fetcher.fetch("http://shop.example/rebound")
    assert error.value.code == ErrorCode.UNSAFE_ADDRESS
    assert calls == 2
    assert raw.dials == [(PUBLIC_IP, 80)]


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
async def test_numeric_ip_dial_preserves_original_host_and_verified_tls(
    scheme: str, port: int
) -> None:
    calls: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        calls.append((host, port))
        return ("2606:4700:4700:0000:0000:0000:0000:1111",)

    raw = RecordingBackend([response_chunks()])
    async with SafeProductFetcher(resolver=resolver, network_backend=raw) as fetcher:
        await fetcher.fetch(f"{scheme}://shop.example/item?q=variant")
    assert calls == [("shop.example", port)]
    assert raw.dials == [("2606:4700:4700::1111", port)]
    stream = raw.streams[0]
    request = b"".join(stream.writes)
    assert b"GET /item?q=variant HTTP/1.1\r\n" in request
    assert b"Host: shop.example\r\n" in request
    if scheme == "https":
        assert stream.tls_host == "shop.example"
        assert stream.tls_context is not None
        assert stream.tls_context.check_hostname
        assert stream.tls_context.verify_mode == ssl.CERT_REQUIRED
    else:
        assert stream.tls_host is None
    assert stream.closed


async def test_direct_public_literal_is_pinned_without_resolver() -> None:
    async def unexpected_resolver(host: str, port: int) -> tuple[str, ...]:
        pytest.fail("IP literals do not require DNS")

    raw = RecordingBackend([response_chunks()])
    safe = PublicNetworkBackend(resolver=unexpected_resolver, backend=raw)
    stream = await safe.connect_tcp(PUBLIC_IP, 80)
    await stream.aclose()
    assert raw.dials == [(PUBLIC_IP, 80)]


async def test_unix_socket_is_always_rejected() -> None:
    safe = PublicNetworkBackend(backend=RecordingBackend([]))
    with pytest.raises(ProductCheckError) as error:
        await safe.connect_unix_socket("/var/run/docker.sock")
    assert error.value.code == ErrorCode.UNSAFE_ADDRESS


async def test_default_socket_backend_cannot_resolve_hostname_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run real AnyIO/asyncio connection code up to the socket dial, then stop it."""
    import socket

    loop = asyncio.get_running_loop()
    socket_dials: list[tuple[str, int]] = []

    async def stop_at_socket(sock: socket.socket, address: tuple[str, int]) -> None:
        socket_dials.append(address)
        raise OSError("Synthetic connection refusal at actual socket dial")

    async def unexpected_dns(*args: object, **kwargs: object) -> list[object]:
        pytest.fail("Pinned numerical TCP destination must not trigger another DNS resolution")

    monkeypatch.setattr(loop, "sock_connect", stop_at_socket)
    monkeypatch.setattr(loop, "getaddrinfo", unexpected_dns)
    safe = PublicNetworkBackend(resolver=public_resolver)
    with pytest.raises(httpcore.ConnectError):
        await safe.connect_tcp("shop.example", 443)
    assert socket_dials == [(PUBLIC_IP, 443)]


# Synthetic public certificate only; its private key is not needed or stored.
ENVIRONMENT_CA = """-----BEGIN CERTIFICATE-----
MIICLTCCAdICCQDfnVLabp+/xjAKBggqhkjOPQQDAjAkMSIwIAYDVQQDDBlNaW1p
dC1FbnZpcm9ubWVudC1UZXN0LUNBMB4XDTI2MTAwMjE2MjAwMVoXDTM2MDkyOTE2
MjAwMVowJDEiMCAGA1UEAwwZTWltaXQtRW52aXJvbm1lbnQtVGVzdC1DQTCCAUsw
ggEDBgcqhkjOPQIBMIH3AgEBMCwGByqGSM49AQECIQD/////AAAAAQAAAAAAAAAA
AAAAAP///////////////zBbBCD/////AAAAAQAAAAAAAAAAAAAAAP//////////
/////AQgWsY12Ko6k+ez671VdpiGvGUdBrDMU7D2O848PifSYEsDFQDEnTYIhucE
k2pmeOETnSa3gZ9+kARBBGsX0fLhLEJH+Lzm5WOkQPJ3A32BLeszoPShOUXYmMKW
T+NC4v4af5uO5+tKfA+eFivOM1drMV7Oy7ZAaDe/UfUCIQD/////AAAAAP//////
////vOb6racXnoTzucrC/GMlUQIBAQNCAATzGCGPRT4OzacEWq4RxRP8JR15Xbwl
llC0NBbRgrIXx00UFEzmjrtDQ5/7Z8Aa2rQed95B3hg9f4xpuHZD4t54MAoGCCqG
SM49BAMCA0kAMEYCIQDfTq6v6Ll0NtNJROQDok40L1nuIvy6tKfuMVKXJplkDgIh
AL1ZjE6R5ncSO+13Qfbr41yolb4IWmN3WsWGdFYuib3M
-----END CERTIFICATE-----
"""


async def test_valid_environment_ca_and_keylog_do_not_change_tls_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ca_file = tmp_path / "environment-ca.pem"
    ca_file.write_text(ENVIRONMENT_CA)
    ca_directory = tmp_path / "cert-directory"
    ca_directory.mkdir()
    keylog_file = tmp_path / "tls-session-keys.log"
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", str(ca_directory))
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog_file))
    expected = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    expected.load_verify_locations(cafile=certifi.where())
    raw = RecordingBackend([response_chunks()])
    async with SafeProductFetcher(resolver=public_resolver, network_backend=raw) as fetcher:
        await fetcher.fetch("https://shop.example/item")
    actual = raw.streams[0].tls_context
    assert actual is not None
    assert actual.check_hostname and actual.verify_mode == ssl.CERT_REQUIRED
    assert actual.cert_store_stats() == expected.cert_store_stats()
    assert set(actual.get_ca_certs(binary_form=True)) == set(
        expected.get_ca_certs(binary_form=True)
    )
    assert actual.keylog_filename is None
    assert not keylog_file.exists()
