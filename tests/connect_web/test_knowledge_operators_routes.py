"""Condition search and quick filter HTTP behavior without external services."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web.app import _safe_next, create_app
from tests.connect_web.test_knowledge_list_routes import Pg
from tests.connect_web.test_search_routes import _auth_cookie, _config, _verifier_ok


class SearchPg(Pg):
    def __init__(self) -> None:
        super().__init__()
        self.filters: list[dict[str, Any]] = []

    def list_knowledge_documents(
        self, conn: Any, *, limit: int, offset: int, **filters: Any
    ) -> tuple[list[dict[str, Any]], int]:
        self.filters.append(filters)
        return super().list_knowledge_documents(conn, limit=limit, offset=offset)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, SearchPg]:
    pg = SearchPg()
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config(), search_verifier=_verifier_ok))
    return client, pg


def test_combined_filters_ranges_controls_and_links(setup: tuple[TestClient, SearchPg]) -> None:
    client, pg = setup
    q = "tag:食品 file:提案 -下書き"
    params = {"view": "docs", "q": q, "tag": "industry:食品", "quick": "proposal", "page": "2"}
    pg.rows, pg.total = [{"id": "stable", "title": "提案資料"} for _ in range(50)], 101
    response = client.get("/search?" + urlencode(params), cookies=_auth_cookie())
    assert response.status_code == 200
    assert pg.filters == [{"q": q, "tag": ("industry", "食品"), "quick": "proposal"}]
    assert pg.calls == [("docs", 50, 50)]
    assert pg.connections[0]["user_role"] == "user"
    assert "資料 101 件中 51–100 件" in response.text
    page = BeautifulSoup(response.text, "html.parser")
    form = page.select_one('form[action="/search"]')
    assert form["method"] == "get"
    values = {node["name"]: node.get("value", "") for node in form.select("input")}
    assert values == {**params, "page": "1"}
    assert page.select_one("#docsQuery")["maxlength"] == "300"
    assert page.select_one('label[for="docsQuery"]').text == "条件で絞る"
    assert "AI で探す（検索タブ）" in page.text
    assert page.select_one(".quick-filters a[aria-current=true]").text == "提案書"
    links = page.select('nav[aria-label="ページ切替"] a')
    assert {parse_qs(urlsplit(a["href"]).query)["page"][0] for a in links} == {"1", "3"}
    for link in links:
        values = parse_qs(urlsplit(link["href"]).query)
        assert (
            values["q"] == [q]
            and values["tag"] == ["industry:食品"]
            and values["quick"] == ["proposal"]
        )
    for link in page.select(".quick-filters a"):
        values = parse_qs(urlsplit(link["href"]).query)
        assert values["q"] == [q] and values["tag"] == ["industry:食品"] and values["page"] == ["1"]
    all_link = next(a for a in page.select(".quick-filters a") if a.text == "すべて")
    assert "quick" not in parse_qs(urlsplit(all_link["href"]).query)
    pg.rows = []
    page = BeautifulSoup(
        client.get("/search?" + urlencode(params), cookies=_auth_cookie()).text, "html.parser"
    )
    first = next(a for a in page.select(".knowledge-list a") if a.text == "先頭ページへ")
    assert parse_qs(urlsplit(first["href"]).query) == {
        **{k: [v] for k, v in params.items()},
        "page": ["1"],
    }


@pytest.mark.parametrize("quick", ["recent", "proposal", "minutes"])
def test_login_and_safe_next_preserve_query_and_quick(
    setup: tuple[TestClient, SearchPg], quick: str
) -> None:
    client, pg = setup
    params = {"view": "docs", "q": r'"引用 句" 50% A_B C:\資料', "quick": quick, "page": "2"}
    target = "/search?" + urlencode(params)
    assert parse_qs(urlsplit(_safe_next(target)).query) == {k: [v] for k, v in params.items()}
    response = client.get(target, follow_redirects=False)
    assert response.status_code == 303 and pg.connections == []
    nxt = parse_qs(urlsplit(response.headers["location"]).query)["next"][0]
    login = client.get(response.headers["location"])
    assert BeautifulSoup(login.text, "html.parser").select_one("input[name=next]")["value"] == nxt
    response = client.post(
        "/search/auth/verify", data={"credential": "ok", "next": nxt}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == nxt


@pytest.mark.parametrize(
    ("q", "message"),
    [
        ("x" * 301, "300文字"),
        ("x" * 101, "1語100文字"),
        (" ".join(["x"] * 11), "10語"),
        ("[属性:値]", "この書き方はまだ使えません"),
        ("[属性:null]", "この書き方はまだ使えません"),
        ("/abc.*/", "この書き方はまだ使えません"),
        ("path:docs", "この書き方はまだ使えません"),
        ("-path:clients", "この書き方はまだ使えません"),
        ("bad\x00", "制御文字"),
    ],
)
def test_bad_or_unsupported_query_is_visible_and_never_fetches(
    setup: tuple[TestClient, SearchPg], q: str, message: str
) -> None:
    client, pg = setup
    target = "/search?" + urlencode({"view": "docs", "q": q})
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 400 and message in response.text
    assert "一覧を取得できませんでした" not in response.text
    assert "件中" not in response.text and pg.connections == []
    assert _safe_next(target) == "/app"
    assert BeautifulSoup(response.text, "html.parser").select_one("#docsQuery") is not None


def test_unknown_quick_and_query_key_whitelist(setup: tuple[TestClient, SearchPg]) -> None:
    client, pg = setup
    response = client.get("/search?view=docs&quick=admin", cookies=_auth_cookie())
    assert response.status_code == 400 and "このクイックフィルタは使えません" in response.text
    assert pg.connections == []
    for target in (
        "/search?view=clients&q=食品",
        "/search?view=docs&q=a&q=b",
        "/search?view=docs&quick=recent&quick=minutes",
        "/search?view=tags&q=食品",
    ):
        assert _safe_next(target) == "/app"


@pytest.mark.parametrize(
    "prefix",
    ["https://evil.example/search", "//evil.example/search", "/search-evil", "/search/../search"],
)
def test_next_rejects_external_and_prefix_paths(prefix: str) -> None:
    assert (
        _safe_next(prefix + "?" + urlencode({"view": "docs", "q": "食品", "quick": "recent"}))
        == "/app"
    )


def test_query_is_escaped_and_db_failure_is_separate(setup: tuple[TestClient, SearchPg]) -> None:
    client, pg = setup
    q = '<img src=x onerror=alert(1)> "quoted"'
    target = "/search?" + urlencode({"view": "docs", "q": q, "quick": "recent"})
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 200 and q not in response.text
    page = BeautifulSoup(response.text, "html.parser")
    assert (
        page.select("img, script:not([data-knowledge-bookmarks])") == []
        and page.select_one("#docsQuery")["value"] == q
    )
    assert "0 件中 表示0件" in page.text
    pg.error = True
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 503 and "一覧を取得できませんでした" in response.text
    assert "件中" not in response.text and "PRIVATE" not in response.text
