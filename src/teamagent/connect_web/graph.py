"""資料ナレッジを Obsidian 風グラフ（ノード=資料・エッジ=共有タグ）に変換する純関数群。

I/O を持たず完全にユニットテスト可能。``build_graph`` は ``list_documents_for_graph``
が返す行（dict）を受け取り、フロント（canvas force-directed）に渡す nodes/edges を組む。

エッジの意味（Obsidian の相互リンク相当）:
- 2 資料が同じ ``cls_project`` / ``client_name`` / ``cls_industry`` を共有していれば結ぶ。
- 強さの優先度は project > client > industry（同一ペアは最強の理由 1 本に集約）。
- ハリネズミ化を避けるため、巨大グループは全結合せず鎖状に間引き、さらにノード次数を
  ``max_degree`` で上限化する（決定的・乱数なし）。
"""

from __future__ import annotations

from typing import Any

# 同一タグ値を共有するグループが、この件数以下なら全ペア結合（クリーク）、
# 超える場合は ID 順の鎖（consecutive）だけ結ぶ＝エッジ爆発を防ぐ。
_CLIQUE_LIMIT = 12

# 理由の優先度（小さいほど強い）。同一ペアは最強の理由のみ残す。
_REASONS: tuple[tuple[str, str], ...] = (
    ("cls_project", "project"),
    ("client_name", "client"),
    ("cls_industry", "industry"),
)


def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def build_graph(docs: list[dict[str, Any]], *, max_degree: int = 6) -> dict[str, Any]:
    """資料行リストから ``{"nodes": [...], "edges": [...]}`` を組む。

    各ノード: ``{id, title, url, source_type, industry, project, doc_type, group}``。
    ``group`` は配色キー（cls_doc_type → source_type → "other" の順でフォールバック）。
    各エッジ: ``{source, target, reason}``（reason 例 ``"project:ニチレイ"``）。無向・重複排除。
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
                "group": group,
                "excerpt": _norm(d.get("excerpt")) or None,
            }
        )

    # ノード並びを ID 昇順で決定的にする（DB の行順に依存させない）。
    nodes.sort(key=lambda n: n["id"])

    valid_ids = seen_ids
    # (a,b) → (priority, reason_text)。priority は _REASONS の出現順（小さいほど強い）。
    candidates: dict[tuple[int, int], tuple[int, str]] = {}

    for priority, (key, label) in enumerate(_REASONS):
        groups: dict[str, list[int]] = {}
        for nid in sorted(valid_ids):
            val = _norm(by_id[nid].get(key))
            if val:
                groups.setdefault(val, []).append(nid)
        for val, members in groups.items():
            if len(members) < 2:
                continue
            members_sorted = sorted(members)
            if len(members_sorted) <= _CLIQUE_LIMIT:
                pairs = [
                    (members_sorted[i], members_sorted[j])
                    for i in range(len(members_sorted))
                    for j in range(i + 1, len(members_sorted))
                ]
            else:
                # 鎖状: 連続するノードだけ結ぶ（エッジ数 = len-1 で線形）
                pairs = [
                    (members_sorted[i], members_sorted[i + 1])
                    for i in range(len(members_sorted) - 1)
                ]
            reason_text = f"{label}:{val}"
            for a, b in pairs:
                if candidates.get((a, b), (99, ""))[0] > priority:
                    candidates[(a, b)] = (priority, reason_text)

    # 次数上限を守りつつ、強い理由から貪欲に採用する（決定的）。
    ordered = sorted(candidates.items(), key=lambda kv: (kv[1][0], kv[0]))
    degree: dict[int, int] = {}
    edges: list[dict[str, Any]] = []
    for (a, b), (_prio, reason_text) in ordered:
        if degree.get(a, 0) >= max_degree or degree.get(b, 0) >= max_degree:
            continue
        edges.append({"source": a, "target": b, "reason": reason_text})
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    return {"nodes": nodes, "edges": edges}
