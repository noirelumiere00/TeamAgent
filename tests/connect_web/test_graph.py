"""connect_web.graph.build_graph の純関数テスト（I/O なし）。"""

from __future__ import annotations

from typing import Any

from teamagent.connect_web.graph import (
    INDUSTRY_GROUP_MAX,
    build_graph,
    concept_edges,
)


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


def test_shared_industry_makes_edge_when_group_small() -> None:
    # 小さな業界グループ（<= INDUSTRY_GROUP_MAX）は弱いリンクとして結ぶ。
    g = build_graph([_doc(1, cls_industry="食品"), _doc(2, cls_industry="食品")])
    assert len(g["nodes"]) == 2
    assert g["edges"] == [{"source": 1, "target": 2, "reason": "industry:食品", "strength": "weak"}]


def test_no_shared_tag_no_edge() -> None:
    g = build_graph([_doc(1, cls_industry="食品"), _doc(2, cls_industry="化粧品")])
    assert g["edges"] == []


def test_project_beats_industry_as_reason() -> None:
    # 同じ industry かつ同じ project を共有 → 強い理由(project)1 本に集約される。
    g = build_graph(
        [
            _doc(1, cls_industry="食品", cls_project="ニチレイ"),
            _doc(2, cls_industry="食品", cls_project="ニチレイ"),
        ]
    )
    assert g["edges"] == [
        {"source": 1, "target": 2, "reason": "project:ニチレイ", "strength": "strong"}
    ]


def test_client_edge_and_dedup_with_industry() -> None:
    # client と industry を両方共有 → client が優先（project < client < industry）。
    g = build_graph(
        [
            _doc(1, client_name="アサヒ", cls_industry="飲料"),
            _doc(2, client_name="アサヒ", cls_industry="飲料"),
        ]
    )
    assert g["edges"] == [
        {"source": 1, "target": 2, "reason": "client:アサヒ", "strength": "medium"}
    ]


def test_strength_field_per_reason() -> None:
    # 3 種の理由がそれぞれ正しい strength を持つことを確認。
    docs = [
        _doc(1, cls_project="P"),
        _doc(2, cls_project="P"),  # project → strong
        _doc(3, client_name="C"),
        _doc(4, client_name="C"),  # client → medium
        _doc(5, cls_industry="食品"),
        _doc(6, cls_industry="食品"),  # industry(小) → weak
    ]
    g = build_graph(docs)
    by_pair = {(e["source"], e["target"]): e["strength"] for e in g["edges"]}
    assert by_pair[(1, 2)] == "strong"
    assert by_pair[(3, 4)] == "medium"
    assert by_pair[(5, 6)] == "weak"


def test_large_industry_group_makes_no_edges() -> None:
    # 大量の資料が同じ業界 "食品" を共有 → 弱いリンクなのでエッジ 0 本（ハリネズミ抑制）。
    docs = [_doc(i, cls_industry="食品") for i in range(1, 51)]  # 50 件
    g = build_graph(docs, max_degree=99)
    assert g["edges"] == []


def test_industry_group_at_threshold_chains_not_clique() -> None:
    # ちょうど上限件数の業界グループ → クリークではなく鎖（len-1 本）。
    docs = [_doc(i, cls_industry="食品") for i in range(1, INDUSTRY_GROUP_MAX + 1)]
    g = build_graph(docs, max_degree=99)
    # クリークなら n*(n-1)/2 本。鎖なら n-1 本。
    assert len(g["edges"]) == INDUSTRY_GROUP_MAX - 1
    assert all(e["strength"] == "weak" for e in g["edges"])


def test_industry_just_over_threshold_no_edges() -> None:
    # 上限を 1 件超えた業界グループ → エッジ 0 本（弱リンクは小グループ限定）。
    docs = [_doc(i, cls_industry="食品") for i in range(1, INDUSTRY_GROUP_MAX + 2)]
    g = build_graph(docs, max_degree=99)
    assert g["edges"] == []


def test_project_still_connects_in_large_group() -> None:
    # 業界が同じでも、同一 project を共有していれば（大グループでも鎖で）結ばれる。
    docs = [_doc(i, cls_industry="食品", cls_project="ニチレイ") for i in range(1, 51)]
    g = build_graph(docs, max_degree=99)
    # 50 件 > _CLIQUE_LIMIT → project は鎖（len-1=49 本）。全て strong。
    assert len(g["edges"]) == 49
    assert all(e["reason"] == "project:ニチレイ" for e in g["edges"])
    assert all(e["strength"] == "strong" for e in g["edges"])


def test_client_clique_for_small_group() -> None:
    # 小さな顧客グループ（<= _CLIQUE_LIMIT）は全結合（クリーク）。
    docs = [_doc(i, client_name="アサヒ") for i in range(1, 5)]  # 4 件
    g = build_graph(docs, max_degree=99)
    # クリーク: 4*3/2 = 6 本、全て medium。
    assert len(g["edges"]) == 6
    assert all(e["strength"] == "medium" for e in g["edges"])


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
    # 6 件が同一 project を共有（小グループ → クリーク）。max_degree=2 で各次数 <= 2。
    docs = [_doc(i, cls_project="P") for i in range(1, 7)]
    g = build_graph(docs, max_degree=2)
    deg: dict[int, int] = {}
    for e in g["edges"]:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    assert deg and max(deg.values()) <= 2


def test_default_max_degree_caps_dense_project_group() -> None:
    # 既定 max_degree(=4) でクリーク級の project グループでも次数が上限以下。
    docs = [_doc(i, cls_project="P") for i in range(1, 9)]  # 8 件 = _CLIQUE_LIMIT
    g = build_graph(docs)  # default max_degree
    deg: dict[int, int] = {}
    for e in g["edges"]:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    assert deg and max(deg.values()) <= 4


def test_large_project_group_is_chained_not_clique() -> None:
    # _CLIQUE_LIMIT 超の project グループは鎖状（len-1 本）に間引かれエッジ爆発しない。
    docs = [_doc(i, cls_project="P") for i in range(1, 21)]  # 20 件
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
    # 100 と 200 は first-seen の project=Pa で結ばれる（last-seen Pb なら結ばれない）。
    assert {
        "source": 100,
        "target": 200,
        "reason": "project:Pa",
        "strength": "strong",
    } in g["edges"]


def test_determinism_same_input_same_output() -> None:
    # 同一入力（順序違い）でも nodes/edges は決定的に同じ。
    docs1 = [
        _doc(3, cls_project="P"),
        _doc(1, cls_project="P"),
        _doc(2, cls_project="P"),
    ]
    docs2 = list(reversed(docs1))
    assert build_graph(docs1) == build_graph(docs2)


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


def test_node_payload_new_classification_axes() -> None:
    # L2 射影: cls_solution / cls_budget / cls_target が node に乗る。
    g = build_graph(
        [
            _doc(
                7,
                cls_solution="動画広告",
                cls_budget="500万",
                cls_target="20代女性",
            )
        ]
    )
    n = g["nodes"][0]
    assert n["solution"] == "動画広告"
    assert n["budget"] == "500万"
    assert n["target"] == "20代女性"


def test_node_new_axes_default_none_when_absent() -> None:
    # 新軸が無い doc は None（後方互換: キーは常に存在する）。
    g = build_graph([_doc(1, cls_project="P")])
    n = g["nodes"][0]
    assert n["solution"] is None
    assert n["budget"] is None
    assert n["target"] is None


# --- L3A: concept_edges 純関数の単体テスト ---------------------------------------


def test_concept_edges_orthogonal_vectors_no_edge() -> None:
    # 直交ベクトル → cosine 0 → しきい値未満 → エッジ 0 本。
    vecs = {1: [1.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0], 3: [0.0, 0.0, 1.0]}
    assert concept_edges(vecs) == []


def test_concept_edges_close_pair_makes_one_edge() -> None:
    # ほぼ同方向の 2 ベクトル → concept エッジ 1 本（source<target・reason/strength=concept）。
    vecs = {1: [1.0, 0.0], 2: [0.99, 0.01]}
    edges = concept_edges(vecs, threshold=0.82)
    assert edges == [{"source": 1, "target": 2, "reason": "concept", "strength": "concept"}]


def test_concept_edges_threshold_filters() -> None:
    # cosine が閾値ちょうど未満なら張らない。45度 (cos≈0.707) は 0.82 未満。
    vecs = {1: [1.0, 0.0], 2: [1.0, 1.0]}
    assert concept_edges(vecs, threshold=0.82) == []
    # 閾値を下げれば張られる。
    assert len(concept_edges(vecs, threshold=0.5)) == 1


def test_concept_edges_skips_existing_pairs() -> None:
    # タグ一致で既に結ばれたペアは concept エッジを張らない（既存優先）。
    vecs = {1: [1.0, 0.0], 2: [0.99, 0.01]}
    edges = concept_edges(vecs, threshold=0.82, existing_pairs=frozenset({(1, 2)}))
    assert edges == []


def test_concept_edges_zero_vector_ignored() -> None:
    # ゼロベクトルは正規化できないので比較対象外（エッジに現れない）。
    vecs = {1: [1.0, 0.0], 2: [0.99, 0.01], 3: [0.0, 0.0]}
    edges = concept_edges(vecs, threshold=0.82)
    ids = {e["source"] for e in edges} | {e["target"] for e in edges}
    assert 3 not in ids


def test_concept_edges_max_degree_cap() -> None:
    # 同方向の 5 ベクトルクラスタ。max_degree=1 で各ノードの concept 次数 <= 1。
    vecs = {i: [1.0, 0.001 * i] for i in range(1, 6)}
    edges = concept_edges(vecs, threshold=0.82, k=4, max_degree=1)
    deg: dict[int, int] = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    assert deg and max(deg.values()) <= 1


def test_concept_edges_deterministic() -> None:
    # 入力 dict の順序に依らず決定的に同じ。
    base = {1: [1.0, 0.0], 2: [0.99, 0.02], 3: [0.98, 0.04]}
    rev = {k: base[k] for k in reversed(list(base))}
    assert concept_edges(base, threshold=0.82) == concept_edges(rev, threshold=0.82)


# --- L3A: build_graph 統合（concept_vectors 引数） --------------------------------


def test_build_graph_concept_vectors_none_unchanged() -> None:
    # concept_vectors=None（既定）は現状と完全一致（後方互換）。
    docs = [_doc(1, cls_project="P"), _doc(2, cls_project="P"), _doc(3, cls_industry="食品")]
    assert build_graph(docs) == build_graph(docs, concept_vectors=None)


def test_build_graph_appends_concept_edges() -> None:
    # タグ無しで意味的に近い 2 doc → concept エッジが追記される。
    docs = [_doc(1), _doc(2)]
    g = build_graph(docs, concept_vectors={1: [1.0, 0.0], 2: [0.99, 0.01]})
    assert g["edges"] == [{"source": 1, "target": 2, "reason": "concept", "strength": "concept"}]


def test_build_graph_concept_does_not_duplicate_tag_edges() -> None:
    # 同一 project で既にタグ一致エッジがある近接ペアには concept を重ねない。
    docs = [_doc(1, cls_project="P"), _doc(2, cls_project="P")]
    g = build_graph(docs, concept_vectors={1: [1.0, 0.0], 2: [0.99, 0.01]})
    # project エッジ 1 本のみ。concept は重複回避で張られない。
    assert g["edges"] == [{"source": 1, "target": 2, "reason": "project:P", "strength": "strong"}]


def test_build_graph_concept_ignores_unknown_node_ids() -> None:
    # グラフに存在しない node_id のベクトルは無視される。
    docs = [_doc(1), _doc(2)]
    g = build_graph(docs, concept_vectors={1: [1.0, 0.0], 2: [0.99, 0.01], 999: [0.99, 0.01]})
    ids = {e["source"] for e in g["edges"]} | {e["target"] for e in g["edges"]}
    assert 999 not in ids
