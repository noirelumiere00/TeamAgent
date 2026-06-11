"""SSRF 中央バリデータ（スクレイプ/動画ツール用）。

OpenClaw 等 金庫外の頭脳がツール越しに渡す URL を、ネットワーク I/O に届く直前で
**1 か所**検証する。許可ドメインの厳密末尾一致＋内部/メタデータ IP 拒否＋http(s) 限定で、
SSRF（社内サービス・クラウドメタデータ 169.254.169.254・localhost・file:// 等への到達）を
構造的に塞ぐ。

3 層分離: Adapter 層。``video_download`` / ``tiktok_scraper`` / ``skills.video`` から使う。
スクレイプ系ツール（USE_VIDEO_TOOLS / USE_TIKTOK_TOOLS）でのみ実行され、P1 薄殻
（4 ナレッジツール）では import すらされない＝既定 OFF への影響は無い。

既知の残存リスク（P2 検討・記録のみ）: DNS rebinding（検証時の解決と yt-dlp/Node の実接続が
別タイミングのため TOCTOU 余地が残る）。allowlist＋内部IP拒否＋社内 company_shared＋読取専用で
実害を限定する判断。完全対処（pinned-connect）は費用対効果が見合わないため P2 へ。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

# 既定は保守的（スクレイプ対象の公開プラットフォームのみ）。env SCRAPE_ALLOWED_DOMAINS で上書き。
_DEFAULT_ALLOWED: frozenset[str] = frozenset(
    {"youtube.com", "youtu.be", "tiktok.com", "instagram.com", "instagr.am"}
)

_MAX_URL_LEN = 2048

# ホスト名 → 解決 IP 文字列リスト（テストで差し替え可＝単体テストを非ネットワーク化）。
IpResolver = Callable[[str], list[str]]


class UrlGuardError(ValueError):
    """SSRF allowlist 違反。呼び出し側がユーザー向け案内に変換する（生 URL はログに出さない）。"""


def allowed_domains_from_env() -> frozenset[str]:
    """``SCRAPE_ALLOWED_DOMAINS``（カンマ区切り）。未設定/空は保守的既定。

    env 規約は identity.py:91（会社ドメイン解決）と同形（strip().lower()）。先頭ドットは除去。
    """
    raw = os.environ.get("SCRAPE_ALLOWED_DOMAINS")
    if not raw:
        return _DEFAULT_ALLOWED
    doms = frozenset(d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip())
    return doms or _DEFAULT_ALLOWED


def _host_matches(host: str, allowed: frozenset[str]) -> bool:
    """末尾一致（部分文字列禁止）。host == dom もしくは host が *.dom のみ許可。

    ``attacker.com/?x=tiktok.com``（部分文字列）も ``eviltiktok.com``（接尾辞偽装）も弾く。
    """
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in allowed)


def _ip_is_blocked(ip_s: str) -> bool:
    """IP 文字列が内部/メタデータ/予約レンジなら True（ValueError は呼び出し側が処理）。"""
    ip = ipaddress.ip_address(ip_s)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16・fe80::/10（AWS IMDS を含む）
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _default_resolve(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, None) if ai[4]]


def _host_blocked_ip(host: str, resolve: IpResolver) -> bool:
    """host が内部/メタデータ/予約 IP に解決されるなら True。

    IP リテラル直書き（http://169.254.169.254/...）は解決不要で判定。ホスト名は resolve() で
    全アドレスを解決して検査。解決不能/空は fail-closed（True）。
    """
    try:
        return _ip_is_blocked(host)
    except ValueError:
        pass  # IP リテラルではない＝ホスト名なので解決する
    try:
        addrs = resolve(host)
    except OSError:
        return True  # 名前解決不能 → 拒否
    if not addrs:
        return True
    return any(_ip_is_blocked(a) for a in addrs)


def validate_scrape_url(
    url: str,
    *,
    request_id: str | None = None,
    resolve: IpResolver | None = None,
    check_dns: bool = True,
) -> str:
    """スクレイプ対象 URL を SSRF allowlist で検証し、通れば正規化 URL を返す。

    ``check_dns=True``（既定・**実ネットワーク I/O 直前の adapter backstop 用**）は DNS 解決先の
    内部IPまで検査。``check_dns=False``（skill 層の早期拒否用）はスキーム/host/ドメイン allowlist
    の安価な検査のみ＝非ネットワークで決定的（IPリテラル・非許可ドメインはここで弾かれ、許可ドメイン
    が内部IPに解決する稀ケースだけ adapter backstop に委ねる）。

    Raises:
        UrlGuardError: 空 / 長すぎ / 非http(s) / host無し / 非許可ドメイン / (check_dns時)内部IP。
    """
    if not url or not url.strip():
        raise UrlGuardError("URL_EMPTY: URL が空です")
    cleaned = url.strip()
    if len(cleaned) > _MAX_URL_LEN:
        raise UrlGuardError("URL_TOO_LONG: URL が長すぎます")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise UrlGuardError("URL_SCHEME_BLOCKED: http(s) のURLのみ許可されます")
    host = parsed.hostname  # urllib が user:pass@ / :port / [IPv6] を厳密分離
    if not host:
        raise UrlGuardError("URL_NO_HOST: ホスト名がありません")
    allowed = allowed_domains_from_env()
    if not _host_matches(host, allowed):
        logger.warning("url_guard_domain_blocked", request_id=request_id, host=host)
        raise UrlGuardError("URL_DOMAIN_BLOCKED: 許可されていないドメインです")
    if check_dns and _host_blocked_ip(host, resolve or _default_resolve):
        logger.warning("url_guard_ip_blocked", request_id=request_id, host=host)
        raise UrlGuardError("URL_IP_BLOCKED: 内部アドレスへのアクセスは禁止です")
    return cleaned


__all__ = ["UrlGuardError", "allowed_domains_from_env", "validate_scrape_url"]
