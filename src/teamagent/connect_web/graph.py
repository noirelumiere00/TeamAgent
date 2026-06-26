"""資料ナレッジを Obsidian 風グラフ（ノード=資料・エッジ=共有タグ）に変換する純関数群。

I/O を持たず完全にユニットテスト可能。``build_graph`` は ``list_documents_for_graph``
が返す行（dict）を受け取り、フロント（canvas force-directed）に渡す nodes/edges を組む。

エッジの意味（Obsidian の相互リンク相当）と「強さ」:
- 2 資料が同じ ``cls_project`` / ``client_name`` / ``cls_industry`` を共有していれば結ぶ。
- 強さの優先度は project > client > industry（同一ペアは最強の理由 1 本に集約）。
- ``cls_project`` / ``client_name`` は「具体的・意味のある」リンク（同一案件・同一顧客）。
  小グループは全結合（クリーク）、大グループは ID 順の鎖で間引く。
- ``cls_industry`` は「広すぎる・弱い」リンク。数百件が "食品" を共有するような粗い軸なので、
  **小さな業界グループだけ・かつ鎖状のみ**で結ぶ。大きな業界グループはエッジを張らない
  （= ハリネズミ化＝"全部が似ている"ノイズを抑制する）。
- さらにノード次数を ``max_degree`` で上限化する（決定的・乱数なし）。

各エッジには後方互換な追加フィールド ``strength``（strong/medium/weak）を付与する。
フロントは未知フィールドを無視するため、``{source, target, reason}`` は不変。
"""

from __future__ import annotations

import math
from typing import Any

# project / client（具体的で意味のあるリンク）のグループが、この件数以下なら全ペア結合
# （クリーク）、超える場合は ID 順の鎖（consecutive）だけ結ぶ＝エッジ爆発を防ぐ。
# 6→当初12 から下げてあり、案件/顧客グループでも過剰結合しないようにしている（tunable）。
_CLIQUE_LIMIT = 8

# 業界（cls_industry）は粗すぎる軸（例: 数百件が "食品"）なので、別枠で強く間引く。
# - 業界グループの件数がこの上限以下のときだけリンクする（大きい業界はエッジ 0 本）。
# - その場合でもクリークは作らず「鎖状（len-1 本）」のみ。
# これにより "全部が似ている" ハリネズミ化を防ぎ、具体リンク（案件/顧客）を主役にする。
INDUSTRY_GROUP_MAX = 5

# 理由（key, label, strength）。priority は出現順（小さいほど強い）。
# strength はフロント描画用の追加メタ（strong=案件・medium=顧客・weak=業界）。
_REASONS: tuple[tuple[str, str, str], ...] = (
    ("cls_project", "project", "strong"),
    ("client_name", "client", "medium"),
    ("cls_industry", "industry", "weak"),
)

# industry をどう扱うかの key 名（上の _REASONS と一致させる）。
_INDUSTRY_KEY = "cls_industry"

# --- L3A: 意味クラスタ・エッジ（concept edges）の既定パラメータ -------------------
# 各ノードの上位 k 近傍のうち cosine >= しきい値のものだけを弱リンクで結ぶ。
# しきい値は高め（0.82）で「明確に意味が近い」資料同士だけを結び、ノイズを抑える。
# max_degree は concept エッジによるノード過密を抑える（タグ一致エッジとは独立に数える）。
_CONCEPT_K = 4
_CONCEPT_THRESHOLD = 0.82
_CONCEPT_MAX_DEGREE = 3
# concept エッジの表示メタ（タグ一致の strong/medium/weak と区別できる別値）。
_CONCEPT_STRENGTH = "concept"
_CONCEPT_REASON = "concept"


def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_vec(vec: list[float]) -> list[float] | None:
    """ベクトルを L2 正規化する。ゼロベクトル / 空は None（= 比較対象外）。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return None
    return [x / norm for x in vec]


def _cosine_unit(a: list[float], b: list[float]) -> float:
    """**正規化済**ベクトル同士の cosine（= 内積）。長さ不一致は短い方に合わせる。"""
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def concept_edges(
    node_vectors: dict[int, list[float]],
    *,
    k: int = _CONCEPT_K,
    threshold: float = _CONCEPT_THRESHOLD,
    max_degree: int = _CONCEPT_MAX_DEGREE,
    existing_pairs: frozenset[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """代表ベクトルから意味的に近い資料同士の弱リンク（concept エッジ）を決定的に張る。

    各ノードの代表ベクトルを L2 正規化し、cosine kNN（n は数百なので総当たり O(n^2)）で
    上位 ``k`` 近傍を採り、cosine >= ``threshold`` のペアだけを弱リンクとして結ぶ。
    乱数なし・ソート安定で完全に決定的。``max_degree`` で過密を抑制する。

    Args:
        node_vectors: ``{node_id: 代表ベクトル(list[float])}``。ゼロ/空ベクトルは無視。
        k: 各ノードが見る近傍数の上限。
        threshold: この cosine 以上のペアだけを結ぶ（高いほど厳しい）。
        max_degree: concept エッジによる各ノードの次数上限。
        existing_pairs: 既にタグ一致エッジで結ばれた無向ペア集合（``(min, max)``）。
            ここに含まれるペアは concept エッジを**張らない**（既存エッジ優先）。

    Returns:
        ``{"source", "target", "reason", "strength"}`` の list（無向・``source < target``）。
        ``reason`` / ``strength`` は共に ``"concept"``。
    """
    existing = existing_pairs or frozenset()
    # 正規化（ゼロ/空は除外）。ID 昇順で決定性を担保。
    units: dict[int, list[float]] = {}
    for nid in sorted(node_vectors):
        u = _normalize_vec(node_vectors[nid])
        if u is not None:
            units[nid] = u
    ids = sorted(units)

    # 各ノードについて上位 k 近傍（cosine 降順・同点は ID 昇順）を候補化する。
    candidates: dict[tuple[int, int], float] = {}
    for a in ids:
        sims: list[tuple[float, int]] = []
        for b in ids:
            if b == a:
                continue
            sims.append((_cosine_unit(units[a], units[b]), b))
        # cosine 降順、同点は近傍 ID 昇順で決定的に。
        sims.sort(key=lambda sv: (-sv[0], sv[1]))
        for sim, b in sims[:k]:
            if sim < threshold:
                break  # 降順なのでこれ以降も全て閾値未満
            pair = (a, b) if a < b else (b, a)
            if pair in existing:
                continue  # タグ一致エッジと重複させない（既存優先）
            # 双方向に候補入りし得るので、より高い cosine を採用（決定的）。
            if sim > candidates.get(pair, -1.0):
                candidates[pair] = sim

    # 次数上限を守りつつ cosine の高いペアから貪欲採用（決定的: cosine 降→pair 昇）。
    ordered = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
    degree: dict[int, int] = {}
    edges: list[dict[str, Any]] = []
    for (a, b), _sim in ordered:
        if degree.get(a, 0) >= max_degree or degree.get(b, 0) >= max_degree:
            continue
        edges.append(
            {"source": a, "target": b, "reason": _CONCEPT_REASON, "strength": _CONCEPT_STRENGTH}
        )
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    return edges


def _pairs_for_group(members_sorted: list[int], *, key: str) -> list[tuple[int, int]]:
    """1 グループ（同一タグ値の資料群）から結ぶべき無向ペアを決定的に返す。

    - 業界（``cls_industry``）= 弱いリンク:
        グループが小さい（``len <= INDUSTRY_GROUP_MAX``）ときだけ、かつ鎖状のみ。
        大きい業界グループは [] を返す（ハリネズミ化を防ぐ）。
    - 案件 / 顧客 = 具体的リンク:
        小グループ（``len <= _CLIQUE_LIMIT``）は全ペア（クリーク）、
        大グループは ID 順の鎖（consecutive）で間引く。
    """
    n = len(members_sorted)
    if n < 2:
        return []

    if key == _INDUSTRY_KEY:
        # 弱いリンク: 小さい業界グループだけ・鎖状のみ。
        if n > INDUSTRY_GROUP_MAX:
            return []
        return [(members_sorted[i], members_sorted[i + 1]) for i in range(n - 1)]

    # 具体リンク（project / client）: 小はクリーク、大は鎖。
    if n <= _CLIQUE_LIMIT:
        return [(members_sorted[i], members_sorted[j]) for i in range(n) for j in range(i + 1, n)]
    return [(members_sorted[i], members_sorted[i + 1]) for i in range(n - 1)]


def build_graph(
    docs: list[dict[str, Any]],
    *,
    max_degree: int = 4,
    concept_vectors: dict[int, list[float]] | None = None,
    concept_k: int = _CONCEPT_K,
    concept_threshold: float = _CONCEPT_THRESHOLD,
    concept_max_degree: int = _CONCEPT_MAX_DEGREE,
) -> dict[str, Any]:
    """資料行リストから ``{"nodes": [...], "edges": [...]}`` を組む。

    各ノード: ``{id, title, url, source_type, industry, project, client_name,
    doc_type, solution, budget, target, group, excerpt}``。``group`` は配色キー
    （cls_doc_type → source_type → "other" の順でフォールバック）。

    各エッジ: ``{source, target, reason, strength}``。``reason`` 例 ``"project:ニチレイ"``、
    ``strength`` は ``strong``（案件）/``medium``（顧客）/``weak``（業界）。無向・重複排除。
    ``strength`` は後方互換な追加フィールド（フロントは未知フィールドを無視する）。

    concept_vectors（L3A 意味クラスタ・エッジ）:
        ``{node_id: 代表ベクトル}`` を与えると、タグ一致エッジに加えて
        埋め込みで意味的に近い資料同士を弱リンク（``reason="concept"`` /
        ``strength="concept"``）で**追記**する。既存タグ一致エッジと重複するペアは張らない。
        ``None``（既定 = env OFF・ベクトル未供給）のときは追記せず**現状と完全一致**。
        env-gate ``GRAPH_CONCEPT_EDGES`` の判定とベクトル取得は呼び出し側（graph route /
        pgvector）の責務で、本関数は os.environ を読まない（純関数）。
        ``concept_k`` / ``concept_threshold`` / ``concept_max_degree`` は呼び出し側から
        env 値（``GRAPH_CONCEPT_K`` / ``GRAPH_CONCEPT_THRESHOLD``）で上書きできる。E5 系の
        埋め込みは無関係ペアでも cosine ベースラインが高めなので、実データで較正して再ビルド
        無しにしきい値を上げ下げできるようにしている（団子化の再発防止）。
    """
    nodes: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    # node 表示と edge 用のタグ参照を「同じ first-seen の doc」に統一する。
    # （別々に作ると node=first-seen・by_id=last-seen で表示とエッジが食い違う）
    by_id: dict[int, dict[str, Any]] = {}
    for d in docs:
        raw_id = d.get("node_id")
        if raw_id is None:
            continue
        node_id = int(raw_id)
        if node_id in seen_ids:
            continue
        seen_ids.add(node_id)
        by_id[node_id] = d
        doc_type = _norm(d.get("cls_doc_type")) or None
        industry = _norm(d.get("cls_industry")) or None
        project = _norm(d.get("cls_project")) or None
        client = _norm(d.get("client_name")) or None
        # 第2世代の分類軸（別 agent が分類器に追加）。表示・フィルタ用に node にも乗せる。
        solution = _norm(d.get("cls_solution")) or None
        budget = _norm(d.get("cls_budget")) or None
        target = _norm(d.get("cls_target")) or None
        source_type = _norm(d.get("source_type")) or None
        group = doc_type or source_type or "other"
        nodes.append(
            {
                "id": node_id,
                "title": _norm(d.get("title")) or "(無題)",
                "url": d.get("source_uri"),
                "source_type": source_type,
                "industry": industry,
                "project": project,
                "client_name": client,
                "doc_type": doc_type,
                "solution": solution,
                "budget": budget,
                "target": target,
                "group": group,
                "excerpt": _norm(d.get("excerpt")) or None,
            }
        )

    # ノード並びを ID 昇順で決定的にする（DB の行順に依存させない）。
    nodes.sort(key=lambda n: n["id"])

    valid_ids = seen_ids
    # (a,b) → (priority, reason_text, strength)。priority は _REASONS の出現順（小ほど強い）。
    candidates: dict[tuple[int, int], tuple[int, str, str]] = {}

    for priority, (key, label, strength) in enumerate(_REASONS):
        groups: dict[str, list[int]] = {}
        for nid in sorted(valid_ids):
            val = _norm(by_id[nid].get(key))
            if val:
                groups.setdefault(val, []).append(nid)
        for val, members in groups.items():
            members_sorted = sorted(members)
            pairs = _pairs_for_group(members_sorted, key=key)
            if not pairs:
                continue
            reason_text = f"{label}:{val}"
            for a, b in pairs:
                # 既により強い（priority が小さい）理由で結ばれていれば降格しない。
                # = 案件/顧客で結ばれたペアを業界に格下げしない（具体リンク優先）。
                if candidates.get((a, b), (99, "", ""))[0] > priority:
                    candidates[(a, b)] = (priority, reason_text, strength)

    # 次数上限を守りつつ、強い理由から貪欲に採用する（決定的）。
    ordered = sorted(candidates.items(), key=lambda kv: (kv[1][0], kv[0]))
    degree: dict[int, int] = {}
    edges: list[dict[str, Any]] = []
    for (a, b), (_prio, reason_text, strength) in ordered:
        if degree.get(a, 0) >= max_degree or degree.get(b, 0) >= max_degree:
            continue
        edges.append({"source": a, "target": b, "reason": reason_text, "strength": strength})
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    # L3A: 代表ベクトルが供給された時だけ concept エッジを既存エッジに追記する。
    # ベクトル未供給（None）なら何もしない＝旧挙動と完全一致（後方互換）。
    if concept_vectors:
        # グラフに実在するノードのベクトルだけを対象にする（孤立 ID は無視）。
        vecs = {nid: v for nid, v in concept_vectors.items() if nid in valid_ids}
        if vecs:
            existing_pairs = frozenset(
                (e["source"], e["target"])
                if e["source"] < e["target"]
                else (e["target"], e["source"])
                for e in edges
            )
            edges.extend(
                concept_edges(
                    vecs,
                    k=concept_k,
                    threshold=concept_threshold,
                    max_degree=concept_max_degree,
                    existing_pairs=existing_pairs,
                )
            )

    return {"nodes": nodes, "edges": edges}
