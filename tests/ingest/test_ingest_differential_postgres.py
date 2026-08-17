"""差分取り込み（``INGEST_DIFFERENTIAL``）の実 SQL round-trip 検証。

``TEAMAGENT_TEST_DB_DSN`` が明示された disposable PostgreSQL だけで実行する
（tests/ingest/test_ingest_retry_claim_cap_postgres.py と同方式）。

2 階層で検証する:

1. ``get_document_content_hashes`` の実 SQL（documents の実 jsonb 照合）
   — pgvector 拡張が無い CI（postgres:16-alpine）でも走る
2. handler → 実 upsert → 実照合 → スキップの end-to-end
   「同一内容の 2 回目 run は Bedrock 呼び出しゼロ」
   — chunks.embedding が vector(1024) のため pgvector 拡張が必要。無い環境では
     skip される（ロジック自体は tests/ingest/test_ingest_differential.py が
     フェイクで全環境検証している）
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.content_hash import INGEST_CONTENT_HASH_KEY
from teamagent.ingest.loader import GSheetSpec, GSheetsTabSpec
from teamagent.ingest.pipeline import _IngestUnchangedCollector
from teamagent.ingest.repository import IngestRepository

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")

pytestmark = pytest.mark.skipif(
    _DB_DSN is None,
    reason="disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN",
)


class _TransactionPgVector:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        yield self._conn


_DOCUMENTS_DDL = """
    CREATE TABLE documents (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source_type     document_source_type NOT NULL,
        source_uri      TEXT,
        external_id     TEXT NOT NULL,
        title           TEXT,
        owner_email     TEXT NOT NULL,
        acl_emails      TEXT[] NOT NULL DEFAULT '{}',
        acl_groups      TEXT[] NOT NULL DEFAULT '{}',
        client_code     TEXT,
        metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
        modified_at     TIMESTAMPTZ,
        ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT documents_source_external_unique UNIQUE (source_type, external_id)
    )
"""

# infra/migrations/0001 の chunks と同型（embedding は pgvector の vector(1024)）。
_CHUNKS_DDL = """
    CREATE TABLE chunks (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        chunk_idx       INT NOT NULL,
        content         TEXT NOT NULL,
        contextualized  TEXT,
        embedding       vector(1024),
        page_num        INT,
        metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
    )
"""


def _prepare_schema(conn: Any) -> None:
    """disposable schema に enum + documents を作る（migration 0001 の必要部分と同型）。

    実 migration 0001 をそのまま流さないのは、(a) ``CREATE EXTENSION vector`` が
    CI の postgres:16-alpine に無い、(b) enum への 'gsheets' 追加（migration 0004 の
    ``ALTER TYPE ... ADD VALUE``）が同一 transaction 内では使用不能、のため。
    新規 CREATE TYPE なら同一 transaction 内でも使える。
    """
    from psycopg import sql

    schema = f"ingest_differential_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cur.execute(
            """
            CREATE TYPE document_source_type AS ENUM
                ('pdf', 'gdrive', 'gmail', 'slack', 'other', 'gsheets')
            """
        )
        cur.execute(_DOCUMENTS_DDL)


@contextmanager
def _repository(*, with_chunks: bool = False) -> Iterator[tuple[Any, IngestRepository]]:
    import psycopg

    assert _DB_DSN is not None
    if with_chunks:
        # vector 型は DB 全体の拡張なので、schema 準備前に別接続（autocommit）で入れる。
        # 拡張が無い環境（CI の postgres:16-alpine）は end-to-end だけ skip する。
        try:
            with psycopg.connect(_DB_DSN, autocommit=True) as bootstrap:
                bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error:
            pytest.skip("pgvector extension unavailable (postgres:16-alpine 等)")
    with psycopg.connect(_DB_DSN) as conn:
        _prepare_schema(conn)
        if with_chunks:
            with conn.cursor() as cur:
                cur.execute(_CHUNKS_DDL)
        repository = IngestRepository(
            _TransactionPgVector(conn),  # type: ignore[arg-type]
            app_role=None,
        )
        yield conn, repository
        conn.rollback()


def _seed_document(
    conn: Any,
    *,
    source_type: str,
    external_id: str,
    metadata_json: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (source_type, external_id, owner_email, metadata)
            VALUES (%s::document_source_type, %s, 'seed@x.jp', %s::jsonb)
            """,
            (source_type, external_id, metadata_json),
        )


# -----------------------------------------------------------
# 1) get_document_content_hashes の実 SQL（CI でも走る）
# -----------------------------------------------------------
def test_get_document_content_hashes_returns_only_stored_hashes() -> None:
    with _repository() as (conn, repository):
        _seed_document(
            conn,
            source_type="gsheets",
            external_id="sheet:1:2",
            metadata_json=f'{{"{INGEST_CONTENT_HASH_KEY}": "aaa111", "tab_name": "t"}}',
        )
        _seed_document(
            conn,
            source_type="gsheets",
            external_id="sheet:1:3",
            metadata_json='{"tab_name": "t"}',  # hash 未保存（差分化以前の既存 doc）
        )
        _seed_document(
            conn,
            source_type="slack",
            external_id="sheet:1:2",  # 同じ external_id でも source_type が違えば別物
            metadata_json=f'{{"{INGEST_CONTENT_HASH_KEY}": "bbb222"}}',
        )

        result = repository.get_document_content_hashes(
            "gsheets",
            ["sheet:1:2", "sheet:1:3", "sheet:1:999"],  # 999 は未登録
        )
        assert result == {"sheet:1:2": "aaa111"}

        assert repository.get_document_content_hashes("slack", ["sheet:1:2"]) == {
            "sheet:1:2": "bbb222"
        }
        assert repository.get_document_content_hashes("gsheets", []) == {}


def test_get_document_content_hashes_strips_nul_in_requested_ids() -> None:
    with _repository() as (conn, repository):
        _seed_document(
            conn,
            source_type="gsheets",
            external_id="sheet:1:2",
            metadata_json=f'{{"{INGEST_CONTENT_HASH_KEY}": "aaa111"}}',
        )
        # NUL 混入 id は repository 境界の _strip_nul と同じ規約で照合される
        result = repository.get_document_content_hashes("gsheets", ["sheet:1:2\x00", "\x00"])
        assert result == {"sheet:1:2": "aaa111"}


# -----------------------------------------------------------
# 2) end-to-end: 実 upsert → 実照合 → 「2 回目は Bedrock ゼロ」（pgvector 必須）
# -----------------------------------------------------------
class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_passage(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1] * 1024


class _CountingBedrock:
    def __init__(self) -> None:
        self.converse_calls = 0

    def converse(self, **kwargs: Any) -> Any:
        self.converse_calls += 1
        return SimpleNamespace(
            text='{"project": "テスト案件", "industry": "コスメ", "doc_type": "提案書"}'
        )


def _install_counting_classifier(monkeypatch: pytest.MonkeyPatch) -> _CountingBedrock:
    bedrock = _CountingBedrock()
    monkeypatch.setenv("USE_DOC_CLASSIFY", "1")
    monkeypatch.setattr(
        "teamagent.adapters.bedrock_client.BedrockClient.from_env",
        classmethod(lambda cls, **kwargs: bedrock),
    )
    return bedrock


def _install_gsheet_rows(
    monkeypatch: pytest.MonkeyPatch, rows: tuple[tuple[str, str], ...]
) -> None:
    from teamagent.adapters.gsheets_client import TabRows

    fake_client = MagicMock()
    fake_client.get_tab_rows.return_value = TabRows(
        sheet_id="1V",
        tab_name="フォーム回答 1",
        headers=("業界", "温度感"),
        rows=rows,
        row_count=len(rows),
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )


def _run_gsheet(
    repository: IngestRepository,
    *,
    request_id: str,
    collector: _IngestUnchangedCollector | None = None,
) -> tuple[tuple[int, int], _CountingEmbedder]:
    from teamagent.ingest.pipeline import _ingest_gsheet

    spec = GSheetSpec(
        sheet_id="1V",
        sheet_name="FB",
        description="",
        tabs=(GSheetsTabSpec(gid=537831563, tab_name="フォーム回答 1"),),
    )
    embedder = _CountingEmbedder()
    result = _ingest_gsheet(
        spec,
        embedder=embedder,  # type: ignore[arg-type]
        repository=repository,
        owner_email="x@y.jp",
        dry_run=False,
        request_id=request_id,
        unchanged_collector=collector,
    )
    return result, embedder


def _select_documents(conn: Any) -> dict[str, dict[str, Any]]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT external_id, metadata FROM documents ORDER BY external_id")
        return {str(r["external_id"]): dict(r["metadata"]) for r in cur.fetchall()}


def test_end_to_end_second_run_identical_is_bedrock_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 DB round-trip: 同一内容の 2 回目 run は Bedrock 呼び出しゼロ・DB 無変更。"""
    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    for name in ("USE_CONTEXTUAL_INGEST", "USE_ENTITY_TAGS", "EMBEDDER_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    bedrock = _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, (("飲食", "高"), ("コスメ", "中")))

    with _repository(with_chunks=True) as (conn, repository):
        (docs1, _), embedder1 = _run_gsheet(repository, request_id="r1")
        assert docs1 == 2
        assert bedrock.converse_calls == 2
        assert embedder1.calls == 2
        after_run1 = _select_documents(conn)
        assert set(after_run1) == {"1V:537831563:2", "1V:537831563:3"}
        for metadata in after_run1.values():
            assert metadata[INGEST_CONTENT_HASH_KEY]  # 実 jsonb に hash が保存された
            assert metadata["cls_project"] == "テスト案件"

        collector = _IngestUnchangedCollector()
        (docs2, chunks2), embedder2 = _run_gsheet(repository, request_id="r2", collector=collector)
        assert (docs2, chunks2) == (0, 0)
        assert bedrock.converse_calls == 2  # ★ 2 回目の Bedrock 呼び出しはゼロ
        assert embedder2.calls == 0
        assert collector.count_for("gsheets") == 2
        assert _select_documents(conn) == after_run1  # metadata（cls_* 含む）は無傷

        # 3 回目: 1 行だけ変更 → その行だけ再分類・再 upsert・hash 更新
        _install_gsheet_rows(monkeypatch, (("飲食", "高"), ("コスメ", "低に変更")))
        collector3 = _IngestUnchangedCollector()
        (docs3, _), _ = _run_gsheet(repository, request_id="r3", collector=collector3)
        assert docs3 == 1
        assert bedrock.converse_calls == 3
        assert collector3.count_for("gsheets") == 1
        after_run3 = _select_documents(conn)
        assert after_run3["1V:537831563:2"] == after_run1["1V:537831563:2"]
        assert (
            after_run3["1V:537831563:3"][INGEST_CONTENT_HASH_KEY]
            != after_run1["1V:537831563:3"][INGEST_CONTENT_HASH_KEY]
        )


def test_end_to_end_flag_off_reprocesses_and_stores_no_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 DB round-trip: フラグ OFF は毎回全件再処理し、hash も保存しない（従来挙動）。"""
    monkeypatch.delenv("INGEST_DIFFERENTIAL", raising=False)
    bedrock = _install_counting_classifier(monkeypatch)
    _install_gsheet_rows(monkeypatch, (("飲食", "高"), ("コスメ", "中")))

    with _repository(with_chunks=True) as (conn, repository):
        (docs1, _), _ = _run_gsheet(repository, request_id="r1")
        (docs2, _), _ = _run_gsheet(repository, request_id="r2")
        assert (docs1, docs2) == (2, 2)
        assert bedrock.converse_calls == 4  # 毎回再分類（従来挙動）
        for metadata in _select_documents(conn).values():
            assert INGEST_CONTENT_HASH_KEY not in metadata
