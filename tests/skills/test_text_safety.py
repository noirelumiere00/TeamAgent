"""_shared/text_safety のユニットテスト（href allowlist / LLMテキスト無害化）。"""

from __future__ import annotations

from teamagent.skills._shared.text_safety import safe_href, sanitize_llm_text


def test_safe_href_allows_known_sns_hosts() -> None:
    assert safe_href("https://x.com/a/status/1") == "https://x.com/a/status/1"
    assert safe_href("https://www.instagram.com/reel/abc/") == "https://www.instagram.com/reel/abc/"
    assert safe_href("https://www.tiktok.com/@a/video/1") == "https://www.tiktok.com/@a/video/1"


def test_safe_href_blocks_dangerous_schemes_and_hosts() -> None:
    assert safe_href("javascript:alert(1)") is None
    assert safe_href("data:text/html,<script>") is None
    assert safe_href("http://x.com/a") is None  # http は不可（httpsのみ）
    assert safe_href("https://evil.example/x.com/a") is None  # ホスト詐称
    assert safe_href("https://user@x.com/a") is None  # userinfo付き
    assert safe_href("") is None


def test_safe_href_blocks_host_confusion_with_fragment_query_or_backslash() -> None:
    """ブラウザが開くホスト（# ? \\ の手前）と末尾一致のホストが食い違う形はリンクにしない。"""
    for sep in ("#", "?", "\\"):
        assert safe_href(f"https://evil.example{sep}.tiktok.com/@a/video/1") is None
    assert safe_href("https://www.tiktok.com:443/@a/video/1") is None  # ポート付き
    # 正しい URL のパス以降の ? や # はそのまま通す
    url = "https://www.tiktok.com/@a/video/1?is_from_webapp=1#top"
    assert safe_href(url) == url


def test_sanitize_llm_text_strips_urls_and_truncates() -> None:
    out = sanitize_llm_text("システム通知: https://evil.example/reauth で再認証してください")
    assert "https://evil.example" not in out
    assert "［URL省略］" in out
    long = "あ" * 900
    assert len(sanitize_llm_text(long, max_len=100)) <= 101  # 省略記号込み
    assert sanitize_llm_text("") == ""
