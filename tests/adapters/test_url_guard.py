"""§N: SSRF 中央バリデータ url_guard の単体テスト（非ネットワーク＝resolve 注入）。

これが SSRF 不変条件の第一保証: 部分文字列bypass・接尾辞偽装・IMDS/非global IP・非HTTPS・
userinfo偽装 を弾き、許可ドメイン（公開IP）は通す。
"""

from __future__ import annotations

import pytest

from teamagent.adapters.url_guard import (
    UrlGuardError,
    _host_matches,
    allowed_domains_from_env,
    validate_scrape_url,
)

# テスト用 resolver: 常に公開 IP を返す（実 DNS を叩かない）。
PUBLIC = ["93.184.216.34"]
PRIVATE = ["10.0.0.1"]


def _pub(_host: str) -> list[str]:
    return PUBLIC


def _priv(_host: str) -> list[str]:
    return PRIVATE


# --- ブロックされるべき SSRF ペイロード（理由は問わず UrlGuardError） ---
@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.com/?x=tiktok.com",  # 部分文字列bypass
        "https://eviltiktok.com/video/1",  # 接尾辞偽装
        "http://169.254.169.254/latest/meta-data/",  # AWS IMDS（IPリテラル）
        "http://127.0.0.1:8787/mcp",  # localhost
        "http://10.0.0.5/",  # private
        "file:///etc/passwd",  # 非http(s)スキーム
        "https://tiktok.com@attacker.com/",  # userinfo偽装 → host=attacker.com
        "https://user@tiktok.com/video/1",  # allowlisted hostでもuserinfoは禁止
        "https://tiktok.com:444/video/1",  # 非canonical port
        "https://evil.example/video/1",  # 非許可ドメイン
        "ftp://tiktok.com/x",  # 非http(s)
        "",  # 空
    ],
)
def test_blocks_ssrf_payloads(url: str) -> None:
    with pytest.raises(UrlGuardError):
        validate_scrape_url(url, resolve=_pub)


# --- 許可ドメイン（公開IP）は通す（過剰ブロックでない） ---
@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@user/video/1",
        "https://youtu.be/abcdef",
        "https://www.youtube.com/watch?v=abc",
        "https://www.instagram.com/reel/xyz/",
    ],
)
def test_allows_known_domains(url: str) -> None:
    assert validate_scrape_url(url, resolve=_pub) == url


def test_allowlisted_domain_resolving_to_internal_ip_is_blocked() -> None:
    # 許可ドメインでも解決先が内部IPなら拒否（DNS経由SSRF / rebinding一次防御）。
    with pytest.raises(UrlGuardError):
        validate_scrape_url("https://tiktok.com/x", resolve=_priv)


@pytest.mark.parametrize(
    "address",
    (
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "2001:db8::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ),
)
def test_allowlisted_domain_rejects_every_non_global_address(address: str) -> None:
    with pytest.raises(UrlGuardError):
        validate_scrape_url("https://tiktok.com/x", resolve=lambda _host: [address])


def test_host_matches_suffix_only() -> None:
    allowed = frozenset({"tiktok.com"})
    assert _host_matches("tiktok.com", allowed)
    assert _host_matches("www.tiktok.com", allowed)
    assert not _host_matches("eviltiktok.com", allowed)
    assert not _host_matches("tiktok.com.evil.com", allowed)


def test_env_override_allowed_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPE_ALLOWED_DOMAINS", "example.com, .foo.test")
    doms = allowed_domains_from_env()
    assert "example.com" in doms and "foo.test" in doms
    assert "tiktok.com" not in doms  # 既定を置き換える
    assert validate_scrape_url("https://example.com/v", resolve=_pub) == "https://example.com/v"
    with pytest.raises(UrlGuardError):
        validate_scrape_url("https://tiktok.com/x", resolve=_pub)


def test_default_allowed_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRAPE_ALLOWED_DOMAINS", raising=False)
    doms = allowed_domains_from_env()
    assert "tiktok.com" in doms and "youtube.com" in doms


def test_check_dns_false_skips_resolution_but_keeps_allowlist() -> None:
    # skill層の安価検証: DNS を引かず（resolver未注入でもネットワーク不要）許可ドメインは通る。
    assert (
        validate_scrape_url("https://www.tiktok.com/@u/video/1", check_dns=False)
        == "https://www.tiktok.com/@u/video/1"
    )
    # 非許可ドメイン・IPリテラルは DNS 無しでも弾く。
    with pytest.raises(UrlGuardError):
        validate_scrape_url("https://evil.example/x", check_dns=False)
    with pytest.raises(UrlGuardError):
        validate_scrape_url("http://169.254.169.254/", check_dns=False)
