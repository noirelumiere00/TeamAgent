"""PgVectorClient.search_similar() の SQL ビルダー単体テスト。

`metadata_filters` を渡したとき、値が SQL リテラルに補間されず必ず
psycopg の placeholder (`%s`) として bind されることを保証する。
将来 Pydantic 側の入力検証が緩和されても SQL injection が発生しないことを
adapter 層で固定するための回帰テスト。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient


def _mock_conn() -> tuple[MagicMock, MagicMock]:
    """conn.cursor() の context manager をモックして cur を返す。

    cur.execute は呼び出し引数を後から検証するため MagicMock のまま、
    cur.fetchall は空 list を返して SearchHit ループをスキップさせる。
    """
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_search_similar_no_filters_emits_no_where() -> None:
    """metadata_filters=None なら WHERE 句は生成されない。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposal_chunks",
        limit=5,
        metadata_col="metadata",
    )
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE" not in sql


def test_search_similar_metadata_filter_uses_placeholders() -> None:
    """metadata_filters を渡すと `metadata->>%s = %s` 形式の SQL になり、値は params 側に行く。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposal_chunks",
        limit=5,
        metadata_filters={"industry": "エネルギー"},
        metadata_col="metadata",
    )

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    # SQL は placeholder のみ。値そのものは含まない
    assert "metadata->>%s = %s" in sql
    assert "エネルギー" not in sql
    assert "'industry'" not in sql  # key 側もリテラル化されない

    # params: [embedding, "industry", "エネルギー", embedding, limit]
    assert "industry" in params
    assert "エネルギー" in params


def test_search_similar_injection_payload_stays_in_params() -> None:
    """悪意ある文字列を渡しても SQL 本体には混入せず、params 側に bind される。

    `'; DROP TABLE chunks; --` を industry に渡しても、生成 SQL に
    そのまま埋め込まれていないこと（=placeholder 経由でしか DB に届かないこと）を確認。
    """
    payload = "'; DROP TABLE chunks; --"
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposal_chunks",
        limit=5,
        metadata_filters={"industry": payload},
        metadata_col="metadata",
    )

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    # 1. SQL 本体に injection payload が一切含まれていない
    assert payload not in sql
    assert "DROP TABLE" not in sql
    # 2. params 側に literal として渡っている（psycopg がエスケープ責任を持つ）
    assert payload in params


def test_search_similar_ignores_metadata_filters_when_no_metadata_col() -> None:
    """metadata 列を持たないテーブル (metadata_col=None) では filter を無視する fail-safe。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposals_chunks",
        limit=5,
        metadata_filters={"industry": "飲食"},
        content_col="text",
        metadata_col=None,
    )
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE" not in sql
    assert "飲食" not in sql


def test_search_similar_new_schema_metadata_filters_uses_placeholders() -> None:
    """search_similar_new_schema も metadata_filters を placeholder にバインドする。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    payload = "'; DROP TABLE documents; --"
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        metadata_filters={"client_company": payload},
    )

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    assert "d.metadata->>%s = %s" in sql
    assert payload not in sql
    assert payload in params
    assert "client_company" in params


def test_search_similar_new_schema_filter_industry_uses_placeholder() -> None:
    """本番経路の filter_industry= 引数を injection しても placeholder にバインドされる。

    filter_industry は Router の auto-detection やスラッシュコマンド
    (industry=...) 経由でユーザー入力の影響を受けるため、metadata_filters とは
    別経路として soft / strict 両分岐で値が SQL リテラルに混入しないことを固定する。
    """
    payload = "'; DROP TABLE chunks; --"

    # soft (strict_industry=False, 既定): industry=値 OR NULL の OR 句
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        filter_industry=payload,
    )
    sql_soft: str = cur.execute.call_args.args[0]
    params_soft: list[Any] = list(cur.execute.call_args.args[1])
    assert "d.metadata->>'industry' = %s" in sql_soft
    assert "IS NULL" in sql_soft  # soft 経路は OR NULL を含む
    assert payload not in sql_soft
    assert "DROP TABLE" not in sql_soft
    assert payload in params_soft

    # strict (strict_industry=True): industry=値 のみ
    conn2, cur2 = _mock_conn()
    client.search_similar_new_schema(
        conn=conn2,
        embedding=[0.1] * 1024,
        limit=5,
        filter_industry=payload,
        strict_industry=True,
    )
    sql_strict: str = cur2.execute.call_args.args[0]
    params_strict: list[Any] = list(cur2.execute.call_args.args[1])
    assert "d.metadata->>'industry' = %s" in sql_strict
    assert payload not in sql_strict
    assert "DROP TABLE" not in sql_strict
    assert payload in params_strict
