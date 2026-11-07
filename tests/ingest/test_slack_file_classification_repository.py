"""Slack 分類 backfill の取得範囲・競合検査・metadata-only SQL 契約。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.repository import IngestRepository


def repository() -> tuple[IngestRepository, Any]:
    pg = MagicMock()
    cur = (
        pg.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    )
    return IngestRepository(pg, owner_email="ingester@example.com"), cur


def test_slack_classification_candidates_are_bounded_unclassified_files_with_eight_chunks() -> None:
    repo, cur = repository()
    cur.fetchall.return_value = [{"external_id": "F1"}]
    assert repo.get_unclassified_slack_files(limit=7) == [{"external_id": "F1"}]
    sql, params = cur.execute.call_args.args
    assert params == (7,)
    assert "d.source_type = 'other'" in sql
    assert "d.metadata->>'topic' = 'Slack 共有資料'" in sql
    assert "NOT EXISTS" in sql and "left(k.key, 4) = 'cls_'" in sql
    assert "SELECT c.content FROM chunks c" in sql
    assert "WHERE c.document_id = d.id" in sql
    assert "ORDER BY c.chunk_idx LIMIT 8" in sql
    assert "d.xmin::text AS row_version" in sql
    assert "ORDER BY d.id" in sql and "LIMIT %s" in sql
    assert "embedding" not in sql and "contextualized" not in sql


@pytest.mark.parametrize("rowcount", [0, 1])
def test_slack_classification_refresh_merges_only_metadata_and_checks_revision(
    rowcount: int,
) -> None:
    repo, cur = repository()
    cur.rowcount = rowcount
    assert repo.refresh_slack_file_classification(
        "F1",
        expected_row_version="123",
        classification_metadata={"cls_project": "架空\x00食品", "client_name": "架空食品"},
        request_id="test",
    ) is bool(rowcount)
    lock_sql, lock_params = cur.execute.call_args_list[0].args
    assert "pg_advisory_xact_lock" in lock_sql
    assert lock_params == ("F1", "other")
    sql, params = cur.execute.call_args.args
    assert sql.split("SET", 1)[1].split("WHERE", 1)[0].strip() == "metadata = metadata || %s::jsonb"
    assert "source_type = 'other' AND external_id = %s" in sql
    assert "xmin::text = %s" in sql
    assert "metadata->>'topic' = 'Slack 共有資料'" in sql
    assert "NOT EXISTS" in sql and "left(k.key, 4) = 'cls_'" in sql
    assert "chunks" not in sql
    assert params[1:] == ("F1", "123")
    assert json.loads(params[0]) == {"cls_project": "架空食品", "client_name": "架空食品"}


def test_slack_classification_empty_result_does_not_write() -> None:
    repo, cur = repository()
    assert not repo.refresh_slack_file_classification(
        "F1", expected_row_version="123", classification_metadata={}, request_id="test"
    )
    cur.execute.assert_not_called()
