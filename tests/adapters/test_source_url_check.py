"""実測と同じ Vertex 転送 URL をフェイク HTTP/DNS だけで検査する。"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import httpx
import pytest

from teamagent.adapters.source_url_check import (
    MAX_SOURCE_URL_CONCURRENCY,
    SourceUrlChecker,
    SourceUrlCheckError,
)

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ-measured"
ORIGINAL = "https://biz.loyalty.co.jp/report/141/"
PUBLIC_IPS = ["93.184.216.34", "2606:4700:4700::1111"]


class _UnreadBody(httpx.SyncByteStream):
    """本文を読んだ時点で落とす。HEAD/GET とも閉じるだけが正しい。"""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("本文を読んではいけない")

    def close(self) -> None:
        self.closed = True


def _checker(
    routes: dict[tuple[str, str], tuple[int, str | None]],
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> tuple[SourceUrlChecker, list[tuple[str, str]], list[_UnreadBody]]:
    calls: list[tuple[str, str]] = []
    bodies: list[_UnreadBody] = []

    def handle(request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url))
        calls.append(key)
        assert request.extensions["timeout"] == {
            "connect": 8.0,
            "read": 8.0,
            "write": 8.0,
            "pool": 8.0,
        }
        status, location = routes[key]
        headers = {"location": location} if location is not None else {}
        body = _UnreadBody()
        bodies.append(body)
        return httpx.Response(status, headers=headers, stream=body)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    return (
        SourceUrlChecker(client=client, resolver=resolver or (lambda _host: PUBLIC_IPS)),
        calls,
        bodies,
    )


def test_grounding_redirect_resolves_302_to_original_url_and_caches() -> None:
    checker, calls, bodies = _checker({("HEAD", REDIRECT): (302, ORIGINAL)})
    assert checker.resolve_grounding_redirect(REDIRECT) == ORIGINAL
    assert checker.resolve_grounding_redirect(REDIRECT) == ORIGINAL
    assert calls == [("HEAD", REDIRECT)]
    assert bodies[0].closed


def test_other_hosts_are_unchanged_without_http_or_dns() -> None:
    def no_dns(_host: str) -> list[str]:
        raise AssertionError("他ホストの解決で DNS を使わない")

    checker, calls, _ = _checker({}, resolver=no_dns)
    assert checker.resolve_grounding_redirect(ORIGINAL) == ORIGINAL
    assert (
        checker.resolve_grounding_redirect("https://vertexaisearch.cloud.google.com.evil.test/x")
        == "https://vertexaisearch.cloud.google.com.evil.test/x"
    )
    assert calls == []


def test_grounding_head_405_uses_get_without_reading_body() -> None:
    checker, calls, bodies = _checker(
        {("HEAD", REDIRECT): (405, None), ("GET", REDIRECT): (302, ORIGINAL)}
    )
    assert checker.resolve_grounding_redirect(REDIRECT) == ORIGINAL
    assert calls == [("HEAD", REDIRECT), ("GET", REDIRECT)]
    assert all(body.closed for body in bodies)


def test_grounding_redirect_must_resolve_and_failure_is_cached() -> None:
    checker, calls, _ = _checker({("HEAD", REDIRECT): (200, None)})
    for _ in range(2):
        with pytest.raises(SourceUrlCheckError, match="grounding_redirect_unresolved"):
            checker.resolve_grounding_redirect(REDIRECT)
    assert calls == [("HEAD", REDIRECT)]


def test_resolved_source_404_is_not_usable() -> None:
    checker, calls, _ = _checker(
        {("HEAD", REDIRECT): (302, ORIGINAL), ("HEAD", ORIGINAL): (404, None)}
    )
    resolved = checker.resolve_grounding_redirect(REDIRECT)
    result = checker.verify_public_url(resolved)
    assert not result.ok
    assert result.status_code == 404
    assert result.url == ORIGINAL
    assert calls == [("HEAD", REDIRECT), ("HEAD", ORIGINAL)]


@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_private_dns_is_rejected_before_http(address: str) -> None:
    checker, calls, _ = _checker({}, resolver=lambda _host: [address])
    result = checker.verify_public_url(ORIGINAL)
    assert not result.ok
    assert result.reason == "non_public_address"
    assert calls == []


def test_all_dns_addresses_must_be_public() -> None:
    checker, calls, _ = _checker({}, resolver=lambda _host: [PUBLIC_IPS[0], "10.0.0.2"])
    assert not checker.verify_public_url(ORIGINAL).ok
    assert calls == []


@pytest.mark.parametrize("host", ["10.0.0.1", "127.0.0.1", "169.254.169.254", "[::1]"])
def test_private_ip_literal_is_rejected_even_with_public_dns_fake(host: str) -> None:
    checker, calls, _ = _checker({})
    result = checker.verify_public_url(f"http://{host}/secret")
    assert not result.ok
    assert result.reason == "non_public_address"
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "https://user@example.com/x",
        "https://example.com:bad/x",
        "",
        "https://[bad-ip]/x",
    ],
)
def test_invalid_or_authenticated_url_is_rejected_before_http(url: str) -> None:
    checker, calls, _ = _checker({})
    assert not checker.verify_public_url(url).ok
    assert calls == []


@pytest.mark.parametrize("head_status", [405, 501])
def test_verify_head_not_supported_uses_get_without_body(head_status: int) -> None:
    checker, calls, bodies = _checker(
        {("HEAD", ORIGINAL): (head_status, None), ("GET", ORIGINAL): (200, None)}
    )
    assert checker.verify_public_url(ORIGINAL).ok
    assert calls == [("HEAD", ORIGINAL), ("GET", ORIGINAL)]
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_bot_block_status_is_reachable_with_flag(status: int) -> None:
    checker, _, _ = _checker({("HEAD", ORIGINAL): (status, None)})
    result = checker.verify_public_url(ORIGINAL)
    assert result.ok and result.bot_blocked
    assert result.reason == "bot_blocked"


@pytest.mark.parametrize("status", [200, 204, 302, 304])
def test_2xx_or_3xx_without_location_is_reachable(status: int) -> None:
    checker, _, _ = _checker({("HEAD", ORIGINAL): (status, None)})
    assert checker.verify_public_url(ORIGINAL).ok


@pytest.mark.parametrize("status", [404, 410, 500, 503])
def test_missing_or_failing_sources_are_rejected(status: int) -> None:
    checker, _, _ = _checker({("HEAD", ORIGINAL): (status, None)})
    result = checker.verify_public_url(ORIGINAL)
    assert not result.ok
    assert result.reason == "http_status"


def test_each_redirect_hop_checks_dns_and_blocks_private_target() -> None:
    def resolve(host: str) -> list[str]:
        return ["10.0.0.1"] if host == "internal.test" else PUBLIC_IPS

    checker, calls, _ = _checker(
        {("HEAD", ORIGINAL): (302, "http://internal.test/secret")}, resolver=resolve
    )
    result = checker.verify_public_url(ORIGINAL)
    assert not result.ok
    assert result.reason == "non_public_address"
    assert calls == [("HEAD", ORIGINAL)]


def test_relative_redirects_follow_at_most_three_hops() -> None:
    initial = "https://example.com/0"
    routes = {("HEAD", f"https://example.com/{i}"): (302, f"/{i + 1}") for i in range(4)}
    checker, calls, _ = _checker(routes)
    result = checker.verify_public_url(initial)
    assert not result.ok
    assert result.reason == "redirect_limit"
    assert len(calls) == 4


def test_three_redirect_hops_can_reach_a_success() -> None:
    initial = "https://example.com/0"
    routes = {("HEAD", f"https://example.com/{i}"): (302, f"/{i + 1}") for i in range(3)}
    routes[("HEAD", "https://example.com/3")] = (200, None)
    checker, calls, _ = _checker(routes)
    assert checker.verify_public_url(initial).ok
    assert len(calls) == 4


def test_dns_failure_returns_ng_without_http() -> None:
    def fail_dns(_host: str) -> list[str]:
        raise socket.gaierror("フェイク DNS 失敗")

    checker, calls, _ = _checker({}, resolver=fail_dns)
    assert checker.verify_public_url(ORIGINAL).reason == "dns_failed"
    assert calls == []


def test_timeout_returns_ng_without_error_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("フェイクの応答本文は返さない", request=request)

    checker = SourceUrlChecker(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        resolver=lambda _host: PUBLIC_IPS,
    )
    assert checker.verify_public_url(ORIGINAL).reason == "timeout"


def test_results_and_shared_redirect_targets_are_cached() -> None:
    second = "https://example.com/another"
    checker, calls, _ = _checker(
        {
            ("HEAD", ORIGINAL): (302, "https://example.com/shared"),
            ("HEAD", second): (302, "https://example.com/shared"),
            ("HEAD", "https://example.com/shared"): (200, None),
        }
    )
    result = checker.verify_public_url(ORIGINAL)
    assert checker.verify_public_url(ORIGINAL) is result
    assert checker.verify_public_url(second).ok
    assert calls.count(("HEAD", "https://example.com/shared")) == 1


def test_duplicate_concurrent_verification_calls_http_once() -> None:
    checker, calls, _ = _checker({("HEAD", ORIGINAL): (200, None)})
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(checker.verify_public_url, [ORIGINAL] * 32))
    assert all(result.ok for result in results)
    assert calls == [("HEAD", ORIGINAL)]


def test_http_concurrency_is_limited_to_eight() -> None:
    reached_limit = Event()
    release = Event()
    count_lock = Lock()
    active = 0
    peak = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with count_lock:
            active += 1
            peak = max(peak, active)
            if active == MAX_SOURCE_URL_CONCURRENCY:
                reached_limit.set()
        assert release.wait(timeout=5)
        with count_lock:
            active -= 1
        return httpx.Response(200)

    checker = SourceUrlChecker(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        resolver=lambda _host: PUBLIC_IPS,
    )
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [
            pool.submit(checker.verify_public_url, f"https://example.com/{i}") for i in range(24)
        ]
        try:
            assert reached_limit.wait(timeout=5)
            assert peak == MAX_SOURCE_URL_CONCURRENCY
        finally:
            release.set()
        assert all(future.result().ok for future in futures)
    assert peak == MAX_SOURCE_URL_CONCURRENCY
