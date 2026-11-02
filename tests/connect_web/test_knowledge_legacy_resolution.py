"""旧 stem と公開 resolver の認可境界・検証。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web.app import _safe_next, _shell_page, create_app
from teamagent.legacy_knowledge import (
    legacy_document_stem,
    resolve_legacy_entries,
    safe_legacy_filename,
)
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config
from tests.scripts.test_export_vault import plan_vault, safe_filename


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "../a/b",
        'a\\b:*?"<>|[]#^',
        "..",
        "__",
        "Con",
        "LPT9",
        "a\x00b",
        "  a\t　b.. ",
        "カ\u3099",
        "長" * 100,
        "a" * 79 + " .",
    ],
)
@pytest.mark.parametrize("max_len", [55, 80])
def test_legacy_filename_matches_exporter(name: str | None, max_len: int) -> None:
    assert safe_legacy_filename(name, fallback="document", max_len=max_len) == safe_filename(
        name, fallback="document", max_len=max_len
    )


@pytest.mark.parametrize(
    "source,metadata",
    [
        ("gdrive", {}),
        ("gsheets", {}),
        ("slack", {"topic": "案件決定"}),
        ("gdrive", {"x_research_tool": "tool"}),
    ],
)
def test_legacy_stem_matches_actual_export_path(source: str, metadata: dict[str, Any]) -> None:
    doc = {
        "id": str(uuid4()),
        "title": "長い資料 / 名前" * 20,
        "source_type": source,
        "external_id": "source-123",
        "client_name": "取引先",
        **metadata,
    }
    files = plan_vault({"取引先": {"documents": [doc], "timeline": []}})
    expected = next(path[5:-3] for path in files if path.startswith("docs/"))
    assert legacy_document_stem({**doc, "metadata": metadata}) == expected


def test_visible_candidates_collision_numbering_identity_and_stable_ids() -> None:
    ids = [str(uuid4()) for _ in range(3)]
    records = [
        {"id": ids[0], "title": "共通 / 資料", "metadata": {"client_name": "株式会社 取引先"}},
        {"id": ids[1], "title": "共通 : 資料", "metadata": {"client_name": "取引先 様"}},
        {"id": ids[2], "title": "FB", "metadata": {"client_name": "FBのみ", "is_sales_fb": "true"}},
    ]
    stem = safe_legacy_filename(records[0]["title"])
    results = resolve_legacy_entries(
        records,
        [
            {"kind": "doc", "value": stem + "_10", "legacy": True},
            {"kind": "open", "value": "docs/" + stem, "legacy": True},
            {"kind": "client", "value": "取引先", "legacy": True},
            {"kind": "client", "value": "FBのみ", "legacy": False},
            {"kind": "doc", "value": ids[2], "legacy": False},
        ],
    )
    assert results[0]["status"] == results[1]["status"] == "ambiguous"
    assert {c["id"] for c in results[0]["candidates"]} == set(ids[:2])
    assert results[2]["status"] == results[3]["status"] == "resolved"
    assert results[4] == {"status": "missing", "candidates": []}
    # 付番の推定だけで一致したものは、可視候補が1件でも自動移動しない。
    inferred = resolve_legacy_entries(
        records[:1], [{"kind": "doc", "value": stem + "_2", "legacy": True}]
    )
    assert inferred[0]["status"] == "ambiguous" and len(inferred[0]["candidates"]) == 1


class ResolverPg:
    def __init__(self) -> None:
        self.connections: list[dict[str, Any]] = []
        self.fail = False

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[None]:
        self.connections.append(kwargs)
        yield None

    def resolve_knowledge_links(
        self, conn: Any, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("PRIVATE ERROR")
        return [{"status": "missing", "candidates": []} for entry in entries]


def test_resolver_http_validation_auth_rls_failure_and_bookmark_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = ResolverPg()
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config()))
    url = "/api/v1/knowledge/resolve"
    valid = {"kind": "doc", "value": "資料", "legacy": True}
    assert client.post(url, json=[valid]).status_code == 401
    assert pg.connections == []
    invalid = [
        [],
        [valid] * 101,
        [{**valid, "value": "a" * 501}],
        [{**valid, "kind": "other"}],
        [{**valid, "value": "../x"}],
        [{**valid, "value": "a\x7fb"}],
        [{**valid, "legacy": False}],
        [{**valid, "legacy": "true"}],
        [{**valid, "extra": True}],
        [{**valid, "value": ""}],
    ]
    for entries in invalid:
        response = client.post(url, json=entries, cookies=_auth_cookie())
        assert response.status_code == 400, entries
    assert pg.connections == []
    response = client.post(url, json=[valid], cookies=_auth_cookie())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"results": [{"status": "missing", "candidates": []}]}
    assert pg.connections == [
        {
            "app_role": "teamagent_app",
            "user_email": _EMAIL,
            "user_groups": ["vectorinc.co.jp"],
            "user_role": "user",
        }
    ]
    pg.fail = True
    failure = client.post(url, json=[valid], cookies=_auth_cookie())
    assert failure.status_code == 503 and "PRIVATE" not in failure.text
    target = "/search?view=bookmarks"
    response = client.get(target, follow_redirects=False)
    assert (
        response.status_code == 303
        and "next=%2Fsearch%3Fview%3Dbookmarks" in response.headers["location"]
    )
    assert _safe_next(target) == target
    for invalid_target in (
        target + "&page=1",
        target + "&x=1",
        "https://evil.test" + target,
        "//evil.test" + target,
        "/search-evil?view=bookmarks",
    ):
        assert _safe_next(invalid_target) == "/app"
    response = client.get(target, cookies=_auth_cookie())
    assert response.status_code == 200 and 'id="bookmarkItems"' in response.text
    assert response.text.count("data-knowledge-bookmarks") == 1
    evil = '"><script>alert(1)</script>'
    page = BeautifulSoup(_shell_page(evil, mode="list"), "html.parser")
    assert page.select_one("#knowledgeBookmarkRoot")["data-user"] == evil.lower()
    assert all(evil not in script.text for script in page.select("script"))
