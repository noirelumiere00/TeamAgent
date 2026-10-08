"""調査の出典 URL を解決し、本文を読まずに公開サイトへの到達を確かめる。

HTTP クライアントと DNS resolver は注入できる。検査前に全解決先の ``is_global`` を
確認し、リダイレクトでも毎回同じ検査をする。モデルが書いた URL の抽出は扱わない。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from threading import BoundedSemaphore, Lock
from typing import TypeVar
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
MAX_SOURCE_URL_CONCURRENCY = 8
SOURCE_URL_TIMEOUT_S = 8.0
MAX_SOURCE_URL_REDIRECTS = 3

IpResolver = Callable[[str], Sequence[str]]
_T = TypeVar("_T")


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


class SourceUrlChecker:
    """同一 URL を重複検査せず、同時作業を最大 8 に制限する同期アダプタ。"""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        resolver: IpResolver | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=SOURCE_URL_TIMEOUT_S, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._resolver = resolver or _resolve_ips
        self._slots = BoundedSemaphore(MAX_SOURCE_URL_CONCURRENCY)
        self._cache_lock = Lock()
        self._resolved: dict[str, Future[str]] = {}
        self._verified: dict[str, Future[UrlCheckResult]] = {}
        self._http_results: dict[str, Future[tuple[int, str | None]]] = {}

    def close(self) -> None:
        """このアダプタが作った HTTP クライアントだけを閉じる。"""
        if self._owns_client:
            self._client.close()

    def _cached(self, cache: dict[str, Future[_T]], url: str, compute: Callable[[], _T]) -> _T:
        with self._cache_lock:
            pending = cache.get(url)
            owner = pending is None
            if pending is None:
                pending = Future()
                cache[url] = pending
        if owner:
            try:
                pending.set_result(compute())
            except Exception as exc:
                pending.set_exception(exc)
        return pending.result()

    def _check_public(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            # 不正な port も接続前に落とす。認証付き URL は出典として受け付けない。
            _ = parsed.port
        except ValueError as exc:
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
        try:
            addresses = self._resolver(host)
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

    def _request(self, method: str, url: str) -> tuple[int, str | None]:
        return self._cached(
            self._http_results, f"{method}\0{url}", lambda: self._request_uncached(method, url)
        )

    def _request_uncached(self, method: str, url: str) -> tuple[int, str | None]:
        # HEAD も GET も stream にし、応答本文は読む前に閉じる。
        with self._client.stream(
            method, url, timeout=SOURCE_URL_TIMEOUT_S, follow_redirects=False
        ) as response:
            return response.status_code, response.headers.get("location")

    def resolve_grounding_redirect(self, uri: str) -> str:
        """Vertex の転送 URL のみ HEAD で元 URL に解決する。失敗は例外。"""
        return self._cached(self._resolved, uri, lambda: self._resolve_uncached(uri))

    def _resolve_uncached(self, uri: str) -> str:
        try:
            host = urlsplit(uri).hostname
        except ValueError as exc:
            raise SourceUrlCheckError("invalid_url") from exc
        if host != GROUNDING_REDIRECT_HOST:
            return uri
        with self._slots:
            self._check_public(uri)
            try:
                status, location = self._request("HEAD", uri)
                if status == 405:
                    self._check_public(uri)
                    status, location = self._request("GET", uri)
            except httpx.TimeoutException as exc:
                raise SourceUrlCheckError("timeout") from exc
            except httpx.HTTPError as exc:
                raise SourceUrlCheckError("http_error") from exc
            except httpx.InvalidURL as exc:
                raise SourceUrlCheckError("invalid_url") from exc
        if 300 <= status < 400 and location:
            return urljoin(uri, location)
        raise SourceUrlCheckError("grounding_redirect_unresolved")

    def verify_public_url(self, url: str) -> UrlCheckResult:
        """URL の実在を判定する。拒否や接続失敗もキャッシュする。"""
        return self._cached(self._verified, url, lambda: self._verify_uncached(url))

    def _verify_uncached(self, url: str) -> UrlCheckResult:
        current = url
        status: int | None = None
        reason = "redirect_limit"
        with self._slots:
            try:
                for redirect_count in range(MAX_SOURCE_URL_REDIRECTS + 1):
                    self._check_public(current)
                    status, location = self._request("HEAD", current)
                    if status in (405, 501):
                        # GET が別の接続になるため DNS の全公開アドレス検査も再実施。
                        self._check_public(current)
                        status, location = self._request("GET", current)
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
