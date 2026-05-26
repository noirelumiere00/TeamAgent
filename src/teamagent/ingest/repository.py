"""documents / chunks テーブルへの ingest 用 repository。

Sprint 3 / PR-6。pipeline.py から呼ばれて、ON CONFLICT で idempotent INSERT する。

設計：
- 1 document = 1 source の論理単位（Slack thread / Gmail msg / Drive file / Sheet row）
- 1 document に N 個の chunk（PDF ページ分割 / 長文 split 等）
- INSERT は UNIQUE (source_type, external_id) で衝突したら UPDATE
- chunks は document_id 経由で再構築（既存 chunk を削除→再投入）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from teamagent.adapters.pgvector_client import PgVectorClient

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# repository が受け取る正規化済みデータ型
# -----------------------------------------------------------
@dataclass(frozen=True)
class DocumentUpsert:
    """1 document の upsert 入力。"""

    source_type: str  # 'pdf' | 'gdrive' | 'gmail' | 'slack' | 'other'
    external_id: str  # UNIQUE 制約のキー
    owner_email: str  # ingest 実行者の email
    acl_emails: list[str] = field(default_factory=list)
    acl_groups: list[str] = field(default_factory=list)
    source_uri: str | None = None
    title: str | None = None
    client_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    modified_at: str | None = None  # ISO8601 or None


@dataclass(frozen=True)
class ChunkUpsert:
    """1 chunk の upsert 入力。"""

    chunk_idx: int
    content: str
    embedding: list[float]  # 1024 次元
    contextualized: str | None = None
    page_num: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------
# repository 本体
# -----------------------------------------------------------
class IngestRepository:
    """documents/chunks への idempotent upsert を担当。

    pipeline.py がこの repository を呼んで、各 adapter から取得した document/chunks を
    本番 RDS に投入する。
    """

    def __init__(
        self,
        pgvector: PgVectorClient,
        *,
        app_role: str | None = "teamagent_app",
        owner_email: str | None = None,
    ) -> None:
        """app_role を渡す前提（migration 0002 で導入の teamagent_app）。

        ingest 用には admin role でも良いが、CLAUDE.md 6-bis の最小権限原則で
        アプリ実行ロール経由を既定にする。owner_email は admin override 用に使う。
        """
        self._pgvector = pgvector
        self._app_role = app_role
        self._owner_email = owner_email

    def upsert_document_with_chunks(
        self,
        doc: DocumentUpsert,
        chunks: list[ChunkUpsert],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        """1 document + その chunks をトランザクション内で upsert する。

        Returns: document.id (UUID 文字列)
        """
        # admin role で実行（ingest は ACL を書き込む側 = admin 相当）
        # user_email=owner_email を渡しておくと WITH CHECK で許可される
        with self._pgvector.connection(
            app_role=self._app_role,
            user_email=self._owner_email or doc.owner_email,
            user_role="admin",
        ) as conn:
            document_id = self._upsert_document(conn, doc)
            if replace_existing_chunks:
                self._delete_chunks(conn, document_id)
            self._insert_chunks(conn, document_id, chunks)

        logger.info(
            "ingest_repository_upsert_done",
            request_id=request_id,
            source_type=doc.source_type,
            external_id=doc.external_id,
            document_id=document_id,
            chunk_count=len(chunks),
        )
        return document_id

    # -------------------------------------------------------
    # 内部
    # -------------------------------------------------------
    @staticmethod
    def _upsert_document(
        conn: psycopg.Connection[dict[str, Any]],
        doc: DocumentUpsert,
    ) -> str:
        import json

        sql = """
            INSERT INTO documents
                (source_type, external_id, source_uri, title, owner_email,
                 acl_emails, acl_groups, client_code, metadata, modified_at)
            VALUES (%s::document_source_type, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s::timestamptz)
            ON CONFLICT (source_type, external_id)
            DO UPDATE SET
                source_uri = EXCLUDED.source_uri,
                title = EXCLUDED.title,
                owner_email = EXCLUDED.owner_email,
                acl_emails = EXCLUDED.acl_emails,
                acl_groups = EXCLUDED.acl_groups,
                client_code = EXCLUDED.client_code,
                metadata = EXCLUDED.metadata,
                modified_at = EXCLUDED.modified_at
            RETURNING id
        """
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql,
                (
                    doc.source_type,
                    doc.external_id,
                    doc.source_uri,
                    doc.title,
                    doc.owner_email,
                    doc.acl_emails,
                    doc.acl_groups,
                    doc.client_code,
                    json.dumps(doc.metadata, ensure_ascii=False),
                    doc.modified_at,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("documents upsert returned no id")
            return str(row["id"])

    @staticmethod
    def _delete_chunks(conn: psycopg.Connection[dict[str, Any]], document_id: str) -> None:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))

    @staticmethod
    def _insert_chunks(
        conn: psycopg.Connection[dict[str, Any]],
        document_id: str,
        chunks: list[ChunkUpsert],
    ) -> None:
        if not chunks:
            return
        import json

        sql = """
            INSERT INTO chunks
                (document_id, chunk_idx, content, contextualized, embedding, page_num, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb)
        """
        with conn.cursor() as cur:
            for c in chunks:
                cur.execute(
                    sql,
                    (
                        document_id,
                        c.chunk_idx,
                        c.content,
                        c.contextualized,
                        c.embedding,
                        c.page_num,
                        json.dumps(c.metadata, ensure_ascii=False),
                    ),
                )
