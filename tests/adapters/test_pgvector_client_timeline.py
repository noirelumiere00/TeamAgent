"""PgVectorClient.list_client_timeline() の SQL ビルダー単体テスト。

client_name は placeholder 化され SQL injection から保護されること、
is_sales_fb 限定と時系列 (modified_at 昇順) 順序を固定する。
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


def test_blank_client_returns_empty_without_db() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn = MagicMock()
    assert client.list_client_timeline(conn=conn, client_name="  ") == []
    conn.cursor.assert_not_called()


def test_client_name_bound_as_like_placeholder() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    malicious = "'; DROP TABLE chunks; --"
    client.list_client_timeline(conn=conn, client_name=malicious, limit=20)

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    assert "d.metadata->>'client_name' LIKE %s" in sql
    assert "DROP TABLE" not in sql
    assert f"%{malicious}%" in params


def test_limits_to_sales_fb_and_orders_chronologically() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_client_timeline(conn=conn, client_name="日本ガイシ")

    sql: str = cur.execute.call_args.args[0]
    assert "d.metadata->>'is_sales_fb' = 'true'" in sql
    assert "ORDER BY d.modified_at ASC" in sql


def test_projects_gsheet_reaction_and_shared_memo_into_hit_metadata() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn(
        [
            {
                "chunk_id": 1,
                "content": "フォーム回答本文",
                "occurred_at": "2026-07-01",
                "source_uri": "gsheet://sheet/row/2",
                "title": "営業FB",
                "client_name": "日本ガイシ",
                "client_reaction": "前向きだが予算を懸念",
                "shared_memo": "決裁者同席で次回提案",
            }
        ]
    )

    hits = client.list_client_timeline(conn=conn, client_name="日本ガイシ")

    sql: str = cur.execute.call_args.args[0]
    assert "d.metadata->>'client_reaction' AS client_reaction" in sql
    assert "d.metadata->>'shared_memo' AS shared_memo" in sql
    assert hits[0].metadata["client_reaction"] == "前向きだが予算を懸念"
    assert hits[0].metadata["shared_memo"] == "決裁者同席で次回提案"
