"""PgVectorClient.search_lexical_new_schema() の SQL ビルダー単体テスト。

語彙検索の term は **ユーザークエリ由来** (hybrid.extract_terms) なので、
SQL リテラルに補間されず必ず psycopg placeholder (`%s`) として bind される
ことを保証する。BM25 ハイブリッド (Sprint 5) の SQL injection 回帰テスト。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient


def _mock_conn(df_rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    """count → df → scoring の 3 回 execute を捌くモック。

    fetchone は総 chunk 数、fetchall は 1 回目=df rows / 2 回目=結果 [] を返す。
    """
    cur = MagicMock()
    cur.fetchone.return_value = {"n": 1000}
    cur.fetchall.side_effect = [df_rows, []]
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_lexical_empty_terms_returns_empty_without_query() -> None:
    """terms が空なら DB を一切叩かず [] を返す。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn = MagicMock()
    assert client.search_lexical_new_schema(conn=conn, terms=[]) == []
    conn.cursor.assert_not_called()


def test_lexical_all_terms_df_zero_returns_empty() -> None:
    """全 term が df=0 (どこにも出現しない) なら [] を返す。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, _ = _mock_conn(df_rows=[{"term": "存在しない語", "df": 0}])
    assert client.search_lexical_new_schema(conn=conn, terms=["存在しない語"]) == []


def test_lexical_terms_are_bound_as_placeholders_not_interpolated() -> None:
    """term は LIKE %s の placeholder に bind され、SQL 本文に値が出ない (injection 防止)。"""
    client = PgVectorClient(dsn="postgresql://stub")
    malicious = "'; DROP TABLE chunks; --"
    conn, cur = _mock_conn(df_rows=[{"term": malicious, "df": 3}])

    client.search_lexical_new_schema(conn=conn, terms=[malicious], limit=30)

    # 3 回目の execute がスコアリング本クエリ
    scoring_call = cur.execute.call_args_list[-1]
    sql: str = scoring_call.args[0]
    params: list[Any] = list(scoring_call.args[1])

    # SQL は placeholder のみ。悪意ある term 文字列が本文に補間されていない
    assert "CASE WHEN c.content LIKE %s" in sql
    assert "DROP TABLE" not in sql
    # term は params 側に LIKE パターンとして入る
    assert f"%{malicious}%" in params


def test_lexical_industry_filter_uses_placeholder() -> None:
    """filter_industry を渡すと metadata->>'industry' = %s で bind される。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn(df_rows=[{"term": "提案", "df": 50}])

    client.search_lexical_new_schema(
        conn=conn,
        terms=["提案"],
        filter_industry="エネルギー",
        strict_industry=True,
    )

    scoring_call = cur.execute.call_args_list[-1]
    sql: str = scoring_call.args[0]
    params: list[Any] = list(scoring_call.args[1])

    assert "d.metadata->>'industry' = %s" in sql
    assert "エネルギー" not in sql
    assert "エネルギー" in params


def test_lexical_metadata_filters_use_placeholders() -> None:
    """metadata_filters の key/value がともに placeholder 化される。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn(df_rows=[{"term": "提案", "df": 50}])

    # bant_score は SELECT 出力列に無い key なので、リテラル化検査が曖昧にならない
    client.search_lexical_new_schema(
        conn=conn,
        terms=["提案"],
        metadata_filters={"bant_score": "A"},
    )

    scoring_call = cur.execute.call_args_list[-1]
    sql: str = scoring_call.args[0]
    params: list[Any] = list(scoring_call.args[1])

    assert "d.metadata->>%s = %s" in sql
    assert "'bant_score'" not in sql  # filter key もリテラル化されない
    assert "bant_score" in params
    assert "A" in params
