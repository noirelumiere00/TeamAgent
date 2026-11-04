"""Document details and full-scope tag HTTP checks; no external I/O."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web.app import _safe_next, create_app
from tests.connect_web.test_knowledge_list_routes import Pg
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config, _verifier_ok

DOC_ID = "12345678-1234-1234-abcd-123456789abc"


class DocsTagsPg(Pg):
    def __init__(self) -> None:
        super().__init__()
        self.document: dict[str, Any] | None = None
        self.tags: list[dict[str, Any]] = []
        self.doc_calls: list[str] = []
        self.tag_calls = 0
        self.filters: list[tuple[str, str] | None] = []

    def get_knowledge_document(self, conn: Any, document_id: str) -> dict[str, Any] | None:
        assert conn is self
        self.doc_calls.append(document_id)
        if self.error:
            raise RuntimeError("PRIVATE")
        return self.document

    def list_knowledge_tags(self, conn: Any) -> list[dict[str, Any]]:
        assert conn is self
        self.tag_calls += 1
        if self.error:
            raise RuntimeError("PRIVATE")
        return self.tags

    def list_knowledge_documents(
        self, conn: Any, *, limit: int, offset: int, tag: tuple[str, str] | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        self.filters.append(tag)
        return super().list_knowledge_documents(conn, limit=limit, offset=offset)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, DocsTagsPg]:
    pg = DocsTagsPg()
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config(), search_verifier=_verifier_ok))
    return client, pg


def test_detail_properties_tags_excerpt_and_list_title_link(
    setup: tuple[TestClient, DocsTagsPg],
) -> None:
    client, pg = setup
    pg.document = {
        "id": DOC_ID,
        "title": "資料タイトル",
        "modified_at": "2026-11-02",
        "doc_type": "提案書",
        "industry": "食品",
        "client_name": "顧客 / A",
        "source_type": "gdrive",
        "source_uri": "gdrive://FILE_42",
        "excerpt": "代表本文\n改行",
    }
    response = client.get(f"/search?view=doc&id={DOC_ID}", cookies=_auth_cookie())
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.h1.text == "資料タイトル" and page.pre.text == "代表本文\n改行"
    assert "2,000文字" in page.text and "2026-11-02" in page.text
    assert page.select_one('a[href="/search/client/%E9%A1%A7%E5%AE%A2%20%2F%20A"]')
    assert page.select_one('a[href="https://drive.google.com/file/d/FILE_42/view"]')
    tag_queries = [parse_qs(urlsplit(a["href"]).query) for a in page.select("li a")]
    assert {q["tag"][0] for q in tag_queries} == {
        "industry:食品",
        "doc_type:提案書",
        "client_name:顧客 / A",
        "source_type:gdrive",
    }
    assert pg.doc_calls == [DOC_ID]
    assert pg.connections == [
        {
            "app_role": "teamagent_app",
            "user_email": _EMAIL,
            "user_groups": ["vectorinc.co.jp"],
            "user_role": "user",
        }
    ]
    assert page.select_one(".ws-tabs a[aria-current=page]").text == "資料一覧"
    assert page.select_one('.ws-tabs a[href="/search?view=tags"]')
    pg.rows, pg.total = [pg.document], 1
    page = BeautifulSoup(
        client.get("/search?view=docs", cookies=_auth_cookie()).text, "html.parser"
    )
    link = page.select_one(f'a[data-doc-id="{DOC_ID}"]')
    assert link.text == "資料タイトル" and link["href"] == f"/search?view=doc&id={DOC_ID}"
    assert DOC_ID not in page.text


def test_missing_and_failure_are_distinct(setup: tuple[TestClient, DocsTagsPg]) -> None:
    client, pg = setup
    responses = [
        client.get(f"/search?view=doc&id={doc_id}", cookies=_auth_cookie())
        for doc_id in (DOC_ID, str(UUID(int=1)))
    ]
    assert all(r.status_code == 404 for r in responses)
    assert responses[0].text == responses[1].text
    assert "見つからないか、閲覧できません" in responses[0].text
    pg.error = True
    response = client.get(f"/search?view=doc&id={DOC_ID}", cookies=_auth_cookie())
    assert response.status_code == 503 and "資料を取得できませんでした" in response.text
    assert "見つからないか" not in BeautifulSoup(response.text, "html.parser").get_text()
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize(
    "query",
    [
        "view=doc",
        "view=doc&id=",
        "view=doc&id=invalid",
        f"view=doc&id={DOC_ID.replace('-', '')}",
        f"view=doc&id={{{DOC_ID}}}",
        f"view=doc&id=urn:uuid:{DOC_ID}",
        f"view=doc&id={DOC_ID}&id={DOC_ID}",
        f"view=doc&id={DOC_ID}&page=1",
        f"view=doc&id={DOC_ID}%00",
        f"view=doc&id={DOC_ID}%0a",
    ],
)
def test_invalid_id_falls_back_to_document_list(
    setup: tuple[TestClient, DocsTagsPg], query: str
) -> None:
    client, pg = setup
    response = client.get("/search?" + query, cookies=_auth_cookie())
    assert response.status_code == 200 and "<h1>資料一覧</h1>" in response.text
    assert pg.doc_calls == [] and pg.calls == [("docs", 50, 0)]
    assert _safe_next("/search?" + query) == "/"


@pytest.mark.parametrize(
    "query",
    [
        f"view=doc&id={DOC_ID.upper()}",
        "view=tags",
        urlencode({"view": "docs", "tag": "industry:食品:飲料 / A&B", "page": 2}),
    ],
)
def test_login_preserves_new_targets(setup: tuple[TestClient, DocsTagsPg], query: str) -> None:
    client, pg = setup
    target = _safe_next("/search?" + query)
    assert target != "/"
    response = client.get("/search?" + query, follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query) == {"next": [target]}
    assert pg.connections == []
    login = client.get(response.headers["location"])
    assert (
        BeautifulSoup(login.text, "html.parser").select_one("input[name=next]")["value"] == target
    )
    response = client.post(
        "/search/auth/verify", data={"credential": "ok", "next": target}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == target


def test_tag_groups_counts_links_and_filtered_paging(setup: tuple[TestClient, DocsTagsPg]) -> None:
    client, pg = setup
    pg.tags = [
        {"axis": "industry", "value": "食品", "document_count": 51},
        {"axis": "doc_type", "value": "食品", "document_count": 3},
        {"axis": "client_name", "value": "顧客", "document_count": 2},
        {"axis": "source_type", "value": "gdrive", "document_count": 54},
    ]
    response = client.get("/search?view=tags", cookies=_auth_cookie())
    assert response.status_code == 200 and pg.tag_calls == 1
    page = BeautifulSoup(response.text, "html.parser")
    assert [h.text for h in page.select("h2")] == ["業界", "資料タイプ", "取引先", "出典"]
    assert "食品：51 件" in page.text and "食品：3 件" in page.text
    queries = [parse_qs(urlsplit(a["href"]).query) for a in page.select("li a")]
    assert {q["tag"][0] for q in queries} == {
        "industry:食品",
        "doc_type:食品",
        "client_name:顧客",
        "source_type:gdrive",
    }
    assert all(q["view"] == ["docs"] and q["page"] == ["1"] for q in queries)
    assert pg.connections[0]["user_role"] == "user"
    pg.total, pg.rows = 101, [{"id": DOC_ID, "title": "資料"} for _ in range(50)]
    target = "/search?" + urlencode({"view": "docs", "tag": "industry:食品", "page": 2})
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 200 and pg.filters == [("industry", "食品")]
    assert pg.calls == [("docs", 50, 50)] and "資料 101 件中 51–100 件" in response.text
    assert "絞り込み：業界：食品" in response.text
    page = BeautifulSoup(response.text, "html.parser")
    links = page.select('nav[aria-label="ページ切替"] a')
    assert {parse_qs(urlsplit(a["href"]).query)["page"][0] for a in links} == {"1", "3"}
    assert all(parse_qs(urlsplit(a["href"]).query)["tag"] == ["industry:食品"] for a in links)
    pg.rows = []
    page = BeautifulSoup(client.get(target, cookies=_auth_cookie()).text, "html.parser")
    first = next(a for a in page.select(".knowledge-list a") if a.text == "先頭ページへ")
    assert parse_qs(urlsplit(first["href"]).query)["tag"] == ["industry:食品"]
    assert "このページは範囲外" in page.text


def test_empty_tags_and_failure(setup: tuple[TestClient, DocsTagsPg]) -> None:
    client, pg = setup
    response = client.get("/search?view=tags", cookies=_auth_cookie())
    assert response.status_code == 200 and "閲覧できるタグはありません" in response.text
    pg.error = True
    response = client.get("/search?view=tags", cookies=_auth_cookie())
    assert response.status_code == 503 and "タグを取得できませんでした" in response.text
    assert "閲覧できるタグはありません" not in response.text and "PRIVATE" not in response.text


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "industry",
        ":食品",
        "unknown:食品",
        "cls_industry:食品",
        "industry:",
        "industry:  ",
        "industry:a\x00",
        "industry:a\n",
        "industry:a\\b",
        "industry:" + "a" * 501,
    ],
)
def test_invalid_tag_never_reaches_db(setup: tuple[TestClient, DocsTagsPg], tag: str) -> None:
    client, pg = setup
    target = "/search?" + urlencode({"view": "docs", "tag": tag})
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 200 and 'id="mainList"' in response.text
    assert _safe_next(target) == "/" and pg.connections == []


@pytest.mark.parametrize(
    "query",
    [
        "view=tags&page=1",
        "view=tags&tag=industry:食品",
        "view=clients&tag=industry:食品",
        "view=docs&tag=industry:a&tag=industry:b",
        f"view=doc&id={DOC_ID}&next=https://evil.example",
    ],
)
def test_query_key_whitelist(setup: tuple[TestClient, DocsTagsPg], query: str) -> None:
    client, pg = setup
    assert _safe_next("/search?" + query) == "/"
    response = client.get("/search?" + query, cookies=_auth_cookie())
    assert response.status_code == 200 and pg.doc_calls == [] and pg.tag_calls == 0


@pytest.mark.parametrize(
    "target",
    [
        f"https://evil.example/search?view=doc&id={DOC_ID}",
        f"//evil.example/search?view=doc&id={DOC_ID}",
        f"/search-evil?view=doc&id={DOC_ID}",
        "/search/tags?view=tags",
        "/search?view=tags#fragment",
        "/search?view=tags\n",
        "/search?view=docs&tag=industry:a%0d",
        "/search?view=docs&tag=industry:a%5c",
    ],
)
def test_safe_next_rejects_external_prefix_and_unsafe_targets(target: str) -> None:
    assert _safe_next(target) == "/"


def test_dynamic_detail_and_tag_text_are_escaped(setup: tuple[TestClient, DocsTagsPg]) -> None:
    client, pg = setup
    evil = '<script>alert("x")</script>'
    pg.document = {
        "id": DOC_ID,
        **{
            k: evil
            for k in ("title", "doc_type", "industry", "client_name", "modified_at", "excerpt")
        },
        "source_uri": "javascript:alert(1)",
    }
    pg.tags = [{"axis": "industry", "value": evil, "document_count": evil}]
    for target in (
        f"/search?view=doc&id={DOC_ID}",
        "/search?view=tags",
        "/search?" + urlencode({"view": "docs", "tag": "industry:" + evil}),
    ):
        response = client.get(target, cookies=_auth_cookie())
        assert response.status_code == 200 and evil not in response.text
        assert "&lt;script&gt;" in response.text
        page = BeautifulSoup(response.text, "html.parser")
        assert page.select("script:not([data-knowledge-bookmarks])") == []
        assert all(not a["href"].startswith("javascript:") for a in page.select("a[href]"))


def test_tags_tab_on_existing_shells(setup: tuple[TestClient, DocsTagsPg]) -> None:
    client, _ = setup
    for target in ("/search", "/search/graph", "/search?view=clients", "/search?view=docs"):
        page = BeautifulSoup(client.get(target, cookies=_auth_cookie()).text, "html.parser")
        assert page.select_one('.ws-tabs a[href="/search?view=tags"]')
