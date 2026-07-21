"""media worker の外向き通信に適用する SSRF/DNS guard。"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request

from teamagent.media.url_policy import ACQUIRE_HOST_SUFFIXES, acquire_host_allowed

ALLOWED_ACQUIRE_HOST_SUFFIXES = ACQUIRE_HOST_SUFFIXES
ALLOWED_YTDLP_EXTRACTORS = (
    "youtube",
    "youtube:tab",
    "tiktok",
    "instagram",
)

Resolver = Callable[..., list[tuple[Any, ...]]]


class MediaSsrfError(ValueError):
    """URL/DNS が public media fetch 契約を満たさない。"""


class PublicHttpsRedirectHandler(HTTPRedirectHandler):
    """Revalidate every redirect before urllib follows it."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_public_address(raw: str) -> bool:
    address = ipaddress.ip_address(raw)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _allowed_host(host: str) -> bool:
    return acquire_host_allowed(host)


def validate_public_https_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> str:
    """canonical HTTPS + 全DNS結果publicを要求する。"""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host:
        raise MediaSsrfError("MEDIA_URL_HTTPS_REQUIRED")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise MediaSsrfError("MEDIA_URL_AUTHORITY_BLOCKED")
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaSsrfError("MEDIA_URL_DNS_FAILED") from exc
    addresses = {str(record[4][0]).split("%", 1)[0] for record in records if record[4]}
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise MediaSsrfError("MEDIA_URL_PRIVATE_ADDRESS_BLOCKED")
    return url


def validate_acquire_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> str:
    """HTTPS + 3サービス allowlist + 全DNS結果publicを要求する。"""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not _allowed_host(host):
        raise MediaSsrfError("MEDIA_URL_DOMAIN_BLOCKED")
    return validate_public_https_url(url, resolver=resolver)


@contextlib.contextmanager
def public_dns_only() -> Iterator[None]:
    """process内の全Python DNS解決をpublic address限定にする。

    media worker は1 process 1 jobのため、このprocess-global patchを安全に使える。
    yt-dlpにはnative HLSを強制し、外部ffmpegによる未検証network fetchは行わせない。
    """

    original = socket.getaddrinfo

    def guarded_getaddrinfo(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        records = original(*args, **kwargs)
        addresses = {str(record[4][0]).split("%", 1)[0] for record in records if record[4]}
        if not addresses or any(not _is_public_address(address) for address in addresses):
            raise MediaSsrfError("MEDIA_DNS_PRIVATE_ADDRESS_BLOCKED")
        return records

    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original


__all__ = [
    "ALLOWED_ACQUIRE_HOST_SUFFIXES",
    "ALLOWED_YTDLP_EXTRACTORS",
    "MediaSsrfError",
    "PublicHttpsRedirectHandler",
    "public_dns_only",
    "validate_acquire_url",
    "validate_public_https_url",
]
