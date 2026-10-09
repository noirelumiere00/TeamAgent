"""出典 URL の一時失敗・固定 IP・絶対期限を外部接続なしで確かめる。"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import httpx
import pytest

from teamagent.adapters import source_url_check
from teamagent.adapters.source_url_check import SourceUrlChecker, SourceUrlCheckError

URL = "https://example.com/report"
REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/fake"
PUBLIC_IP = "93.184.216.34"


@pytest.mark.parametrize("failure", ["timeout", "http_error", 500, 503])
def test_temporary_http_failure_is_not_cached(failure: str | int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("fake", request=request)
            if failure == "http_error":
                raise httpx.ConnectError("fake", request=request)
            return httpx.Response(int(failure))
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=lambda _host: [PUBLIC_IP])
        assert not checker.verify_public_url(URL).ok
        assert checker.verify_public_url(URL).ok
        assert calls == 2


def test_temporary_dns_failure_is_not_cached() -> None:
    calls = 0

    def resolve(_host: str) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise socket.gaierror("fake")
        return [PUBLIC_IP]

    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        checker = SourceUrlChecker(client=client, resolver=resolve)
        assert checker.verify_public_url(URL).reason == "dns_failed"
        assert checker.verify_public_url(URL).ok
        assert calls == 2


@pytest.mark.parametrize("failure", ["timeout", "http_error", 429, 503])
def test_temporary_grounding_redirect_failure_can_recover(failure: str | int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("fake", request=request)
            if failure == "http_error":
                raise httpx.ConnectError("fake", request=request)
            return httpx.Response(int(failure))
        return httpx.Response(302, headers={"location": URL})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=lambda _host: [PUBLIC_IP])
        with pytest.raises(SourceUrlCheckError):
            checker.resolve_grounding_redirect(REDIRECT)
        assert checker.resolve_grounding_redirect(REDIRECT) == URL
        assert calls == 2


def test_failure_after_redirect_does_not_poison_shared_http_cache() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == URL:
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(503 if calls.count(url) == 1 else 200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=lambda _host: [PUBLIC_IP])
        assert not checker.verify_public_url(URL).ok
        assert checker.verify_public_url(URL).ok
        assert calls == [URL, "https://example.com/final", "https://example.com/final"]


def test_permanent_failure_remains_cached() -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=lambda _host: [PUBLIC_IP])
        first = checker.verify_public_url(URL)
        assert checker.verify_public_url(URL) is first
        assert calls == 1


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeSocket:
    """socket API を模すだけで、OS の socket/DNS/connect は呼ばない。"""

    def __init__(self, response: bytes, clock: _Clock | None = None) -> None:
        self.response = response
        self.clock = clock
        self.read_delay = 0.0
        self.write_delay = 0.0
        self.timeout: float | None = None
        self.address: tuple[str, int] | None = None
        self.written = b""
        self.closed = False

    def setsockopt(self, *_args: Any) -> None:
        pass

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[str, int]) -> None:
        self.address = address

    def _advance(self, amount: float) -> None:
        if self.clock is not None:
            if self.timeout is not None and amount >= self.timeout:
                self.clock.now += self.timeout
                raise TimeoutError("fake bounded socket operation")
            self.clock.now += amount

    def send(self, buffer: bytes) -> int:
        self._advance(self.write_delay)
        part = buffer[:1] if self.write_delay else buffer
        self.written += part
        return len(part)

    def recv(self, max_bytes: int) -> bytes:
        self._advance(self.read_delay)
        size = min(max_bytes, 1) if self.read_delay else max_bytes
        part, self.response = self.response[:size], self.response[size:]
        return part

    def close(self) -> None:
        self.closed = True


def _fake_sockets(monkeypatch: pytest.MonkeyPatch, sockets: list[_FakeSocket]) -> None:
    pending = iter(sockets)
    monkeypatch.setattr(source_url_check.socket, "socket", lambda *_args, **_kw: next(pending))


def test_default_transport_connects_only_to_checked_ip_without_second_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls: list[str] = []

    def fake_dns(host: str, *_args: Any, **_kw: Any) -> list[tuple[Any, ...]]:
        dns_calls.append(host)
        # 古い実装の接続時再解決なら private IP が返る。
        address = PUBLIC_IP if len(dns_calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]

    sock = _FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    _fake_sockets(monkeypatch, [sock])
    monkeypatch.setattr(source_url_check.socket, "getaddrinfo", fake_dns)
    checker = SourceUrlChecker()
    try:
        assert checker.verify_public_url("http://rebind.example/report").ok
    finally:
        checker.close()
    assert dns_calls == ["rebind.example"]
    assert sock.address == (PUBLIC_IP, 80)
    assert b"Host: rebind.example\r\n" in sock.written
    assert sock.closed


def test_https_keeps_original_idna_host_for_tls_and_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = _FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    _fake_sockets(monkeypatch, [sock])
    server_names: list[str | None] = []
    resolved_names: list[str] = []

    class FakeTlsContext:
        def set_alpn_protocols(self, protocols: list[str]) -> None:
            assert protocols == ["http/1.1"]

        def wrap_socket(self, stream: _FakeSocket, *, server_hostname: str | None) -> _FakeSocket:
            server_names.append(server_hostname)
            return stream

    monkeypatch.setattr(
        source_url_check.httpx, "create_ssl_context", lambda **_kw: FakeTlsContext()
    )

    def resolve(host: str) -> list[str]:
        resolved_names.append(host)
        return [PUBLIC_IP]

    checker = SourceUrlChecker(resolver=resolve)
    try:
        assert checker.verify_public_url("https://faß.example/report").ok
    finally:
        checker.close()
    assert resolved_names == ["xn--fa-hia.example"]
    assert server_names == ["xn--fa-hia.example"]
    assert sock.address == (PUBLIC_IP, 443)
    assert b"Host: xn--fa-hia.example\r\n" in sock.written


def test_tls_socket_is_closed_when_handshake_finishes_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    raw = _FakeSocket(b"")
    wrapped = _FakeSocket(b"")
    _fake_sockets(monkeypatch, [raw])

    class FakeTlsContext:
        def set_alpn_protocols(self, protocols: list[str]) -> None:
            assert protocols == ["http/1.1"]

        def wrap_socket(self, stream: _FakeSocket, *, server_hostname: str | None) -> _FakeSocket:
            assert stream is raw
            assert server_hostname == "example.com"
            clock.now = 1.0
            return wrapped

    monkeypatch.setattr(
        source_url_check.httpx, "create_ssl_context", lambda **_kw: FakeTlsContext()
    )
    checker = SourceUrlChecker(resolver=lambda _host: [PUBLIC_IP], total_timeout_s=1.0, clock=clock)
    try:
        result = checker.verify_public_url(URL)
    finally:
        checker.close()
    assert result.reason == "deadline_exceeded"
    assert raw.closed
    assert wrapped.closed


@pytest.mark.parametrize("host", ["faß.example", "σς.example"])
def test_dns_check_uses_httpx_idna_host_and_rejects_private_address(host: str) -> None:
    resolved: list[str] = []
    transport_calls: list[str] = []
    punycode_host = httpx.URL(f"http://{host}").raw_host.decode("ascii")

    def resolve(name: str) -> list[str]:
        resolved.append(name)
        return ["127.0.0.1"] if name == punycode_host else [PUBLIC_IP]

    def handle(request: httpx.Request) -> httpx.Response:
        transport_calls.append(str(request.url))
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=resolve)
        assert checker.verify_public_url(f"http://{host}/secret").reason == "non_public_address"
    assert resolved == [punycode_host]
    assert transport_calls == []


def test_default_transport_checks_private_redirect_before_second_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = _FakeSocket(b"HTTP/1.1 302 Found\r\nLocation: http://internal.example/secret\r\n\r\n")
    _fake_sockets(monkeypatch, [sock])
    checker = SourceUrlChecker(
        resolver=lambda host: ["10.0.0.1"] if host == "internal.example" else [PUBLIC_IP]
    )
    try:
        result = checker.verify_public_url("http://example.com/report")
    finally:
        checker.close()
    assert not result.ok
    assert result.reason == "non_public_address"
    assert sock.address == (PUBLIC_IP, 80)


def test_head_to_get_revalidates_and_blocks_changed_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = _FakeSocket(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
    _fake_sockets(monkeypatch, [sock])
    calls = 0

    def resolve(_host: str) -> list[str]:
        nonlocal calls
        calls += 1
        return [PUBLIC_IP] if calls == 1 else ["169.254.169.254"]

    checker = SourceUrlChecker(resolver=resolve)
    try:
        result = checker.verify_public_url("http://example.com/report")
    finally:
        checker.close()
    assert result.reason == "non_public_address"
    assert calls == 2
    assert sock.written.startswith(b"HEAD ")


@pytest.mark.parametrize("operation", ["read", "write"])
def test_default_transport_absolute_deadline_bounds_trickled_io(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    clock = _Clock()
    sock = _FakeSocket(b"HTTP/1.1 200 OK\r\nX-Pad: trickled-header\r\n\r\n", clock)
    setattr(sock, f"{operation}_delay", 0.4)
    _fake_sockets(monkeypatch, [sock])
    checker = SourceUrlChecker(resolver=lambda _host: [PUBLIC_IP], total_timeout_s=1.0, clock=clock)
    try:
        result = checker.verify_public_url("http://example.com/report")
    finally:
        checker.close()
    assert not result.ok
    assert result.reason in {"timeout", "deadline_exceeded"}
    assert clock.now <= 1.0
    assert sock.closed


def test_redirect_hops_share_one_absolute_deadline() -> None:
    clock = _Clock()
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        clock.now += 0.6
        return httpx.Response(302, headers={"location": f"/{calls}"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(
            client=client, resolver=lambda _host: [PUBLIC_IP], total_timeout_s=1.0, clock=clock
        )
        result = checker.verify_public_url(URL)
    assert result.reason == "deadline_exceeded"
    assert calls == 2


def test_hung_dns_returns_by_deadline_without_holding_http_slot() -> None:
    entered, release = Event(), Event()

    def resolve(_host: str) -> list[str]:
        entered.set()
        assert release.wait(2)
        return [PUBLIC_IP]

    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        checker = SourceUrlChecker(client=client, resolver=resolve, total_timeout_s=0.02)
        try:
            result = checker.verify_public_url(URL)
            assert entered.is_set()
            assert result.reason == "deadline_exceeded"
            assert checker._slots.acquire(blocking=False)
            checker._slots.release()
        finally:
            release.set()


def test_wait_for_concurrency_slot_is_bounded() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        checker = SourceUrlChecker(
            client=client, resolver=lambda _host: [PUBLIC_IP], total_timeout_s=0.01
        )
        for _ in range(source_url_check.MAX_SOURCE_URL_CONCURRENCY):
            assert checker._slots.acquire(blocking=False)
        try:
            assert checker.verify_public_url(URL).reason == "deadline_exceeded"
        finally:
            for _ in range(source_url_check.MAX_SOURCE_URL_CONCURRENCY):
                checker._slots.release()


def test_duplicate_waiter_has_own_bounded_wait() -> None:
    entered, release = Event(), Event()

    def handle(_request: httpx.Request) -> httpx.Response:
        entered.set()
        assert release.wait(2)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(
            client=client, resolver=lambda _host: [PUBLIC_IP], total_timeout_s=0.02
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner = pool.submit(checker.verify_public_url, URL)
            assert entered.wait(1)
            try:
                assert checker.verify_public_url(URL).reason == "deadline_exceeded"
            finally:
                release.set()
            assert not owner.result().ok
