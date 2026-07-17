"""PgVectorClient.list_client_timeline_recent() の SQL ビルダー単体テスト（実 DB 0）。

契約（カルテページ/Vault エクスポータの「最新 N 件」土台）:
- list_client_timeline（ASC LIMIT＝FB 多数クライアントで【最古の N 件】になる）の
  修正版として、modified_at DESC NULLS LAST（同日内 chunk_idx DESC）+ LIMIT で
  【最新 N 件】を取得する
- 取得後に Python 側で反転し、既存契約どおり【古い順】（timeline[-1]＝最新）で返す
- 射影列は list_client_timeline と同一（deal_phase / bant_score / occurred_at 等）
- client_name は placeholder bind（SQL injection 安全）・空白のみは SQL を発行せず []
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


def _fb_row(occurred_at: str, chunk_id: int, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "content": f"{occurred_at} の商談メモ",
        "occurred_at": occurred_at,
        "source_uri": f"slack://C1/{occurred_at}",
        "title": "営業FB",
        "client_name": "出光興産",
        "deal_phase": None,
        "bant_score": None,
        "channel_type": None,
        "positive_reaction": None,
        "negative_reaction": None,
        "next_action": None,
        "proposed_menu": None,
    }
    row.update(over)
    return row


def test_orders_desc_nulls_last_with_limit() -> None:
    """DESC NULLS LAST + chunk_idx DESC + LIMIT（最新 N 件を取り、NULL 日付は最初に落ちる）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_client_timeline_recent(conn, "出光興産", limit=50)
    sql: str = cur.execute.call_args.args[0]
    assert "ORDER BY d.modified_at DESC NULLS LAST, c.chunk_idx DESC" in sql
    assert "LIMIT %s" in sql


def test_projects_same_columns_as_list_client_timeline() -> None:
    """射影列は list_client_timeline と同一（カルテ側のメタ合成が同じ形で動く）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_client_timeline_recent(conn, "出光興産")
    sql: str = cur.execute.call_args.args[0]
    for col in (
        "to_char(d.modified_at AT TIME ZONE 'Asia/Tokyo', 'YYYY-MM-DD') AS occurred_at",
        "d.metadata->>'deal_phase' AS deal_phase",
        "d.metadata->>'bant_score' AS bant_score",
        "d.metadata->>'channel_type' AS channel_type",
        "d.metadata->>'next_action' AS next_action",
        "d.metadata->>'proposed_menu' AS proposed_menu",
    ):
        assert col in sql
    assert "d.metadata->>'is_sales_fb' = 'true'" in sql


def test_client_name_is_bound_not_embedded() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_client_timeline_recent(conn, "日本ガイシ", limit=5)
    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = cur.execute.call_args.args[1]
    assert "日本ガイシ" not in sql  # injection 安全（placeholder bind）
    assert params == ["%日本ガイシ%", 5]


def test_reverses_desc_rows_to_oldest_first() -> None:
    """DB からは新しい順で届くが、返り値は古い順（timeline[-1]＝最新の契約）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    rows = [
        _fb_row("2026-06-15", 3, deal_phase="提案", bant_score="B（前向き）"),
        _fb_row("2026-06-01", 2, deal_phase="ヒアリング"),
        _fb_row("2026-05-01", 1, deal_phase="初回接触"),
    ]
    conn, _ = _mock_conn(rows)
    hits = client.list_client_timeline_recent(conn, "出光興産", limit=50)
    assert [h.metadata["occurred_at"] for h in hits] == [
        "2026-05-01",
        "2026-06-01",
        "2026-06-15",
    ]
    latest = hits[-1]
    assert latest.metadata["deal_phase"] == "提案"
    assert latest.metadata["bant_score"] == "B（前向き）"
    assert latest.metadata["is_sales_fb"] is True
    assert latest.metadata["source_type"] == "slack"


def test_blank_client_returns_empty_without_query() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    assert client.list_client_timeline_recent(conn, "   ") == []
    cur.execute.assert_not_called()
