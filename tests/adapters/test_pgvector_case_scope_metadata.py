"""検索境界で事例の既知業種と取引先名を落とさない（10-16 実機退行）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient

_INDUSTRY_KEYS = (
    "campaign_industry",
    "case_industry",
    "cls_industry",
    "industry",
    "client_industry",
    "advertiser_industry",
)


def _connection() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {
            "chunk_id": 42,
            "content": "施策の本文",
            "score": 0.9,
            "source_type": "gdrive",
            "source_uri": "gdrive://CASE",
            "title": "事例.pdf",
            "client_name": "伊藤ハム",
            "cls_project": "伊藤ハム",
            **dict.fromkeys(_INDUSTRY_KEYS, "食品"),
        }
    ]
    return conn, cur


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("search_similar_new_schema", {"embedding": [0.1]}),
        ("search_drive_by_client_names", {"client_names": ["伊藤ハム"]}),
        ("list_by_metadata", {"metadata_filters": {"bant_score": "A"}}),
        ("list_client_timeline", {"client_name": "伊藤ハム"}),
        ("list_client_timeline_recent", {"client_name": "伊藤ハム"}),
    ],
)
def test_case_scope_industry_and_owner_survive_search_boundary(
    method: str, kwargs: dict[str, Any]
) -> None:
    conn, cur = _connection()
    hits = getattr(PgVectorClient(dsn="postgresql://stub"), method)(conn, **kwargs)
    sql = cur.execute.call_args.args[0]
    for key in _INDUSTRY_KEYS:
        assert f"metadata->>'{key}' AS {key}" in sql
        assert hits[0].metadata[key] == "食品"
    assert hits[0].metadata["client_name"] == "伊藤ハム"
    if method == "search_drive_by_client_names":
        assert hits[0].metadata["cls_project"] == "伊藤ハム"


def test_campaign_file_candidates_keep_existing_industry_metadata() -> None:
    conn, cur = _connection()
    cur.fetchall.return_value[0]["advertiser_idx"] = 1
    rows = PgVectorClient(dsn="postgresql://stub").find_drive_files_for_advertisers(
        conn, ["伊藤ハム"]
    )
    sql = cur.execute.call_args.args[0]
    for key in _INDUSTRY_KEYS:
        assert f"metadata->>'{key}' AS {key}" in sql
        assert rows[0][key] == "食品"


def test_client_industry_lookup_skips_empty_classification_values() -> None:
    conn, cur = _connection()
    cur.fetchone.return_value = {"industry": "食品"}
    assert (
        PgVectorClient(dsn="postgresql://stub").get_industry_for_client(conn, "伊藤ハム") == "食品"
    )
    sql = cur.execute.call_args.args[0]
    for key in ("case_industry", "cls_industry"):
        assert f"NULLIF(d.metadata->>'{key}', '')" in sql
