"""ポータルの認証・表示権限・共通ナビ（外部 I/O なし）。"""

from __future__ import annotations

import html
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.adapters import dm_history, personal_memory_admin
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web import app as connect_app
from teamagent.connect_web import portal
from teamagent.dashboard.auth import make_session
from tests.connect_web.test_library import Pg
from tests.connect_web.test_search_routes import (
    _EMAIL,
    _SECRET,
    _auth_cookie,
    _config,
    _verifier_ok,
)

_PUBLIC_SITES = {
    "動画事例を探す": "/library",
    "資料・ナレッジを探す": "/search",
    "取引先の状況を確認": "/search?view=clients&page=1",
    "自分の会話履歴": "/app/history",
}
_ADMIN_SITES = {
    "利用状況": "/admin",
    "本人メモ管理": "/admin/memory",
    "会話履歴管理": "/admin/history",
}
_PUBLIC_PATHS = set(_PUBLIC_SITES.values())
_ADMIN_PATHS = set(_ADMIN_SITES.values())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    monkeypatch.setattr(personal_memory_admin, "is_admin", lambda email: False)
    monkeypatch.setattr(dm_history, "is_admin", lambda email: False)
    return TestClient(
        connect_app.create_app(
            search_config=_config(), search_verifier=_verifier_ok, admin_pg=Pg()
        ),
        follow_redirects=False,
    )


def _paths(page: BeautifulSoup, selector: str) -> set[str]:
    return {str(link["href"]) for link in page.select(selector)}


def test_home_requires_login(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/search/login?next=%2F"


def test_home_public_sites_and_security_without_external_io(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Home は検索・Slack・LLM・DB・静的成果物の取得を呼ばない")

    monkeypatch.setattr(PgVectorClient, "from_env", unexpected)
    monkeypatch.setattr(dm_history.DmHistoryReader, "from_env", unexpected)
    monkeypatch.setattr(connect_app, "_resolve_app_html", unexpected)
    no_search = Mock(side_effect=unexpected)
    isolated = TestClient(
        connect_app.create_app(search_config=_config(), search_skill_factory=no_search)
    )
    response = isolated.get("/", cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.title is not None and page.title.text == "NewsTV Connect"
    assert _paths(page, ".connect-card") == _PUBLIC_PATHS
    assert _paths(page, ".connect-menu a") == _PUBLIC_PATHS
    assert len(page.select(".connect-card")) == 4
    assert page.select('a[href="/app"]') == []
    assert page.select_one('.connect-brand[href="/"][aria-current="page"]') is not None
    assert "準備中" not in response.text
    assert page.select("img, link, iframe, script") == []
    assert page.select_one(".connect-section-admin") is None
    assert [heading.find(string=True, recursive=False) for heading in page.select("h2")] == [
        "動画の事例から探す",
        "社内の知見をたどる",
        "自分の会話を振り返る",
    ]
    assert [label.text for label in page.select(".connect-section-heading h2 span")] == [
        "VIDEO ARCHIVE",
        "KNOWLEDGE",
        "MY AICO",
    ]
    assert page.select(".connect-card .connect-eyebrow") == []
    assert _paths(page, ".connect-section-archive .connect-card") == {"/library"}
    assert _paths(page, ".connect-section-knowledge .connect-card") == {
        "/search",
        "/search?view=clients&page=1",
    }
    search_card = page.select_one('.connect-card[href="/search"]')
    assert search_card.select_one("h3").text == "資料・ナレッジを探す"
    assert search_card.select_one("p").text == (
        "提案書・議事録を言葉で検索。取引先一覧・資料一覧・タグからもたどれます。"
    )
    assert _paths(page, ".connect-section-personal .connect-card") == {"/app/history"}
    assert page.select("script") == []
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["content-security-policy"] == connect_app._CONTENT_SECURITY_POLICY
    assert response.headers["x-frame-options"] == "DENY"
    no_search.assert_not_called()


@pytest.mark.parametrize("emails", [None, "", " \t ", _EMAIL, "other@example.test"])
def test_home_admin_card_follows_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, emails: str | None
) -> None:
    if emails is not None:
        monkeypatch.setenv("CONNECT_ADMIN_EMAILS", emails)
    response = client.get("/", cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    for selector in (".connect-card", ".connect-menu a"):
        assert ("/admin" in _paths(page, selector)) == (emails == _EMAIL)


@pytest.mark.parametrize("admin_path", sorted(_ADMIN_PATHS))
def test_each_admin_sees_only_their_links(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, admin_path: str
) -> None:
    monkeypatch.setattr(
        connect_app,
        "_admin_emails",
        lambda: frozenset({_EMAIL}) if admin_path == "/admin" else frozenset(),
    )
    monkeypatch.setattr(
        personal_memory_admin, "is_admin", lambda email: admin_path == "/admin/memory"
    )
    monkeypatch.setattr(dm_history, "is_admin", lambda email: admin_path == "/admin/history")
    response = client.get("/", cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert _paths(page, ".connect-card") == _PUBLIC_PATHS | {admin_path}
    assert _paths(page, ".connect-menu a") == _PUBLIC_PATHS | {admin_path}
    assert _paths(page, ".connect-section-admin .connect-card") == {admin_path}


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("selector", [".connect-card", ".connect-menu a"])
def test_site_names_link_to_expected_destinations(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, admin: bool, selector: str
) -> None:
    monkeypatch.setattr(
        connect_app, "_admin_emails", lambda: frozenset({_EMAIL}) if admin else frozenset()
    )
    monkeypatch.setattr(personal_memory_admin, "is_admin", lambda email: admin)
    monkeypatch.setattr(dm_history, "is_admin", lambda email: admin)
    page = BeautifulSoup(client.get("/", cookies=_auth_cookie()).text, "html.parser")
    links = page.select(selector)
    expected = _PUBLIC_SITES | (_ADMIN_SITES if admin else {})
    assert len(links) == len(expected)
    assert {
        (link.select_one("h3") or link).get_text(strip=True): link["href"] for link in links
    } == expected


def test_sites_is_the_single_source_for_cards_and_navigation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = portal.Site("extra", "追加 <サイト>", "/extra", "説明 <文>", "search")
    monkeypatch.setattr(portal, "SITES", (*portal.SITES, site))
    page = BeautifulSoup(client.get("/", cookies=_auth_cookie()).text, "html.parser")
    for selector in (".connect-card", ".connect-menu a"):
        assert _paths(page, selector) == _PUBLIC_PATHS | {site.path}
        link = page.select_one(f'{selector}[href="{site.path}"]')
        assert link is not None and site.name in link.text
        assert link.select("サイト") == []
    assert html.escape(site.description) in str(page)
    assert len(page.select(".connect-card")) == len(portal.visible_sites(_EMAIL))


def test_site_paths_are_escaped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    site = portal.Site("extra", "追加", '/extra?q=" onclick="bad()', "説明", "search")
    monkeypatch.setattr(portal, "SITES", (site,))
    page = BeautifulSoup(client.get("/", cookies=_auth_cookie()).text, "html.parser")
    for selector in (".connect-card", ".connect-menu a"):
        link = page.select_one(selector)
        assert link["href"] == site.path
        assert "onclick" not in link.attrs


def test_no_empty_shelves(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal, "SITES", ())
    page = BeautifulSoup(client.get("/", cookies=_auth_cookie()).text, "html.parser")
    assert page.select(".connect-section") == []
    assert page.select_one("h1") is not None


@pytest.mark.parametrize("path", ["/", "/search"])
def test_email_is_escaped(client: TestClient, path: str) -> None:
    email = '<script>alert("email")</script>@example.test'
    response = client.get(
        path, cookies={"ta_search_session": make_session(email, _SECRET, ttl_s=3600)}
    )
    assert response.status_code == 200
    assert email not in response.text
    assert html.escape(email) in response.text
    page = BeautifulSoup(response.text, "html.parser")
    label = page.select_one(".connect-email")
    assert label is not None and label.text == email
    assert label.select("script") == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/", "/"),
        ("/app", "/app"),
        ("/library", "/library"),
        ("/library/cases?category=tto", "/library/cases?category=tto"),
        ("/library/videos", "/library/videos"),
        ("/library/admin/campaigns", "/library/admin/campaigns"),
        ("/library-evil", "/"),
        ("/library/../admin", "/"),
        ("/library/%2f%2fevil.example", "/"),
        ("/library/%5cevil", "/"),
        ("//evil.example", "/"),
        ("/\\evil", "/"),
        ("/archive-evil", "/"),
        ("/?next=//evil.example", "/"),
        (None, "/"),
        ("", "/"),
        ("   ", "/"),
    ],
)
def test_safe_next(target: str | None, expected: str) -> None:
    assert connect_app._safe_next(target) == expected


@pytest.mark.parametrize("target", [None, "/", "/app"])
def test_login_return_target_and_default(client: TestClient, target: str | None) -> None:
    data = {"credential": "ok"}
    if target is not None:
        data["next"] = target
    response = client.post("/search/auth/verify", data=data)
    assert response.status_code == 303
    assert response.headers["location"] == (target or "/")
    if target == "/app":
        legacy = client.get(response.headers["location"])
        assert legacy.status_code == 303 and legacy.headers["location"] == "/search"
        assert client.get(legacy.headers["location"]).status_code == 200


@pytest.mark.parametrize("path", ["/search", "/search/graph"])
def test_shell_html_includes_common_header_and_height_css(client: TestClient, path: str) -> None:
    response = client.get(path, cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert len(page.select(".connect-header")) == 1
    assert page.select_one('.connect-brand[href="/"]') is not None
    assert page.select_one('.connect-menu a[href="/search"][aria-current="page"]') is not None
    # 高さ計算の CSS が HTML に組み込まれたことを確認する。実描画の高さは検証しない。
    assert "height:calc(100vh - var(--connect-header-height))" in response.text
    assert "height:calc(100vh - var(--connect-header-height) - 150px)" in response.text
    assert page.select_one("#shell #mainGraph canvas") is not None
    assert page.select_one("body.connect-page, .connect-home") is None
    assert portal.PORTAL_STYLE in response.text


def test_library_has_return_link_and_keeps_nonce_csp(client: TestClient) -> None:
    response = client.get("/library", cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one('header .main-nav a[href="/"]').text == "Connect ホーム"
    assert page.select_one('header .brand[href="/library"]') is not None
    assert page.select_one(".connect-header") is None
    nonce = page.select_one("style")["nonce"]
    assert f"'nonce-{nonce}'" in response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]


def test_portal_and_site_routes_resolve(client: TestClient) -> None:
    # 新しい FastAPI は include_router を入れ子で持つため、app.routes の平坦な数え上げに頼らない。
    for path in ("/", "/search", "/library", "/library/cases", "/library/videos"):
        assert client.get(path, cookies=_auth_cookie()).status_code == 200, path
    assert 'class="connect-home"' in client.get("/", cookies=_auth_cookie()).text


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("query", ["", "?x=y", "?next=https://evil.example&view=doc&id=secret"])
def test_legacy_app_redirect_is_unconditional_and_drops_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, authenticated: bool, query: str
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("旧入口は認証・静的成果物取得を呼ばない")

    monkeypatch.setattr(connect_app, "_resolve_app_html", unexpected)
    monkeypatch.setattr(connect_app, "verify_session", unexpected)
    response = client.get("/app" + query, cookies=_auth_cookie() if authenticated else {})
    assert response.status_code == 303
    assert response.headers["location"] == "/search"
    assert response.content == b""
    assert "set-cookie" not in response.headers
    assert "connect-header" not in response.text
