"""調査の出典 URL を解決し、本文を読まずに公開サイトへの到達を確かめる。

HTTP クライアントと DNS resolver は注入できる。既定 transport は HTTP と同じ IDNA
ホストを解決し、全解決先の ``is_global`` を確認した IP にだけ接続する。各転送先も
同じ検査をする。DNS・待機・ヘッダ受信を含む全体期限を設け、本文は読まない。
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock, Thread
from typing import TypeVar
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from httpcore._backends.sync import SyncStream
from pydantic import BaseModel, ConfigDict

GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
MAX_SOURCE_URL_CONCURRENCY = 8
SOURCE_URL_TIMEOUT_S = 8.0
SOURCE_URL_TOTAL_TIMEOUT_S = 16.0
MAX_SOURCE_URL_REDIRECTS = 3

IpResolver = Callable[[str], Sequence[str]]
_T = TypeVar("_T")
# OS の getaddrinfo は取消できない。期限超過後の残存 worker 数も全ジョブを通して有界。
_DNS_SLOTS = BoundedSemaphore(MAX_SOURCE_URL_CONCURRENCY)
_TRANSIENT_REASONS = {"timeout", "http_error", "dns_failed", "deadline_exceeded"}


class UrlCheckResult(BaseModel):
    """元の検査対象と到達先、到達可否。401/403/429 は bot_blocked を残す。"""

    model_config = ConfigDict(frozen=True)

    url: str
    final_url: str
    ok: bool
    bot_blocked: bool = False
    status_code: int | None = None
    reason: str = ""


class SourceUrlCheckError(ValueError):
    """出典 URL が安全に解決できなかった。本文や HTTP 応答内容を含めない。"""


def _resolve_ips(host: str) -> list[str]:
    return [
        str(item[4][0])
        for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if item[4]
    ]


@dataclass(frozen=True)
class _ConnectionTarget:
    host: str
    addresses: tuple[str, ...]
    deadline: float
    clock: Callable[[], float]

    def remaining(self, timeout: float | None = None) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise SourceUrlCheckError("deadline_exceeded")
        return min(remaining, timeout) if timeout is not None else remaining


_CONNECTION_TARGET: ContextVar[_ConnectionTarget | None] = ContextVar(
    "source_url_connection_target", default=None
)


class _DeadlineStream(SyncStream):
    """read ごとに絶対期限を適用し、ヘッダの小出しでも期限を延ばさない。"""

    def __init__(self, sock: socket.socket, target: _ConnectionTarget) -> None:
        super().__init__(sock)
        self._target = target

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return super().read(max_bytes, self._target.remaining(timeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        # SyncStream.write は部分送信ごとに同じ timeout を更新するためここで期限を適用。
        try:
            while buffer:
                self._sock.settimeout(self._target.remaining(timeout))
                sent = self._sock.send(buffer)
                if sent == 0:
                    raise httpcore.WriteError("connection_closed")
                buffer = buffer[sent:]
        except TimeoutError as exc:
            raise httpcore.WriteTimeout("timeout") from exc
        except OSError as exc:
            raise httpcore.WriteError("http_error") from exc

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        sock: socket.socket | None = None
        try:
            self._sock.settimeout(self._target.remaining(timeout))
            # SNI と証明書検査には接続 IP でなく元の IDNA ホストを使う。
            sock = ssl_context.wrap_socket(self._sock, server_hostname=server_hostname)
            self._target.remaining()
            return _DeadlineStream(sock, self._target)
        except Exception as exc:
            # wrap_socket が FD を移した後は元の socket.close だけでは閉じられない。
            if sock is not None:
                if sock is not None:
                    sock.close()
            self.close()
            if isinstance(exc, TimeoutError):
                raise httpcore.ConnectTimeout("timeout") from exc
            if isinstance(exc, OSError):
                raise httpcore.ConnectError("http_error") from exc
            raise


class _PinnedBackend(httpcore.NetworkBackend):
    """検査済み数値 IP へ直接 connect し、接続時にホスト名を再解決しない。"""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        target = _CONNECTION_TARGET.get()
        if target is None or target.host != host or local_address is not None:
            raise SourceUrlCheckError("invalid_connection_target")
        last_error: OSError | None = None
        for address in target.addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise SourceUrlCheckError("non_public_address")
            sock = None
            try:
                sock = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET)
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(target.remaining(timeout))
                sock.connect((str(ip), port))
                target.remaining()
                return _DeadlineStream(sock, target)
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
            except Exception:
                if sock is not None:
                    sock.close()
                raise
        if isinstance(last_error, socket.timeout):
            raise httpcore.ConnectTimeout("timeout") from last_error
        raise httpcore.ConnectError("http_error") from last_error


class _SourceUrlTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        # HTTPTransport の型付き変換・例外対応を利用し、backend だけを差し替える。
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=MAX_SOURCE_URL_CONCURRENCY,
            max_keepalive_connections=0,
            network_backend=_PinnedBackend(),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = request.extensions.get("source_url_target")
        if not isinstance(target, _ConnectionTarget):
            raise SourceUrlCheckError("invalid_connection_target")
        token = _CONNECTION_TARGET.set(target)
        try:
            return super().handle_request(request)
        finally:
            _CONNECTION_TARGET.reset(token)


class SourceUrlChecker:
    """同一 URL を重複検査せず、同時作業を最大 8 に制限する同期アダプタ。"""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        resolver: IpResolver | None = None,
        total_timeout_s: float = SOURCE_URL_TOTAL_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if total_timeout_s <= 0:
            raise ValueError("total_timeout_s must be positive")
        self._client = client or httpx.Client(
            transport=_SourceUrlTransport(),
            timeout=SOURCE_URL_TIMEOUT_S,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._resolver = resolver or _resolve_ips
        self._clock = clock
        self._total_timeout_s = total_timeout_s
        self._slots = BoundedSemaphore(MAX_SOURCE_URL_CONCURRENCY)
        self._cache_lock = Lock()
        self._resolved: dict[str, Future[str]] = {}
        self._verified: dict[str, Future[UrlCheckResult]] = {}
        self._http_results: dict[str, Future[tuple[int, str | None]]] = {}

    def close(self) -> None:
        """このアダプタが作った HTTP クライアントだけを閉じる。"""
        if self._owns_client:
            self._client.close()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SourceUrlCheckError("deadline_exceeded")
        return remaining

    def _cached(
        self,
        cache: dict[str, Future[_T]],
        url: str,
        compute: Callable[[], _T],
        deadline: float,
        keep: Callable[[_T], bool] = lambda _result: True,
    ) -> _T:
        with self._cache_lock:
            pending = cache.get(url)
            owner = pending is None
            if pending is None:
                pending = Future()
                cache[url] = pending
        if owner:
            retain = True
            try:
                result = compute()
                retain = keep(result)
                pending.set_result(result)
            except Exception as exc:
                retain = isinstance(exc, SourceUrlCheckError) and str(exc) not in _TRANSIENT_REASONS
                pending.set_exception(exc)
            finally:
                if not retain:
                    with self._cache_lock:
                        if cache.get(url) is pending:
                            del cache[url]
        # 既に完了した値は期限内の作業を再実行しない。進行中の共有待機だけを有界にする。
        if pending.done():
            return pending.result()
        try:
            return pending.result(timeout=self._remaining(deadline))
        except FutureTimeoutError as exc:
            raise SourceUrlCheckError("deadline_exceeded") from exc

    @contextmanager
    def _slot(self, deadline: float) -> Iterator[None]:
        if not self._slots.acquire(timeout=self._remaining(deadline)):
            raise SourceUrlCheckError("deadline_exceeded")
        try:
            yield
        finally:
            self._slots.release()

    def _resolve_bounded(self, host: str, deadline: float) -> Sequence[str]:
        if not _DNS_SLOTS.acquire(timeout=self._remaining(deadline)):
            raise SourceUrlCheckError("deadline_exceeded")
        pending: Future[Sequence[str]] = Future()

        def resolve() -> None:
            try:
                pending.set_result(self._resolver(host))
            except Exception as exc:
                pending.set_exception(exc)
            finally:
                _DNS_SLOTS.release()

        try:
            Thread(target=resolve, name="source-url-dns", daemon=True).start()
        except BaseException:
            _DNS_SLOTS.release()
            raise
        try:
            return pending.result(timeout=self._remaining(deadline))
        except FutureTimeoutError as exc:
            raise SourceUrlCheckError("deadline_exceeded") from exc

    def _check_public(self, url: str, deadline: float) -> _ConnectionTarget:
        try:
            parsed = urlsplit(url)
            host = httpx.URL(url).raw_host.decode("ascii")
            # 不正な port も接続前に落とす。認証付き URL は出典として受け付けない。
            _ = parsed.port
        except (ValueError, httpx.InvalidURL) as exc:
            raise SourceUrlCheckError("invalid_url") from exc
        if parsed.scheme not in ("http", "https") or not host:
            raise SourceUrlCheckError("invalid_url")
        if parsed.username is not None or parsed.password is not None:
            raise SourceUrlCheckError("credentials_not_allowed")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise SourceUrlCheckError("non_public_address")
        if literal is not None:
            addresses: Sequence[str] = [str(literal)]
        else:
            try:
                addresses = self._resolve_bounded(host, deadline)
            except OSError as exc:
                raise SourceUrlCheckError("dns_failed") from exc
        if not addresses:
            raise SourceUrlCheckError("dns_failed")
        try:
            if not all(ipaddress.ip_address(address).is_global for address in addresses):
                raise SourceUrlCheckError("non_public_address")
        except ValueError as exc:
            if isinstance(exc, SourceUrlCheckError):
                raise
            raise SourceUrlCheckError("dns_invalid_address") from exc
        return _ConnectionTarget(host, tuple(addresses), deadline, self._clock)

    def _request(self, method: str, url: str, target: _ConnectionTarget) -> tuple[int, str | None]:
        return self._cached(
            self._http_results,
            f"{method}\0{url}",
            lambda: self._request_uncached(method, url, target),
            target.deadline,
            keep=lambda result: result[0] < 500 and result[0] != 429,
        )

    def _request_uncached(
        self, method: str, url: str, target: _ConnectionTarget
    ) -> tuple[int, str | None]:
        # HEAD も GET も stream にし、応答本文は読む前に閉じる。
        with self._client.stream(
            method,
            url,
            timeout=target.remaining(SOURCE_URL_TIMEOUT_S),
            follow_redirects=False,
            extensions={"source_url_target": target},
        ) as response:
            target.remaining()
            return response.status_code, response.headers.get("location")

    def resolve_grounding_redirect(self, uri: str) -> str:
        """Vertex の転送 URL のみ HEAD で元 URL に解決する。失敗は例外。"""
        deadline = self._clock() + self._total_timeout_s
        return self._cached(
            self._resolved, uri, lambda: self._resolve_uncached(uri, deadline), deadline
        )

    def _resolve_uncached(self, uri: str, deadline: float) -> str:
        try:
            host = urlsplit(uri).hostname
        except ValueError as exc:
            raise SourceUrlCheckError("invalid_url") from exc
        if host != GROUNDING_REDIRECT_HOST:
            return uri
        with self._slot(deadline):
            target = self._check_public(uri, deadline)
            try:
                status, location = self._request("HEAD", uri, target)
                if status == 405:
                    target = self._check_public(uri, deadline)
                    status, location = self._request("GET", uri, target)
            except httpx.TimeoutException as exc:
                raise SourceUrlCheckError("timeout") from exc
            except httpx.HTTPError as exc:
                raise SourceUrlCheckError("http_error") from exc
            except httpx.InvalidURL as exc:
                raise SourceUrlCheckError("invalid_url") from exc
        if status >= 500 or status == 429:
            raise SourceUrlCheckError("http_error")
        if 300 <= status < 400 and location:
            return urljoin(uri, location)
        raise SourceUrlCheckError("grounding_redirect_unresolved")

    def verify_public_url(self, url: str) -> UrlCheckResult:
        """URL の到達を判定する。恒久的な結果だけを保持し、一時失敗は再検査可能。"""
        deadline = self._clock() + self._total_timeout_s
        try:
            return self._cached(
                self._verified,
                url,
                lambda: self._verify_uncached(url, deadline),
                deadline,
                keep=lambda result: (
                    result.reason not in _TRANSIENT_REASONS
                    and (result.status_code or 0) < 500
                    and result.status_code != 429
                ),
            )
        except SourceUrlCheckError as exc:
            return UrlCheckResult(url=url, final_url=url, ok=False, reason=str(exc))

    def _verify_uncached(self, url: str, deadline: float) -> UrlCheckResult:
        current = url
        status: int | None = None
        reason = "redirect_limit"
        with self._slot(deadline):
            try:
                for redirect_count in range(MAX_SOURCE_URL_REDIRECTS + 1):
                    target = self._check_public(current, deadline)
                    status, location = self._request("HEAD", current, target)
                    if status in (405, 501):
                        target = self._check_public(current, deadline)
                        status, location = self._request("GET", current, target)
                    if 300 <= status < 400 and location:
                        if redirect_count == MAX_SOURCE_URL_REDIRECTS:
                            return UrlCheckResult(
                                url=url,
                                final_url=current,
                                ok=False,
                                status_code=status,
                                reason="redirect_limit",
                            )
                        current = urljoin(current, location)
                        continue
                    bot_blocked = status in (401, 403, 429)
                    ok = 200 <= status < 400 or bot_blocked
                    return UrlCheckResult(
                        url=url,
                        final_url=current,
                        ok=ok,
                        bot_blocked=bot_blocked,
                        status_code=status,
                        reason="bot_blocked" if bot_blocked else ("" if ok else "http_status"),
                    )
            except SourceUrlCheckError as exc:
                reason = str(exc)
            except httpx.TimeoutException:
                reason = "timeout"
            except httpx.HTTPError:
                reason = "http_error"
            except httpx.InvalidURL:
                reason = "invalid_url"
        return UrlCheckResult(
            url=url, final_url=current, ok=False, status_code=status, reason=reason
        )
