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

_PUBLIC_PATHS = {"/search", "/app", "/library", "/app/history"}
_ADMIN_PATHS = {"/admin", "/admin/memory", "/admin/history"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(connect_app, "_admin_emails", lambda: frozenset())
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
    assert page.select_one('.connect-brand[href="/"][aria-current="page"]') is not None
    assert "準備中" not in response.text
    assert page.select("script") == []
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["content-security-policy"] == connect_app._CONTENT_SECURITY_POLICY
    assert response.headers["x-frame-options"] == "DENY"
    no_search.assert_not_called()


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


@pytest.mark.parametrize("path", ["/", "/search", "/library"])
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
        ("//evil.example", "/app"),
        ("/\\evil", "/app"),
        ("/archive-evil", "/app"),
        ("/?next=//evil.example", "/app"),
        (None, "/app"),
    ],
)
def test_safe_next(target: str | None, expected: str) -> None:
    assert connect_app._safe_next(target) == expected


@pytest.mark.parametrize("target", ["/", "/app"])
def test_login_return_target_and_default(client: TestClient, target: str) -> None:
    data = {"credential": "ok"}
    if target == "/":
        data["next"] = target
    response = client.post("/search/auth/verify", data=data)
    assert response.status_code == 303
    assert response.headers["location"] == target


@pytest.mark.parametrize(
    ("path", "current"),
    [
        ("/search", "/search"),
        ("/search/graph", "/search"),
        ("/library", "/library"),
        ("/library/v/tiktok/123", "/library"),
        ("/library/admin/accounts", "/library"),
        ("/library/admin/campaigns", "/library"),
    ],
)
def test_common_header_on_shell_and_library(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, current: str
) -> None:
    if "/admin/" in path:
        monkeypatch.setattr(connect_app, "_admin_emails", lambda: frozenset({_EMAIL}))
        # ライブラリにも差し替えた判定関数を登録時に渡す。
        client = TestClient(connect_app.create_app(search_config=_config(), admin_pg=Pg()))
    response = client.get(path, cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert len(page.select(".connect-header")) == 1
    assert page.select_one('.connect-brand[href="/"]') is not None
    assert page.select_one(f'.connect-menu a[href="{current}"][aria-current="page"]') is not None
    if current == "/search":
        assert "height:calc(100vh - var(--connect-header-height))" in response.text
        assert "height:calc(100vh - var(--connect-header-height) - 150px)" in response.text
        assert page.select_one("#shell #mainGraph canvas") is not None
    else:
        csp = response.headers["content-security-policy"]
        assert "'unsafe-inline'" not in csp
        for element in page.select("style,script"):
            assert f"'nonce-{element['nonce']}'" in csp


def test_static_app_response_is_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = '<!doctype html><html lang="ja"><body>固定成果物</body></html>'
    monkeypatch.setattr(connect_app, "_resolve_app_html", lambda: {"html": artifact})
    response = client.get("/app", cookies=_auth_cookie())
    assert response.status_code == 200
    assert response.content == artifact.encode()
    assert "connect-header" not in response.text
