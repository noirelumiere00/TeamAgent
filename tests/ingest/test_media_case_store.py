"""Reuse the repository and RLS identity; keep state out of the vector corpus."""

from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.media_cases import OWNER, MediaCaseStore
from teamagent.ingest.repository import ChunkUpsert, IngestRepository
from teamagent.skills.media_case_search.schema import MediaCaseInput
from teamagent.skills.media_case_search.skill import query_filters, render_hits


def test_campaign_search_keeps_all_scope_members_for_complete_totals():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        {"metadata": {"post_id": "123"}, "score": 0.8}
    ]
    query, filters = query_filters(MediaCaseInput(query="国内 旅行 CP", pr_only=True))
    filters.update(posted_from="2026-01-01", posted_to="2026-10-28")
    result = PgVectorClient("unused").search_media_cases(
        conn, [1.0, 0.0], filters=filters, query=query, embedding_col="embedding_cohere"
    )
    sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert "candidates AS MATERIALIZED" in sql
    assert "d.metadata->>'pr_status'='disclosed'" in sql
    assert "d.metadata->>'corpus'='media_case'" in sql
    assert "LIMIT" not in sql
    assert "campaign_types" not in sql and "jsonb_array_elements" not in sql
    assert "国内" not in sql
    assert params[:2] == ["2026-01-01", "2026-10-28"]
    assert "c.embedding_cohere IS NOT NULL" in sql
    assert result == [{"post_id": "123", "score": 0.8}]
    with pytest.raises(ValueError):
        PgVectorClient("unused").search_media_cases(
            conn, [], filters={}, query="", embedding_col="embedding); DROP TABLE chunks"
        )


def test_domestic_campaign_card_is_first_even_if_unrelated_candidates_arrive():
    _, filters = query_filters(MediaCaseInput(query="国内 旅行 CP"))

    def hit(post_id, **item):
        return {
            "post_id": post_id,
            "score": 0.9,
            "account_handle": "test",
            "post_url": f"https://www.tiktok.com/@test/video/{post_id}",
            "items": [
                {
                    "brand": post_id,
                    "category": "旅行",
                    "campaign_types": ["campaign"],
                    "geography": "domestic",
                    "summary": "国内旅行キャンペーン",
                    **item,
                }
            ],
        }

    message = render_hits(
        [
            hit("海外例", geography="overseas"),
            hit("新商品例", campaign_types=["new_product"]),
            hit("国内CP例"),
        ],
        filters,
    )
    assert "国内CP例" in message
    assert "海外例" not in message
    assert "関連では" in message and "新商品例" in message
    assert "PR 不明" in message and "選択" not in message


def test_storage_acl_state_no_chunks_and_embedding_column(monkeypatch):
    monkeypatch.setattr(
        "teamagent.ingest.media_cases.shared_company_domains_from_env",
        lambda: frozenset({"example.com"}),
    )
    monkeypatch.setenv("EMBEDDING_COLUMN", "embedding_cohere")
    pg, embedder = MagicMock(), MagicMock()
    store = MediaCaseStore(pg, embedder)
    store.column = "embedding_cohere"
    store.repository = MagicMock()
    store.save_state("test", {"run_id": "run-1"})
    doc, chunks = store.repository.upsert_document_with_chunks.call_args.args
    assert doc.source_type == "other" and doc.owner_email == OWNER
    assert doc.metadata["corpus"] == "media_case_state" and not chunks and not doc.acl_groups
    metadata = {"post_id": "1", "title": "企業", "post_url": "https://www.tiktok.com/@test/video/1"}
    store.save_post(metadata, "国内旅行キャンペーン", "test")
    doc, chunks = store.repository.upsert_document_with_chunks.call_args.args
    assert doc.acl_groups == ["example.com"] and doc.metadata["corpus"] == "media_case"
    assert chunks[0].embedding_column == "embedding_cohere"
    embedder.embed_passage.assert_called_once_with("国内旅行キャンペーン")
    store.save_post(metadata, "", "test")
    assert not store.repository.upsert_document_with_chunks.call_args.args[1]
    identity = {
        "identity_verified": True,
        "user_email": "a@example.com",
        "user_groups": ["example.com"],
    }
    store.search("旅行", {}, identity, 3)
    assert pg.connection.call_args.kwargs == {
        "app_role": "teamagent_app",
        "user_email": "a@example.com",
        "user_groups": ["example.com"],
        "user_role": "member",
    }
    with pytest.raises(PermissionError):
        store.search("旅行", {}, {"user_email": "a@example.com"}, 3)


@pytest.mark.parametrize("column", ["embedding", "embedding_cohere", "evil_column"])
def test_repository_only_writes_allowed_vector_column(column):
    conn = MagicMock()
    chunk = ChunkUpsert(chunk_idx=0, content="text", embedding=[1.0], embedding_column=column)
    if column == "evil_column":
        with pytest.raises(ValueError):
            IngestRepository._insert_chunks(conn, "doc", [chunk])
        conn.cursor.return_value.__enter__.return_value.execute.assert_not_called()
    else:
        IngestRepository._insert_chunks(conn, "doc", [chunk])
        sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        assert f"contextualized, {column}, page_num" in sql


@pytest.mark.parametrize("acquired", [True, False])
def test_worker_lock_is_dedicated_session_and_released_on_failure(monkeypatch, acquired):
    pg, conn = MagicMock(), MagicMock()
    pg.dsn = "unused"
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {"acquired": acquired}
    constructor = MagicMock()
    constructor.return_value.connection.return_value = nullcontext(conn)
    monkeypatch.setattr("teamagent.ingest.media_cases.PgVectorClient", constructor)
    store = MediaCaseStore(pg, MagicMock())
    if acquired:
        with pytest.raises(RuntimeError):
            with store.worker_lock() as alive:
                alive()
                raise RuntimeError("worker interrupted")
        assert conn.execute.call_args.args[0] == "SELECT pg_advisory_unlock(1847264301)"
    else:
        with store.worker_lock() as alive:
            assert alive is None
        conn.execute.assert_not_called()
    constructor.assert_called_once_with("unused")
    pg.connection.assert_not_called()
