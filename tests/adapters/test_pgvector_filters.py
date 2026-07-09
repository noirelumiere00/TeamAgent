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


# ── embedding_col 配線（既定 embedding＝恒等 / Cohere 列切替 / injection 防止） ──


def test_embedding_col_default_is_embedding_identity() -> None:
    """embedding_col 未指定（既定 embedding）は従来 SQL と完全一致（バイト不変・後方互換）。"""
    sql_base, params_base = _run()
    sql_default, params_default = _run(embedding_col="embedding")
    assert sql_base == sql_default
    assert params_base == params_default
    # 既定では e5 列をそのまま使う
    assert "c.embedding <=> %s::vector" in sql_default
    assert "embedding_cohere" not in sql_default


def test_embedding_col_cohere_switches_both_score_and_order_by() -> None:
    """embedding_cohere 指定で score 算出と ORDER BY の両方が並行列に切り替わる。"""
    sql, params = _run(embedding_col="embedding_cohere")
    assert "1 - (c.embedding_cohere <=> %s::vector) AS score" in sql
    assert "ORDER BY c.embedding_cohere <=> %s::vector" in sql
    # 旧 e5 列 c.embedding は類似度演算には使われない
    assert "c.embedding <=>" not in sql
    # params は embedding（埋め込みベクトル）が score と ORDER BY の 2 回 + limit のみで不変
    assert params[0] == [0.1] * 1024


def test_embedding_col_rejects_injection() -> None:
    """許可リスト外の列名（injection 試行）は ValueError で即落とす。"""
    import pytest

    with pytest.raises(ValueError, match="embedding_col"):
        _run(embedding_col="embedding; DROP TABLE chunks --")


# ── exclude_templates / exclude_recurring（テンプレ・定期報告の文書単位除外） ──


def test_exclude_templates_adds_coalesce_clause_without_params() -> None:
    """exclude_templates=True で固定リテラル COALESCE 句が入り、bind params は増えない。"""
    _, params_base = _run()
    sql, params = _run(exclude_templates=True)
    assert "COALESCE((d.metadata->>'cls_is_template')::bool, false) = false" in sql
    # 値 bind は一切増えない（句は固定リテラルのみ＝injection 面の追加リスクなし）
    assert params == params_base
    assert "cls_is_recurring" not in sql


def test_exclude_recurring_adds_coalesce_clause_without_params() -> None:
    _, params_base = _run()
    sql, params = _run(exclude_recurring=True)
    assert "COALESCE((d.metadata->>'cls_is_recurring')::bool, false) = false" in sql
    assert params == params_base
    assert "cls_is_template" not in sql


def test_exclude_templates_and_recurring_coexist_with_and() -> None:
    sql, _ = _run(exclude_templates=True, exclude_recurring=True)
    assert "COALESCE((d.metadata->>'cls_is_template')::bool, false) = false" in sql
    assert "COALESCE((d.metadata->>'cls_is_recurring')::bool, false) = false" in sql
    assert " AND " in sql


def test_exclude_flags_off_sql_byte_identical() -> None:
    """両フラグ既定 False（未指定）は従来 SQL とバイト等価（後方互換）。"""
    sql_base, params_base = _run()
    sql_off, params_off = _run(exclude_templates=False, exclude_recurring=False)
    assert sql_base == sql_off
    assert params_base == params_off
    assert "cls_is_template" not in sql_off
    assert "cls_is_recurring" not in sql_off


def test_exclude_flags_combine_with_other_filters() -> None:
    """既存 exclude（boilerplate/duplicates）や sticky と AND 共存する。"""
    sql, params = _run(
        exclude_templates=True,
        exclude_recurring=True,
        exclude_boilerplate=True,
        sticky_filters={"cls_doc_type": "提案書"},
    )
    assert "COALESCE((c.metadata->>'boilerplate')::bool, false) = false" in sql
    assert "COALESCE((d.metadata->>'cls_is_template')::bool, false) = false" in sql
    assert "COALESCE((d.metadata->>'cls_is_recurring')::bool, false) = false" in sql
    assert "cls_doc_type" in params
    assert "提案書" in params
