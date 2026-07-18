"""documents / chunks テーブルへの ingest 用 repository。

Sprint 3 / PR-6。pipeline.py から呼ばれて、ON CONFLICT で idempotent INSERT する。

設計：
- 1 document = 1 source の論理単位（Slack thread / Gmail msg / Drive file / Sheet row）
- 1 document に N 個の chunk（PDF ページ分割 / 長文 split 等）
- INSERT は UNIQUE (source_type, external_id) で衝突したら UPDATE
- chunks は document_id 経由で再構築（既存 chunk を削除→再投入）
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from teamagent.adapters.pgvector_client import PgVectorClient

logger = structlog.get_logger(__name__)

_SCHEMA_REPROBE_SECONDS = 60.0
_SOURCE_RETRY_LEASE_SECONDS = 600
_SOURCE_RETRY_LIMIT = 1000
_INGEST_APPLICATION_NAME = "teamagent-ingest"


def _external_id_ref(external_id: str) -> str:
    """ログ用の非可逆な短い参照。Drive ID 等の完全値をログへ出さない。"""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:12]


def _external_id_hash(external_id: str) -> str:
    """reconciliation照合用の完全SHA-256。raw IDはDB/ログへ出さない。"""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


def _is_md5(value: str) -> bool:
    """lowercase hexadecimal MD5 の表現だけを受ける。"""
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceRetry:
    """増分cursorを越えて持ち越す、lease取得済みsource retry。"""

    external_id: str
    md5_checksum: str | None
    size_bytes: int | None
    mime_type: str
    validator_schema_version: str
    attempt_count: int
    reason: str
    lease_owner: str | None = None
    lease_token: str | None = None


@dataclass(frozen=True)
class GDriveAclSnapshot:
    """ACL-only 同期対象となる non-stale Drive document の最小スナップショット。

    ``row_version`` は PostgreSQL の ``xmin``。同期計画後に ingest 等が同じ行を更新した
    場合を検出し、古い ACL 計画で上書きしないための楽観 lock token として使う。
    本文・metadata・時刻は読み出さない。
    """

    document_id: str
    external_id: str
    owner_email: str
    acl_emails: tuple[str, ...]
    acl_groups: tuple[str, ...]
    row_version: str


@dataclass(frozen=True)
class GDriveAclUpdate:
    """ACL-only UPDATE 1 行分。expected_row_version は計画時の ``xmin``。"""

    document_id: str
    external_id: str
    expected_row_version: str
    owner_email: str
    acl_emails: tuple[str, ...]
    acl_groups: tuple[str, ...]


class GDriveAclOptimisticLockError(RuntimeError):
    """ACL 計画後に対象行が変化したため、全更新を中止した。"""


class SourceRetryUnavailableError(RuntimeError):
    """durable retry queueを確認できず、空queueと区別できない。"""


class SourceRetryLeaseLostError(RuntimeError):
    """claimed retry の owner/token/期限 fence を満たさず、書込みを中止した。"""


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
        # Rolling deploy で additive table が未適用なら、同一process中の反復SQL/警告を抑える。
        self._source_health_available: bool | None = None
        self._connector_runs_available: bool | None = None
        self._source_retries_available: bool | None = None
        self._reconciliation_available: bool | None = None
        self._source_health_retry_after = 0.0
        self._connector_runs_retry_after = 0.0
        self._source_retries_retry_after = 0.0
        self._reconciliation_retry_after = 0.0

    def _schema_probe_allowed(self, schema_key: str) -> bool:
        available = getattr(self, f"_{schema_key}_available")
        retry_after = float(getattr(self, f"_{schema_key}_retry_after"))
        return available is not False or time.monotonic() >= retry_after

    def _schema_probe_succeeded(self, schema_key: str) -> None:
        setattr(self, f"_{schema_key}_available", True)
        setattr(self, f"_{schema_key}_retry_after", 0.0)

    def _schema_probe_failed(self, schema_key: str) -> None:
        setattr(self, f"_{schema_key}_available", False)
        setattr(
            self,
            f"_{schema_key}_retry_after",
            time.monotonic() + _SCHEMA_REPROBE_SECONDS,
        )

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
        with self._document_connection(doc) as conn:
            self._lock_source(conn, doc.source_type, doc.external_id)
            document_id = self._upsert_document(conn, doc)
            if replace_existing_chunks:
                self._delete_chunks(conn, document_id)
            self._insert_chunks(conn, document_id, chunks)

        logger.info(
            "ingest_repository_upsert_done",
            request_id=request_id,
            source_type=doc.source_type,
            external_id_ref=_external_id_ref(doc.external_id),
            document_id=document_id,
            chunk_count=len(chunks),
        )
        return document_id

    def upsert_title_only_if_no_content(
        self,
        doc: DocumentUpsert,
        chunks: list[ChunkUpsert],
        request_id: str,
    ) -> str | None:
        """既存本文 chunk が無い場合だけ title-only fallback を原子的に upsert する。

        通常 upsert と同じ transaction advisory lock を取り、既存本文の確認から
        document/chunk 置換までを 1 transaction に閉じる。別 run / 別 process と競合しても、
        title-only が既存の正常本文を削除することはない。
        """
        if not chunks or not all(chunk.metadata.get("title_only") for chunk in chunks):
            raise ValueError("upsert_title_only_if_no_content requires title-only chunks")

        with self._document_connection(doc) as conn:
            self._lock_source(conn, doc.source_type, doc.external_id)
            if self._has_non_title_content(conn, doc):
                logger.info(
                    "ingest_repository_title_only_suppressed",
                    request_id=request_id,
                    source_type=doc.source_type,
                    external_id_ref=_external_id_ref(doc.external_id),
                )
                return None

            document_id = self._upsert_document(conn, doc)
            self._delete_chunks(conn, document_id)
            self._insert_chunks(conn, document_id, chunks)

        logger.info(
            "ingest_repository_title_only_upsert_done",
            request_id=request_id,
            source_type=doc.source_type,
            external_id_ref=_external_id_ref(doc.external_id),
            document_id=document_id,
        )
        return document_id

    def upsert_document_with_chunks_and_resolve_retry(
        self,
        doc: DocumentUpsert,
        chunks: list[ChunkUpsert],
        request_id: str,
        *,
        source_kind: str,
        source_id: str,
        expected_lease_owner: str,
        expected_lease_token: str,
        replace_existing_chunks: bool = True,
        protect_existing_content: bool = False,
    ) -> str | None:
        """claimed retry の検証・document/chunk upsert・resolveを1 transactionで行う。

        retry row を exact ``(owner, token, pending, unexpired)`` で ``FOR UPDATE`` してから
        document advisory lock を取る。resolve直前にも wall-clock 期限を再確認し、失効して
        いれば例外で transaction 全体を rollback する。したがって takeover 済みの stale
        worker は document/chunk を一切変更できない。

        ``protect_existing_content`` は title-only fallback 専用。既存本文がある場合は
        document/chunk を触らず、同じ transaction 内でretryだけ解消して ``None`` を返す。
        """
        normalized_owner = _strip_nul(expected_lease_owner)
        normalized_token = _strip_nul(expected_lease_token)
        if not normalized_owner or not normalized_token:
            raise ValueError("claimed retry requires non-empty lease owner and token")
        if protect_existing_content and (
            not chunks or not all(chunk.metadata.get("title_only") for chunk in chunks)
        ):
            raise ValueError("protect_existing_content requires title-only chunks")

        retry_id: str
        document_id: str | None = None
        with self._document_connection(doc) as conn:
            lock_sql = """
                SELECT id::text AS id
                FROM ingest_source_retries
                WHERE source_kind = %s
                  AND source_id = %s
                  AND source_type = %s
                  AND external_id = %s
                  AND status = 'pending'
                  AND lease_owner = %s
                  AND lease_token = %s
                  AND lease_expires_at > clock_timestamp()
                FOR UPDATE
            """
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    lock_sql,
                    (
                        source_kind,
                        _strip_nul(source_id),
                        doc.source_type,
                        _strip_nul(doc.external_id),
                        normalized_owner,
                        normalized_token,
                    ),
                )
                retry_row = cur.fetchone()
            if retry_row is None:
                raise SourceRetryLeaseLostError("claimed retry fence rejected document upsert")
            retry_id = str(retry_row["id"])

            self._lock_source(conn, doc.source_type, doc.external_id)
            if not protect_existing_content or not self._has_non_title_content(conn, doc):
                document_id = self._upsert_document(conn, doc)
                if replace_existing_chunks:
                    self._delete_chunks(conn, document_id)
                self._insert_chunks(conn, document_id, chunks)

            resolve_sql = """
                UPDATE ingest_source_retries
                SET status = 'resolved',
                    resolved_at = clock_timestamp(),
                    last_request_id = %s,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = %s::uuid
                  AND status = 'pending'
                  AND lease_owner = %s
                  AND lease_token = %s
                  AND lease_expires_at > clock_timestamp()
            """
            with conn.cursor() as cur:
                cur.execute(
                    resolve_sql,
                    (
                        _strip_nul(request_id),
                        retry_id,
                        normalized_owner,
                        normalized_token,
                    ),
                )
                if int(cur.rowcount or 0) != 1:
                    raise SourceRetryLeaseLostError(
                        "claimed retry expired before document transaction resolved"
                    )

        self._schema_probe_succeeded("source_retries")
        logger.info(
            "ingest_repository_claimed_retry_committed",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            source_type=doc.source_type,
            external_id_ref=_external_id_ref(doc.external_id),
            document_id=document_id,
            chunk_count=len(chunks) if document_id is not None else 0,
            document_suppressed=document_id is None,
        )
        return document_id

    def _document_connection(self, doc: DocumentUpsert) -> Any:
        """document/retry transactionを固定application_nameで開く。"""
        return self._pgvector.connection(
            app_role=self._app_role,
            user_email=self._owner_email or doc.owner_email,
            user_role="admin",
            application_name=_INGEST_APPLICATION_NAME,
        )

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
            application_name=_INGEST_APPLICATION_NAME,
        )

    def find_invalid_source_reason(
        self,
        source_type: str,
        external_id: str,
        md5_checksum: str | None,
        size_bytes: int | None,
        mime_type: str | None = None,
        validator_schema_version: str | None = None,
    ) -> str | None:
        """完全一致する既知 invalid payload の reason を返す。旧schema時は fail-open。"""
        if not md5_checksum or size_bytes is None or not mime_type or not validator_schema_version:
            return None
        if not self._schema_probe_allowed("source_health"):
            return None
        sql = """
            SELECT reason
            FROM ingest_source_health
            WHERE source_type = %s
              AND external_id = %s
              AND md5_checksum = %s
              AND size_bytes = %s
              AND mime_type = %s
              AND validator_schema_version = %s
              AND status = 'invalid_source'
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        sql,
                        (
                            source_type,
                            _strip_nul(external_id),
                            md5_checksum.lower(),
                            size_bytes,
                            mime_type,
                            validator_schema_version,
                        ),
                    )
                    row = cur.fetchone()
            self._schema_probe_succeeded("source_health")
            return str(row["reason"]) if row is not None else None
        except Exception as exc:
            self._schema_probe_failed("source_health")
            # Rolling deploy 中に migration 未適用でも ingest を止めない。ID/SQL引数はログしない。
            logger.warning(
                "ingest_source_health_lookup_unavailable",
                source_type=source_type,
                external_id_ref=_external_id_ref(external_id),
                error_type=type(exc).__name__,
            )
            return None

    def record_invalid_source(
        self,
        source_type: str,
        external_id: str,
        *,
        md5_checksum: str | None,
        size_bytes: int | None,
        reason: str,
        mime_type: str | None,
        validator_schema_version: str,
        request_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """invalid Office payload を fingerprint 単位で upsert。旧schema時は best-effort。"""
        import json

        if (
            not md5_checksum
            or size_bytes is None
            or not reason
            or not mime_type
            or not validator_schema_version
        ):
            return False
        if not self._schema_probe_allowed("source_health"):
            return False
        normalized_md5 = md5_checksum.lower()
        if not _is_md5(normalized_md5) or size_bytes < 0:
            return False
        sql = """
            INSERT INTO ingest_source_health
                (source_type, external_id, md5_checksum, size_bytes, status, reason,
                 mime_type, validator_schema_version,
                 first_seen_at, last_seen_at, observation_count,
                 last_request_id, metadata)
            VALUES (%s, %s, %s, %s, 'invalid_source', %s, %s, %s,
                    now(), now(), 1, %s, %s::jsonb)
            ON CONFLICT (
                source_type, external_id, md5_checksum, size_bytes,
                mime_type, validator_schema_version
            )
            DO UPDATE SET
                status = 'invalid_source',
                reason = EXCLUDED.reason,
                last_seen_at = now(),
                observation_count = ingest_source_health.observation_count + 1,
                last_request_id = EXCLUDED.last_request_id,
                metadata = ingest_source_health.metadata || EXCLUDED.metadata
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            source_type,
                            _strip_nul(external_id),
                            normalized_md5,
                            size_bytes,
                            _strip_nul(reason),
                            _strip_nul(mime_type),
                            _strip_nul(validator_schema_version),
                            _strip_nul(request_id),
                            json.dumps(_sanitize_metadata(metadata or {}), ensure_ascii=False),
                        ),
                    )
            self._schema_probe_succeeded("source_health")
            return True
        except Exception as exc:
            self._schema_probe_failed("source_health")
            logger.warning(
                "ingest_source_health_record_unavailable",
                source_type=source_type,
                external_id_ref=_external_id_ref(external_id),
                reason=reason,
                error_type=type(exc).__name__,
            )
            return False

    def record_connector_run(
        self,
        *,
        request_id: str,
        source_kind: str,
        source_id: str,
        outcome: str,
        documents_upserted: int,
        chunks_inserted: int,
        warning_reasons: dict[str, int] | None = None,
        suppressed_retry_count: int = 0,
        error: str | None = None,
    ) -> bool:
        """source単位の success/success_with_warnings/failed を記録。旧schema時は継続。"""
        import json

        if outcome not in {"success", "success_with_warnings", "failed"}:
            raise ValueError(f"unsupported connector outcome: {outcome}")
        if not self._schema_probe_allowed("connector_runs"):
            return False
        reasons = {
            str(reason): int(count)
            for reason, count in sorted((warning_reasons or {}).items())
            if int(count) > 0
        }
        sql = """
            INSERT INTO ingest_connector_runs
                (request_id, source_kind, source_id, outcome,
                 documents_upserted, chunks_inserted, warning_count,
                 warning_reasons, suppressed_retry_count, last_error, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, now())
            ON CONFLICT (request_id, source_kind, source_id)
            DO UPDATE SET
                outcome = EXCLUDED.outcome,
                documents_upserted = EXCLUDED.documents_upserted,
                chunks_inserted = EXCLUDED.chunks_inserted,
                warning_count = EXCLUDED.warning_count,
                warning_reasons = EXCLUDED.warning_reasons,
                suppressed_retry_count = EXCLUDED.suppressed_retry_count,
                last_error = EXCLUDED.last_error,
                completed_at = now()
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            _strip_nul(request_id),
                            source_kind,
                            _strip_nul(source_id),
                            outcome,
                            documents_upserted,
                            chunks_inserted,
                            sum(reasons.values()),
                            json.dumps(reasons, ensure_ascii=True),
                            suppressed_retry_count,
                            _strip_nul(error),
                        ),
                    )
            self._schema_probe_succeeded("connector_runs")
            return True
        except Exception as exc:
            self._schema_probe_failed("connector_runs")
            logger.warning(
                "ingest_connector_run_record_unavailable",
                request_id=request_id,
                source_kind=source_kind,
                source_id_ref=_external_id_ref(source_id),
                outcome=outcome,
                error_type=type(exc).__name__,
            )
            return False

    def claim_due_source_retries(
        self,
        *,
        source_kind: str,
        source_id: str,
        request_id: str,
        limit: int = _SOURCE_RETRY_LIMIT,
        lease_seconds: int = _SOURCE_RETRY_LEASE_SECONDS,
    ) -> list[SourceRetry]:
        """期限到来retryをleaseする。並行workerは``SKIP LOCKED``で同じrowを取らない。"""
        if limit < 1 or limit > _SOURCE_RETRY_LIMIT:
            raise ValueError(f"retry claim limit must be between 1 and {_SOURCE_RETRY_LIMIT}")
        if lease_seconds < 1:
            raise ValueError("retry lease_seconds must be positive")
        if not self._schema_probe_allowed("source_retries"):
            raise SourceRetryUnavailableError("durable retry queue is temporarily unavailable")
        sql = """
            WITH due AS (
                SELECT id
                FROM ingest_source_retries
                WHERE source_kind = %s
                  AND source_id = %s
                  AND status = 'pending'
                  AND next_attempt_at <= now()
                  AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                ORDER BY next_attempt_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE ingest_source_retries AS retry
            SET lease_owner = %s,
                lease_token = gen_random_uuid()::text,
                lease_expires_at = now() + (%s * interval '1 second')
            FROM due
            WHERE retry.id = due.id
            RETURNING retry.external_id,
                      retry.md5_checksum,
                      retry.size_bytes,
                      retry.mime_type,
                      retry.validator_schema_version,
                      retry.attempt_count,
                      retry.reason,
                      retry.lease_owner,
                      retry.lease_token
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        sql,
                        (
                            source_kind,
                            _strip_nul(source_id),
                            limit,
                            _strip_nul(request_id),
                            lease_seconds,
                        ),
                    )
                    rows = cur.fetchall()
            self._schema_probe_succeeded("source_retries")
            return [
                SourceRetry(
                    external_id=str(row["external_id"]),
                    md5_checksum=(
                        str(row["md5_checksum"]) if row["md5_checksum"] is not None else None
                    ),
                    size_bytes=(int(row["size_bytes"]) if row["size_bytes"] is not None else None),
                    mime_type=str(row["mime_type"]),
                    validator_schema_version=str(row["validator_schema_version"]),
                    attempt_count=int(row["attempt_count"]),
                    reason=str(row["reason"]),
                    lease_owner=(
                        str(row["lease_owner"]) if row.get("lease_owner") is not None else None
                    ),
                    lease_token=(
                        str(row["lease_token"]) if row.get("lease_token") is not None else None
                    ),
                )
                for row in rows
            ]
        except Exception as exc:
            self._schema_probe_failed("source_retries")
            logger.warning(
                "ingest_source_retry_claim_unavailable",
                request_id=request_id,
                source_kind=source_kind,
                source_id_ref=_external_id_ref(source_id),
                error_type=type(exc).__name__,
            )
            raise SourceRetryUnavailableError("durable retry queue could not be claimed") from exc

    def renew_source_retry_lease(
        self,
        *,
        source_kind: str,
        source_id: str,
        source_type: str,
        external_id: str,
        request_id: str,
        expected_lease_owner: str,
        expected_lease_token: str,
        lease_seconds: int = _SOURCE_RETRY_LEASE_SECONDS,
    ) -> bool:
        """現在のownerだけが処理中retryのleaseを延長する。

        lease期限後に同じowner名のworkerが再claimしても``lease_token``が変わるため更新しない。
        長い抽出・embedding中のheartbeatから呼び、二重処理を防ぐ。
        """
        if lease_seconds < 1:
            raise ValueError("retry lease_seconds must be positive")
        normalized_owner = _strip_nul(expected_lease_owner)
        normalized_token = _strip_nul(expected_lease_token)
        if not normalized_owner or not normalized_token:
            return False
        if not self._schema_probe_allowed("source_retries"):
            return False
        sql = """
            UPDATE ingest_source_retries
            SET lease_expires_at = now() + (%s * interval '1 second')
            WHERE source_kind = %s
              AND source_id = %s
              AND source_type = %s
              AND external_id = %s
              AND status = 'pending'
              AND lease_owner = %s
              AND lease_token = %s
              AND lease_expires_at > clock_timestamp()
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            lease_seconds,
                            source_kind,
                            _strip_nul(source_id),
                            source_type,
                            _strip_nul(external_id),
                            normalized_owner,
                            normalized_token,
                        ),
                    )
                    renewed = int(cur.rowcount or 0) > 0
            self._schema_probe_succeeded("source_retries")
            return renewed
        except Exception as exc:
            self._schema_probe_failed("source_retries")
            logger.warning(
                "ingest_source_retry_lease_renew_unavailable",
                request_id=request_id,
                source_kind=source_kind,
                source_id_ref=_external_id_ref(source_id),
                external_id_ref=_external_id_ref(external_id),
                error_type=type(exc).__name__,
            )
            return False

    def record_source_retry(
        self,
        *,
        source_kind: str,
        source_id: str,
        source_type: str,
        external_id: str,
        md5_checksum: str | None,
        size_bytes: int | None,
        mime_type: str,
        validator_schema_version: str,
        reason: str,
        request_id: str,
        metadata: dict[str, Any] | None = None,
        expected_lease_owner: str | None = None,
        expected_lease_token: str | None = None,
        allow_unclaimed: bool = False,
    ) -> bool:
        """transient失敗をfingerprint付きでupsertし、指数backoffを設定する。

        同一request_id・同一fingerprintの再記録はattemptを増やさず冪等。payloadが変われば
        attemptを1へ戻す。leaseは失敗確定時に解放し、次runの期限到来まで再claimしない。

        claimed-worker 経路は、同じ不透明 ``(owner, token)`` の未期限切れ pending lease
        が現在も存在するときだけ更新する。owner名を再利用したABA takeover後でも stale
        worker は successor lease を解放できず、resolved rowも復活できない。
        初回または未claimの scheduling 経路は ``allow_unclaimed=True`` を明示した場合
        だけ許可し、lease 3列がすべてNULLのrowに限定する。owner/token と unclaimed
        opt-in の併用、不完全なfence、暗黙経路は拒否する。
        """
        import json

        normalized_md5 = md5_checksum.lower() if md5_checksum else None
        if normalized_md5 is not None and not _is_md5(normalized_md5):
            return False
        if size_bytes is not None and size_bytes < 0:
            return False
        if not mime_type or not validator_schema_version or not reason:
            return False
        if not self._schema_probe_allowed("source_retries"):
            return False
        normalized_expected_lease_owner = (
            _strip_nul(expected_lease_owner) if expected_lease_owner is not None else None
        )
        normalized_expected_lease_token = (
            _strip_nul(expected_lease_token) if expected_lease_token is not None else None
        )
        if normalized_expected_lease_owner is None and normalized_expected_lease_token is None:
            if not allow_unclaimed:
                return False
        elif (
            not normalized_expected_lease_owner
            or not normalized_expected_lease_token
            or allow_unclaimed
        ):
            return False
        sql = """
            INSERT INTO ingest_source_retries
                (source_kind, source_id, source_type, external_id,
                 md5_checksum, size_bytes, mime_type, validator_schema_version,
                 status, reason, attempt_count, next_attempt_at,
                 first_failed_at, last_failed_at, last_request_id, metadata)
            SELECT
                %s, %s, %s, %s, %s, %s, %s, %s,
                'pending', %s, 1, now() + interval '60 seconds',
                now(), now(), %s, %s::jsonb
            WHERE (%s::text IS NULL AND %s::text IS NULL)
               OR EXISTS (
                    SELECT 1
                    FROM ingest_source_retries AS claimed
                    WHERE claimed.source_kind = %s
                      AND claimed.source_id = %s
                      AND claimed.source_type = %s
                      AND claimed.external_id = %s
                      AND claimed.status = 'pending'
                      AND claimed.lease_owner = %s
                      AND claimed.lease_token = %s
                      AND claimed.lease_expires_at > clock_timestamp()
               )
            ON CONFLICT (source_kind, source_id, source_type, external_id)
            DO UPDATE SET
                md5_checksum = EXCLUDED.md5_checksum,
                size_bytes = EXCLUDED.size_bytes,
                mime_type = EXCLUDED.mime_type,
                validator_schema_version = EXCLUDED.validator_schema_version,
                status = 'pending',
                reason = EXCLUDED.reason,
                attempt_count = CASE
                    WHEN ingest_source_retries.status = 'resolved'
                    THEN 1
                    WHEN ingest_source_retries.last_request_id = EXCLUDED.last_request_id
                         AND ingest_source_retries.md5_checksum
                             IS NOT DISTINCT FROM EXCLUDED.md5_checksum
                         AND ingest_source_retries.size_bytes
                             IS NOT DISTINCT FROM EXCLUDED.size_bytes
                         AND ingest_source_retries.mime_type = EXCLUDED.mime_type
                         AND ingest_source_retries.validator_schema_version
                             = EXCLUDED.validator_schema_version
                    THEN ingest_source_retries.attempt_count
                    WHEN ingest_source_retries.md5_checksum
                             IS NOT DISTINCT FROM EXCLUDED.md5_checksum
                         AND ingest_source_retries.size_bytes
                             IS NOT DISTINCT FROM EXCLUDED.size_bytes
                         AND ingest_source_retries.mime_type = EXCLUDED.mime_type
                         AND ingest_source_retries.validator_schema_version
                             = EXCLUDED.validator_schema_version
                    THEN ingest_source_retries.attempt_count + 1
                    ELSE 1
                END,
                next_attempt_at = CASE
                    WHEN ingest_source_retries.status = 'resolved'
                    THEN now() + interval '60 seconds'
                    WHEN ingest_source_retries.last_request_id = EXCLUDED.last_request_id
                         AND ingest_source_retries.md5_checksum
                             IS NOT DISTINCT FROM EXCLUDED.md5_checksum
                         AND ingest_source_retries.size_bytes
                             IS NOT DISTINCT FROM EXCLUDED.size_bytes
                         AND ingest_source_retries.mime_type = EXCLUDED.mime_type
                         AND ingest_source_retries.validator_schema_version
                             = EXCLUDED.validator_schema_version
                    THEN ingest_source_retries.next_attempt_at
                    WHEN ingest_source_retries.md5_checksum
                             IS NOT DISTINCT FROM EXCLUDED.md5_checksum
                         AND ingest_source_retries.size_bytes
                             IS NOT DISTINCT FROM EXCLUDED.size_bytes
                         AND ingest_source_retries.mime_type = EXCLUDED.mime_type
                         AND ingest_source_retries.validator_schema_version
                             = EXCLUDED.validator_schema_version
                    THEN now() + (
                        LEAST(
                            21600,
                            (60 * power(
                                2,
                                LEAST(ingest_source_retries.attempt_count, 8)
                            ))::integer
                        ) * interval '1 second'
                    )
                    ELSE now() + interval '60 seconds'
                END,
                first_failed_at = CASE
                    WHEN ingest_source_retries.status = 'resolved'
                    THEN now()
                    WHEN ingest_source_retries.md5_checksum
                             IS NOT DISTINCT FROM EXCLUDED.md5_checksum
                         AND ingest_source_retries.size_bytes
                             IS NOT DISTINCT FROM EXCLUDED.size_bytes
                         AND ingest_source_retries.mime_type = EXCLUDED.mime_type
                         AND ingest_source_retries.validator_schema_version
                             = EXCLUDED.validator_schema_version
                    THEN ingest_source_retries.first_failed_at
                    ELSE now()
                END,
                last_failed_at = now(),
                resolved_at = NULL,
                last_request_id = EXCLUDED.last_request_id,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                metadata = ingest_source_retries.metadata || EXCLUDED.metadata
            WHERE (
                %s::text IS NULL
                AND %s::text IS NULL
                AND (
                    (
                        ingest_source_retries.status = 'resolved'
                        AND ingest_source_retries.lease_owner IS NULL
                        AND ingest_source_retries.lease_token IS NULL
                        AND ingest_source_retries.lease_expires_at IS NULL
                    )
                    OR (
                        ingest_source_retries.status = 'pending'
                        AND ingest_source_retries.lease_owner IS NULL
                        AND ingest_source_retries.lease_token IS NULL
                        AND ingest_source_retries.lease_expires_at IS NULL
                    )
                )
            )
            OR (
                %s::text IS NOT NULL
                AND %s::text IS NOT NULL
                AND ingest_source_retries.status = 'pending'
                AND ingest_source_retries.lease_owner = %s
                AND ingest_source_retries.lease_token = %s
                AND ingest_source_retries.lease_expires_at > clock_timestamp()
            )
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            source_kind,
                            _strip_nul(source_id),
                            source_type,
                            _strip_nul(external_id),
                            normalized_md5,
                            size_bytes,
                            _strip_nul(mime_type),
                            _strip_nul(validator_schema_version),
                            _strip_nul(reason),
                            _strip_nul(request_id),
                            json.dumps(_sanitize_metadata(metadata or {}), ensure_ascii=False),
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            source_kind,
                            _strip_nul(source_id),
                            source_type,
                            _strip_nul(external_id),
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                        ),
                    )
                    recorded = int(cur.rowcount or 0) > 0
            self._schema_probe_succeeded("source_retries")
            return recorded
        except Exception as exc:
            self._schema_probe_failed("source_retries")
            logger.warning(
                "ingest_source_retry_record_unavailable",
                request_id=request_id,
                source_kind=source_kind,
                source_id_ref=_external_id_ref(source_id),
                external_id_ref=_external_id_ref(external_id),
                error_type=type(exc).__name__,
            )
            return False

    def resolve_source_retry(
        self,
        *,
        source_kind: str,
        source_id: str,
        source_type: str,
        external_id: str,
        md5_checksum: str | None,
        size_bytes: int | None,
        mime_type: str,
        validator_schema_version: str,
        request_id: str,
        expected_lease_owner: str | None = None,
        expected_lease_token: str | None = None,
        allow_unclaimed: bool = False,
    ) -> bool:
        """成功/永久invalidでpending retryを解消する。DELETE権限は不要。

        claimed worker は exact ``(owner, token)`` と一致する未期限切れ lease のみ解消
        できる。fingerprint はowner/token/期限を迂回しない。unclaimed 経路は
        ``allow_unclaimed=True`` を明示した場合だけ選べ、lease 情報が完全に空かつ
        fingerprint が一致する pending row に限定する。不完全なfenceと暗黙経路は拒否する。
        """
        if not self._schema_probe_allowed("source_retries"):
            return False
        normalized_md5 = md5_checksum.lower() if md5_checksum else None
        normalized_expected_lease_owner = (
            _strip_nul(expected_lease_owner) if expected_lease_owner is not None else None
        )
        normalized_expected_lease_token = (
            _strip_nul(expected_lease_token) if expected_lease_token is not None else None
        )
        if normalized_expected_lease_owner is None and normalized_expected_lease_token is None:
            if not allow_unclaimed:
                return False
        elif (
            not normalized_expected_lease_owner
            or not normalized_expected_lease_token
            or allow_unclaimed
        ):
            return False
        sql = """
            UPDATE ingest_source_retries
            SET status = 'resolved',
                resolved_at = now(),
                last_request_id = %s,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL
            WHERE source_kind = %s
              AND source_id = %s
              AND source_type = %s
              AND external_id = %s
              AND status = 'pending'
              AND (
                  (
                      %s::text IS NOT NULL
                      AND %s::text IS NOT NULL
                      AND lease_owner = %s
                      AND lease_token = %s
                      AND lease_expires_at > clock_timestamp()
                  )
                  OR (
                      %s::text IS NULL
                      AND %s::text IS NULL
                      AND lease_owner IS NULL
                      AND lease_token IS NULL
                      AND lease_expires_at IS NULL
                      AND md5_checksum IS NOT DISTINCT FROM %s
                      AND size_bytes IS NOT DISTINCT FROM %s
                      AND mime_type = %s
                      AND validator_schema_version = %s
                  )
              )
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            _strip_nul(request_id),
                            source_kind,
                            _strip_nul(source_id),
                            source_type,
                            _strip_nul(external_id),
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            normalized_expected_lease_owner,
                            normalized_expected_lease_token,
                            normalized_md5,
                            size_bytes,
                            _strip_nul(mime_type),
                            _strip_nul(validator_schema_version),
                        ),
                    )
                    resolved = int(cur.rowcount) > 0
            self._schema_probe_succeeded("source_retries")
            return resolved
        except Exception as exc:
            self._schema_probe_failed("source_retries")
            logger.warning(
                "ingest_source_retry_resolve_unavailable",
                request_id=request_id,
                source_kind=source_kind,
                source_id_ref=_external_id_ref(source_id),
                external_id_ref=_external_id_ref(external_id),
                error_type=type(exc).__name__,
            )
            return False

    def unresolved_reconciliation_counts(self, source_kind: str) -> dict[str, int]:
        """未解消coverage gapを理由別件数だけ返す。ID/title/contentは射影しない。"""
        if not self._schema_probe_allowed("reconciliation"):
            return {"reconciliation_unavailable": 1}
        sql = """
            SELECT gap_kind, count(*)::bigint AS gap_count
            FROM ingest_reconciliation_gaps
            WHERE source_kind = %s
              AND status = 'unresolved'
            GROUP BY gap_kind
            ORDER BY gap_kind
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, (source_kind,))
                    rows = cur.fetchall()
            self._schema_probe_succeeded("reconciliation")
            return {str(row["gap_kind"]): int(row["gap_count"]) for row in rows}
        except Exception as exc:
            self._schema_probe_failed("reconciliation")
            logger.warning(
                "ingest_reconciliation_lookup_unavailable",
                source_kind=source_kind,
                error_type=type(exc).__name__,
            )
            return {"reconciliation_unavailable": 1}

    def resolve_reconciliation_gaps(
        self,
        *,
        source_kind: str,
        external_id: str,
        request_id: str,
    ) -> int:
        """本文upsert成功時、raw IDを保存せずhash一致する既知gapだけ解消する。"""
        if not self._schema_probe_allowed("reconciliation"):
            return 0
        sql = """
            UPDATE ingest_reconciliation_gaps
            SET status = 'resolved',
                resolved_at = now(),
                last_observed_at = now(),
                last_request_id = %s
            WHERE source_kind = %s
              AND status = 'unresolved'
              AND %s = ANY(source_ref_hashes)
        """
        try:
            with self._ops_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            _strip_nul(request_id),
                            source_kind,
                            _external_id_hash(external_id),
                        ),
                    )
                    resolved = cur.rowcount
            self._schema_probe_succeeded("reconciliation")
            return int(resolved)
        except Exception as exc:
            self._schema_probe_failed("reconciliation")
            logger.warning(
                "ingest_reconciliation_resolve_unavailable",
                request_id=request_id,
                source_kind=source_kind,
                external_id_ref=_external_id_ref(external_id),
                error_type=type(exc).__name__,
            )
            return 0

    def load_connector_state(self, source_kind: str, source_id: str) -> ConnectorState | None:
        """(source_kind, source_id) の前回状態を 1 行ロードする。未登録なら None。"""
        sql = """
            SELECT source_kind, source_id, cursor, oldest, revision,
                   attempt_count, last_error, metadata
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
            metadata=dict(row.get("metadata") or {}),
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
    # Google Drive ACL-only 同期
    # -------------------------------------------------------
    def list_nonstale_gdrive_acl_snapshot(self) -> list[GDriveAclSnapshot]:
        """non-stale gdrive documents の ACL と楽観 lock token だけを返す。

        title / source_uri / client_code / metadata 本体 / modified_at / ingested_at / chunks は
        射影しない。``metadata`` は stale 判定の WHERE 条件にだけ使う。
        """
        sql = """
            SELECT id::text AS document_id,
                   external_id,
                   owner_email,
                   acl_emails,
                   acl_groups,
                   xmin::text AS row_version
            FROM documents
            WHERE source_type = 'gdrive'::document_source_type
              AND COALESCE(metadata->>'stale', '') <> 'true'
            ORDER BY external_id, id
        """
        with self._ops_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return [
            GDriveAclSnapshot(
                document_id=str(row["document_id"]),
                external_id=str(row["external_id"]),
                owner_email=str(row["owner_email"]),
                acl_emails=tuple(str(value) for value in (row["acl_emails"] or ())),
                acl_groups=tuple(str(value) for value in (row["acl_groups"] or ())),
                row_version=str(row["row_version"]),
            )
            for row in rows
        ]

    def update_gdrive_acls(self, updates: list[GDriveAclUpdate]) -> int:
        """ACL 3 列だけを 1 transaction で更新し、更新件数を返す。

        書込み前に全対象を ``FOR UPDATE`` し、計画時の ``xmin`` と照合する。1 行でも
        欠落・stale 化・更新済みなら、最初の UPDATE より前に例外を送出する。その後の
        UPDATE も owner_email / acl_emails / acl_groups 以外を SET しない。途中 DB 失敗時は
        PgVectorClient.connection の transaction rollback により全行 write 0 となる。
        """
        if not updates:
            return 0

        ordered = sorted(updates, key=lambda item: item.document_id)
        document_ids = [item.document_id for item in ordered]
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("duplicate document_id in gdrive ACL update plan")
        if any(not item.owner_email.strip() for item in ordered):
            raise ValueError("owner_email must not be empty in gdrive ACL update plan")

        lock_sql = """
            SELECT id::text AS document_id,
                   external_id,
                   xmin::text AS row_version
            FROM documents
            WHERE source_type = 'gdrive'::document_source_type
              AND COALESCE(metadata->>'stale', '') <> 'true'
              AND id = ANY(%s::uuid[])
            ORDER BY id
            FOR UPDATE
        """
        update_sql = """
            UPDATE documents
            SET owner_email = %s,
                acl_emails = %s,
                acl_groups = %s
            WHERE id = %s::uuid
              AND source_type = 'gdrive'::document_source_type
              AND external_id = %s
        """

        expected = {
            (item.document_id, item.external_id, item.expected_row_version) for item in ordered
        }
        with self._ops_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(lock_sql, (document_ids,))
                locked_rows = cur.fetchall()
            actual = {
                (str(row["document_id"]), str(row["external_id"]), str(row["row_version"]))
                for row in locked_rows
            }
            if actual != expected:
                raise GDriveAclOptimisticLockError(
                    "gdrive ACL snapshot changed before commit; no rows updated"
                )

            updated = 0
            with conn.cursor() as cur:
                for item in ordered:
                    cur.execute(
                        update_sql,
                        (
                            item.owner_email,
                            list(item.acl_emails),
                            list(item.acl_groups),
                            item.document_id,
                            item.external_id,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise GDriveAclOptimisticLockError(
                            "gdrive ACL row changed during commit; transaction rolled back"
                        )
                    updated += 1
        return updated

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
    def _lock_source(
        conn: psycopg.Connection[dict[str, Any]],
        source_type: str,
        external_id: str,
    ) -> None:
        """同一 document key の置換を transaction 内で直列化する。"""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s, hashtextextended(%s, 0))
                )
                """,
                (_strip_nul(external_id), source_type),
            )

    @staticmethod
    def _has_non_title_content(
        conn: psycopg.Connection[dict[str, Any]],
        doc: DocumentUpsert,
    ) -> bool:
        """既存documentに空でない本文chunkがあるかをtransaction内で確認する。"""
        sql = """
            SELECT EXISTS (
                SELECT 1
                FROM documents AS d
                JOIN chunks AS c ON c.document_id = d.id
                WHERE d.source_type = %s::document_source_type
                  AND d.external_id = %s
                  AND COALESCE(c.metadata->>'title_only', 'false') <> 'true'
                  AND btrim(c.content) <> ''
            ) AS has_content
        """
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (doc.source_type, _strip_nul(doc.external_id)))
            row = cur.fetchone()
        return row is not None and bool(row["has_content"])

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
