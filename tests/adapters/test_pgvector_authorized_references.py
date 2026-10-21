"""出典の閲覧可否は、呼び出し元の RLS 接続で資料の実体を引き直して確認する。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from teamagent.adapters.pgvector_client import PgVectorClient


def _connection(rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection, cursor


def test_empty_references_do_not_query() -> None:
    conn = MagicMock()

    assert PgVectorClient(dsn="postgresql://stub").list_authorized_document_references(conn) == []
    conn.cursor.assert_not_called()


def test_reference_ids_are_bound_and_drive_ids_are_source_scoped() -> None:
    conn, cursor = _connection([])
    document_id = "not-a-uuid'; SELECT 1; --"
    uri = "https://drive.google.com/file/d/FILE_A/view"

    assert (
        PgVectorClient(dsn="postgresql://stub").list_authorized_document_references(
            conn,
            document_ids=[document_id],
            drive_file_ids=["FILE_A"],
            source_uris=[uri],
        )
        == []
    )

    sql, params = cursor.execute.call_args.args
    assert document_id not in sql
    assert uri not in sql
    assert "d.id::text = ANY(%s)" in sql
    assert "d.source_type = 'gdrive' AND d.external_id = ANY(%s)" in sql
    assert "d.source_uri = ANY(%s)" in sql
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" in sql
    assert params == [[document_id], ["FILE_A"], [uri]]


def test_returns_only_rows_from_supplied_rls_connection() -> None:
    row = {
        "id": "document-a",
        "document_id": "document-a",
        "title": "shared_material.pdf",
        "source_uri": "gdrive://FILE_A",
        "source_type": "gdrive",
        "external_id": "FILE_A",
        "metadata": {"mime_type": "application/pdf"},
    }
    conn, cursor = _connection([row])
    client = PgVectorClient(dsn="postgresql://stub")

    with patch.object(client, "connection") as reconnect:
        result = client.list_authorized_document_references(
            conn, drive_file_ids=["FILE_A", "INACCESSIBLE_FILE"]
        )

    assert result == [row]
    reconnect.assert_not_called()
    sql = cursor.execute.call_args.args[0]
    for column in ("d.id::text AS id", "d.id::text AS document_id", "d.title", "d.metadata"):
        assert column in sql
