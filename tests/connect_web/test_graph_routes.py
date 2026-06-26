"""connect_web の資料グラフ（/search/graph・/api/v1/graph）ルートのテスト。

graph_docs_provider を注入して実 DB を排除し、認証ゲート / RLS email 受け渡し /
nodes+edges 生成 を検証する。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig

_SECRET = b"unit-test-search-secret-32-bytes!"
_EMAIL = "s-komata@vectorinc.co.jp"


def _config() -> DashboardConfig:
    return DashboardConfig(
        allowed_emails=frozenset({_EMAIL}),
        allowed_hd=None,
        google_client_id="cid-123",
        session_secret=_SECRET,
        dev_bypass=False,
        cookie_secure=False,
    )


_SAMPLE_DOCS: list[dict[str, Any]] = [
    {
        "node_id": 1,
        "title": "ニチレイ 提案書",
        "source_uri": "gdrive://1",
        "source_type": "gdrive",
        "cls_industry": "食品",
        "cls_project": "ニチレイ",
        "cls_doc_type": "提案書",
        "cls_solution": "動画広告",
        "cls_budget": "1000万",
        "cls_target": "ファミリー層",
        "client_name": "ニチレイ",
    },
    {
        "node_id": 2,
        "title": "ニチレイ 議事録",
        "source_uri": "gdrive://2",
        "source_type": "gdrive",
        "cls_industry": "食品",
        "cls_project": "ニチレイ",
        "cls_doc_type": "議事録",
        "client_name": "ニチレイ",
    },
    {
        "node_id": 3,
        "title": "化粧品 提案",
        "source_uri": "gdrive://3",
        "source_type": "gdrive",
        "cls_industry": "化粧品",
        "cls_project": None,
        "cls_doc_type": "提案書",
        "client_name": None,
    },
]


def _build(provider: Any = None) -> tuple[TestClient, list[str]]:
    seen: list[str] = []

    def _provider(email: str) -> list[dict[str, Any]]:
        seen.append(email)
        return _SAMPLE_DOCS

    app = create_app(
        search_config=_config(),
        graph_docs_provider=provider or _provider,
    )
    return TestClient(app), seen


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


def test_graph_ui_unauthenticated_redirects() -> None:
    client, _ = _build()
    r = client.get("/search/graph", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/search/login"


def test_graph_ui_authenticated_renders_canvas() -> None:
    client, _ = _build()
    r = client.get("/search/graph", cookies=_auth_cookie())
    assert r.status_code == 200
    assert "社内ナレッジグラフ" in r.text
    assert 'id="cv"' in r.text  # canvas


def test_api_graph_requires_auth() -> None:
    client, seen = _build()
    r = client.get("/api/v1/graph")
    assert r.status_code == 401
    assert seen == []  # provider は呼ばれない


def test_api_graph_returns_nodes_and_edges() -> None:
    client, seen = _build()
    r = client.get("/api/v1/graph", cookies=_auth_cookie())
    assert r.status_code == 200
    body = r.json()
    assert {n["id"] for n in body["nodes"]} == {1, 2, 3}
    # doc1-doc2 は project 共有でエッジ、doc3 は孤立
    # （edges には後方互換な strength フィールドが付与される）
    assert body["edges"] == [
        {"source": 1, "target": 2, "reason": "project:ニチレイ", "strength": "strong"}
    ]
    # RLS: provider に cookie 由来の本人 email が渡る
    assert seen == [_EMAIL]


def test_api_graph_projects_new_classification_axes() -> None:
    # L2 射影: provider が返す cls_solution/cls_budget/cls_target が node に乗る。
    client, _ = _build()
    r = client.get("/api/v1/graph", cookies=_auth_cookie())
    assert r.status_code == 200
    node1 = next(n for n in r.json()["nodes"] if n["id"] == 1)
    assert node1["solution"] == "動画広告"
    assert node1["budget"] == "1000万"
    assert node1["target"] == "ファミリー層"


def test_api_graph_handles_provider_error() -> None:
    def _boom(email: str) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    client, _ = _build(provider=_boom)
    r = client.get("/api/v1/graph", cookies=_auth_cookie())
    assert r.status_code == 500
    assert r.json()["error"] == "graph_failed"
