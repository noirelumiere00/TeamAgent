"""PgVectorClient.list_documents_for_graph() の SQL ビルダー単体テスト。

実 DB を使わず cursor をモックして、(1) 第2世代分類軸 cls_solution/cls_budget/
cls_target が SELECT に射影されること、(2) with_embeddings フラグが代表ベクトルの
LATERAL JOIN を出し入れし、取得した embedding を float の list に正規化することを固定する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient


def _mock_conn(rows: list[dict[str, Any]] | None = None) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.fetchall.return_value = rows or []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_select_projects_new_classification_axes() -> None:
    """cls_solution / cls_budget / cls_target が SELECT に含まれる（L2 射影）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_graph(conn)
    sql: str = cur.execute.call_args.args[0]
    assert "d.metadata->>'cls_solution' AS cls_solution" in sql
    assert "d.metadata->>'cls_budget' AS cls_budget" in sql
    assert "d.metadata->>'cls_target' AS cls_target" in sql
    # 既存軸も維持
    assert "d.metadata->>'cls_industry' AS cls_industry" in sql
    assert "d.metadata->>'cls_project' AS cls_project" in sql


def test_default_no_embedding_column_or_join() -> None:
    """既定（with_embeddings=False）では embedding 列も LATERAL も出さない（旧挙動）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_graph(conn)
    sql: str = cur.execute.call_args.args[0]
    assert "emb.embedding AS embedding" not in sql
    # excerpt 用 LATERAL は常にあるが、embedding 用 LATERAL は無い
    assert "c.embedding" not in sql


def test_with_embeddings_adds_lateral_join() -> None:
    """with_embeddings=True で代表ベクトル（全チャンクの平均）を LATERAL で取る。

    先頭チャンク（表紙＝テンプレ）ではなく AVG(c.embedding) を使い、共通テンプレを
    施策チャンクで希釈する（concept edges が表紙の同一性で誤結合しないため）。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_graph(conn, with_embeddings=True)
    sql: str = cur.execute.call_args.args[0]
    assert "emb.embedding AS embedding" in sql
    assert "AVG(c.embedding)" in sql
    # 代表ベクトルの LATERAL は先頭 1 件に限定しない（全チャンク平均）。
    assert "SELECT c.embedding" not in sql


def test_with_embeddings_excludes_boilerplate_from_avg() -> None:
    """代表ベクトルの AVG はテンプレ chunk（boilerplate=true）を除外する。

    表紙/会社紹介などの定型 chunk を平均から外し、concept edges が共通テンプレで
    誤結合するのを防ぐ。フラグが無ければ COALESCE(...,false)=false が全 chunk で真に
    なるので旧挙動と同一（後方互換）。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_graph(conn, with_embeddings=True)
    sql: str = cur.execute.call_args.args[0]
    # AVG の LATERAL に boilerplate 除外句が入る
    assert "AVG(c.embedding)" in sql
    assert "COALESCE((c.metadata->>'boilerplate')::bool, false) = false" in sql


def test_default_no_boilerplate_clause() -> None:
    """with_embeddings=False（既定）では boilerplate 句は一切出ない（旧挙動）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_graph(conn)
    sql: str = cur.execute.call_args.args[0]
    assert "boilerplate" not in sql


def test_with_embeddings_normalizes_to_float_list() -> None:
    """取得した embedding を素の float list に正規化して行に乗せる。"""
    client = PgVectorClient(dsn="postgresql://stub")
    row = {
        "node_id": 1,
        "title": "doc",
        "source_uri": "gdrive://1",
        "source_type": "gdrive",
        "cls_industry": None,
        "cls_project": None,
        "cls_doc_type": None,
        "cls_solution": None,
        "cls_budget": None,
        "cls_target": None,
        "client_name": None,
        "excerpt": None,
        "embedding": (0.1, 0.2, 0.3),  # tuple/np 配列を想定
    }
    conn, _ = _mock_conn([row])
    docs = client.list_documents_for_graph(conn, with_embeddings=True)
    assert docs[0]["embedding"] == [0.1, 0.2, 0.3]
    assert all(isinstance(x, float) for x in docs[0]["embedding"])


def test_excludes_suppressed_documents_unconditionally() -> None:
    """非正本（dedup の suppressed=true）doc をグラフのノードから無条件で除外する。

    何も suppressed されていなければ IS DISTINCT FROM 'true' が全 doc で真になり
    no-op（後方互換）。with_embeddings の有無に依らず常に句が入る。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    # 既定（with_embeddings=False）
    conn, cur = _mock_conn()
    client.list_documents_for_graph(conn)
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE d.metadata->>'suppressed' IS DISTINCT FROM 'true'" in sql

    # with_embeddings=True でも句は維持される
    conn2, cur2 = _mock_conn()
    client.list_documents_for_graph(conn2, with_embeddings=True)
    sql2: str = cur2.execute.call_args.args[0]
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" in sql2


def test_with_embeddings_handles_null_embedding() -> None:
    """embedding が NULL（チャンク無し doc）でも壊れない。"""
    client = PgVectorClient(dsn="postgresql://stub")
    row = {
        "node_id": 1,
        "title": "doc",
        "source_uri": None,
        "source_type": None,
        "cls_industry": None,
        "cls_project": None,
        "cls_doc_type": None,
        "cls_solution": None,
        "cls_budget": None,
        "cls_target": None,
        "client_name": None,
        "excerpt": None,
        "embedding": None,
    }
    conn, _ = _mock_conn([row])
    docs = client.list_documents_for_graph(conn, with_embeddings=True)
    assert docs[0]["embedding"] is None
