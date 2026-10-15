"""種グラフのゲート・RLS接続・決定的な隣・旧画面互換を固定する。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.connect_web.app import _RedactAdminUserAccessLog, _safe_next, _shell_page, create_app
from teamagent.connect_web.graph import build_seed_graph, parse_document_ids
from tests.connect_web.test_graph_routes import _EMAIL, _auth_cookie, _build, _config


def uid(i: int) -> str:
    return f"00000000-0000-0000-0000-{i:012d}"


def doc(i: int, **kw: Any) -> dict[str, Any]:
    return dict(document_id=uid(i), title=f"資料{i}", source_uri="https://example.test/doc", **kw)


@pytest.mark.parametrize("flag", [None, "0", "1"])
@pytest.mark.parametrize("query", ["", "?seed=" + uid(1)])
def test_existing_graph_route_preserves_shell_and_login(
    monkeypatch: Any, flag: str | None, query: str
) -> None:
    if flag is None:
        monkeypatch.delenv("GRAPH_SEARCH_VIEW", raising=False)
    else:
        monkeypatch.setenv("GRAPH_SEARCH_VIEW", flag)
    client, seen = _build()
    response = client.get("/search/graph" + query, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/search/login"
    response = client.get("/search/graph" + query, cookies=_auth_cookie())
    assert response.status_code == 200
    assert response.content == _shell_page(_EMAIL, mode="graph").encode()
    assert seen == []


@pytest.mark.parametrize("flag", ["", "0", "true"])
def test_new_routes_off_and_existing_byte_identical(monkeypatch: Any, flag: str) -> None:
    client, seen = _build()
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", flag)
    baseline = [client.get(p, cookies=_auth_cookie()).content for p in ("/search", "/api/v1/graph")]
    seen.clear()
    for path in ("/search/graph/focus", "/api/v1/graph/seed", "/api/v1/graph/neighbors"):
        assert client.get(path, cookies=_auth_cookie()).status_code == 404
        assert client.get(path, follow_redirects=False).status_code == 404
    assert seen == []
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", "1")
    assert baseline == [
        client.get(p, cookies=_auth_cookie()).content for p in ("/search", "/api/v1/graph")
    ]


def test_login_and_safe_next(monkeypatch: Any) -> None:
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", "1")
    client, seen = _build()
    for path in ("/api/v1/graph/seed?ids=" + uid(1), "/api/v1/graph/neighbors?id=" + uid(1)):
        assert client.get(path).status_code == 401
    assert client.get("/search/graph/focus", follow_redirects=False).status_code == 303
    response = client.get("/search/graph/focus?seed=" + uid(1), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/search/login?next=" + quote(
        "/search/graph/focus?seed=" + uid(1), safe=""
    )
    target = unquote(response.headers["location"].split("next=")[1])
    assert _safe_next(target) == "/search/graph/focus?seed=" + uid(1)
    assert _safe_next("/search/graph/focus?seed=" + uid(1) + "&evil=x") == "/app"
    assert _safe_next("//evil.test/search/graph") == "/app"
    assert _safe_next("/search/graph/focus") == "/search/graph/focus"
    for path in ("/search/graph", "/search/graph?seed=" + uid(1)):
        assert _safe_next(path) == "/app"
    assert seen == []


@pytest.mark.parametrize("raw", ["", "x", "<script>", uid(1) + ",", ",".join([uid(1)] * 13)])
def test_invalid_ids(monkeypatch: Any, raw: str) -> None:
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", "1")
    client, seen = _build()
    for path in ("/api/v1/graph/seed?ids=", "/search/graph/focus?seed="):
        assert client.get(path + raw, cookies=_auth_cookie()).status_code == 400
    assert seen == []
    with pytest.raises(ValueError):
        parse_document_ids(raw)


def test_rls_connection_and_hidden_seed(monkeypatch: Any) -> None:
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", "1")
    pg = MagicMock()
    token = object()
    seen = []

    @contextmanager
    def connection(**kwargs: Any) -> Any:
        seen.append(kwargs)
        yield token

    pg.connection.side_effect = connection
    pg.list_documents_for_seed_graph.return_value = [doc(1, cls_project="取引先")]
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config()))
    r = client.get("/api/v1/graph/seed?ids=" + uid(999), cookies=_auth_cookie())
    assert r.json() == {"nodes": [], "edges": []}
    assert seen == [
        dict(
            app_role="teamagent_app",
            user_email=_EMAIL,
            user_groups=[_EMAIL.split("@")[1]],
            user_role="user",
        )
    ]
    pg.list_documents_for_seed_graph.assert_called_once_with(token, [uid(999)])


def test_neighbors_order_limit_dedup_suppressed() -> None:
    seed = doc(1, cls_project="A", advertiser="広告主", campaign="施策", cls_industry="食品")
    docs = [seed, doc(2, cls_industry="食品"), doc(3, advertiser="広告主", campaign="施策")]
    docs += [doc(i, cls_project="A") for i in range(4, 14)]
    docs += [doc(14, cls_project="A", suppressed="true"), doc(15, advertiser="別", campaign="施策")]
    graph = build_seed_graph([*docs, seed], [uid(1), uid(1)])
    assert graph == build_seed_graph(list(reversed(docs)), [uid(1)])
    assert len(graph["nodes"]) == 13
    assert {n["id"] for n in graph["nodes"]} == {uid(i) for i in range(1, 14)}
    assert len(graph["edges"]) == 12
    assert next(n for n in graph["nodes"] if n["id"] == uid(1))["seed"] is True
    # 取引先12件なら施策・タグは上限外。
    crowded = [seed] + [doc(i, cls_project="A") for i in range(4, 20)] + docs[1:3]
    assert {n["id"] for n in build_seed_graph(crowded, [uid(1)])["nodes"]} == {uid(1)} | {
        uid(i) for i in range(4, 16)
    }
    assert build_seed_graph([doc(1, suppressed="true")], [uid(1)]) == {"nodes": [], "edges": []}


def test_seed_and_neighbors_api_limit_and_xss(monkeypatch: Any) -> None:
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", "1")
    docs = [doc(i, cls_project="A", title_unused="x") for i in range(1, 30)]
    docs[0]["title"] = "<script>alert(1)</script>"
    client, seen = _build(lambda email: docs)
    body = client.get(
        "/api/v1/graph/seed?ids=" + uid(1) + "," + uid(1), cookies=_auth_cookie()
    ).json()
    assert len(body["nodes"]) == 13
    assert len({n["id"] for n in body["nodes"]}) == 13
    assert (
        client.get(
            "/api/v1/graph/neighbors?id=" + uid(1) + "," + uid(2), cookies=_auth_cookie()
        ).status_code
        == 400
    )
    page = client.get("/search/graph/focus?seed=" + uid(1), cookies=_auth_cookie()).text
    assert "社内ナレッジグラフ" in page
    assert "<script>alert(1)</script>" not in page
    assert "t.textContent=n.title" in page
    assert "innerHTML" not in page
    assert "encodeURIComponent(client)" in page
    assert seen == []


def test_seed_sql_rls_and_limits() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    PgVectorClient(dsn="postgresql://stub").list_documents_for_seed_graph(conn, [uid(1)])
    sql, params = cur.execute.call_args.args
    assert params == [[uid(1)]]
    assert "id = ANY(%s::uuid[])" in sql
    assert sql.count("suppressed' IS DISTINCT FROM 'true'") == 2
    assert "ORDER BY priority, d.id LIMIT 12" in sql
    assert "ORDER BY d.id LIMIT 200" in sql
    assert "CROSS JOIN LATERAL" in sql


def test_graph_access_queries_redacted() -> None:
    for path in (
        "/search/graph/focus?seed=",
        "/api/v1/graph/seed?ids=",
        "/api/v1/graph/neighbors?id=",
    ):
        record = logging.LogRecord(
            "uvicorn.access", 20, "", 0, "%s %s %s", ("client", "GET", path + uid(1)), None
        )
        assert _RedactAdminUserAccessLog().filter(record)
        assert uid(1) not in str(record.args)


def test_twelve_seeds_include_one_hop_only(monkeypatch: Any) -> None:
    monkeypatch.setenv("GRAPH_SEARCH_VIEW", "1")
    seeds = [doc(i, cls_project=f"client-{i}") for i in range(1, 13)]
    docs = list(seeds)
    for i in range(1, 13):
        docs.extend(doc(i * 100 + j, cls_project=f"client-{i}") for j in range(1, 15))
    docs.append(doc(9999, cls_project="unrelated"))
    client, _ = _build(lambda email: docs)
    response = client.get(
        "/api/v1/graph/seed?ids=" + ",".join(uid(i) for i in range(1, 13)), cookies=_auth_cookie()
    )
    assert response.status_code == 200
    graph = response.json()
    assert len(graph["nodes"]) == 12 * 13
    assert len(graph["edges"]) == 12 * 12
    assert sum(n["seed"] for n in graph["nodes"]) == 12
    assert uid(9999) not in {n["id"] for n in graph["nodes"]}


def test_graph_login_target_redacted_and_existing_login_preserved() -> None:
    secret_path = "/search/login?next=%2Fsearch%2Fgraph%2Ffocus%3Fseed%3D" + uid(1)
    record = logging.LogRecord(
        "uvicorn.access", 20, "", 0, "%s %s %s", ("client", "GET", secret_path), None
    )
    _RedactAdminUserAccessLog().filter(record)
    assert record.args == ("client", "GET", "/search/login?<redacted>")
    record.args = ("client", "GET", "/search/login?next=%2Fsearch")
    _RedactAdminUserAccessLog().filter(record)
    assert record.args == ("client", "GET", "/search/login?next=%2Fsearch")


@pytest.mark.parametrize(
    "path",
    [
        "/search/graph?seed=" + uid(1),
        "/search/login?next=%2Fsearch%2Fgraph%3Fseed%3D" + uid(1),
        "/search/graph/focus-other?seed=" + uid(1),
    ],
)
def test_existing_graph_access_log_preserved(path: str) -> None:
    record = logging.LogRecord(
        "uvicorn.access", 20, "", 0, "%s %s %s", ("client", "GET", path), None
    )
    _RedactAdminUserAccessLog().filter(record)
    assert record.args == ("client", "GET", path)
