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


# Day 7 (2026-05-27): PDF / Doc 抽出時に NUL バイト (0x00) が混入することがあり、
# PostgreSQL の TEXT 列は NUL を許容しないため DataError になる。
# repository 境界で防御的にサニタイズする（境界 1 箇所で済ませる原則）。
def _strip_nul(value: str | None) -> str | None:
    """str 中の NUL バイト (0x00) を除去する。None 透過。"""
    if value is None:
        return None
    return value.replace("\x00", "")


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """metadata dict の文字列値から NUL バイトを再帰的に除去する。"""
    if not meta:
        return meta
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, str):
            out[k] = v.replace("\x00", "")
        elif isinstance(v, dict):
            out[k] = _sanitize_metadata(v)
        elif isinstance(v, list):
            out[k] = [item.replace("\x00", "") if isinstance(item, str) else item for item in v]
        else:
            out[k] = v
    return out


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


@dataclass(frozen=True)
class ConnectorState:
    """connector_state テーブル 1 行の読み出し結果（migration 0012）。

    増分同期（Wave3 / Batch C1）で (source_kind, source_id) ごとの前回 cursor を持ち回る。
    """

    source_kind: str
    source_id: str
    cursor: str | None = None
    oldest: float | None = None
    revision: int | None = None
    attempt_count: int = 0
    last_error: str | None = None


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
    # 増分同期: connector_state（migration 0012）
    # -------------------------------------------------------
    def _ops_connection(self) -> Any:
        """connector_state / ingest_jobs 用の接続（RLS 非対象の ops テーブル）。

        documents/chunks と同じ teamagent_app role 経由で接続する（最小権限）。
        これらの ops テーブルには RLS policy が無いので user_email は不要だが、
        既存 upsert と同条件にそろえて owner_email を渡す。
        """
        return self._pgvector.connection(
            app_role=self._app_role,
            user_email=self._owner_email,
            user_role="admin",
        )

    def load_connector_state(self, source_kind: str, source_id: str) -> ConnectorState | None:
        """(source_kind, source_id) の前回状態を 1 行ロードする。未登録なら None。"""
        sql = """
            SELECT source_kind, source_id, cursor, oldest, revision,
                   attempt_count, last_error
            FROM connector_state
            WHERE source_kind = %s AND source_id = %s
        """
        with self._ops_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (source_kind, source_id))
                row = cur.fetchone()
        if row is None:
            return None
        return ConnectorState(
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            cursor=row["cursor"],
            oldest=row["oldest"],
            revision=row["revision"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    def save_connector_state(
        self,
        source_kind: str,
        source_id: str,
        *,
        cursor: str | None = None,
        oldest: float | None = None,
        revision: int | None = None,
        success: bool = True,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """(source_kind, source_id) の状態を upsert する。

        success=True: cursor/oldest/revision を前進し last_success_at=now()・
        attempt_count=0 にリセット。
        success=False: cursor 等は触らず attempt_count++・last_error を記録
        （backoff / #ops alert しきい値の根拠）。
        """
        import json

        meta_json = json.dumps(_sanitize_metadata(metadata or {}), ensure_ascii=False)
        if success:
            sql = """
                INSERT INTO connector_state
                    (source_kind, source_id, cursor, oldest, revision,
                     last_success_at, attempt_count, last_error, metadata)
                VALUES (%s, %s, %s, %s, %s, now(), 0, NULL, %s::jsonb)
                ON CONFLICT (source_kind, source_id) DO UPDATE SET
                    cursor = COALESCE(EXCLUDED.cursor, connector_state.cursor),
                    oldest = COALESCE(EXCLUDED.oldest, connector_state.oldest),
                    revision = COALESCE(EXCLUDED.revision, connector_state.revision),
                    last_success_at = now(),
                    attempt_count = 0,
                    last_error = NULL,
                    metadata = connector_state.metadata || EXCLUDED.metadata
            """
            params: tuple[Any, ...] = (source_kind, source_id, cursor, oldest, revision, meta_json)
        else:
            sql = """
                INSERT INTO connector_state
                    (source_kind, source_id, attempt_count, last_error, metadata)
                VALUES (%s, %s, 1, %s, %s::jsonb)
                ON CONFLICT (source_kind, source_id) DO UPDATE SET
                    attempt_count = connector_state.attempt_count + 1,
                    last_error = EXCLUDED.last_error,
                    metadata = connector_state.metadata || EXCLUDED.metadata
            """
            params = (source_kind, source_id, _strip_nul(error), meta_json)
        with self._ops_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def record_ingest_job(
        self,
        source_type: str,
        external_id: str,
        *,
        state: str = "COMMITTED",
        batch_id: str | None = None,
        error: str | None = None,
        success: bool = True,
        max_attempts: int = 5,
    ) -> None:
        """ingest_jobs に 1 document の取り込み状態を記録する（migration 0005 の state machine）。

        success=True: ``state``（既定 COMMITTED）へ遷移。COMMITTED なら committed_at を更新。
        success=False: attempt_count++ し、max_attempts 到達で POISON、未満なら FAILED_TRANSIENT。
        (source_type, external_id) UNIQUE で同 document の再試行は同 row を UPDATE する。
        """
        if success:
            sql = """
                INSERT INTO ingest_jobs
                    (source_type, external_id, state, batch_id, attempt_count, committed_at)
                VALUES (%s::document_source_type, %s, %s::ingest_job_state, %s, 0,
                        CASE WHEN %s = 'COMMITTED' THEN now() ELSE NULL END)
                ON CONFLICT (source_type, external_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    batch_id = COALESCE(EXCLUDED.batch_id, ingest_jobs.batch_id),
                    last_error = NULL,
                    committed_at = CASE WHEN EXCLUDED.state = 'COMMITTED' THEN now()
                                        ELSE ingest_jobs.committed_at END
            """
            params: tuple[Any, ...] = (source_type, external_id, state, batch_id, state)
        else:
            sql = """
                INSERT INTO ingest_jobs
                    (source_type, external_id, state, batch_id, attempt_count, last_error)
                VALUES (%s::document_source_type, %s,
                        'FAILED_TRANSIENT'::ingest_job_state, %s, 1, %s)
                ON CONFLICT (source_type, external_id) DO UPDATE SET
                    attempt_count = ingest_jobs.attempt_count + 1,
                    last_error = EXCLUDED.last_error,
                    batch_id = COALESCE(EXCLUDED.batch_id, ingest_jobs.batch_id),
                    state = CASE WHEN ingest_jobs.attempt_count + 1 >= %s
                                 THEN 'POISON'::ingest_job_state
                                 ELSE 'FAILED_TRANSIENT'::ingest_job_state END
            """
            params = (source_type, external_id, batch_id, _strip_nul(error), max_attempts)
        with self._ops_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    # -------------------------------------------------------
    # stale soft-delete（入れ込み v2 2026-07-10・INGEST_MARK_STALE）
    # -------------------------------------------------------
    def list_gdrive_external_ids_with_stale(self) -> list[tuple[str, bool]]:
        """source_type='gdrive' の全 documents の (external_id, 既に stale か) を返す。

        pipeline の run 末尾で「今回観測した external_id 集合」との差集合を取り、
        未観測 doc への stale 印付け候補と量的ブレーキ（50% 超で中止）の分母に使う。
        SELECT のみ（書き込みなし）。
        """
        sql = """
            SELECT external_id,
                   COALESCE(metadata->>'stale', '') = 'true' AS is_stale
            FROM documents
            WHERE source_type = 'gdrive'::document_source_type
        """
        with self._ops_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return [(str(r["external_id"]), bool(r["is_stale"])) for r in rows]

    def mark_documents_stale(self, external_ids: list[str], *, marked_at_iso: str) -> int:
        """指定 external_id の gdrive documents に metadata.stale='true' を jsonb 付与する。

        物理 DELETE はしない（soft-delete）。stale_marked_at には run 日時（ISO8601）を
        記録する。既に stale の doc は呼び出し側で除外される想定（初回付与日時を保持）。
        戻り値: UPDATE した行数。
        """
        if not external_ids:
            return 0
        import json

        patch = json.dumps({"stale": "true", "stale_marked_at": marked_at_iso})
        sql = """
            UPDATE documents
            SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
            WHERE source_type = 'gdrive'::document_source_type
              AND external_id = ANY(%s)
        """
        with self._ops_connection() as conn:
            with conn.cursor() as cur:
                # 大量 doc の一括 UPDATE が既定 statement_timeout(30s) で打ち切られないよう
                # 当該 tx 内だけ無制限にする（pipeline._disable_statement_timeout と同流儀・
                # SET LOCAL は tx 終了で自動失効）。
                cur.execute("SET LOCAL statement_timeout = '0'")  # nosec B608  # 固定リテラル
                cur.execute(sql, (patch, external_ids))
                return int(cur.rowcount or 0)

    def clear_documents_stale(self, external_ids: list[str]) -> int:
        """指定 external_id の gdrive documents から stale / stale_marked_at キーを除去する。

        今回の run で観測された doc の stale 印を解除する（復活対応）。
        stale キーを持つ行だけを対象にして無駄な書き込みを避ける。
        戻り値: UPDATE した行数。
        """
        if not external_ids:
            return 0
        sql = """
            UPDATE documents
            SET metadata = (metadata - 'stale') - 'stale_marked_at'
            WHERE source_type = 'gdrive'::document_source_type
              AND external_id = ANY(%s)
              AND metadata ? 'stale'
        """
        with self._ops_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '0'")  # nosec B608  # 固定リテラル
                cur.execute(sql, (external_ids,))
                return int(cur.rowcount or 0)

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
                    _strip_nul(doc.external_id),
                    _strip_nul(doc.source_uri),
                    _strip_nul(doc.title),
                    _strip_nul(doc.owner_email),
                    [_strip_nul(e) or "" for e in doc.acl_emails],
                    [_strip_nul(g) or "" for g in doc.acl_groups],
                    _strip_nul(doc.client_code),
                    json.dumps(_sanitize_metadata(doc.metadata), ensure_ascii=False),
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
                        _strip_nul(c.content) or "",
                        _strip_nul(c.contextualized),
                        c.embedding,
                        c.page_num,
                        json.dumps(_sanitize_metadata(c.metadata), ensure_ascii=False),
                    ),
                )
