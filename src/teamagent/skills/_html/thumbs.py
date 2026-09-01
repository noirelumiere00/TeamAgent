"""レポートに載せるサムネイル画像の取得と再ホスト。

## なぜ再ホストするか（直リンクにしない理由）

TikTok の ``cover_url`` は署名付き CDN URL で ``x-expires`` を持つ（実測: 発行から数日）。
そのまま ``<img src>`` に置くと、**レポートは生きているのに画像だけが数日で消える**。
納品物として渡すものが後から欠けるのは事故なので、取得した実体を自社の非公開 S3 へ置き、
レポート本体と同じ ``/r`` 短縮URLで参照する（画像の寿命＝レポートリンクの寿命で揃う）。

## なぜ data URI 埋め込みにしないか

実測でカバー画像は 1080x1920 / 約 186KB。10 本ぶんを base64 で埋めると HTML が約 2.5MB になる。
縮小できれば埋め込みが最善（自己完結・印刷可）だが、**Pillow は media extra 専用で mcp image に
入っていない**（``Dockerfile.teamagent-mcp`` は ``--extra mcp --extra embeddings`` のみ）。
core に画像処理系を足すのはサプライチェーン変更を伴うため、本モジュールでは再ホストを採る。
将来 mcp image に Pillow が入るなら、ここを「縮小して data URI」に差し替えれば良い。

## SSRF

``url_guard`` は「スクレイプ対象ページ」のホスト（tiktok.com 等）を許すもので、画像 CDN
（``*.tiktokcdn.com``）は含まれない。用途が違うので allowlist をここに分けて持つ。
https 限定・ホスト末尾一致・解決先が全て global IP・サイズ上限・Content-Type 検査を通す。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import structlog

logger = structlog.get_logger(__name__)

# 画像 CDN の allowlist（末尾一致）。ページ用の url_guard とは用途が違うため別に持つ。
_ALLOWED_IMAGE_HOSTS: tuple[str, ...] = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "ibyteimg.com",
)

_MAX_BYTES = 1_000_000  # 1 枚あたり。これを超える画像は載せない（レポートの肥大を防ぐ）
_TIMEOUT_S = 5
_MAX_IMAGES = 12  # 1 レポートあたり。上位リストの想定本数（最大 30）でも青天井にしない
_WORKERS = 4

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def thumbs_enabled() -> bool:
    """``USE_HTML_REPORT_THUMBS`` が真のときだけ取得する（既定 OFF）。"""
    return (os.environ.get("USE_HTML_REPORT_THUMBS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _host_allowed(host: str) -> bool:
    normalized = (host or "").rstrip(".").lower()
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}") for suffix in _ALLOWED_IMAGE_HOSTS
    )


def _resolves_global(host: str) -> bool:
    """全ての解決先が global IP か（メタデータ 169.254.169.254 / localhost 等への到達を塞ぐ）。"""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    for addr in addrs:
        try:
            if not ipaddress.ip_address(addr).is_global:
                return False
        except ValueError:
            return False
    return True


def fetch_image(url: str) -> tuple[bytes, str] | None:
    """画像を取得して ``(bytes, content_type)`` を返す。許可外・失敗・超過は ``None``。"""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if not _host_allowed(parsed.hostname) or not _resolves_global(parsed.hostname):
        logger.info("thumb_host_rejected", host=parsed.hostname)
        return None
    try:
        # nosec B310: スキーム(https固定)・ホスト(末尾一致allowlist)・解決先(全てglobal IP)を
        # 直前に検証済み。file:/ や custom scheme はここへ到達しない。
        req = Request(url, headers={"User-Agent": "teamagent-report/1.0"})  # nosec B310
        with urlopen(req, timeout=_TIMEOUT_S) as resp:  # nosec B310
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type not in _EXT_BY_TYPE:
                return None
            body = resp.read(_MAX_BYTES + 1)
    except Exception as e:
        logger.info("thumb_fetch_failed", error=type(e).__name__)
        return None
    if not body or len(body) > _MAX_BYTES:
        return None
    return body, content_type


def rehost(url: str, *, request_id: str) -> str | None:
    """CDN 画像を取得し、自社 S3 へ置いた配信URL（``/r`` か presigned）を返す。失敗は ``None``。"""
    fetched = fetch_image(url)
    if fetched is None:
        return None
    body, content_type = fetched

    from teamagent.adapters.report_publish import publish_bytes_result
    from teamagent.skills._shared.report_delivery import delivery_url

    result = publish_bytes_result(
        body,
        content_type=content_type,
        ext=_EXT_BY_TYPE[content_type],
        prefix="vseo-reports/thumbs/",
        request_id=request_id,
    )
    if result is None:
        return None
    return delivery_url(result, request_id=request_id)


def rehost_many(urls: list[str], *, request_id: str) -> dict[str, str]:
    """``{元URL: 配信URL}``。無効・失敗ぶんは含めない（呼び出し側は画像無しで描く）。"""
    if not thumbs_enabled():
        return {}
    targets = [u for u in urls if u][:_MAX_IMAGES]
    if not targets:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = pool.map(lambda u: rehost(u, request_id=request_id), targets)
        for src, dst in zip(targets, results, strict=True):
            if dst:
                out[src] = dst
    logger.info("thumbs_rehosted", request_id=request_id, ok=len(out), requested=len(targets))
    return out


__all__ = ["fetch_image", "rehost", "rehost_many", "thumbs_enabled"]
