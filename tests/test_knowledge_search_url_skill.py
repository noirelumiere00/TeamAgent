"""knowledge_search_url Skill の単体テスト（外部 I/O 無し・env だけ monkeypatch）。

検証の主眼:
1. CONNECT_BASE_URL 設定時は available=True ＋ web_url/graph_url が正しく組み立たる。
2. 末尾スラッシュは正規化される（二重スラッシュにならない）。
3. CONNECT_BASE_URL 未設定/空のときは available=False ＋ URL は None（壊れたリンクを出さない）。
4. build_search_web_links 単体が同じ真実源として機能する（ゲート層と共有）。
"""

from __future__ import annotations

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.knowledge_search_url.schema import KnowledgeSearchUrlInput
from teamagent.skills.knowledge_search_url.skill import (
    KnowledgeSearchUrlSkill,
    build_app_client_link,
    build_app_url,
    build_search_web_links,
)


def _run() -> object:
    skill = KnowledgeSearchUrlSkill()
    return skill.run(KnowledgeSearchUrlInput(), SkillContext(user_id="a@b.co"))


def test_returns_urls_when_base_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp")
    out = _run()
    assert out.available is True  # type: ignore[attr-defined]
    assert out.web_url == "https://connect.example.co.jp/search"  # type: ignore[attr-defined]
    assert out.graph_url == "https://connect.example.co.jp/search/graph"  # type: ignore[attr-defined]
    # 案内文に両 URL と「Googleログイン」の文言が入る。
    assert "https://connect.example.co.jp/search" in out.message  # type: ignore[attr-defined]
    assert "Googleログイン" in out.message  # type: ignore[attr-defined]


def test_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp/")
    out = _run()
    assert out.web_url == "https://connect.example.co.jp/search"  # type: ignore[attr-defined]
    assert out.graph_url == "https://connect.example.co.jp/search/graph"  # type: ignore[attr-defined]


def test_unavailable_when_base_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    out = _run()
    assert out.available is False  # type: ignore[attr-defined]
    assert out.web_url is None  # type: ignore[attr-defined]
    assert out.graph_url is None  # type: ignore[attr-defined]
    # http:// で始まる壊れた/相対リンクを案内文に含めない。
    assert "http" not in out.message  # type: ignore[attr-defined]


def test_unavailable_when_base_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "   ")
    out = _run()
    assert out.available is False  # type: ignore[attr-defined]
    assert out.web_url is None  # type: ignore[attr-defined]


def test_build_search_web_links_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "https://x.test/")
    assert build_search_web_links() == {
        "web_url": "https://x.test/search",
        "graph_url": "https://x.test/search/graph",
    }
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    assert build_search_web_links() == {}
    # 明示 base 引数も末尾スラッシュ正規化される。
    assert build_search_web_links("https://y.test//") == {
        "web_url": "https://y.test/search",
        "graph_url": "https://y.test/search/graph",
    }


# --- Aico Vault（/app）ディープリンク builder（v0.3 Task6・ゲート層と共有する真実源） ---


def test_build_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "https://x.test/")
    assert build_app_url() == "https://x.test/app"
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    assert build_app_url() == ""  # 未設定は空＝壊れた相対リンクを出さない
    assert build_app_url("https://y.test//") == "https://y.test/app"


def test_build_app_client_link_encodes_japanese(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import quote, unquote

    monkeypatch.setenv("CONNECT_BASE_URL", "https://x.test")
    link = build_app_client_link("株式会社ベクトル")
    assert link == "https://x.test/app#client:" + quote("株式会社ベクトル", safe="")
    # フラグメント値は非 ASCII を含まない（Slack の自動リンク化が途中で切れない）。
    assert link.isascii()
    # decode すると元の名前に戻る（app.html 側 decodeURIComponent と対）。
    assert unquote(link.split("#client:", 1)[1]) == "株式会社ベクトル"


def test_build_app_client_link_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    # base 未設定 → 空（名前があっても出さない）。
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    assert build_app_client_link("ベクトル") == ""
    # 名前が空/空白のみ → 空（/app#client: という無意味リンクを出さない）。
    monkeypatch.setenv("CONNECT_BASE_URL", "https://x.test")
    assert build_app_client_link("") == ""
    assert build_app_client_link("   ") == ""


def test_build_app_client_link_encodes_url_specials(monkeypatch: pytest.MonkeyPatch) -> None:
    # 名前に URL 特殊文字（スペース・# ・/）が混ざっても fragment が壊れない。
    monkeypatch.setenv("CONNECT_BASE_URL", "https://x.test")
    link = build_app_client_link("A&B社 #2/営業")
    frag = link.split("#client:", 1)[1]
    assert "#" not in frag and "/" not in frag and " " not in frag
