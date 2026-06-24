"""connect_web.graph.build_graph の純関数テスト（I/O なし）。"""

from __future__ import annotations

from typing import Any

from teamagent.connect_web.graph import build_graph


def _doc(node_id: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "node_id": node_id,
        "title": f"doc{node_id}",
        "source_uri": f"gdrive://{node_id}",
        "source_type": "gdrive",
        "cls_industry": None,
        "cls_project": None,
        "cls_doc_type": None,
        "client_name": None,
    }
    base.update(kw)
    return base


def test_empty_input() -> None:
    g = build_graph([])
    assert g == {"nodes": [], "edges": []}


def test_shared_industry_makes_edge() -> None:
    g = build_graph([_doc(1, cls_industry="食品"), _doc(2, cls_industry="食品")])
    assert len(g["nodes"]) == 2
    assert g["edges"] == [{"source": 1, "target": 2, "reason": "industry:食品"}]


def test_no_shared_tag_no_edge() -> None:
    g = build_graph([_doc(1, cls_industry="食品"), _doc(2, cls_industry="化粧品")])
    assert g["edges"] == []


def test_project_beats_industry_as_reason() -> None:
    # 同じ industry かつ同じ project を共有 → 強い理由(project)1 本に集約される
    g = build_graph(
        [
            _doc(1, cls_industry="食品", cls_project="ニチレイ"),
            _doc(2, cls_industry="食品", cls_project="ニチレイ"),
        ]
    )
    assert g["edges"] == [{"source": 1, "target": 2, "reason": "project:ニチレイ"}]


def test_client_edge_and_dedup_with_industry() -> None:
    # client と industry を両方共有 → client が優先（project < client < industry）
    g = build_graph(
        [
            _doc(1, client_name="アサヒ", cls_industry="飲料"),
            _doc(2, client_name="アサヒ", cls_industry="飲料"),
        ]
    )
    assert g["edges"] == [{"source": 1, "target": 2, "reason": "client:アサヒ"}]


def test_group_fallback() -> None:
    g = build_graph(
        [
            _doc(1, cls_doc_type="提案書"),
            _doc(2, cls_doc_type=None, source_type="slack"),
            _doc(3, cls_doc_type=None, source_type=None),
        ]
    )
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id[1]["group"] == "提案書"
    assert by_id[2]["group"] == "slack"
    assert by_id[3]["group"] == "other"


def test_max_degree_cap() -> None:
    # 6 件が同一 industry を共有（クリーク）。max_degree=2 で各ノードの次数 <= 2。
    docs = [_doc(i, cls_industry="食品") for i in range(1, 7)]
    g = build_graph(docs, max_degree=2)
    deg: dict[int, int] = {}
    for e in g["edges"]:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    assert deg and max(deg.values()) <= 2


def test_large_group_is_chained_not_clique() -> None:
    # _CLIQUE_LIMIT(12) 超のグループは鎖状（len-1 本）に間引かれエッジ爆発しない。
    docs = [_doc(i, cls_industry="食品") for i in range(1, 21)]  # 20 件
    g = build_graph(docs, max_degree=99)
    # クリークなら 20*19/2=190 本。鎖なら 19 本。
    assert len(g["edges"]) == 19


def test_duplicate_node_id_node_and_edges_use_first_seen() -> None:
    # 同一 node_id が 2 行 → ノード表示もエッジ用タグも first-seen の doc に揃う
    # （別々の dedup だとノードは Pa 表示なのにエッジは Pb で結ぶ、という食い違いが起きる）。
    docs = [
        _doc(100, title="A", cls_project="Pa", cls_industry="IA"),
        _doc(100, title="B", cls_project="Pb", cls_industry="IB"),
        _doc(200, title="C", cls_project="Pa", cls_industry="IZ"),
    ]
    g = build_graph(docs)
    node100 = next(n for n in g["nodes"] if n["id"] == 100)
    assert node100["title"] == "A"  # first-seen
    assert node100["project"] == "Pa"
    # 100 と 200 は first-seen の project=Pa で結ばれる（last-seen Pb なら結ばれない）
    assert {"source": 100, "target": 200, "reason": "project:Pa"} in g["edges"]


def test_nodes_sorted_by_id_deterministic() -> None:
    g = build_graph([_doc(30), _doc(10), _doc(20)])
    assert [n["id"] for n in g["nodes"]] == [10, 20, 30]


def test_skips_rows_without_node_id() -> None:
    g = build_graph([_doc(1, cls_industry="食品"), {"title": "no id"}])
    assert [n["id"] for n in g["nodes"]] == [1]


def test_node_payload_fields() -> None:
    g = build_graph(
        [_doc(7, title="提案A", cls_industry="食品", cls_project="X", cls_doc_type="提案書")]
    )
    n = g["nodes"][0]
    assert n["id"] == 7
    assert n["title"] == "提案A"
    assert n["url"] == "gdrive://7"
    assert n["industry"] == "食品"
    assert n["project"] == "X"
    assert n["doc_type"] == "提案書"
    assert n["group"] == "提案書"
