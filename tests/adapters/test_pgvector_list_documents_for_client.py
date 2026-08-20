"""PgVectorClient.list_documents_for_client() の SQL ビルダー単体テスト（実 DB 0）。

契約（カルテページ/Vault エクスポータの土台）:
- modified_at を 'YYYY-MM-DD' で射影する（list_documents_for_graph に無かった穴埋め）
- suppressed（dedup 非正本）と is_sales_fb（FB は timeline 側で出す）を除外する
- cls_project / client_name / title の ILIKE 部分一致で SQL 側絞り込み
  （like pattern は placeholder bind・injection 安全）
- LIKE メタ文字（% _ \\）はエスケープして ESCAPE '\\' を付ける。この結果は
  clientkarte の**自動 DM 添付の候補選定**に使われるため、``client_name='%'`` が
  「会社中の新しい資料 50 件」に化けると他社資料がそのまま送られる
- modified_at DESC NULLS LAST（新しい資料優先）
- 空白のみの client_name は SQL を発行せず [] を即返す
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


def test_projects_modified_at_and_classification_axes() -> None:
    """modified_at('YYYY-MM-DD') と cls_* / client_name / excerpt が SELECT に含まれる。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "出光興産")
    sql: str = cur.execute.call_args.args[0]
    assert "to_char(d.modified_at AT TIME ZONE 'Asia/Tokyo', 'YYYY-MM-DD') AS modified_at" in sql
    assert "d.metadata->>'cls_industry' AS cls_industry" in sql
    assert "d.metadata->>'cls_doc_type' AS cls_doc_type" in sql
    assert "d.metadata->>'cls_solution' AS cls_solution" in sql
    assert "d.metadata->>'client_name' AS client_name" in sql
    assert "ex.excerpt AS excerpt" in sql


def test_where_excludes_suppressed_and_sales_fb() -> None:
    """suppressed と is_sales_fb を IS DISTINCT FROM 'true' で除外する。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "出光興産")
    sql: str = cur.execute.call_args.args[0]
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" in sql
    assert "d.metadata->>'is_sales_fb' IS DISTINCT FROM 'true'" in sql


def test_client_match_is_bound_ilike_on_three_columns() -> None:
    """cls_project / client_name / title の ILIKE 部分一致・like は placeholder bind。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "日本ガイシ", limit=7)
    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = cur.execute.call_args.args[1]
    assert "d.metadata->>'cls_project' ILIKE %s" in sql
    assert "d.metadata->>'client_name' ILIKE %s" in sql
    assert "d.title ILIKE %s" in sql
    # クライアント名そのものは SQL 文字列に埋め込まない（injection 安全）
    assert "日本ガイシ" not in sql
    assert params == ["%日本ガイシ%", "%日本ガイシ%", "%日本ガイシ%", 7]


def test_orders_by_modified_at_desc_nulls_last() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "出光興産")
    sql: str = cur.execute.call_args.args[0]
    assert "ORDER BY d.modified_at DESC NULLS LAST" in sql


def test_excerpt_lateral_deprioritizes_boilerplate() -> None:
    """excerpt は list_documents_for_graph と同じ「テンプレでない最小 chunk」LATERAL。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "出光興産")
    sql: str = cur.execute.call_args.args[0]
    assert sql.count("LEFT JOIN LATERAL") == 1
    lateral = sql.split("LEFT JOIN LATERAL", 1)[1].split(") ex ON true", 1)[0]
    assert "COALESCE((c.metadata->>'boilerplate')::bool, false)" in lateral
    assert "COALESCE((c.metadata->>'title_only')::bool, false)" in lateral
    assert "LIMIT 1" in lateral


def test_blank_client_returns_empty_without_query() -> None:
    """空白のみの client_name は SQL を発行しない（list_client_timeline と同じ契約）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    assert client.list_documents_for_client(conn, "   ") == []
    cur.execute.assert_not_called()


def test_returns_rows_as_plain_dicts() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    row = {
        "title": "出光興産様向け提案書",
        "source_uri": "gdrive://F1",
        "source_type": "gdrive",
        "modified_at": "2026-06-01",
        "cls_industry": "エネルギー",
        "cls_project": "出光興産",
        "cls_doc_type": "提案書",
        "cls_solution": "動画広告",
        "cls_budget": None,
        "cls_target": None,
        "client_name": None,
        "excerpt": "抜粋",
    }
    conn, _ = _mock_conn([row])
    docs = client.list_documents_for_client(conn, "出光興産")
    assert docs == [row]
    assert isinstance(docs[0], dict)


def test_like_wildcards_in_client_name_are_escaped() -> None:
    """``%`` / ``_`` / ``\\`` を素通ししない（ワイルドカードで全社資料を掴ませない）。

    この行の結果は clientkarte が自動 DM 添付の候補選定に使う。素通しだと
    ``client_name='%'`` だけで他社資料が「関連資料」として送られてしまう。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "%")
    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = cur.execute.call_args.args[1]

    assert sql.count("ESCAPE '\\'") == 3
    assert params[0] == "%\\%%"  # 中央の % はリテラル化されている
    assert params[0] == params[1] == params[2]


def test_like_escape_covers_underscore_and_backslash() -> None:
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "a_b\\c")
    params: list[Any] = cur.execute.call_args.args[1]

    assert params[0] == "%a\\_b\\\\c%"


def test_ordinary_client_name_is_unchanged_by_escaping() -> None:
    """エスケープが通常の社名の検索結果を変えない（既存挙動の回帰防止）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.list_documents_for_client(conn, "日本ガイシ")
    params: list[Any] = cur.execute.call_args.args[1]

    assert params[0] == "%日本ガイシ%"
