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
    SharedDriveCrawlSpec,
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
def _collect_all_member_ids(
    client: Any,  # SlackChannelIngestClient だが循環 import 回避で Any
    channel_id: str,
    request_id: str,
    *,
    max_pages: int = 20,
) -> list[str]:
    """conversations.members を全 page 集めて user_id list を返す。"""
    all_ids: list[str] = []
    cursor: str | None = None
    for _ in range(max_pages):
        ids, cursor = client.list_channel_members(
            channel_id=channel_id, request_id=request_id, cursor=cursor
        )
        all_ids.extend(ids)
        if not cursor:
            break
    return all_ids


# プロセス内 users.info キャッシュ（同 user の複数 channel 出現に効く）
_USER_EMAIL_CACHE: dict[str, str | None] = {}


def _resolve_member_emails(
    client: Any,
    user_ids: list[str],
    request_id: str,
) -> list[str]:
    """user_ids を email にバルク解決（キャッシュあり、Bot/deleted は除外）。"""
    # キャッシュにない id だけ users.info を叩く
    uncached = [uid for uid in user_ids if uid not in _USER_EMAIL_CACHE]
    if uncached:
        members = client.get_user_emails(uncached, request_id=request_id)
        for m in members:
            if m.is_bot or m.deleted:
                _USER_EMAIL_CACHE[m.user_id] = None
            else:
                _USER_EMAIL_CACHE[m.user_id] = m.email
    # 解決済 email だけ取り出して dedup
    emails = {_USER_EMAIL_CACHE.get(uid) for uid in user_ids}
    return sorted(e for e in emails if e)


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

    ACL 設計（2026-05-27 更新）:
    「Slack channel に居る人 = そのナレッジを見ていい人」原則。
    channel メンバー全員を一度 users.info で email 解決して acl_emails に投入する。
    yaml の extra_acl_emails があれば union（追加で見せたい人を足せる）。
    """
    from teamagent.adapters.slack_channel_ingest_client import (
        SlackChannelIngestClient,
        format_thread_as_document,
    )

    client = SlackChannelIngestClient.from_env()
    docs_n = 0
    chunks_n = 0

    # 1) channel メンバー全員を email 解決（ACL ベースライン）
    member_ids = _collect_all_member_ids(client, spec.channel_id, request_id)
    member_emails = _resolve_member_emails(client, member_ids, request_id)
    # yaml の extra_acl_emails を union
    channel_acl_emails = sorted(set(member_emails) | set(spec.extra_acl_emails))
    logger.info(
        "ingest_slack_channel_acl_resolved",
        request_id=request_id,
        channel_id=spec.channel_id,
        members=len(member_ids),
        resolved_emails=len(member_emails),
        total_acl=len(channel_acl_emails),
    )

    # ACL fail-safe: email 解決ゼロかつ extra_acl_emails も無い → channel ごと skip
    # （Slack OAuth に users:read.email が未付与 / Bot が channel に居ない等を想定）
    if not channel_acl_emails:
        logger.warning(
            "ingest_slack_channel_skipped_no_acl",
            request_id=request_id,
            channel_id=spec.channel_id,
            channel_name=spec.channel_name,
            hint=(
                "channel メンバーの email 解決ゼロかつ yaml の extra_acl_emails も空。"
                "Slack App に users:read.email スコープを追加するか、"
                "yaml の extra_acl_emails で明示的に許可ユーザーを指定してください。"
                "fail-safe で本チャネルの取り込みを skip します。"
            ),
        )
        return 0, 0

    # 2) 1 ページのみ取得（増分取り込みは Sprint 4 で cursor / oldest 永続化）
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
        # ACL: channel メンバー全員（resolved） + yaml extra（少なくとも owner は確実に入る）
        acl_emails = channel_acl_emails or [owner_email]

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
                "channel_member_count": len(channel_acl_emails),
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


def _list_all_gdrive_files(
    client: Any,
    folder_id: str | None,
    request_id: str,
    mime_type_filter: str | None,
    *,
    max_pages: int = 100,
) -> list[Any]:
    """folder 内 file を pagination を回して全件取得する（max_pages で防壁）。"""
    out: list[Any] = []
    token: str | None = None
    for _ in range(max_pages):
        files, token = client.list_files(
            folder_id=folder_id,
            request_id=request_id,
            page_size=100,
            page_token=token,
            mime_type_filter=mime_type_filter,
        )
        out.extend(files)
        if not token:
            break
    return out


def _resolve_drive_file_acl(
    client: Any,
    file_id: str,
    request_id: str,
    fallback_owner_email: str,
) -> tuple[str, list[str], list[str]]:
    """Drive file の ACL を解決する。

    戻り値: (owner_email, acl_emails, acl_groups)
    permissions.list 失敗時は fallback_owner_email + [fallback_owner_email] を返す。
    """
    from teamagent.adapters.gdrive_client import extract_acl_emails

    try:
        perms = client.list_permissions(file_id=file_id, request_id=request_id)
    except Exception:
        logger.exception("gdrive_list_permissions_failed", file_id=file_id)
        return fallback_owner_email, [fallback_owner_email], []

    # owner_email: role='owner' の最初の user
    owner_email = fallback_owner_email
    for p in perms:
        if p.role == "owner" and p.type == "user" and p.email_address and not p.deleted:
            owner_email = p.email_address
            break

    acl_emails, acl_groups = extract_acl_emails(perms)
    # owner は必ず ACL に含める（自分が ingest した document を fail-safe で見られるように）
    if owner_email not in acl_emails:
        acl_emails.append(owner_email)
    return owner_email, acl_emails, acl_groups


_PDF_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
    }
)


# Day 7: 共有ドライブ全自動 crawl の営業資料判定用 whitelist / noise filter
# 営業に役立つドキュメントの拡張子 / mime_type のみ取り込み対象とする。
_SALES_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
        "application/msword",  # doc (legacy)
        "application/vnd.ms-powerpoint",  # ppt (legacy)
        "application/vnd.ms-excel",  # xls (legacy)
        "application/vnd.google-apps.document",  # gdoc
        "application/vnd.google-apps.spreadsheet",  # gsheet
        "application/vnd.google-apps.presentation",  # gslide
        "text/plain",
        "text/markdown",
        "text/csv",
    }
)

# Google ネイティブ形式（size が None で返ってくる）
_GOOGLE_NATIVE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
    }
)

# ノイズキーワード（営業資料として価値が低い tmp/backup/test 系）
_NOISE_NAME_KEYWORDS: tuple[str, ...] = (
    ".tmp",
    "_tmp",
    "_old",
    "_backup",
    "_bk",
    ".bak",
    "_test",
    "test_",
    "_draft",
    "ignore",
    "trash",
    "duplicate",
    "コピー",
    "（コピー）",
)


def _is_sales_relevant(
    f: Any,  # DriveFile だが循環 import 回避で Any
    *,
    modified_within_days: int | None = 730,
) -> tuple[bool, str]:
    """Drive file が営業資料として取り込む価値ありそうか判定する。

    判定基準:
    1. mime_type が whitelist に含まれる（PDF / Office / Google native / text）
    2. 名前にノイズキーワードが含まれない（tmp / backup / test / コピー 等）
    3. size が妥当（100 byte 未満 / 50 MB 超は除外。Google native は size=None で許容）
    4. modified_within_days 以内に更新されている（None なら無視）

    戻り値: (relevant, reason)。reason は採用/除外理由（ログ用）。
    """
    import datetime as _dt

    # 1. mime type
    if f.mime_type not in _SALES_ALLOWED_MIME_TYPES:
        return False, f"mime_type not in whitelist: {f.mime_type}"

    # 2. ノイズキーワード
    name_lower = f.name.lower()
    for kw in _NOISE_NAME_KEYWORDS:
        if kw in name_lower or kw in f.name:
            return False, f"noise keyword in name: {kw}"

    # 3. サイズチェック（Google native は size=None で OK）
    if f.mime_type not in _GOOGLE_NATIVE_MIME_TYPES:
        if f.size is None:
            return False, "size unknown (non-native, suspicious)"
        if f.size < 100:
            return False, f"size too small: {f.size} bytes"
        if f.size > 50 * 1024 * 1024:
            return False, f"size too large: {f.size} bytes"

    # 4. 更新日時
    if modified_within_days is not None and f.modified_time:
        try:
            modified_dt = _dt.datetime.fromisoformat(f.modified_time.replace("Z", "+00:00"))
            cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=modified_within_days)
            if modified_dt < cutoff:
                return False, f"stale: modified {f.modified_time}"
        except (ValueError, TypeError):
            # parse 失敗時は許容（fail-safe で取り込み）
            pass

    return True, "passed"


def _ingest_gdrive_folder(
    spec: GDriveFolderSpec,
    *,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
) -> tuple[int, int]:
    """1 Drive folder を取り込む。

    対象: spec.mime_type_filter で絞り込んだファイル（既定は spec 側で application/pdf）。
    - PDF: download_file_bytes → pypdf でテキスト抽出 → ページごとにチャンク化 → embedding
    - 非 PDF: title だけ embed して 1 chunk（メタデータ用、将来 Google Doc export 対応で拡張）

    ACL は permissions.list を呼んで documents.acl_emails / acl_groups に写像する。
    """
    from teamagent.adapters.gdrive_client import GDriveClient
    from teamagent.ingest.pdf_extract import chunk_pages, extract_pdf_pages

    # Day 7: folder bulk ingest のため readonly=True で drive.readonly スコープを使う。
    # Internal OAuth なので CASA 審査不要。drive.file ではフォルダ単位の取り込みが
    # できない（per-file opened only の制約）ため、readonly に切替。
    client = GDriveClient.from_env(readonly=True)
    docs_n = 0
    chunks_n = 0
    skipped: list[str] = []

    files = _list_all_gdrive_files(
        client=client,
        folder_id=spec.folder_id,
        request_id=request_id,
        mime_type_filter=spec.mime_type_filter,
    )

    for f in files:
        # ACL を permissions.list で解決
        file_owner_email, acl_emails, acl_groups = _resolve_drive_file_acl(
            client=client,
            file_id=f.id,
            request_id=request_id,
            fallback_owner_email=f.owners_email[0] if f.owners_email else owner_email,
        )

        # 本文抽出: PDF のみ実 chunk 化、それ以外は title のみ
        chunks: list[ChunkUpsert] = []
        if f.mime_type in _PDF_MIME_TYPES:
            try:
                data = client.download_file_bytes(file_id=f.id, request_id=request_id)
            except Exception:
                logger.exception(
                    "gdrive_pdf_download_failed",
                    file_id=f.id,
                    file_name=f.name,
                )
                skipped.append(f.id)
                continue
            try:
                pages = extract_pdf_pages(data)
            except Exception:
                logger.exception(
                    "gdrive_pdf_extract_failed",
                    file_id=f.id,
                    file_name=f.name,
                )
                skipped.append(f.id)
                continue
            page_chunks = chunk_pages(pages, size=500, overlap=100)
            if not page_chunks:
                logger.warning("gdrive_pdf_empty_text", file_id=f.id, file_name=f.name)
                skipped.append(f.id)
                continue
            for idx, (page_num, text) in enumerate(page_chunks):
                chunks.append(
                    ChunkUpsert(
                        chunk_idx=idx,
                        content=text,
                        embedding=embedder.embed(text),
                        metadata={"page_num": page_num},
                    )
                )
        else:
            # 非 PDF: 雛形と同じ（title + mime のみ、Google Doc export 対応は Sprint 4 で）
            text = f"{f.name} ({f.mime_type})"
            chunks.append(
                ChunkUpsert(
                    chunk_idx=0,
                    content=text,
                    embedding=embedder.embed(text),
                    metadata={"mime_type": f.mime_type, "title_only": True},
                )
            )

        doc = DocumentUpsert(
            source_type="gdrive",
            external_id=f.id,
            source_uri=f.web_view_link or f"gdrive://{f.id}",
            title=f.name,
            owner_email=file_owner_email,
            acl_emails=acl_emails,
            acl_groups=acl_groups,
            metadata={
                **spec.extra_metadata,
                "mime_type": f.mime_type,
                "size": f.size,
                "drive_folder_id": spec.folder_id,
                "drive_folder_name": spec.folder_name,
            },
            modified_at=f.modified_time,
        )
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
        skipped=len(skipped),
        dry_run=dry_run,
    )
    return docs_n, chunks_n


def _ingest_shared_drives_crawl(
    spec: SharedDriveCrawlSpec,
    *,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
) -> tuple[int, int]:
    """共有ドライブ全件 crawl + 営業資料フィルタで取り込む (Day 7, 2026-05-27)。

    対象: s-komata がメンバーになっている全共有ドライブ。
    - spec.name_filter が指定されてれば substring match で絞る
    - 各ドライブを再帰 walk
    - spec.sales_relevance_filter=True なら _is_sales_relevant で営業価値判定
    - ACL は permissions.list で解決して acl_emails / acl_groups に写像
    - PDF はテキスト抽出 + chunk 化、それ以外は title のみで 1 chunk
    """
    from teamagent.adapters.gdrive_client import GDriveClient
    from teamagent.ingest.pdf_extract import chunk_pages, extract_pdf_pages

    client = GDriveClient.from_env(readonly=True)
    docs_n = 0
    chunks_n = 0
    skipped_count = 0
    filtered_count = 0

    # 1. 共有ドライブ列挙
    all_drives = client.list_shared_drives(request_id=request_id)
    if spec.name_filter:
        drives = [d for d in all_drives if any(kw in d.name for kw in spec.name_filter)]
    else:
        drives = all_drives
    logger.info(
        "ingest_shared_drives_filtered",
        request_id=request_id,
        total=len(all_drives),
        after_name_filter=len(drives),
        name_filter=list(spec.name_filter),
    )

    for drive in drives:
        # 2. 各ドライブを再帰 walk
        files = client.walk_files_recursive(
            root_id=drive.id,
            request_id=request_id,
            drive_id=drive.id,
            max_files=spec.max_files_per_drive,
        )
        logger.info(
            "ingest_shared_drive_walked",
            request_id=request_id,
            drive_id=drive.id,
            drive_name=drive.name,
            files_found=len(files),
        )

        for f in files:
            # 3. 営業関連判定
            if spec.sales_relevance_filter:
                relevant, _reason = _is_sales_relevant(
                    f, modified_within_days=spec.modified_within_days
                )
                if not relevant:
                    filtered_count += 1
                    continue

            # 4. ACL 解決
            file_owner_email, acl_emails, acl_groups = _resolve_drive_file_acl(
                client=client,
                file_id=f.id,
                request_id=request_id,
                fallback_owner_email=f.owners_email[0] if f.owners_email else owner_email,
            )

            # 5. 本文抽出
            chunks: list[ChunkUpsert] = []
            if f.mime_type in _PDF_MIME_TYPES:
                try:
                    data = client.download_file_bytes(file_id=f.id, request_id=request_id)
                except Exception:
                    logger.exception(
                        "shared_drive_pdf_download_failed",
                        file_id=f.id,
                        file_name=f.name,
                        drive_id=drive.id,
                    )
                    skipped_count += 1
                    continue
                try:
                    pages = extract_pdf_pages(data)
                except Exception:
                    logger.exception(
                        "shared_drive_pdf_extract_failed",
                        file_id=f.id,
                        file_name=f.name,
                    )
                    skipped_count += 1
                    continue
                page_chunks = chunk_pages(pages, size=500, overlap=100)
                if not page_chunks:
                    skipped_count += 1
                    continue
                for idx, (page_num, text) in enumerate(page_chunks):
                    chunks.append(
                        ChunkUpsert(
                            chunk_idx=idx,
                            content=text,
                            embedding=embedder.embed(text),
                            metadata={"page_num": page_num},
                        )
                    )
            else:
                # 非 PDF: title + mime のみ（Google Doc/Sheet/Slide export 対応は Sprint 4）
                text = f"{f.name} ({f.mime_type})"
                chunks.append(
                    ChunkUpsert(
                        chunk_idx=0,
                        content=text,
                        embedding=embedder.embed(text),
                        metadata={"mime_type": f.mime_type, "title_only": True},
                    )
                )

            # 6. DocumentUpsert 組み立て
            doc = DocumentUpsert(
                source_type="gdrive",
                external_id=f.id,
                source_uri=f.web_view_link or f"gdrive://{f.id}",
                title=f.name,
                owner_email=file_owner_email,
                acl_emails=acl_emails,
                acl_groups=acl_groups,
                metadata={
                    **spec.extra_metadata,
                    "mime_type": f.mime_type,
                    "size": f.size,
                    "shared_drive_id": drive.id,
                    "shared_drive_name": drive.name,
                    "via": "shared_drive_crawl",
                },
                modified_at=f.modified_time,
            )
            docs_n += 1
            chunks_n += len(chunks)
            if not dry_run:
                repository.upsert_document_with_chunks(doc, chunks, request_id=request_id)

    logger.info(
        "ingest_shared_drives_crawl_done",
        request_id=request_id,
        drives_processed=len(drives),
        documents=docs_n,
        chunks=chunks_n,
        skipped=skipped_count,
        filtered_out=filtered_count,
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
                source_type="gsheets",  # migration 0004 で ENUM に追加済
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
        kinds = kinds or ["slack", "gdrive", "gsheets", "shared_drives"]
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
        if "shared_drives" in kinds:
            # 共有ドライブ全自動 crawl: spec が 0 or 1 件（yaml の単一 toggle）
            shared_spec = sources.shared_drives_crawl
            if shared_spec is not None and shared_spec.enabled:
                result.by_kind["shared_drives"] = self._run_kind(
                    "shared_drives",
                    (shared_spec,),
                    _ingest_shared_drives_crawl,
                    request_id=request_id,
                )
            else:
                logger.info(
                    "ingest_shared_drives_skipped",
                    request_id=request_id,
                    reason=("shared_drives_crawl が yaml に未定義 or enabled=false の場合 skip"),
                )
                result.by_kind["shared_drives"] = IngestStats(source_kind="shared_drives")

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
