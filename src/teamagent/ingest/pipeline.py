"""ingest_sources.yaml に基づく取り込みパイプライン（Sprint 3 PR-6）。

3 種類の adapter（slack/gdrive/gsheets）を横断ディスパッチし、
正規化された DocumentUpsert + ChunkUpsert を IngestRepository に渡す。

設計：
- adapter は **直接呼ばない**: IngestRunner に factory を inject してテスト可能化
- 各 source 単位で exception を catch → ログだけ出して次へ（partial failure 許容）
- dry-run モード: DB 投入しないで件数だけ集計
- embedder は LocalE5Embedder を流用（既存）

Usage (CLI):
    python scripts/ingest_sources.py --sources slack --dry-run
    python scripts/ingest_sources.py --sources slack,gsheets --commit
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from teamagent.ingest.loader import (
    GDriveFolderSpec,
    GSheetSpec,
    IngestSources,
    SlackChannelSpec,
)
from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert, IngestRepository

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# 結果集計
# -----------------------------------------------------------
@dataclass
class IngestStats:
    """1 source kind の集計。"""

    source_kind: str  # 'slack' | 'gdrive' | 'gsheets'
    documents_upserted: int = 0
    chunks_inserted: int = 0
    sources_processed: int = 0
    sources_skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    """ingest 全体の結果。"""

    by_kind: dict[str, IngestStats] = field(default_factory=dict)

    def total_documents(self) -> int:
        return sum(s.documents_upserted for s in self.by_kind.values())

    def total_errors(self) -> int:
        return sum(len(s.errors) for s in self.by_kind.values())


# -----------------------------------------------------------
# Embedder Protocol（teamagent.adapters.embeddings_client.Embedder と互換）
# -----------------------------------------------------------
class _EmbedderProto(Protocol):
    def embed(self, text: str) -> list[float]: ...


# -----------------------------------------------------------
# 個別 source 取り込み handler（adapter は遅延 import）
# -----------------------------------------------------------
def _ingest_slack_channel(
    spec: SlackChannelSpec,
    *,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
) -> tuple[int, int]:
    """1 Slack channel を取り込む。戻り値: (documents 数, chunks 数)。

    各 thread を 1 document、その本文を 1 chunk として保存する最小実装。
    複数 chunk への分割は Sprint 4 で（PDF 添付の本文取り込み等）。
    """
    from teamagent.adapters.slack_channel_ingest_client import (
        SlackChannelIngestClient,
        collect_thread_participants,
        format_thread_as_document,
    )

    client = SlackChannelIngestClient.from_env()
    docs_n = 0
    chunks_n = 0

    # 1 ページのみ（増分取り込みは Sprint 4 で cursor / oldest 永続化）
    batch = client.list_channel_history(
        channel_id=spec.channel_id,
        request_id=request_id,
        limit=100,
    )

    for parent in batch.messages:
        if not parent.is_top_level:
            continue
        # スレッドなら replies を取得
        replies: list[Any] = []
        if parent.is_thread_parent:
            replies_batch = client.list_thread_replies(
                channel_id=spec.channel_id,
                thread_ts=parent.thread_ts or parent.ts,
                request_id=request_id,
            )
            replies = list(replies_batch.messages)

        text = format_thread_as_document(parent, replies)
        if not text.strip():
            continue
        participant_ids = collect_thread_participants(parent, replies)
        # email 解決は Sprint 4 で users.info キャッシュ経由（ここでは empty）
        # → acl_emails が空 = RLS 経由で fail-safe（誰も見えない）
        # 暫定: extra_acl_emails があれば使用、なければ owner だけ
        acl_emails = list(spec.extra_acl_emails) or [owner_email]

        external_id = f"{spec.channel_id}:{parent.thread_ts or parent.ts}"
        doc = DocumentUpsert(
            source_type="slack",
            external_id=external_id,
            source_uri=f"slack://{spec.channel_id}/{parent.thread_ts or parent.ts}",
            title=f"{spec.channel_name} {parent.ts}",
            owner_email=owner_email,
            acl_emails=acl_emails,
            metadata={
                **spec.extra_metadata,
                "channel_name": spec.channel_name,
                "participant_count": len(participant_ids),
            },
            modified_at=None,
        )
        chunks = [
            ChunkUpsert(
                chunk_idx=0,
                content=text,
                embedding=embedder.embed(text),
                metadata={"reply_count": parent.reply_count},
            )
        ]
        docs_n += 1
        chunks_n += len(chunks)
        if not dry_run:
            repository.upsert_document_with_chunks(doc, chunks, request_id=request_id)

    logger.info(
        "ingest_slack_channel_done",
        channel_id=spec.channel_id,
        channel_name=spec.channel_name,
        documents=docs_n,
        chunks=chunks_n,
        dry_run=dry_run,
    )
    return docs_n, chunks_n


def _ingest_gdrive_folder(
    spec: GDriveFolderSpec,
    *,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
) -> tuple[int, int]:
    """1 Drive folder を取り込む（雛形：ファイル名 + メタのみ、本文抽出は Sprint 4）。"""
    from teamagent.adapters.gdrive_client import GDriveClient

    client = GDriveClient.from_env()
    docs_n = 0
    chunks_n = 0

    files, _ = client.list_files(
        folder_id=spec.folder_id,
        request_id=request_id,
        page_size=100,
        mime_type_filter=spec.mime_type_filter,
    )

    for f in files:
        # ACL は permissions.list で取得（Sprint 4 で本格実装、ここでは owner のみ）
        acl_emails = [owner_email]
        external_id = f.id
        # 雛形：本文は title だけ embed（PDF 本文抽出は Sprint 4 で）
        text = f"{f.name} ({f.mime_type})"
        doc = DocumentUpsert(
            source_type="gdrive",
            external_id=external_id,
            source_uri=f.web_view_link or f"gdrive://{f.id}",
            title=f.name,
            owner_email=owner_email,
            acl_emails=acl_emails,
            metadata={
                **spec.extra_metadata,
                "mime_type": f.mime_type,
                "size": f.size,
            },
            modified_at=f.modified_time,
        )
        chunks = [
            ChunkUpsert(
                chunk_idx=0,
                content=text,
                embedding=embedder.embed(text),
                metadata={},
            )
        ]
        docs_n += 1
        chunks_n += len(chunks)
        if not dry_run:
            repository.upsert_document_with_chunks(doc, chunks, request_id=request_id)

    logger.info(
        "ingest_gdrive_folder_done",
        folder_id=spec.folder_id,
        folder_name=spec.folder_name,
        documents=docs_n,
        chunks=chunks_n,
        dry_run=dry_run,
    )
    return docs_n, chunks_n


def _ingest_gsheet(
    spec: GSheetSpec,
    *,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
) -> tuple[int, int]:
    """1 Sheet を取り込む（row_unit=True で 1 行 = 1 document）。"""
    from teamagent.adapters.gsheets_client import (
        GSheetsClient,
        build_external_id,
        format_row_as_document,
    )

    client = GSheetsClient.from_env()
    docs_n = 0
    chunks_n = 0

    for tab in spec.tabs:
        tab_rows = client.get_tab_rows(
            sheet_id=spec.sheet_id, tab_name=tab.tab_name, request_id=request_id
        )
        if not tab_rows.headers:
            continue
        for row_idx, row in enumerate(tab_rows.rows, start=2):  # 1=headers, 2 から data
            text = format_row_as_document(tab_rows.headers, row)
            if not text.strip():
                continue
            external_id = build_external_id(spec.sheet_id, tab.gid, row_idx)
            doc = DocumentUpsert(
                source_type="other",  # gsheets を ENUM に追加するのは migration 0003 で
                external_id=external_id,
                source_uri=f"https://docs.google.com/spreadsheets/d/{spec.sheet_id}/edit?gid={tab.gid}#gid={tab.gid}&range={row_idx}:{row_idx}",
                title=f"{spec.sheet_name} - {tab.tab_name} - row {row_idx}",
                owner_email=owner_email,
                acl_emails=[owner_email],
                metadata={**spec.extra_metadata, "tab_name": tab.tab_name, "row_idx": row_idx},
                modified_at=None,
            )
            chunks = [
                ChunkUpsert(
                    chunk_idx=0,
                    content=text,
                    embedding=embedder.embed(text),
                    metadata={},
                )
            ]
            docs_n += 1
            chunks_n += len(chunks)
            if not dry_run:
                repository.upsert_document_with_chunks(doc, chunks, request_id=request_id)

    logger.info(
        "ingest_gsheet_done",
        sheet_id=spec.sheet_id,
        sheet_name=spec.sheet_name,
        documents=docs_n,
        chunks=chunks_n,
        dry_run=dry_run,
    )
    return docs_n, chunks_n


# -----------------------------------------------------------
# IngestRunner（orchestrator）
# -----------------------------------------------------------
class IngestRunner:
    """ingest_sources.yaml に基づく 3 source 取り込みのオーケストレータ。"""

    def __init__(
        self,
        repository: IngestRepository,
        embedder: _EmbedderProto,
        *,
        owner_email: str,
        dry_run: bool = True,
    ) -> None:
        self._repo = repository
        self._embedder = embedder
        self._owner_email = owner_email
        self._dry_run = dry_run

    def run(
        self,
        sources: IngestSources,
        *,
        kinds: list[str] | None = None,
    ) -> IngestResult:
        """指定 kinds の source を取り込む。

        kinds: ['slack','gdrive','gsheets'] のサブセット。None なら全部。
        """
        kinds = kinds or ["slack", "gdrive", "gsheets"]
        result = IngestResult()
        request_id = f"ingest-{uuid.uuid4().hex[:12]}"

        logger.info(
            "ingest_runner_start",
            request_id=request_id,
            kinds=kinds,
            dry_run=self._dry_run,
            owner_email=self._owner_email,
        )

        if "slack" in kinds:
            result.by_kind["slack"] = self._run_kind(
                "slack",
                sources.slack_channels,
                _ingest_slack_channel,
                request_id=request_id,
            )
        if "gdrive" in kinds:
            result.by_kind["gdrive"] = self._run_kind(
                "gdrive",
                sources.gdrive_folders,
                _ingest_gdrive_folder,
                request_id=request_id,
            )
        if "gsheets" in kinds:
            result.by_kind["gsheets"] = self._run_kind(
                "gsheets", sources.gsheets, _ingest_gsheet, request_id=request_id
            )

        logger.info(
            "ingest_runner_done",
            request_id=request_id,
            total_documents=result.total_documents(),
            total_errors=result.total_errors(),
            dry_run=self._dry_run,
        )
        return result

    def _run_kind(
        self,
        kind: str,
        specs: tuple[Any, ...],
        handler: Any,
        *,
        request_id: str,
    ) -> IngestStats:
        stats = IngestStats(source_kind=kind)
        for spec in specs:
            try:
                docs_n, chunks_n = handler(
                    spec,
                    embedder=self._embedder,
                    repository=self._repo,
                    owner_email=self._owner_email,
                    dry_run=self._dry_run,
                    request_id=request_id,
                )
                stats.documents_upserted += docs_n
                stats.chunks_inserted += chunks_n
                stats.sources_processed += 1
            except Exception as e:
                logger.exception(
                    "ingest_source_failed",
                    request_id=request_id,
                    kind=kind,
                    spec=str(spec)[:200],
                )
                stats.sources_skipped += 1
                stats.errors.append(f"{type(e).__name__}: {e}")
        return stats
