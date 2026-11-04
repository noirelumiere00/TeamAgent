"""Client attribute HTTP/query/XSS checks; no external I/O."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web.app import _KARTE_JS, _safe_next, create_app
from teamagent.karte_attributes import CLIENT_QUICK_FILTERS
from tests.connect_web.test_knowledge_list_routes import Pg
from tests.connect_web.test_search_routes import _auth_cookie, _config, _verifier_ok


class ClientPg(Pg):
    def __init__(self) -> None:
        super().__init__()
        self.filters: list[str | None] = []

    def list_knowledge_clients(
        self, conn: Any, *, limit: int, offset: int, quick: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        self.filters.append(quick)
        return super().list_knowledge_clients(conn, limit=limit, offset=offset)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, ClientPg]:
    pg = ClientPg()
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config(), search_verifier=_verifier_ok))
    return client, pg


@pytest.mark.parametrize("quick", CLIENT_QUICK_FILTERS)
def test_filter_url_login_and_paging(setup: tuple[TestClient, ClientPg], quick: str) -> None:
    client, pg = setup
    params = {"view": "clients", "quick": quick, "page": "2"}
    target = "/search?" + urlencode(params)
    canonical = _safe_next(target)
    assert parse_qs(urlsplit(canonical).query) == {k: [v] for k, v in params.items()}
    login = client.get(target, follow_redirects=False)
    assert login.status_code == 303
    assert parse_qs(urlsplit(login.headers["location"]).query) == {"next": [canonical]}
    result = client.post(
        "/search/auth/verify", data={"credential": "ok", "next": canonical}, follow_redirects=False
    )
    assert result.status_code == 303 and result.headers["location"] == canonical
    pg.rows, pg.total = (
        [
            {
                "name": "顧客",
                "document_count": 61,
                "fb_count": 62,
                "owners": ["担当A"],
                "temperature": "高",
                "last_contact": "2026-10-01",
                "has_homework": True,
            }
        ],
        51,
    )
    response = client.get(target, cookies=_auth_cookie())
    assert (
        response.status_code == 200 and pg.filters == [quick] and pg.calls == [("clients", 50, 50)]
    )
    assert pg.connections[0]["user_role"] == "user"
    page = BeautifulSoup(response.text, "html.parser")
    assert (
        page.select_one(".quick-filters a[aria-current=true]").text == CLIENT_QUICK_FILTERS[quick]
    )
    assert [td.get_text() for td in page.select("td")][1:] == [
        "61",
        "62",
        "—",
        "担当A",
        "高",
        "2026-10-01",
        "あり",
    ]
    link = page.select_one('nav[aria-label="ページ切替"] a')
    assert parse_qs(urlsplit(link["href"]).query) == {
        "view": ["clients"],
        "quick": [quick],
        "page": ["1"],
    }
    pg.rows = []
    page = BeautifulSoup(client.get(target, cookies=_auth_cookie()).text, "html.parser")
    link = next(a for a in page.select(".knowledge-list a") if a.text == "先頭ページへ")
    assert parse_qs(urlsplit(link["href"]).query)["quick"] == [quick]


@pytest.mark.parametrize("quick", ["unknown", "recent", "proposal", "<script>x</script>", ""])
def test_bad_client_filter_never_fetches(setup: tuple[TestClient, ClientPg], quick: str) -> None:
    client, pg = setup
    target = "/search?" + urlencode({"view": "clients", "quick": quick})
    assert _safe_next(target) == "/"
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 400 and "この取引先クイックフィルタは使えません" in response.text
    assert pg.connections == []


@pytest.mark.parametrize(
    "target",
    [
        "/search?view=clients&quick=homework&quick=high",
        "/search?view=docs&quick=homework",
        "//evil.example/search?view=clients&quick=homework",
        "https://evil.example/search?view=clients&quick=homework",
        "/search-evil?view=clients&quick=homework",
        "/search?view=clients&quick=homework%00",
    ],
)
def test_client_filter_safe_next_whitelist(target: str) -> None:
    assert _safe_next(target) == "/"


def test_attribute_values_are_escaped(setup: tuple[TestClient, ClientPg]) -> None:
    client, pg = setup
    evil = '<script>alert("x")</script>'
    pg.rows, pg.total = (
        [
            {
                "name": evil,
                "document_count": 1,
                "owners": [evil],
                "temperature": evil,
                "last_contact": evil,
                "has_homework": True,
            }
        ],
        1,
    )
    response = client.get("/search?view=clients", cookies=_auth_cookie())
    assert (
        response.status_code == 200
        and evil not in response.text
        and "&lt;script&gt;" in response.text
    )
    assert not BeautifulSoup(response.text, "html.parser").select(
        "script:not([data-knowledge-bookmarks])"
    )
    assert "innerHTML" not in _KARTE_JS  # summary values use textContent/createElement


def test_existing_header_keys_and_new_attributes_stay_separate() -> None:
    attributes = {
        "owners": ["担当A"],
        "temperature": "高",
        "last_contact": "2026-10-01",
        "next_action": "次の連絡",
        "next_action_short": "次の連絡",
        "has_homework": True,
        "fb_count": 70,
        "doc_count": 80,
    }
    client = TestClient(
        create_app(
            search_config=_config(),
            client_karte_provider=lambda email, name: {
                "timeline": [],
                "documents": [],
                "attributes": attributes,
            },
        )
    )
    response = client.get("/api/v1/client/顧客", cookies=_auth_cookie())
    assert response.status_code == 200
    data = response.json()
    assert data["header"]["fb_count"] == data["header"]["doc_count"] == 0
    assert data["attributes"] == attributes
    assert "renderHeader(data.header||{},data.attributes)" in _KARTE_JS
    assert "活動総数" in _KARTE_JS and "最大50件" in _KARTE_JS


def test_summary_renders_even_when_the_display_lists_are_empty() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is required for the karte rendering check")
    harness = r"""
const assert=require('node:assert/strict');
const nodes=new Map();
function element(){return {children:[],textContent:'',appendChild(n){this.children.push(n);}};}
global.document={getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},
  createElement:element};
global.window={__karteClient:'顧客'};
global.fetch=async()=>({status:200,ok:true,json:async()=>({header:{fb_count:0,doc_count:0},
  timeline:[],documents:[],attributes:{fb_count:60,doc_count:70,owners:['<script>'],
  has_homework:true,temperature:'高',last_contact:'2026-01-01',next_action_short:'連絡'}})});
__SCRIPT__
setImmediate(()=>{
 function text(n){return n.textContent+n.children.map(text).join('');}
 const content=text(nodes.get('khead'));
 assert.ok(content.includes('FB 60件・資料 70件'));
 assert.ok(content.includes('担当')&&content.includes('<script>'));
 assert.ok(content.includes('最大50件'));
 assert.ok(!content.includes('まだ記録がありません'));
});
""".replace("__SCRIPT__", _KARTE_JS)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
