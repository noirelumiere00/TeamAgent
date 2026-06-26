"""search_similar_new_schema の sticky_filters / metadata_contains SQL ビルダー単体テスト。

設計 v2 §4 の検証項目:
- ``__client__`` が cls_project / client_name / title の OR-ILIKE 3 句 + ESCAPE + placeholder
- LIKE メタ文字（% / _ / \\）が bind 前にエスケープされ pattern に展開される
- sticky_filters の等価 AND（budget）と soft（__budget_or_unknown__）の OR 句
- 未指定時 params が一切増えない（後方互換・SQL バイト不変）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient


def _mock_conn() -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _run(**kwargs: Any) -> tuple[str, list[Any]]:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(conn=conn, embedding=[0.1] * 1024, limit=5, **kwargs)
    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])
    return sql, params


def test_client_contains_builds_or_group_with_escape() -> None:
    """__client__ は cls_project / client_name / title の OR-ILIKE で ESCAPE 付き。"""
    sql, params = _run(metadata_contains={"__client__": "日本ガイシ"})
    assert "d.metadata->>'cls_project' ILIKE %s ESCAPE" in sql
    assert "d.metadata->>'client_name' ILIKE %s ESCAPE" in sql
    assert "d.title ILIKE %s ESCAPE" in sql
    # OR で 1 グループに括られている
    assert " OR d.metadata->>'client_name' ILIKE" in sql
    # 値は SQL リテラルに混入しない（placeholder のみ）
    assert "日本ガイシ" not in sql
    # pattern は %wrap されて params 側に 3 回 bind される（3 句）
    assert params.count("%日本ガイシ%") == 3


def test_like_metachars_escaped_in_pattern() -> None:
    """% / _ / \\ が bind 前にエスケープされ pattern に展開される。"""
    _, params = _run(metadata_contains={"__client__": "a%b_c\\d"})
    # \\ → \\\\, % → \%, _ → \_ の順でエスケープ済みパターンが入る
    assert "%a\\%b\\_c\\\\d%" in params


def test_non_client_contains_single_ilike() -> None:
    """__client__ 以外のキーは単独 ILIKE（key も placeholder）。"""
    sql, params = _run(metadata_contains={"cls_solution": "保存率"})
    assert "d.metadata->>%s ILIKE %s ESCAPE" in sql
    assert "cls_solution" in params
    assert "%保存率%" in params


def test_sticky_filter_equality_and() -> None:
    """sticky_filters は等価 AND で placeholder bind（budget strict）。"""
    sql, params = _run(sticky_filters={"cls_budget": "500万〜"})
    assert "d.metadata->>%s = %s" in sql
    assert "cls_budget" in params
    assert "500万〜" in params
    assert "500万〜" not in sql  # リテラル化されない


def test_sticky_budget_or_unknown_soft() -> None:
    """__budget_or_unknown__ は (cls_budget=値 OR cls_budget='不明') の soft 句。"""
    sql, params = _run(sticky_filters={"__budget_or_unknown__": "100〜500万"})
    assert "d.metadata->>'cls_budget' = %s OR d.metadata->>'cls_budget' = '不明'" in sql
    # band 値は 1 つだけ bind（OR の片側はリテラル '不明'）
    assert "100〜500万" in params
    assert params.count("100〜500万") == 1


def test_no_new_filters_means_no_extra_params() -> None:
    """sticky/metadata_contains 未指定なら params は従来どおり（後方互換）。"""
    sql_base, params_base = _run()
    sql_new, params_new = _run(sticky_filters=None, metadata_contains=None)
    assert sql_base == sql_new
    assert params_base == params_new
    # WHERE 句が一切生成されない（フィルタ 0 件）
    assert "WHERE" not in sql_new
    assert "ILIKE" not in sql_new


def test_client_and_budget_coexist() -> None:
    """client（OR-ILIKE）と budget（等価）は AND で共存する。"""
    sql, params = _run(
        metadata_contains={"__client__": "ACME"},
        sticky_filters={"cls_budget": "〜100万"},
    )
    assert "ILIKE" in sql
    assert "d.metadata->>%s = %s" in sql
    assert params.count("%ACME%") == 3
    assert "cls_budget" in params
    assert "〜100万" in params
