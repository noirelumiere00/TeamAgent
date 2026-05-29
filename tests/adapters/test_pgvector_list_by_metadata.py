"""PgVectorClient.list_by_metadata() の SQL ビルダー単体テスト。

集約・一覧クエリ用のメタデータ列挙。フィルタ key/value は LIKE placeholder に
bind され SQL injection から保護されること、is_sales_fb 限定と recency 順序を固定する。
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


def test_empty_filters_returns_empty_without_db() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn = MagicMock()
    assert client.list_by_metadata(conn=conn, metadata_filters={}) == []
    conn.cursor.assert_not_called()


def test_filters_use_like_placeholders_not_interpolated() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    malicious = "'; DROP TABLE documents; --"
    client.list_by_metadata(conn=conn, metadata_filters={"bant_score": malicious}, limit=5)

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    assert "d.metadata->>%s LIKE %s" in sql
    assert "DROP TABLE" not in sql
    # key はそのまま、value は %...% で bind
    assert "bant_score" in params
    assert f"%{malicious}%" in params


def test_limits_to_sales_fb_and_orders_by_recency() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_by_metadata(conn=conn, metadata_filters={"channel_type": "代理店"})

    sql: str = cur.execute.call_args.args[0]
    assert "d.metadata->>'is_sales_fb' = 'true'" in sql
    assert "ORDER BY d.modified_at DESC" in sql
