"""``list_documents_with_text``（事例抽出の読み出し）の SQL 契約。

抽出は全文が要るので chunks を結合する。母集団は cls_doc_type で必ず絞り、空の doc_types では
SQL を 1 本も発行しない（全件抽出＝費用事故を防ぐ）。
"""

from __future__ import annotations

from typing import Any

from teamagent.adapters.pgvector_client import PgVectorClient


class _Cursor:
    def __init__(self, owner: _Conn) -> None:
        self._owner = owner

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._owner.sql.append(sql)
        self._owner.params.append(params)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._owner.rows


class _Conn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.sql: list[str] = []
        self.params: list[Any] = []
        self.rows = rows or []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _client() -> PgVectorClient:
    return PgVectorClient("postgresql://user:pw@localhost/db")


def test_empty_doc_types_emits_no_sql() -> None:
    conn = _Conn()
    assert _client().list_documents_with_text(conn, doc_types=[" ", ""]) == []  # type: ignore[arg-type]
    assert conn.sql == []


def test_sql_joins_chunks_and_filters_by_doc_type() -> None:
    conn = _Conn(rows=[{"document_id": "d1", "external_id": "x", "full_text": "本文"}])
    docs = _client().list_documents_with_text(  # type: ignore[arg-type]
        conn, doc_types=["提案書", "報告書", "報告書"], since="2025-01-01", limit=10
    )
    assert docs == [{"document_id": "d1", "external_id": "x", "full_text": "本文"}]
    sql = conn.sql[0]
    assert "string_agg(c.content" in sql
    assert "ORDER BY c.chunk_idx ASC" in sql
    assert "d.metadata->>'cls_doc_type' = ANY(%(doc_types)s)" in sql
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" in sql
    assert "d.metadata->>'stale' IS DISTINCT FROM 'true'" in sql
    assert "d.metadata->>'cls_is_template' IS DISTINCT FROM 'true'" in sql
    assert "(c.metadata->>'boilerplate')::bool" in sql
    assert "d.modified_at >= %(since)s::date" in sql
    assert "d.external_id" in sql and "d.metadata->>'case_external_use'" in sql
    params = conn.params[0]
    assert params["doc_types"] == ["提案書", "報告書"]  # 重複除去
    assert params["since"] == "2025-01-01"
    assert params["limit"] == 10


def test_since_omitted_has_no_date_clause() -> None:
    conn = _Conn()
    _client().list_documents_with_text(conn, doc_types=["報告書"], limit=0)  # type: ignore[arg-type]
    assert "since" not in conn.params[0]
    assert "%(since)s" not in conn.sql[0]
    assert conn.params[0]["limit"] == 1  # 最低 1
