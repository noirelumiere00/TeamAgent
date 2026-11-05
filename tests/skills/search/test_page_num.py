"""chunk のページ列と旧 metadata を検索・配信出力まで引き継ぐ。"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from teamagent.adapters.pgvector_client import PgVectorClient, _chunk_page_num
from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_search_dates_exposure import _build, _pgvector

PAGE_CASES = [
    (None, 7, 7),
    (None, "12", 12),
    (None, " +12 ", 12),
    (None, None, None),
    (None, "not a page", None),
    (None, "1.5", None),
    (None, "1_000", None),
    (None, "0", None),
    (None, "-2", None),
    (None, True, None),
    (None, [7], None),
    (3, 7, 3),
    (0, 7, None),
]
SEARCH_METHODS = [
    ("search_similar_new_schema", {"embedding": [0.1]}),
    ("list_by_metadata", {"metadata_filters": {"client_name": "顧客"}}),
    ("search_drive_by_client_names", {"client_names": ["顧客"]}),
    ("search_topic_by_terms", {"embedding": [0.1], "topic": "案件決定", "terms": ["顧客"]}),
]


@pytest.mark.parametrize("method,kwargs", SEARCH_METHODS)
@pytest.mark.parametrize("column,metadata,expected", PAGE_CASES)
def test_search_page_num_falls_back_to_chunk_metadata(
    method: str, kwargs: dict[str, Any], column: int | None, metadata: Any, expected: int | None
) -> None:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {
            "chunk_id": 1,
            "content": "資料の本文",
            "score": 0.9,
            "page_num": column,
            "metadata_page_num": (
                json.dumps(metadata)
                if metadata is not None and not isinstance(metadata, str)
                else metadata
            ),
            "title": "資料.pdf",
            "source_type": "gdrive",
            "source_uri": "gdrive://FILE1",
        }
    ]
    hits = getattr(PgVectorClient(dsn="postgresql://stub"), method)(conn, **kwargs)

    sql = cursor.execute.call_args.args[0]
    assert "c.page_num" in sql
    assert "c.metadata->>'page_num' AS metadata_page_num" in sql
    assert hits[0].metadata.get("page_num") == expected

    skill = _build(_pgvector(hits))
    output = skill.run(
        input=SearchInput(query="資料", include_answer=False), ctx=SkillContext(request_id="page")
    )
    assert output.hits[0].page_num == expected
    assert fanout._output_hits(skill, hits, {})[0].page_num == expected


@pytest.mark.parametrize("column,metadata,expected", PAGE_CASES)
def test_legacy_search_page_num_prefers_column(
    column: int | None, metadata: Any, expected: int | None
) -> None:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        {
            "chunk_id": 1,
            "content": "本文",
            "score": 0.9,
            "page_num": column,
            "metadata": {"page_num": metadata},
        }
    ]
    hits = PgVectorClient(dsn="postgresql://stub").search_similar(
        conn, [0.1], table="proposal_chunks", extra_cols=["page_num"]
    )
    assert hits[0].metadata.get("page_num") == expected


@pytest.mark.skipif(
    not os.environ.get("TEAMAGENT_TEST_DB_DSN"), reason="Requires TEAMAGENT_TEST_DB_DSN"
)
@pytest.mark.parametrize("column,metadata,expected", PAGE_CASES)
def test_page_num_postgres(column: int | None, metadata: Any, expected: int | None) -> None:
    with psycopg.connect(os.environ["TEAMAGENT_TEST_DB_DSN"], row_factory=dict_row) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("SET LOCAL search_path TO pg_temp")
            conn.execute("""
                CREATE TEMP TABLE documents (
                    id int, source_uri text, source_type text, title text,
                    metadata jsonb, modified_at timestamptz, ingested_at timestamptz
                ) ON COMMIT DROP
            """)
            conn.execute("""
                CREATE TEMP TABLE chunks (
                    id int, document_id int, chunk_idx int, content text,
                    contextualized text, page_num int, metadata jsonb
                ) ON COMMIT DROP
            """)
            conn.execute("""
                INSERT INTO documents VALUES (
                    1, 'gdrive://FILE1', 'gdrive', '資料.pdf',
                    '{"is_sales_fb":"true","client_name":"顧客"}', now(), now()
                )
            """)
            conn.execute(
                "INSERT INTO chunks VALUES (1, 1, 0, '本文', NULL, %s, %s)",
                (column, Jsonb({"page_num": metadata})),
            )
            # 検索 SQL と同じ取り出し方（列と metadata->>'page_num'）を本物の PostgreSQL で通す。
            # 検索全体の絞り込みは上の偽の接続のテストで確かめる。
            row = conn.execute(
                "SELECT c.page_num, c.metadata->>'page_num' AS metadata_page_num FROM chunks c"
            ).fetchone()
            assert _chunk_page_num(row) == expected
