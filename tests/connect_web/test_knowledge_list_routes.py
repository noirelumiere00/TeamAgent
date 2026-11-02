"""List HTTP/auth/query/rendering checks without external I/O."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web.app import _safe_next, create_app
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config, _verifier_ok


class Pg:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.total = 0
        self.error = False
        self.calls: list[tuple[str, int, int]] = []
        self.connections: list[dict[str, Any]] = []

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[object]:
        self.connections.append(kwargs)
        yield self

    def read(
        self, conn: Any, view: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        assert conn is self
        self.calls.append((view, limit, offset))
        if self.error:
            raise RuntimeError("PRIVATE database exception details")
        return self.rows, self.total

    def list_knowledge_clients(
        self, conn: Any, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        return self.read(conn, "clients", limit, offset)

    def list_knowledge_documents(
        self, conn: Any, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        return self.read(conn, "docs", limit, offset)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Pg]:
    pg = Pg()
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config(), search_verifier=_verifier_ok))
    return client, pg


@pytest.mark.parametrize("view", ["clients", "docs"])
def test_login_keeps_view_page_through_redirect_and_verify(
    setup: tuple[TestClient, Pg], view: str
) -> None:
    client, pg = setup
    target = f"/search?view={view}&page=2"
    response = client.get(target, follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/search/login"
    assert parse_qs(location.query) == {"next": [target]}
    assert pg.connections == pg.calls == []
    login = client.get(response.headers["location"])
    assert (
        BeautifulSoup(login.text, "html.parser").select_one("input[name=next]")["value"] == target
    )
    result = client.post(
        "/search/auth/verify", data={"credential": "ok", "next": target}, follow_redirects=False
    )
    assert result.status_code == 303
    assert result.headers["location"] == target


@pytest.mark.parametrize("view", ["clients", "docs"])
def test_visible_page_uses_rls_connection_and_range(
    setup: tuple[TestClient, Pg], view: str
) -> None:
    client, pg = setup
    pg.total = 51
    pg.rows = (
        [{"name": "顧客", "document_count": 3, "latest_fb_at": "2026-01-01"}]
        if view == "clients"
        else [
            {
                "id": "stable-uuid",
                "title": "資料タイトル",
                "modified_at": "2026-01-02",
                "doc_type": "提案書",
                "client_name": "顧客",
                "source_type": "gdrive",
                "source_uri": "gdrive://FILE_42",
            }
        ]
    )
    response = client.get(f"/search?view={view}&page=2", cookies=_auth_cookie())
    assert response.status_code == 200
    assert pg.calls == [(view, 50, 50)]
    assert pg.connections == [
        {
            "app_role": "teamagent_app",
            "user_email": _EMAIL,
            "user_groups": ["vectorinc.co.jp"],
            "user_role": "user",
        }
    ]
    page = BeautifulSoup(response.text, "html.parser")
    assert f"{'取引先' if view == 'clients' else '資料'} 51 件中 51–51 件" in page.text
    if view == "clients":
        assert "各最大50件" in page.text and "部分一致" in page.text
    assert page.select_one(f'.ws-tabs a[aria-current=page][href="/search?view={view}&page=1"]')
    assert page.select_one(f'nav[aria-label="ページ切替"] a[href="/search?view={view}&page=1"]')
    assert "次の50件" not in page.text
    # List views do not boot the graph/search fetch engines.
    assert page.select("script:not([data-knowledge-bookmarks])") == []
    if view == "clients":
        assert page.select_one('a[href="/search/client/%E9%A1%A7%E5%AE%A2"]')
    else:
        assert page.select_one('[data-doc-id="stable-uuid"]') is not None
        assert "stable-uuid" not in page.text  # 利用者には UUID を見せない
        assert "資料タイトル" in page.text and "2026-01-02" in page.text
        assert page.select_one('a[href="https://drive.google.com/file/d/FILE_42/view"]')


def test_next_page_and_total_are_separate(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.total = 51
    pg.rows = [{"name": f"顧客{i}", "document_count": 1} for i in range(50)]
    response = client.get("/search?view=clients", cookies=_auth_cookie())
    assert "取引先 51 件中 1–50 件" in response.text
    assert 'href="/search?view=clients&amp;page=2"' in response.text
    assert pg.calls == [("clients", 50, 0)]


@pytest.mark.parametrize("view", ["clients", "docs"])
def test_empty_out_of_range_and_failure_are_distinct(
    setup: tuple[TestClient, Pg], view: str
) -> None:
    client, pg = setup
    response = client.get(f"/search?view={view}&page=1", cookies=_auth_cookie())
    assert response.status_code == 200 and "0 件中 表示0件" in response.text
    assert "閲覧できる項目はありません" in response.text
    pg.total = 51
    response = client.get(f"/search?view={view}&page=3", cookies=_auth_cookie())
    assert response.status_code == 200 and "51 件中 表示0件" in response.text
    assert (
        "このページは範囲外" in response.text and "閲覧できる項目はありません" not in response.text
    )
    pg.error = True
    response = client.get(f"/search?view={view}", cookies=_auth_cookie())
    assert response.status_code == 503 and "一覧を取得できませんでした" in response.text
    assert "件中" not in response.text and "PRIVATE" not in response.text
    assert (
        "このページは範囲外" not in response.text
        and "閲覧できる項目はありません" not in response.text
    )


@pytest.mark.parametrize("view", ["clients", "docs"])
def test_html_escape_and_safe_source_links(setup: tuple[TestClient, Pg], view: str) -> None:
    client, pg = setup
    evil = '<script>alert("private")</script>'
    pg.total = 1
    pg.rows = (
        [{"name": evil, "document_count": evil, "latest_fb_at": evil}]
        if view == "clients"
        else [
            {
                "id": evil,
                "title": evil,
                "modified_at": evil,
                "doc_type": evil,
                "client_name": evil,
                "source_uri": 'javascript:alert("private")',
            }
        ]
    )
    response = client.get(f"/search?view={view}", cookies=_auth_cookie())
    assert response.status_code == 200 and evil not in response.text
    assert "&lt;script&gt;" in response.text
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select("script:not([data-knowledge-bookmarks])") == []
    assert all(not str(link["href"]).startswith("javascript:") for link in page.select("a[href]"))


@pytest.mark.parametrize(
    "uri",
    [
        'https://example.com/?q="<script>',
        "https://[broken",
        "https://example.com/\npath",
        "//evil.example",
    ],
)
def test_source_url_boundary(setup: tuple[TestClient, Pg], uri: str) -> None:
    client, pg = setup
    pg.rows, pg.total = [{"id": "id", "title": "資料", "source_uri": uri}], 1
    response = client.get("/search?view=docs", cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select("script:not([data-knowledge-bookmarks])") == []
    sources = page.select(".knowledge-list td:last-child a")
    assert len(sources) == (1 if uri.startswith("https://example.com/?") else 0)
    if sources:
        assert sources[0]["href"] == uri


_INVALID_QUERIES = [
    "view=evil",
    "view=docs&page=0",
    "view=docs&page=-1",
    "view=docs&page=10001",
    "view=docs&page=01",
    "view=docs&page=1.0",
    "view=docs&page=abc",
    "view=docs&page=",
    "view=docs&page=" + "9" * 300,
    "view=docs&view=clients",
    "view=docs&page=1&page=2",
    "view=docs&next=https://evil.example",
    "view=docs%00",
    "view=docs&page=1%0a",
    "view=",
    "page=2",
]


@pytest.mark.parametrize("query", _INVALID_QUERIES)
def test_invalid_query_defaults_to_existing_search_without_db(
    setup: tuple[TestClient, Pg], query: str
) -> None:
    client, pg = setup
    response = client.get("/search?" + query, cookies=_auth_cookie())
    assert response.status_code == 200
    assert 'id="mainList"' in response.text and 'id="mainGraph"' in response.text
    assert pg.calls == pg.connections == []
    assert _safe_next("/search?" + query) == "/"
    response = client.get("/search?" + query, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/search/login?next=%2Fsearch"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/search?view=docs",
        "//evil.example/search?view=docs",
        "/search-evil?view=docs",
        "/search/client/foo?view=docs",
        "/search/../search?view=docs",
        "/search?view=docs#fragment",
        "/search?view=docs&page=1\n",
        "/search?view=docs\\evil",
        "http://[bad",
        "/search%3fview=docs",
    ],
)
def test_safe_next_rejects_unlisted_targets(target: str) -> None:
    assert _safe_next(target) == "/"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/search?view=docs", "/search?view=docs&page=1"),
        ("/search?page=2&view=clients", "/search?view=clients&page=2"),
        ("/search?view=docs&page=10000", "/search?view=docs&page=10000"),
    ],
)
def test_safe_next_rebuilds_only_allowed_values(target: str, expected: str) -> None:
    assert _safe_next(target) == expected


def test_existing_search_and_graph_keep_engines_and_list_links(
    setup: tuple[TestClient, Pg],
) -> None:
    client, pg = setup
    for path in ("/search", "/search/graph"):
        response = client.get(path, cookies=_auth_cookie())
        assert response.status_code == 200
        page = BeautifulSoup(response.text, "html.parser")
        assert (
            page.select_one("#mainList") is not None and page.select_one("#mainGraph") is not None
        )
        assert page.select("script")
        for view in ("clients", "docs"):
            assert page.select_one(f'.ws-tabs a[href="/search?view={view}&page=1"]')
    assert pg.connections == pg.calls == []
