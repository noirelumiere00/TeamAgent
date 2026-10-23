"""訪問相談の商材語は、本人の接続で題名と全本文チャンクから直接探す。"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient


def _connection() -> tuple[MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [
        {
            "chunk_id": 24,
            "content": "初回説明資料。\nＣＢＤオイルの商品説明を収録。",
            "score": 0.3,
            "page_num": 8,
            "source_uri": "gdrive://ANONYMOUS2024",
            "source_type": "gdrive",
            "title": "匿名企業の初回説明資料",
            "doc_metadata": {"client_name": "匿名企業", "cls_industry": "化粧品"},
            "updated_at": "2024-08-24",
        }
    ]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _patterns(params: list[Any]) -> list[list[str]]:
    return [
        value
        for value in params
        if isinstance(value, list)
        and value
        and all(
            isinstance(item, str) and item.startswith("%") and item.endswith("%") for item in value
        )
    ]


def test_category_search_uses_title_and_all_raw_chunks_on_the_supplied_connection() -> None:
    conn, cursor = _connection()
    pg = PgVectorClient(dsn="postgresql://stub")
    hits = pg.search_topic_by_terms(conn, [0.1], topic=None, terms=["CBDオイル"], limit=30)

    conn.cursor.assert_called_once_with()
    sql, params = cursor.execute.call_args.args
    query = " ".join(sql.lower().split())
    where = query.split("where", 1)[1]
    # 変異: 語の条件を外すと、同業種のヘアケア案件まで候補になる。
    assert re.search(r"(?:i?like|~~\*?)\s+any", where)
    assert "c.content" in where and "d.title" in where and " or " in where
    for column in ("c.content", "d.title"):
        assert re.search(r"normalize\(\s*" + re.escape(column) + r"\s*,\s*nfkc\s*\)", where)
    assert "regexp_replace" in where or "translate(" in where
    assert "chunk_idx" not in where
    assert "->>'topic'" not in where
    assert "->>'client_name'" not in where and "->>'cls_project'" not in where
    assert "->>'industry'" not in where and "->>'cls_industry'" not in where
    assert "d.source_type in ('gdrive', 'other')" in where
    assert "d.source_type = 'gsheets'" in where and "->>'case_corpus' = 'true'" in where
    assert "->>'is_knowledge_share' = 'true'" in where
    assert "->>'suppressed' is distinct from 'true'" in where
    assert re.search(r"c\.content\s+as\s+content", query)
    assert "c.contextualized" not in query
    assert _patterns(params) and all(patterns == ["%cbdオイル%"] for patterns in _patterns(params))
    assert hits[0].content == "初回説明資料。\nＣＢＤオイルの商品説明を収録。"
    assert hits[0].metadata["page_num"] == 8
    assert hits[0].metadata["updated_at"] == "2024-08-24"
    assert hits[0].metadata["client_name"] == "匿名企業"


def test_category_search_limits_documents_after_deduplicating_matching_chunks() -> None:
    conn, cursor = _connection()
    pg = PgVectorClient(dsn="postgresql://stub")
    pg.search_topic_by_terms(conn, [0.1], topic=None, terms=["CBDオイル"], limit=3)

    sql, params = cursor.execute.call_args.args
    query = " ".join(sql.lower().split())
    # 1本の長い資料だけで候補枠を埋めず、資料ごとに本文の一致を残す。
    dedup = re.search(
        r"(?:distinct\s+on\s*\(|partition\s+by\s+|group\s+by\s+)(?:d\.id|c\.document_id)",
        query,
    )
    assert dedup is not None
    assert dedup.start() < query.rindex("limit")
    assert params[-1] == 3


@pytest.mark.parametrize(
    ("term", "pattern"),
    [
        ("ＣＢＤ オイル", "%cbdオイル%"),
        ("cBd・ｵｲﾙ", "%cbdオイル%"),
        ("CBD－オイル", "%cbdオイル%"),
        (" ＣＢＤ ・オイル-A_%\\ ", "%cbdオイルa\\_\\%\\\\%"),
    ],
)
def test_category_terms_normalize_spelling_without_unescaping_like(term: str, pattern: str) -> None:
    conn, cursor = _connection()
    pg = PgVectorClient(dsn="postgresql://stub")
    pg.search_topic_by_terms(conn, [0.1], topic=None, terms=[term])

    _, params = cursor.execute.call_args.args
    patterns = _patterns(params)
    assert patterns and all(value == [pattern] for value in patterns)


def test_existing_topic_search_keeps_its_topic_and_original_term_pattern() -> None:
    conn, cursor = _connection()
    pg = PgVectorClient(dsn="postgresql://stub")
    hits = pg.search_topic_by_terms(conn, [0.1], topic="案件決定", terms=["A%D_K"], limit=3)

    sql, params = cursor.execute.call_args.args
    assert "d.metadata->>'topic' = %s" in sql
    assert params[1] == "案件決定" and params[2] == ["%A\\%D\\_K%"]
    assert "COALESCE(c.contextualized, c.content) AS content" in sql
    assert hits[0].metadata["client_name"] == "匿名企業"


def test_category_search_accepts_configured_cohere_column_and_rejects_sql_identifier() -> None:
    conn, cursor = _connection()
    pg = PgVectorClient(dsn="postgresql://stub")
    pg.search_topic_by_terms(
        conn, [0.1], topic=None, terms=["乳製品"], embedding_col="embedding_cohere"
    )
    assert "c.embedding_cohere <=>" in cursor.execute.call_args.args[0]
    cursor.reset_mock()
    assert not pg.search_topic_by_terms(
        conn, [0.1], topic=None, terms=["乳製品"], embedding_col="x;--"
    )
    cursor.execute.assert_not_called()
