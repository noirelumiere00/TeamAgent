"""Normalization/counting without a database; actual RLS/SQL lives in connect_web tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient


def test_clients_merge_before_count_sort_and_page() -> None:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {"name": f"顧客{i:03}", "is_fb": False, "fb_date": "2099-01-01"}
        for i in reversed(range(51))
    ] + [
        {"name": "株式会社 顧客000 様", "is_fb": False, "fb_date": None},
        {"name": "顧客000", "is_fb": True, "fb_date": "2026-01-01"},
        {"name": "顧客000", "is_fb": True, "fb_date": "2026-02-01"},
        {"name": "顧客001", "is_fb": True, "fb_date": "2026-02-01"},
        {"name": "FBのみ", "is_fb": True, "fb_date": None},
        {"name": None, "is_fb": False, "fb_date": None},
        {"name": " \t ", "is_fb": False, "fb_date": None},
    ]
    pg = PgVectorClient("fixture")
    rows, total = pg.list_knowledge_clients(conn, limit=2)
    assert total == 52
    assert rows == [
        {"name": "株式会社 顧客000 様", "document_count": 2, "latest_fb_at": "2026-02-01"},
        {"name": "顧客001", "document_count": 1, "latest_fb_at": "2026-02-01"},
    ]
    rows, total = pg.list_knowledge_clients(conn, offset=50)
    assert total == 52
    assert [row["name"] for row in rows] == ["顧客049", "顧客050"]
    assert pg.list_knowledge_clients(conn, offset=100) == ([], 52)
    # The visible input has no LIMIT, and is independent of chunk multiplicity.
    query = cursor.execute.call_args.args[0]
    assert "LIMIT" not in query and "JOIN chunks" not in query


def test_documents_empty_page_keeps_total_and_stable_id() -> None:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    pg = PgVectorClient("fixture")
    cursor.fetchall.return_value = [{"id": None, "total": 51}]
    assert pg.list_knowledge_documents(conn, offset=100) == ([], 51)
    assert cursor.execute.call_args.args[1] == [50, 100]
    cursor.fetchall.return_value = [{"id": None, "total": 0}]
    assert pg.list_knowledge_documents(conn) == ([], 0)
    row: dict[str, Any] = {"id": "stable-uuid", "title": "資料", "total": 51}
    cursor.fetchall.return_value = [row]
    assert pg.list_knowledge_documents(conn) == ([{"id": "stable-uuid", "title": "資料"}], 51)


def test_tag_filter_whitelist_and_parameterized_exact_value() -> None:
    import pytest

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"id": None, "total": 0}]
    pg = PgVectorClient("fixture")
    value = "50%_O'Reilly"
    assert pg.list_knowledge_documents(conn, tag=("industry", value), offset=50) == ([], 0)
    query, params = cursor.execute.call_args.args
    assert "WHERE industry = %s" in query and value not in query
    assert params == [value, 50, 50]
    with pytest.raises(ValueError, match="invalid knowledge tag axis"):
        pg.list_knowledge_documents(conn, tag=("industry; DROP TABLE documents", value))
    cursor.fetchone.return_value = None
    assert pg.get_knowledge_document(conn, "12345678-1234-1234-abcd-123456789abc") is None
    assert cursor.execute.call_args.args[1] == ["12345678-1234-1234-abcd-123456789abc"]


def test_operator_values_are_bound_and_like_metacharacters_are_escaped() -> None:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"id": None, "total": 0}]
    pg = PgVectorClient("fixture")
    pg.list_knowledge_documents(conn, q=r"file:50%_売上\案 -tag:秘密", quick="proposal")
    query, params = cursor.execute.call_args.args
    assert r"50%_売上\案" not in query and "秘密" not in query and "提案書" not in query
    assert params == ["提案書", r"%50\%\_売上\\案%", "秘密", "秘密", "秘密", "秘密", 50, 0]
    assert "FROM chunks" not in query  # metadata/name filters do not load chunk text
    pg.list_knowledge_documents(conn, q="本文", quick="recent")
    query, params = cursor.execute.call_args.args
    assert "FROM chunks" in query and "normalize(COALESCE(excerpt" in query
    assert params == [30, "%本文%", "%本文%", "%本文%", "%本文%", "%本文%", 50, 0]
