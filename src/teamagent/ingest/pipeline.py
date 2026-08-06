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

import datetime as _dt
import hashlib
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from teamagent.identity import shared_company_domains_from_env
from teamagent.ingest.boilerplate import mark_boilerplate
from teamagent.ingest.docdedup import mark_duplicate_documents
from teamagent.ingest.form_mappings import _normalize_form_label
from teamagent.ingest.gsheet_classification_overrides import (
    apply_gsheet_industry_override,
)
from teamagent.ingest.loader import (
    GDriveFolderSpec,
    GSheetSpec,
    IngestSources,
    SharedDriveCrawlSpec,
    SlackChannelSpec,
)
from teamagent.ingest.ops_alert import IngestOpsAlerter
from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert, IngestRepository

if TYPE_CHECKING:
    from teamagent.ingest.classify import DocClassifier
    from teamagent.ingest.contextualize import ChunkContextualizer

logger = structlog.get_logger(__name__)

MAX_INGEST_CHUNKS_PER_FILE = 2_000
MAX_INGEST_EMBEDDINGS_PER_FILE = 2_000
MAX_INGEST_EXTRACTED_CHARACTERS = 2_000_000
_SOURCE_RETRY_LEASE_SECONDS = 600
_SOURCE_RETRY_HEARTBEAT_SECONDS = 120.0
_CONNECTOR_VALIDATOR_METADATA_KEY = "office_validator_schema_version"


class GDrivePaginationIncompleteError(RuntimeError):
    """Drive paginationがloopまたは安全ページ上限で完走できなかった。"""


class IngestDurabilityError(RuntimeError):
    """cursorを進める前提となるdurable stateを確立できなかった。"""


class _RetryLeaseLostError(IngestDurabilityError):
    """処理中retryのleaseを更新できず、owner継続を証明できない。"""


class _RetryResolutionDurabilityError(IngestDurabilityError):
    """claim済みretryを確実にresolvedへ遷移できなかった。"""


class _IngestContentVolumeError(ValueError):
    """chunk/embeddingのファイル単位hard capを超えた。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass
class _DurabilityTracker:
    failures: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    @property
    def failed(self) -> bool:
        return bool(self.failures)


@dataclass
class _RetryLeaseHeartbeat:
    repository: IngestRepository
    source_kind: str
    source_id: str
    external_id: str
    trace_request_id: str
    lease_owner: str
    lease_token: str
    durability_tracker: _DurabilityTracker
    last_renewed_monotonic: float = 0.0

    def __call__(self, force: bool = False) -> None:
        now = time.monotonic()
        if (
            not force
            and self.last_renewed_monotonic > 0
            and now - self.last_renewed_monotonic < _SOURCE_RETRY_HEARTBEAT_SECONDS
        ):
            return
        renewer = getattr(self.repository, "renew_source_retry_lease", None)
        renewed = False
        if callable(renewer):
            try:
                renewed = bool(
                    renewer(
                        source_kind=self.source_kind,
                        source_id=self.source_id,
                        source_type="gdrive",
                        external_id=self.external_id,
                        request_id=self.trace_request_id,
                        expected_lease_owner=self.lease_owner,
                        expected_lease_token=self.lease_token,
                        lease_seconds=_SOURCE_RETRY_LEASE_SECONDS,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "ingest_source_retry_lease_renew_failed",
                    request_id=self.trace_request_id,
                    source_kind=self.source_kind,
                    source_id_ref=_external_id_ref(self.source_id),
                    external_id_ref=_external_id_ref(self.external_id),
                    error_type=type(exc).__name__,
                )
        if not renewed:
            self.durability_tracker.add("retry_lease_renew_failed")
            raise _RetryLeaseLostError("retry lease ownership could not be renewed")
        self.last_renewed_monotonic = now


# 対象2フォームの実セルは ja_JP の業務時刻で、ファイル記録の同一連番および Slack
# 投稿epochとの突合でも JST wall time と確認済み。Spreadsheet property の timeZone は
# Etc/GMT だが、FORMATTED_VALUE 自体を運用上の投稿時刻として扱う契約に固定する。
# 汎用シートへは使わず、下の fb/knowledge 判定を通った行だけに適用する。
_JST = _dt.timezone(_dt.timedelta(hours=9))
_GSHEET_TIMESTAMP_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


def _slack_message_modified_at(ts: str | None) -> str | None:
    """Slack の epoch 秒を DB 用 ISO8601 (UTC) にする。壊れた値は日付なしで継続。"""
    try:
        return _dt.datetime.fromtimestamp(float(ts or ""), tz=_dt.UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _gsheet_row_modified_at(fields: Mapping[str, str]) -> str | None:
    """Google Form の日本時間タイムスタンプを timezone 付き ISO8601 にする。

    Sheets API の FORMATTED_VALUE は実データで ``YYYY/MM/DD H:MM:SS``。秒なし・
    日付のみも既存/将来行のため受ける。正本の ``タイムスタンプ`` を優先し、空/不正時
    だけファイル記録の ``処理日時`` へ倒す。推測せず、両方を解釈できなければ None。
    """
    normalized = {_normalize_form_label(k): v for k, v in fields.items()}
    for label in ("タイムスタンプ", "処理日時"):
        raw = str(normalized.get(label) or "").strip()
        if not raw:
            continue
        for fmt in _GSHEET_TIMESTAMP_FORMATS:
            try:
                return _dt.datetime.strptime(raw, fmt).replace(tzinfo=_JST).isoformat()
            except ValueError:
                continue
    return None


def _envflag(name: str, default: str = "false") -> bool:
    """ENV を bool に変換（"1"/"true"/"yes" を True とみなす・factory._envflag と同流儀）。

    増分同期（``USE_INCREMENTAL_SYNC``）は既定 OFF。設定時のみ cursor 駆動の差分取得に切り替わる。

    末尾/前後空白は ``.strip()`` で落としてから判定する（skill.py の ``_envflag`` と同流儀）。
    これが無いと ``"true\n"`` 等の末尾空白付き値が無効化されて意図せず OFF 扱いになる。
    """
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _envint(name: str, default: int) -> int:
    """ENV を int に変換。未設定/空文字/非数値は ``default`` にフォールバックする。

    ``int(os.environ[...])`` を try 外で直接呼ぶと ``""`` や ``"3x"`` 等の非数値で
    ``ValueError`` が送出され ingest 全体が CRASH する。空文字も既定値に倒す
    （skill.py:202-210 の ``_envint`` と同流儀）。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _envfloat(name: str, default: float) -> float:
    """ENV を float に変換。未設定/空文字/非数値は ``default`` にフォールバックする。

    ``float(os.environ[...])`` を try 外で直接呼ぶと非数値で ``ValueError`` が送出され
    ingest 全体が CRASH する（skill.py:212-220 の ``_envfloat`` と同流儀）。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _disable_corpus_scan_timeouts(conn: Any) -> None:
    """コーパス横断処理の2 timeoutを当該transaction内だけ無制限（0）にする。

    コーパス横断の印付け（boilerplate / docdedup）は全 chunk / 全 doc を走査する重い
    UPDATE。SQL実行中は ``statement_timeout``、Pythonで比較している間は接続がidle-in-txに
    なるため ``idle_in_transaction_session_timeout`` の双方を解除しないと、本番30秒設定で
    fail-openの無音no-opになる。``SET LOCAL`` はcommit/rollbackで自動失効するため、検索系の
    別接続や次transactionには影響しない。SQLは固定リテラルのみで動的値を埋めない。
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '0'")  # nosec B608  # 固定リテラル・動的値なし
        cur.execute("SET LOCAL idle_in_transaction_session_timeout = '0'")  # nosec B608  # 固定リテラル・動的値なし


def _spec_source_id(spec: Any) -> str | None:
    """source spec から connector_state の source_id に使う識別子を引く（無ければ None）。"""
    for attr in ("channel_id", "folder_id", "sheet_id"):
        value = getattr(spec, attr, None)
        if value:
            return str(value)
    return None


def _external_id_ref(external_id: str) -> str:
    """ログ用の非可逆な短い参照。Drive ID/完全名をログへ出さず相関だけ可能にする。"""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:12]


def _drain_changes(
    client: Any,
    start_token: str,
    request_id: str,
    *,
    max_pages: int = 50,
) -> tuple[set[str], str | None]:
    """Drive changes.list を辿り、変更 file_id 集合と次回 cursor を返す。

    戻り値: (changed_file_ids, next_cursor)。next_cursor は最終ページの
    new_start_page_token（無ければ最後に使った token を保つ）。
    removed=True の変更は再取り込み対象外なので除外する。
    """
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    changed: set[str] = set()
    token: str | None = start_token
    next_cursor: str | None = start_token
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        if token is None:
            break
        if token in seen_tokens:
            raise GDrivePaginationIncompleteError("Drive changes pagination token loop")
        seen_tokens.add(token)
        batch = client.get_changes(page_token=token, request_id=request_id)
        for change in batch.changes:
            if change.file_id and not change.removed:
                changed.add(change.file_id)
        if batch.new_start_page_token:
            next_cursor = batch.new_start_page_token
        token = batch.next_page_token
    if token is not None:
        raise GDrivePaginationIncompleteError(
            f"Drive changes pagination exceeded {max_pages} pages"
        )
    return changed, next_cursor


# ナレッジ台帳行が「99_一次倉庫」系（検索対象外の生データ置き場）を指しているかを見る列。
# 実シート dump(2026-07-15) のヘッダ準拠。フォルダ/ファイル名を持ちうる運用列だけを見る。
# ⚠️ 現行 dump では **到達不能な保険**（旧_保管先フォルダ は date_client 形式・99_ は 0 件、
# 資料の概要_メイン は 提案/レポート 等の素の値）。将来 99_ 系の列値が現れた時に備えた保険で、
# 「今 99 を守れている」証明ではない。prod 稼働中のフォーム回答タブが持つ「ドライブ格納」を
# あえて含めないのは、その行に安定 ID を与えると既存 document を孤児化するため（stale 検出なし）。
# 判定規則そのものは gdrive 取込と同一の正本 regex（DEFAULT_EXCLUDE_FOLDER_NAME_RE）を
# _ingest_gsheet 内で lazy import して再利用する（sheet 側で独自ルールを持たない）。
# 本文(embedding/抜粋)に載せない運用列。人間の知見を含まないのに長大で、先頭に来ると
# export_vault の 160 字抜粋を食い潰し、ポイント/なぜ/フリーコメント が抜粋から落ちる。
# ※ヘッダは _normalize_form_label を通してから照合する（実シートは「保存ファイル(リンク付き)」が
#   半角括弧・フォーム回答タブは全角と揺れるため、生文字列一致だと黙って外れる）。
#
# ⚠️ **保存ファイル（リンク付き）は除外しない**。ファイル名は各行唯一の検索キー（資料名での
# キーワード検索が当たる）で、160 字問題の主犯ではない。主犯は**先頭列**にある 100 字超の
# Slack file URL（ファイルをアップ）。ファイル名は列順で ポイント/なぜ より後ろ＝抜粋窓を食わない。
_KNOWLEDGE_OPS_COLUMNS: frozenset[str] = frozenset(
    {
        "ファイルをアップ",  # Slack file URL（長大・知見ゼロ・160字問題の主犯）
        "タイムスタンプ",
        "連番",
        "保存ファイルリンク",  # Drive URL（長大）。ファイル名は 保存ファイル(リンク付き) に残す
        "保管先フォルダ",  # 資料の概要_メイン と完全ミラー（NN_ 接頭の有無のみ）＝冗長
        "旧_保管先フォルダ",
        "処理日時",
        "処理エラー",
        "保管先フォルダID記録（GAS処理）",
        "ドライブ格納",
    }
)
_KNOWLEDGE_OPS_NORM: frozenset[str] = frozenset(
    _normalize_form_label(h) for h in _KNOWLEDGE_OPS_COLUMNS
)


def _company_acl_groups() -> list[str]:
    """§G 会社共有: ``TEAMAGENT_SHARED_COMPANY_DOMAINS`` を ``documents.acl_groups`` に付与する。

    未設定なら ``[]``（従来挙動・後方互換）。設定時は会社メンバー identity が
    RLS の acl_groups intersect で当該 doc を読める＝社内ナレッジの横連携を有効化。
    Slack/GSheet 取込は従来 channel/owner スコープのため、本付与で会社共有にそろえる。
    """
    return sorted(shared_company_domains_from_env() or frozenset())


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
    warning_reasons: dict[str, int] = field(default_factory=dict)
    known_invalid_suppressed: int = 0

    @property
    def warning_count(self) -> int:
        return sum(self.warning_reasons.values())

    @property
    def outcome(self) -> str:
        if self.errors:
            return "failed"
        if self.warning_count:
            return "success_with_warnings"
        return "success"


@dataclass
class IngestResult:
    """ingest 全体の結果。"""

    by_kind: dict[str, IngestStats] = field(default_factory=dict)

    def total_documents(self) -> int:
        return sum(s.documents_upserted for s in self.by_kind.values())

    def total_errors(self) -> int:
        return sum(len(s.errors) for s in self.by_kind.values())

    def total_warnings(self) -> int:
        return sum(s.warning_count for s in self.by_kind.values())

    @property
    def outcome(self) -> str:
        if self.total_errors():
            return "failed"
        if self.total_warnings():
            return "success_with_warnings"
        return "success"


@dataclass(frozen=True)
class _WarningSnapshot:
    reasons: dict[str, int]
    suppressed: int


@dataclass
class _IngestWarningCollector:
    """source単位の分類済みwarningを、本文/完全IDなしで集計する。"""

    _reason_counts: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    _suppressed_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(
        self,
        source_kind: str,
        source_id: str,
        reason: str,
        *,
        suppressed: bool = False,
    ) -> None:
        key = (source_kind, source_id)
        counts = self._reason_counts.setdefault(key, {})
        counts[reason] = counts.get(reason, 0) + 1
        if suppressed:
            self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1

    def add_count(
        self,
        source_kind: str,
        source_id: str,
        reason: str,
        count: int,
    ) -> None:
        """reconciliation等の既集計件数をID非依存で加算する。"""
        if count < 1:
            return
        key = (source_kind, source_id)
        counts = self._reason_counts.setdefault(key, {})
        counts[reason] = counts.get(reason, 0) + count

    def snapshot(self, source_kind: str, source_id: str) -> _WarningSnapshot:
        key = (source_kind, source_id)
        return _WarningSnapshot(
            reasons=dict(self._reason_counts.get(key, {})),
            suppressed=self._suppressed_counts.get(key, 0),
        )

    def delta(
        self,
        source_kind: str,
        source_id: str,
        before: _WarningSnapshot,
    ) -> _WarningSnapshot:
        after = self.snapshot(source_kind, source_id)
        reasons = {
            reason: count - before.reasons.get(reason, 0)
            for reason, count in after.reasons.items()
            if count - before.reasons.get(reason, 0) > 0
        }
        return _WarningSnapshot(
            reasons=reasons,
            suppressed=max(0, after.suppressed - before.suppressed),
        )


_OFFICE_WARNING_REASONS = frozenset(
    {
        "html_response",
        "truncated_download",
        "size_mismatch",
        "checksum_mismatch",
        "corrupt_zip",
        "format_mismatch",
        "unsafe_archive",
        "encrypted_office",
        "download_too_large",
        "office_download_failed",
        "office_extract_failed",
        "office_empty_text",
        "unsafe_content_volume",
    }
)
_PERSISTENT_OFFICE_INVALID_REASONS = frozenset(
    {
        "corrupt_zip",
        "format_mismatch",
        "unsafe_archive",
        "unsafe_content_volume",
        "encrypted_office",
    }
)
_PDF_WARNING_REASONS = frozenset(
    {
        "pdf_download_failed",
        "pdf_extract_failed",
        "pdf_empty_text",
        "pdf_content_too_large",
    }
)
_PDF_VALIDATOR_SCHEMA_VERSION = "pdf-extract-v1"
_GENERIC_DRIVE_VALIDATOR_SCHEMA_VERSION = "gdrive-content-v1"


def _normalized_office_warning_reason(reason: str) -> str:
    return reason if reason in _OFFICE_WARNING_REASONS else "invalid_source"


def _should_skip_unchanged_gdrive_file(
    repository: IngestRepository,
    f: Any,
    *,
    request_id: str,
) -> bool:
    """保存済みMD5と一致するbinary fileは重い処理へ入る前に除外する。"""
    if not _envflag("USE_UNCHANGED_SKIP", "true"):
        return False

    current_checksum = getattr(f, "md5_checksum", None)
    # Google native形式はMD5を持たない。更新を取りこぼさないよう必ず従来処理へ流す。
    if not current_checksum:
        return False

    lookup = getattr(repository, "get_document_checksum", None)
    if not callable(lookup):
        return False
    try:
        stored_checksum = lookup("gdrive", f.id)
    except Exception as exc:
        # lookup障害では取り込みを止めず、従来のdownload/extract経路へfail-openする。
        logger.warning(
            "gdrive_checksum_lookup_failed",
            request_id=request_id,
            file_ref=_external_id_ref(f.id),
            error_type=type(exc).__name__,
        )
        return False

    unchanged = bool(stored_checksum) and str(stored_checksum).lower() == str(
        current_checksum
    ).lower()
    if unchanged:
        logger.info(
            "gdrive_unchanged_skipped",
            request_id=request_id,
            file_ref=_external_id_ref(f.id),
        )
    return unchanged


def _known_invalid_office_reason(
    repository: IngestRepository,
    f: Any,
) -> str | None:
    """ID+MD5+size+MIME+validator世代が一致する既知invalidだけを抑止する。"""
    from teamagent.ingest.office_extract import OFFICE_VALIDATOR_SCHEMA_VERSION

    md5_checksum = getattr(f, "md5_checksum", None)
    size_bytes = getattr(f, "size", None)
    if not md5_checksum or size_bytes is None:
        return None
    lookup = getattr(repository, "find_invalid_source_reason", None)
    if not callable(lookup):
        return None
    try:
        reason = lookup(
            "gdrive",
            f.id,
            md5_checksum,
            size_bytes,
            f.mime_type,
            OFFICE_VALIDATOR_SCHEMA_VERSION,
        )
        return str(reason) if reason is not None else None
    except Exception as exc:
        logger.warning(
            "ingest_source_health_lookup_failed",
            file_ref=_external_id_ref(f.id),
            error_type=type(exc).__name__,
        )
        return None


def _record_office_warning(
    *,
    repository: IngestRepository,
    f: Any,
    category: str,
    request_id: str,
    dry_run: bool,
    warning_collector: _IngestWarningCollector | None,
    warning_source_kind: str,
    warning_source_id: str,
    event: str,
    actual_bytes: int | None,
    expected_bytes: int | None,
    known_invalid: bool = False,
    error_type: str | None = None,
) -> None:
    """Office skipを集計し、確定的invalid payloadだけ fingerprint 単位で永続化する。"""
    from teamagent.ingest.office_extract import OFFICE_VALIDATOR_SCHEMA_VERSION

    reason = _normalized_office_warning_reason(category)
    if warning_collector is not None:
        warning_collector.add(
            warning_source_kind,
            warning_source_id,
            reason,
            suppressed=known_invalid,
        )

    md5_checksum = getattr(f, "md5_checksum", None)
    size_bytes = getattr(f, "size", None)
    should_record = known_invalid or reason in _PERSISTENT_OFFICE_INVALID_REASONS
    if not dry_run and should_record and md5_checksum and size_bytes is not None:
        recorder = getattr(repository, "record_invalid_source", None)
        if callable(recorder):
            try:
                recorder(
                    "gdrive",
                    f.id,
                    md5_checksum=md5_checksum,
                    size_bytes=size_bytes,
                    reason=reason,
                    mime_type=f.mime_type,
                    validator_schema_version=OFFICE_VALIDATOR_SCHEMA_VERSION,
                    request_id=request_id,
                    metadata={
                        "validation": "ooxml_pre_upsert",
                        "validator_schema_version": OFFICE_VALIDATOR_SCHEMA_VERSION,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "ingest_source_health_record_failed",
                    request_id=request_id,
                    file_ref=_external_id_ref(f.id),
                    category=reason,
                    error_type=type(exc).__name__,
                )

    logger.warning(
        event,
        request_id=request_id,
        file_ref=_external_id_ref(f.id),
        mime_type=f.mime_type,
        category=reason,
        actual_bytes=actual_bytes,
        expected_bytes=expected_bytes,
        known_invalid=known_invalid,
        retry_suppressed=known_invalid,
        error_type=error_type,
        existing_document_preserved=True,
    )


def _validator_schema_for_drive_file(f: Any) -> str:
    from teamagent.ingest.office_extract import (
        OFFICE_BINARY_MIMES,
        OFFICE_VALIDATOR_SCHEMA_VERSION,
    )

    if f.mime_type in OFFICE_BINARY_MIMES:
        return OFFICE_VALIDATOR_SCHEMA_VERSION
    if f.mime_type in _PDF_MIME_TYPES:
        return _PDF_VALIDATOR_SCHEMA_VERSION
    return _GENERIC_DRIVE_VALIDATOR_SCHEMA_VERSION


def _record_source_retry(
    *,
    repository: IngestRepository,
    f: Any,
    source_kind: str,
    source_id: str,
    reason: str,
    request_id: str,
    dry_run: bool,
    enabled: bool,
    durability_tracker: _DurabilityTracker | None = None,
    expected_lease_owner: str | None = None,
    expected_lease_token: str | None = None,
) -> bool:
    """incremental transient失敗をcursorと独立したdurable queueへ残す。"""
    if dry_run or not enabled:
        return True
    claimed = expected_lease_owner is not None or expected_lease_token is not None
    if claimed and (not expected_lease_owner or not expected_lease_token):
        if durability_tracker is not None:
            durability_tracker.add("retry_claim_fence_invalid")
        logger.error(
            "ingest_source_retry_claim_fence_invalid",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            file_ref=_external_id_ref(f.id),
        )
        return False
    recorder = getattr(repository, "record_source_retry", None)
    if not callable(recorder):
        if durability_tracker is not None:
            durability_tracker.add("retry_recorder_unavailable")
        return False
    try:
        persisted = bool(
            recorder(
                source_kind=source_kind,
                source_id=source_id,
                source_type="gdrive",
                external_id=f.id,
                md5_checksum=getattr(f, "md5_checksum", None),
                size_bytes=getattr(f, "size", None),
                mime_type=f.mime_type,
                validator_schema_version=_validator_schema_for_drive_file(f),
                reason=reason,
                request_id=request_id,
                metadata={"retry_class": "transient"},
                expected_lease_owner=expected_lease_owner,
                expected_lease_token=expected_lease_token,
                allow_unclaimed=not claimed,
            )
        )
    except Exception as exc:
        logger.warning(
            "ingest_source_retry_record_failed",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            file_ref=_external_id_ref(f.id),
            error_type=type(exc).__name__,
        )
        persisted = False
    if not persisted:
        if durability_tracker is not None:
            durability_tracker.add("retry_persistence_failed")
        logger.error(
            "ingest_source_retry_not_durable",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            file_ref=_external_id_ref(f.id),
            reason=reason,
        )
    return persisted


def _resolve_source_retry(
    *,
    repository: IngestRepository,
    f: Any,
    source_kind: str,
    source_id: str,
    request_id: str,
    dry_run: bool,
    expected_lease_owner: str | None = None,
    expected_lease_token: str | None = None,
    durability_tracker: _DurabilityTracker | None = None,
) -> bool:
    """成功/永久invalidでretryを解消し、claim済みなら失敗をdurability errorにする。"""
    if dry_run:
        return True
    resolution_required = expected_lease_owner is not None or expected_lease_token is not None
    if resolution_required and (not expected_lease_owner or not expected_lease_token):
        if durability_tracker is not None:
            durability_tracker.add("retry_claim_fence_invalid")
        raise _RetryResolutionDurabilityError("claimed retry fence is incomplete")
    resolver = getattr(repository, "resolve_source_retry", None)
    if not callable(resolver):
        if resolution_required:
            if durability_tracker is not None:
                durability_tracker.add("retry_resolver_unavailable")
            raise _RetryResolutionDurabilityError("retry resolver is unavailable")
        return False
    try:
        resolved = bool(
            resolver(
                source_kind=source_kind,
                source_id=source_id,
                source_type="gdrive",
                external_id=f.id,
                md5_checksum=getattr(f, "md5_checksum", None),
                size_bytes=getattr(f, "size", None),
                mime_type=f.mime_type,
                validator_schema_version=_validator_schema_for_drive_file(f),
                request_id=request_id,
                expected_lease_owner=expected_lease_owner,
                expected_lease_token=expected_lease_token,
                allow_unclaimed=not resolution_required,
            )
        )
    except Exception as exc:
        logger.warning(
            "ingest_source_retry_resolve_failed",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            file_ref=_external_id_ref(f.id),
            error_type=type(exc).__name__,
        )
        if resolution_required:
            if durability_tracker is not None:
                durability_tracker.add("retry_resolution_failed")
            raise _RetryResolutionDurabilityError("claimed retry could not be resolved") from exc
        return False
    if resolution_required and not resolved:
        if durability_tracker is not None:
            durability_tracker.add("retry_resolution_rejected")
        logger.error(
            "ingest_source_retry_resolution_not_durable",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            file_ref=_external_id_ref(f.id),
        )
        raise _RetryResolutionDurabilityError("claimed retry resolution was rejected")
    return resolved


def _resolve_reconciliation_gap(
    *,
    repository: IngestRepository,
    f: Any,
    request_id: str,
    dry_run: bool,
) -> None:
    """本文upsert成功時だけ、監査baselineのhashed source refを解消する。"""
    if dry_run:
        return
    resolver = getattr(repository, "resolve_reconciliation_gaps", None)
    if not callable(resolver):
        return
    try:
        resolver(source_kind="gdrive", external_id=f.id, request_id=request_id)
    except Exception as exc:
        logger.warning(
            "ingest_reconciliation_resolve_failed",
            request_id=request_id,
            file_ref=_external_id_ref(f.id),
            error_type=type(exc).__name__,
        )


def _record_pdf_warning(
    *,
    f: Any,
    category: str,
    request_id: str,
    warning_collector: _IngestWarningCollector | None,
    warning_source_kind: str,
    warning_source_id: str,
    event: str,
    error_type: str | None = None,
) -> None:
    reason = category if category in _PDF_WARNING_REASONS else "pdf_extract_failed"
    if warning_collector is not None:
        warning_collector.add(warning_source_kind, warning_source_id, reason)
    logger.warning(
        event,
        request_id=request_id,
        file_ref=_external_id_ref(f.id),
        mime_type=f.mime_type,
        category=reason,
        error_type=error_type,
        existing_document_preserved=True,
    )


def _send_ops_warning_summary(
    alerter: IngestOpsAlerter,
    *,
    kind: str,
    warning_reasons: dict[str, int],
    suppressed_retry_count: int,
    request_id: str,
    dry_run: bool,
) -> bool:
    """分類済みwarning集計だけを既存ops webhookへ送る。ID・名前・本文は含めない。"""
    reasons = {
        str(reason)[:80]: int(count)
        for reason, count in sorted(warning_reasons.items())
        if int(count) > 0
    }
    if dry_run or not alerter.enabled or not reasons:
        return False

    import httpx

    warning_count = sum(reasons.values())
    title = f":warning: Ingest {kind} completed with {warning_count} warning(s)"
    reason_lines = "\n".join(f"• `{reason}`: {count}" for reason, count in reasons.items())
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Kind*\n{kind}"},
                {"type": "mrkdwn", "text": "*Outcome*\nsuccess_with_warnings"},
                {"type": "mrkdwn", "text": f"*Warnings*\n{warning_count}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Known retries suppressed*\n{suppressed_retry_count}",
                },
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reasons*\n{reason_lines}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*Request ID* `{request_id}`"}],
        },
    ]
    try:
        response = httpx.post(
            str(alerter.webhook_url),
            json={"blocks": blocks, "text": title},
            timeout=alerter.timeout_s,
        )
        ok = response.status_code == 200
        logger.info(
            "ops_ingest_warning_sent",
            request_id=request_id,
            kind=kind,
            warning_count=warning_count,
            suppressed_retry_count=suppressed_retry_count,
            status=response.status_code,
            ok=ok,
        )
        return ok
    except Exception as exc:
        logger.warning(
            "ops_ingest_warning_send_failed",
            request_id=request_id,
            kind=kind,
            warning_count=warning_count,
            error_type=type(exc).__name__,
        )
        return False


# -----------------------------------------------------------
# Embedder Protocol（teamagent.adapters.embeddings_client.Embedder と互換）
# -----------------------------------------------------------
class _EmbedderProto(Protocol):
    def embed(self, text: str) -> list[float]: ...
    # 取り込みは passage 側プレフィックスで埋め込む（e5 非対称・embeddings_client 参照）。
    def embed_passage(self, text: str) -> list[float]: ...


def _bounded_chunk_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """chunk listを構築中からhard capし、巨大本文での一時メモリ膨張を防ぐ。"""
    from teamagent.ingest.pdf_extract import ChunkLimitExceededError, chunk_pages

    extracted_chars = 0
    for _, text in pages:
        extracted_chars += len(text)
        if extracted_chars > MAX_INGEST_EXTRACTED_CHARACTERS:
            raise _IngestContentVolumeError("extracted_text_limit")
    try:
        return chunk_pages(
            pages,
            size=500,
            overlap=100,
            max_chunks=MAX_INGEST_CHUNKS_PER_FILE,
        )
    except ChunkLimitExceededError as exc:
        raise _IngestContentVolumeError("chunk_limit") from exc


def _embed_page_chunks(
    page_chunks: list[tuple[int, str]],
    *,
    embedder: _EmbedderProto,
    lease_heartbeat: Callable[[bool], None] | None = None,
) -> list[ChunkUpsert]:
    """embedding件数をhard capし、利用可能ならまとめて埋め込む。"""
    if len(page_chunks) > MAX_INGEST_EMBEDDINGS_PER_FILE:
        raise _IngestContentVolumeError("embedding_limit")

    chunks: list[ChunkUpsert] = []
    embed_batch = getattr(embedder, "embed_passage_batch", None)
    if callable(embed_batch):
        batch_size = max(1, _envint("EMBED_BATCH_SIZE", 16))
        for offset in range(0, len(page_chunks), batch_size):
            batch = page_chunks[offset : offset + batch_size]
            # 行列演算の直前と直後にleaseを更新し、各バッチ中の失効余地を狭める。
            if lease_heartbeat is not None:
                lease_heartbeat(False)
            embeddings = list(embed_batch([text for _, text in batch]))
            if lease_heartbeat is not None:
                lease_heartbeat(False)
            if len(embeddings) != len(batch):
                raise ValueError("embed_passage_batch returned an unexpected number of embeddings")
            for batch_idx, ((page_num, content), embedding) in enumerate(
                zip(batch, embeddings, strict=True)
            ):
                chunks.append(
                    ChunkUpsert(
                        chunk_idx=offset + batch_idx,
                        content=content,
                        embedding=embedding,
                        metadata={"page_num": page_num},
                    )
                )
        return chunks

    # バッチ API を持たない実装は従来どおり 1 chunk ずつ処理する。
    for idx, (page_num, text) in enumerate(page_chunks):
        if lease_heartbeat is not None:
            lease_heartbeat(False)
        chunks.append(
            ChunkUpsert(
                chunk_idx=idx,
                content=text,
                embedding=embedder.embed_passage(text),
                metadata={"page_num": page_num},
            )
        )
    return chunks


def _record_generic_drive_warning(
    *,
    f: Any,
    category: str,
    request_id: str,
    warning_collector: _IngestWarningCollector | None,
    warning_source_kind: str,
    warning_source_id: str,
    event: str,
    error_type: str | None = None,
) -> None:
    """Google native/plain textの失敗を本文・名前・raw IDなしで集計する。"""
    if warning_collector is not None:
        warning_collector.add(
            warning_source_kind,
            warning_source_id,
            category,
        )
    logger.warning(
        event,
        request_id=request_id,
        file_ref=_external_id_ref(f.id),
        mime_type=f.mime_type,
        category=category,
        error_type=error_type,
        existing_document_preserved=True,
    )


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


def _is_title_only(chunks: list[ChunkUpsert]) -> bool:
    """この chunk 群が title_only フォールバック（未対応 mime の雛形）かを判定する。

    title_only 経路は chunk を 1 つだけ作り metadata に ``title_only=True`` を立てる。
    本文抽出経路の chunk には ``title_only`` フラグが無い（page_num を持つ）ので区別できる。
    空 chunk は title_only 扱いしない（呼び出し側で別途処理される）。
    """
    return bool(chunks) and all(c.metadata.get("title_only") for c in chunks)


def _guarded_upsert(
    repository: IngestRepository,
    doc: DocumentUpsert,
    chunks: list[ChunkUpsert],
    *,
    request_id: str,
    content_registry: set[tuple[str, str]] | None = None,
) -> bool:
    """§2 ガード付き upsert。本文版を書いた key を title_only 版で上書きしない。

    ``content_registry`` の process-local guard に加え、実 repository では同一 source key の
    transaction advisory lock と既存 content chunk の DB 確認を行う。別 run / 別 process
    から title-only が到来しても本文を上書きしない。

    ``content_registry`` は (source_type, external_id) → 本文版を書いたか、を記録する set。
    None なら呼び出し 1 回かぎりの空 set ＝ ガードは（同一呼び出し内でしか効かないが）安全側。
    ``IngestRunner.run`` は folder/crawl で 1 個を共有して渡す（run 間では新規 set ＝漏れない）。

    戻り値: 実際に upsert を行ったら True、ガードで skip したら False。
    """
    registry = content_registry if content_registry is not None else set()
    key = (doc.source_type, doc.external_id)
    title_only = _is_title_only(chunks)
    if title_only and key in registry:
        logger.info(
            "ingest_skip_title_only_over_content",
            source_type=doc.source_type,
            external_id_ref=_external_id_ref(doc.external_id),
            request_id=request_id,
            guard="process_registry",
        )
        return False
    if title_only:
        guarded_upsert = getattr(repository, "upsert_title_only_if_no_content", None)
        if callable(guarded_upsert):
            document_id = guarded_upsert(doc, chunks, request_id=request_id)
            if document_id is None:
                logger.info(
                    "ingest_skip_title_only_over_content",
                    source_type=doc.source_type,
                    external_id_ref=_external_id_ref(doc.external_id),
                    request_id=request_id,
                    guard="database",
                )
                return False
            return True
    repository.upsert_document_with_chunks(doc, chunks, request_id=request_id)
    if not title_only:
        registry.add(key)
    return True


def _guarded_claimed_retry_upsert(
    repository: IngestRepository,
    doc: DocumentUpsert,
    chunks: list[ChunkUpsert],
    *,
    f: Any,
    source_kind: str,
    source_id: str,
    request_id: str,
    lease_owner: str,
    lease_token: str,
    durability_tracker: _DurabilityTracker | None,
    content_registry: set[tuple[str, str]] | None = None,
) -> bool:
    """claimed retryをfence検証・document更新・resolveの単一transactionで処理する。"""
    registry = content_registry if content_registry is not None else set()
    key = (doc.source_type, doc.external_id)
    title_only = _is_title_only(chunks)

    # process内ですでに本文を書いたtitle-only退行ガードはDB write自体が不要。retry resolve
    # だけをexact fenceで行い、resolver failureはclaimed durability failureとして扱う。
    if title_only and key in registry:
        logger.info(
            "ingest_skip_title_only_over_content",
            source_type=doc.source_type,
            external_id_ref=_external_id_ref(doc.external_id),
            request_id=request_id,
            guard="process_registry",
        )
        _resolve_source_retry(
            repository=repository,
            f=f,
            source_kind=source_kind,
            source_id=source_id,
            request_id=request_id,
            dry_run=False,
            expected_lease_owner=lease_owner,
            expected_lease_token=lease_token,
            durability_tracker=durability_tracker,
        )
        return False

    atomic_upsert = getattr(
        repository,
        "upsert_document_with_chunks_and_resolve_retry",
        None,
    )
    if not callable(atomic_upsert):
        if durability_tracker is not None:
            durability_tracker.add("retry_atomic_upsert_unavailable")
        raise _RetryResolutionDurabilityError("claimed retry atomic upsert is unavailable")

    try:
        document_id = atomic_upsert(
            doc,
            chunks,
            request_id=request_id,
            source_kind=source_kind,
            source_id=source_id,
            expected_lease_owner=lease_owner,
            expected_lease_token=lease_token,
            protect_existing_content=title_only,
        )
    except Exception as exc:
        if durability_tracker is not None:
            durability_tracker.add("retry_atomic_upsert_failed")
        logger.warning(
            "ingest_claimed_retry_atomic_upsert_failed",
            request_id=request_id,
            source_kind=source_kind,
            source_id_ref=_external_id_ref(source_id),
            file_ref=_external_id_ref(doc.external_id),
            error_type=type(exc).__name__,
        )
        raise _RetryResolutionDurabilityError(
            "claimed retry document transaction was rejected"
        ) from exc

    if document_id is False:
        if durability_tracker is not None:
            durability_tracker.add("retry_atomic_upsert_rejected")
        raise _RetryResolutionDurabilityError("claimed retry atomic upsert was rejected")
    if document_id is None:
        logger.info(
            "ingest_skip_title_only_over_content",
            source_type=doc.source_type,
            external_id_ref=_external_id_ref(doc.external_id),
            request_id=request_id,
            guard="database",
        )
        return False
    if not title_only:
        registry.add(key)
    return True


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
    from teamagent.ingest.classify import build_classifier_from_env
    from teamagent.ingest.contextualize import build_contextualizer_from_env

    client = SlackChannelIngestClient.from_env()
    # ナレッジ自動分類（USE_DOC_CLASSIFY=1 のときだけ非 None。channel 単位で 1 回構築）。
    classifier = build_classifier_from_env()
    # Contextual Retrieval（USE_CONTEXTUAL_INGEST=1 のときだけ非 None。channel 単位で 1 回構築）。
    contextualizer = build_contextualizer_from_env()
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

        # Day 8 (2026-05-28): 営業 FB 投稿の構造化。
        # `*商流*` `*顧客名*` 等の Slack bold marker をパースして metadata に first-class field 化。
        # FB じゃない通常投稿は parse_fb_post() が空 dict を返すので副作用ゼロ。
        # 列名 alias 等の詳細は slack_fb_parser.py を参照。
        from teamagent.ingest.slack_fb_parser import (
            extract_client_name,
            parse_fb_post,
        )

        fb_metadata = parse_fb_post(text)
        derived_client_name = extract_client_name(fb_metadata) if fb_metadata else None

        external_id = f"{spec.channel_id}:{parent.thread_ts or parent.ts}"
        doc_metadata: dict[str, Any] = {
            **spec.extra_metadata,
            "channel_name": spec.channel_name,
            "channel_member_count": len(channel_acl_emails),
        }
        if fb_metadata:
            doc_metadata["is_sales_fb"] = True
            doc_metadata.update(fb_metadata)
            if derived_client_name:
                doc_metadata["client_name"] = derived_client_name

        # ナレッジ自動分類（案件 / 業界 / 資料種別 / 商談フェーズ）。
        # USE_DOC_CLASSIFY=1 のときだけ classifier が非 None。
        # 失敗しても取り込みは継続（fail-open）。cls_* キーは client_name 等の既存キーと
        # 衝突しないので update でマージする。contextualize より前（元の thread 本文）に実行。
        thread_title = f"{spec.channel_name} {parent.ts}"
        if classifier is not None:
            try:
                classification = classifier.classify(
                    title=thread_title, text=text, request_id=request_id
                )
            except Exception:
                logger.exception(
                    "slack_classify_unexpected",
                    channel_id=spec.channel_id,
                    thread_ts=parent.thread_ts or parent.ts,
                )
                classification = None
            if classification is not None:
                doc_metadata.update(classification.as_metadata())

        doc = DocumentUpsert(
            source_type="slack",
            external_id=external_id,
            source_uri=f"slack://{spec.channel_id}/{parent.thread_ts or parent.ts}",
            title=f"{spec.channel_name} {parent.ts}",
            owner_email=owner_email,
            acl_emails=acl_emails,
            acl_groups=_company_acl_groups(),  # §G 会社共有（未設定なら []）
            metadata=doc_metadata,
            # Slack ts は投稿時刻そのもの。従来 None だったため /app で全投稿が
            # 「日付不明」になっていた。外部 API や LLM で推測せず、元 epoch を正本にする。
            modified_at=_slack_message_modified_at(parent.thread_ts or parent.ts),
        )
        chunks = [
            ChunkUpsert(
                chunk_idx=0,
                content=text,
                embedding=embedder.embed_passage(text),
                metadata={"reply_count": parent.reply_count},
            )
        ]
        # Contextual Retrieval: thread 本文を full_text に文脈前置詞を付与（fail-open 内蔵）。
        # contextualizer が None（既定）なら chunks はそのまま＝従来挙動・完全後方互換。
        if contextualizer is not None:
            chunks = contextualizer.contextualize_chunks(
                doc.title or spec.channel_name, text, chunks, request_id
            )
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
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    out: list[Any] = []
    token: str | None = None
    seen_tokens: set[str | None] = set()
    for _ in range(max_pages):
        if token in seen_tokens:
            raise GDrivePaginationIncompleteError("Drive files pagination token loop")
        seen_tokens.add(token)
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
    if token:
        raise GDrivePaginationIncompleteError(f"Drive files pagination exceeded {max_pages} pages")
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

# Google ネイティブ本文化（INGEST_RICH_EXTRACT=1 のときだけ有効）。
# gdoc は従来から folder 経路で本文化されている（_process_one_gdrive_file 〜815-844）。
# gslide / gsheet / plain-text は rich モードで初めて本文 chunk になる。
_GSLIDE_NATIVE_MIME = "application/vnd.google-apps.presentation"
_GSHEET_NATIVE_MIME = "application/vnd.google-apps.spreadsheet"

# rich モードで本文化する plain-text 系 mime（download_file_bytes → decode → chunk）。
_PLAIN_TEXT_MIMES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
    }
)

# Google ネイティブ gsheet を crawl で本文化する際のセル/行ガード（xlsx の上限ガードと対称）。
# 1 タブあたり、1 spreadsheet あたりの行数を上限で打ち切り、暴走を防ぐ。
_GSHEET_MAX_ROWS_PER_TAB = 2000
_GSHEET_MAX_ROWS_PER_SHEET = 20000


def _rich_extract_enabled() -> bool:
    """``INGEST_RICH_EXTRACT`` を 1 回だけ読む（"1"/"true"/"yes" で有効・既定 OFF）。

    OFF のとき pipeline の挙動は現行と 1 バイトも変えない（後方互換）。
    呼び出し側（folder / crawl handler）が handler 開始時に 1 回だけ呼ぶ。
    """
    return _envflag("INGEST_RICH_EXTRACT")


def _decode_text_bytes(data: bytes) -> str:
    """text/plain・markdown・csv のバイト列を UTF-8（不正バイトは置換）でデコードする。"""
    return data.decode("utf-8", errors="replace")


def _extract_gslide_pages(file_id: str, request_id: str) -> list[tuple[int, str]] | None:
    """gslide 本文＋ノートを取得して [(1, text)] を返す（fail-open: 失敗時 None）。

    None を返すと呼び出し側はwarningとしてskipする。lazy importでadapter依存を
    この分岐内に閉じる。
    """
    try:
        from teamagent.adapters.gslides_client import GSlidesClient

        gslides = GSlidesClient.from_env()
        content = gslides.get_presentation_text(file_id, request_id)
        text = (content.text or "").strip()
    except Exception:
        logger.warning(
            "gdrive_gslide_extract_failed",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
            exc_info=True,
        )
        return None
    if not text:
        logger.warning(
            "gdrive_gslide_empty_text",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
        )
        return None
    return [(1, text)]


def _extract_gsheet_native_pages(file_id: str, request_id: str) -> list[tuple[int, str]] | None:
    """Google ネイティブ gsheet をタブ単位で document 化（fail-open: 失敗時 None）。

    get_sheet_metadata でタブを列挙 → 各タブ get_tab_rows → format_row_as_document。
    xlsx と同様にセル/行の上限ガードを入れ、暴走を防ぐ。タブ＝page_num（1 始まり）。
    None を返すと呼び出し側はwarningとしてskipする。
    """
    try:
        from teamagent.adapters.gsheets_client import (
            GSheetsClient,
            format_row_as_document,
        )

        gsheets = GSheetsClient.from_env()
        meta = gsheets.get_sheet_metadata(sheet_id=file_id, request_id=request_id)
        pages: list[tuple[int, str]] = []
        total_rows = 0
        for tab_idx, tab in enumerate(meta.tabs, start=1):
            tab_rows = gsheets.get_tab_rows(
                sheet_id=file_id, tab_name=tab.title, request_id=request_id
            )
            if not tab_rows.headers:
                continue
            lines: list[str] = []
            for row in tab_rows.rows:
                doc_text = format_row_as_document(tab_rows.headers, row)
                if doc_text.strip():
                    if (
                        total_rows >= _GSHEET_MAX_ROWS_PER_SHEET
                        or len(lines) >= _GSHEET_MAX_ROWS_PER_TAB
                    ):
                        raise _IngestContentVolumeError("native_sheet_row_limit")
                    lines.append(doc_text)
                    total_rows += 1
            if lines:
                pages.append((tab_idx, "\n\n".join(lines)))
    except _IngestContentVolumeError:
        raise
    except Exception:
        logger.warning(
            "gdrive_gsheet_extract_failed",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
            exc_info=True,
        )
        return None
    if not pages:
        logger.warning(
            "gdrive_gsheet_empty_text",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
        )
        return None
    return pages


def _extract_plaintext_pages(
    client: Any, file_id: str, request_id: str
) -> list[tuple[int, str]] | None:
    """text/plain・markdown・csv を download → UTF-8 decode → [(1, text)]（fail-open）。"""
    try:
        data = client.download_file_bytes(file_id=file_id, request_id=request_id)
        text = _decode_text_bytes(data).strip()
    except Exception:
        logger.warning(
            "gdrive_plaintext_extract_failed",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
            exc_info=True,
        )
        return None
    if not text:
        logger.warning(
            "gdrive_plaintext_empty_text",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
        )
        return None
    return [(1, text)]


def _rich_native_pages(f: Any, *, client: Any, request_id: str) -> list[tuple[int, str]] | None:
    """rich モードで gdoc/gslide/gsheet/plain-text を本文ページ化する。

    対象 mime でないか、抽出に失敗（fail-open）した場合は ``None`` を返し、
    呼び出し側はwarningとしてskipする。
    呼び出し側は ``INGEST_RICH_EXTRACT`` が ON のときだけ本関数を呼ぶこと。
    """
    from teamagent.ingest.office_extract import GDOC_NATIVE_MIME

    mime = f.mime_type
    if mime == GDOC_NATIVE_MIME:
        return _extract_gdoc_pages(f.id, request_id)
    if mime == _GSLIDE_NATIVE_MIME:
        return _extract_gslide_pages(f.id, request_id)
    if mime == _GSHEET_NATIVE_MIME:
        return _extract_gsheet_native_pages(f.id, request_id)
    if mime in _PLAIN_TEXT_MIMES:
        return _extract_plaintext_pages(client, f.id, request_id)
    return None


def _extract_gdoc_pages(file_id: str, request_id: str) -> list[tuple[int, str]] | None:
    """gdoc 本文を Docs API で取得して [(1, text)] を返す（fail-open: 失敗時 None）。

    folder 経路の既存 gdoc 実装（〜815-844）と同じ作法。crawl 経路から共用する。
    """
    try:
        from teamagent.adapters.gdocs_client import GDocsClient

        gdocs = GDocsClient.from_env()
        doc_content = gdocs.get_document_text(document_id=file_id, request_id=request_id)
        text = (doc_content.text or "").strip()
    except Exception:
        logger.warning(
            "gdrive_gdoc_extract_failed",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
            exc_info=True,
        )
        return None
    if not text:
        logger.warning(
            "gdrive_gdoc_empty_text",
            file_ref=_external_id_ref(file_id),
            request_id=request_id,
        )
        return None
    return [(1, text)]


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
    content_registry: set[tuple[str, str]] | None = None,
    exclude_folder_name_re: str | None = None,
    observed_gdrive_ids: set[str] | None = None,
    truncated_walk_roots: set[str] | None = None,
    warning_collector: _IngestWarningCollector | None = None,
) -> tuple[int, int]:
    """1 Drive folder を取り込む。

    対象: spec.mime_type_filter で絞り込んだファイル（既定は spec 側で application/pdf）。
    - PDF: download_file_bytes → pypdf でテキスト抽出 → ページごとにチャンク化 → embedding
    - 非 PDF: title だけ embed して 1 chunk（メタデータ用、将来 Google Doc export 対応で拡張）

    ACL は permissions.list を呼んで documents.acl_emails / acl_groups に写像する。

    入れ込み v2 (2026-07-10):
    - exclude_folder_name_re: サブフォルダ名の除外 regex（yaml グローバルキー由来。
      None ならコード既定 DEFAULT_EXCLUDE_FOLDER_NAME_RE・空文字 "" で除外なし）。
    - observed_gdrive_ids: run 中に Drive 上で観測した file_id を集める set
      （INGEST_MARK_STALE の stale 差集合用。run 単位で 1 個を共有）。
    - truncated_walk_roots: walk が max_files 上限で打ち切られた root（folder_id）を
      集める set（stale 堅牢化。打ち切り run では観測集合が不完全＝mark を skip する）。
    """
    from teamagent.adapters.gdrive_client import (
        DEFAULT_EXCLUDE_FOLDER_NAME_RE,
        DEFAULT_WALK_MAX_FILES,
        GDriveClient,
    )
    from teamagent.ingest.classify import build_classifier_from_env
    from teamagent.ingest.contextualize import build_contextualizer_from_env
    from teamagent.ingest.office_extract import OFFICE_VALIDATOR_SCHEMA_VERSION

    # yaml キー未記載（None）→ コード既定の 99_一次倉庫系除外。空文字 "" → 除外なし。
    effective_exclude_re = (
        exclude_folder_name_re
        if exclude_folder_name_re is not None
        else DEFAULT_EXCLUDE_FOLDER_NAME_RE
    )

    # Day 7: folder bulk ingest のため readonly=True で drive.readonly スコープを使う。
    # Internal OAuth なので CASA 審査不要。drive.file ではフォルダ単位の取り込みが
    # できない（per-file opened only の制約）ため、readonly に切替。
    client = GDriveClient.from_env(readonly=True)
    # ナレッジ自動分類（USE_DOC_CLASSIFY=1 のときだけ非 None。folder 単位で 1 回構築）。
    classifier = build_classifier_from_env()
    # Contextual Retrieval（USE_CONTEXTUAL_INGEST=1 のときだけ非 None。folder 単位で 1 回構築）。
    contextualizer = build_contextualizer_from_env()
    docs_n = 0
    chunks_n = 0
    skipped: list[str] = []
    warning_source_kind = "gdrive"
    warning_source_id = spec.folder_id
    warning_before = (
        warning_collector.snapshot(warning_source_kind, warning_source_id)
        if warning_collector is not None
        else _WarningSnapshot(reasons={}, suppressed=0)
    )
    durability_tracker = _DurabilityTracker()

    # 増分同期（USE_INCREMENTAL_SYNC=1 で opt-in・既定 OFF＝従来フル走査で完全後方互換）。
    # connector_state の前回 cursor から Drive changes.list の差分（変更 file のみ）に絞り込む。
    incremental = _envflag("USE_INCREMENTAL_SYNC")
    changed_ids: set[str] | None = None
    next_cursor: str | None = None
    retry_claims: list[Any] = []
    retry_ids: set[str] = set()
    retry_lease_owners: dict[str, str] = {}
    retry_lease_tokens: dict[str, str] = {}
    if incremental:
        prior_cursor: str | None = None
        state: Any | None = None
        try:
            state = repository.load_connector_state("gdrive", spec.folder_id)
            prior_cursor = state.cursor if state else None
        except Exception:
            logger.exception(
                "connector_state_load_failed", source_kind="gdrive", source_id=spec.folder_id
            )
        prior_validator_version = (
            str((getattr(state, "metadata", {}) or {}).get(_CONNECTOR_VALIDATOR_METADATA_KEY) or "")
            if state is not None
            else ""
        )
        validator_generation_changed = bool(prior_cursor) and (
            prior_validator_version != OFFICE_VALIDATOR_SCHEMA_VERSION
        )
        if validator_generation_changed:
            logger.warning(
                "gdrive_validator_generation_changed",
                request_id=request_id,
                folder_ref=_external_id_ref(spec.folder_id),
                previous_validator=prior_validator_version or "unrecorded",
                current_validator=OFFICE_VALIDATOR_SCHEMA_VERSION,
                action="full_revalidation",
            )
        if prior_cursor and not validator_generation_changed:
            try:
                changed_ids, next_cursor = _drain_changes(client, prior_cursor, request_id)
                logger.info(
                    "gdrive_incremental_changes",
                    folder_id=spec.folder_id,
                    changed=len(changed_ids),
                )
            except Exception:
                logger.exception("gdrive_get_changes_failed", folder_id=spec.folder_id)
                changed_ids = None  # 差分取得失敗 → フル走査にフォールバック
        if changed_ids is None:
            # 初回・validator世代変更・差分取得失敗はフル走査。走査開始前のtokenを次回基点にする。
            try:
                next_cursor = client.get_start_page_token(request_id)
            except Exception:
                logger.exception("gdrive_start_page_token_failed", folder_id=spec.folder_id)
        if not dry_run:
            claimer = getattr(repository, "claim_due_source_retries", None)
            if callable(claimer):
                try:
                    retry_claims = list(
                        claimer(
                            source_kind="gdrive",
                            source_id=spec.folder_id,
                            request_id=request_id,
                        )
                    )
                    for retry in retry_claims:
                        external_id = str(retry.external_id)
                        lease_owner = str(getattr(retry, "lease_owner", "") or "")
                        lease_token = str(getattr(retry, "lease_token", "") or "")
                        if not lease_owner or not lease_token:
                            durability_tracker.add("retry_claim_fence_invalid")
                            logger.error(
                                "ingest_source_retry_claim_fence_invalid",
                                request_id=request_id,
                                source_kind="gdrive",
                                source_id_ref=_external_id_ref(spec.folder_id),
                                file_ref=_external_id_ref(external_id),
                            )
                            raise IngestDurabilityError(
                                "claimed retry did not include an owner/token fence"
                            )
                        retry_ids.add(external_id)
                        retry_lease_owners[external_id] = lease_owner
                        retry_lease_tokens[external_id] = lease_token
                except IngestDurabilityError:
                    raise
                except Exception as exc:
                    durability_tracker.add("retry_claim_failed")
                    logger.warning(
                        "ingest_source_retry_claim_failed",
                        request_id=request_id,
                        source_kind="gdrive",
                        source_id_ref=_external_id_ref(spec.folder_id),
                        error_type=type(exc).__name__,
                    )
            else:
                durability_tracker.add("retry_claimer_unavailable")

    if spec.include_subfolders:
        # Day 7 (2026-05-27): walk_files_recursive はサブフォルダを BFS する。
        # mime_type は server-side で絞れないので、client-side で post-filter する。
        all_files = client.walk_files_recursive(
            root_id=spec.folder_id,
            request_id=request_id,
            exclude_folder_name_re=effective_exclude_re,
        )
        # stale 堅牢化: walk が max_files（既定値）で打ち切られた可能性がある場合は
        # run 単位フラグに集約し、partial setでcursor/staleを進めないようsourceを失敗させる。
        # len == 上限の完走との区別がadapter I/F上つかないため、安全側にfail-closedする。
        if len(all_files) >= DEFAULT_WALK_MAX_FILES:
            if truncated_walk_roots is not None:
                truncated_walk_roots.add(spec.folder_id)
            raise GDrivePaginationIncompleteError(
                f"Drive recursive listing reached {DEFAULT_WALK_MAX_FILES} file safety limit"
            )
        # stale 差集合用: mime post-filter の**前**（Drive 上に存在が確認できた全 file）で
        # 観測済みを記録する（設定の絞り込みで存在中の file が stale 誤爆しないよう安全側）。
        if observed_gdrive_ids is not None:
            observed_gdrive_ids.update(f.id for f in all_files)
        if spec.mime_type_filter:
            files = [f for f in all_files if f.mime_type == spec.mime_type_filter]
        else:
            files = all_files
    else:
        files = _list_all_gdrive_files(
            client=client,
            folder_id=spec.folder_id,
            request_id=request_id,
            mime_type_filter=spec.mime_type_filter,
        )
        # こちらは server-side mime filter 後しか列挙できない（観測＝列挙できた file）。
        if observed_gdrive_ids is not None:
            observed_gdrive_ids.update(f.id for f in files)

    # 増分絞り込みの**前**に観測を取る理由: 増分 run でも列挙自体はフル走査なので、
    # 「変更が無かっただけの file」が stale 扱いになる誤爆を防げる。

    listed_ids = {f.id for f in files}
    missing_retry_ids = retry_ids - listed_ids
    if missing_retry_ids:
        if warning_collector is not None:
            warning_collector.add_count(
                warning_source_kind,
                warning_source_id,
                "retry_source_missing",
                len(missing_retry_ids),
            )
        retry_recorder = getattr(repository, "record_source_retry", None)
        if not dry_run and not callable(retry_recorder):
            durability_tracker.add("retry_recorder_unavailable")
        elif callable(retry_recorder) and not dry_run:
            for retry in retry_claims:
                if retry.external_id not in missing_retry_ids:
                    continue
                try:
                    persisted = bool(
                        retry_recorder(
                            source_kind="gdrive",
                            source_id=spec.folder_id,
                            source_type="gdrive",
                            external_id=retry.external_id,
                            md5_checksum=retry.md5_checksum,
                            size_bytes=retry.size_bytes,
                            mime_type=retry.mime_type,
                            validator_schema_version=retry.validator_schema_version,
                            reason="retry_source_missing",
                            request_id=request_id,
                            metadata={"retry_class": "source_not_listed"},
                            expected_lease_owner=retry_lease_owners[retry.external_id],
                            expected_lease_token=retry_lease_tokens[retry.external_id],
                        )
                    )
                except Exception as exc:
                    persisted = False
                    logger.warning(
                        "ingest_source_retry_missing_record_failed",
                        request_id=request_id,
                        source_id_ref=_external_id_ref(spec.folder_id),
                        external_id_ref=_external_id_ref(retry.external_id),
                        error_type=type(exc).__name__,
                    )
                if not persisted:
                    durability_tracker.add("retry_persistence_failed")

    # 増分: 新規変更または期限到来retryだけに絞る（changed_ids=Noneならフル走査）。
    if changed_ids is not None:
        selected_ids = changed_ids | retry_ids
        files = [f for f in files if f.id in selected_ids]

    for f in files:
        # Day 7 (2026-05-27): 1 ファイル失敗で全体停止しないよう全体を try/except でラップ。
        # 既存の細かい try/except (download/extract) はそのまま生かす。
        lease_heartbeat: Callable[[bool], None] | None = None
        if incremental and not dry_run and f.id in retry_ids:
            lease_heartbeat = _RetryLeaseHeartbeat(
                repository=repository,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                external_id=f.id,
                trace_request_id=request_id,
                lease_owner=retry_lease_owners[f.id],
                lease_token=retry_lease_tokens[f.id],
                durability_tracker=durability_tracker,
            )
        try:
            docs_added, chunks_added = _process_one_gdrive_file(
                f=f,
                spec=spec,
                client=client,
                embedder=embedder,
                repository=repository,
                owner_email=owner_email,
                dry_run=dry_run,
                request_id=request_id,
                skipped=skipped,
                classifier=classifier,
                contextualizer=contextualizer,
                content_registry=content_registry,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                durable_retry=incremental,
                durability_tracker=durability_tracker,
                lease_heartbeat=lease_heartbeat,
                retry_lease_owner=retry_lease_owners.get(f.id),
                retry_lease_token=retry_lease_tokens.get(f.id),
            )
            docs_n += docs_added
            chunks_n += chunks_added
            if incremental and not dry_run and docs_added > 0:
                _safe_record_job(repository, "gdrive", f.id, success=True, request_id=request_id)
        except _RetryLeaseLostError:
            logger.warning(
                "gdrive_retry_lease_lost",
                request_id=request_id,
                file_ref=_external_id_ref(f.id),
                folder_ref=_external_id_ref(spec.folder_id),
            )
            skipped.append(f.id)
            _safe_record_job(
                repository,
                "gdrive",
                f.id,
                success=False,
                error="retry_lease_lost",
                request_id=request_id,
            )
            continue
        except _RetryResolutionDurabilityError:
            logger.warning(
                "gdrive_retry_resolution_failed",
                request_id=request_id,
                file_ref=_external_id_ref(f.id),
                folder_ref=_external_id_ref(spec.folder_id),
            )
            skipped.append(f.id)
            _safe_record_job(
                repository,
                "gdrive",
                f.id,
                success=False,
                error="retry_resolution_failed",
                request_id=request_id,
            )
            continue
        except Exception:
            logger.exception(
                "gdrive_file_unexpected_error",
                file_ref=_external_id_ref(f.id),
                folder_ref=_external_id_ref(spec.folder_id),
            )
            skipped.append(f.id)
            if incremental and not dry_run:
                _record_source_retry(
                    repository=repository,
                    f=f,
                    source_kind=warning_source_kind,
                    source_id=warning_source_id,
                    reason="gdrive_file_unexpected_error",
                    request_id=request_id,
                    dry_run=dry_run,
                    enabled=True,
                    durability_tracker=durability_tracker,
                    expected_lease_owner=retry_lease_owners.get(f.id),
                    expected_lease_token=retry_lease_tokens.get(f.id),
                )
                _safe_record_job(
                    repository,
                    "gdrive",
                    f.id,
                    success=False,
                    error="gdrive_file_unexpected_error",
                    request_id=request_id,
                )
            continue

    warning_delta = (
        warning_collector.delta(warning_source_kind, warning_source_id, warning_before)
        if warning_collector is not None
        else _WarningSnapshot(reasons={}, suppressed=0)
    )

    if incremental and not dry_run and durability_tracker.failed:
        logger.error(
            "gdrive_cursor_blocked_by_durability_failure",
            request_id=request_id,
            folder_ref=_external_id_ref(spec.folder_id),
            failures=durability_tracker.failures,
            next_cursor_present=bool(next_cursor),
        )
        raise IngestDurabilityError("durable retry state was not established; cursor not advanced")

    # 成功時に cursor を前進保存（次回はこの cursor 以降の差分だけ取る）。
    if incremental and not dry_run:
        try:
            repository.save_connector_state(
                "gdrive",
                spec.folder_id,
                cursor=next_cursor,
                success=True,
                metadata={
                    "outcome": ("success_with_warnings" if warning_delta.reasons else "success"),
                    "warning_count": sum(warning_delta.reasons.values()),
                    "warning_reasons": warning_delta.reasons,
                    "known_invalid_suppressed": warning_delta.suppressed,
                    _CONNECTOR_VALIDATOR_METADATA_KEY: OFFICE_VALIDATOR_SCHEMA_VERSION,
                },
            )
        except Exception as exc:
            logger.exception("connector_state_save_failed", folder_id=spec.folder_id)
            raise IngestDurabilityError("connector cursor state could not be saved") from exc

    logger.info(
        "ingest_gdrive_folder_done",
        folder_id=spec.folder_id,
        folder_name=spec.folder_name,
        documents=docs_n,
        chunks=chunks_n,
        skipped=len(skipped),
        warning_count=sum(warning_delta.reasons.values()),
        warning_reasons=warning_delta.reasons,
        known_invalid_suppressed=warning_delta.suppressed,
        outcome="success_with_warnings" if warning_delta.reasons else "success",
        incremental=incremental,
        dry_run=dry_run,
    )
    return docs_n, chunks_n


def _safe_record_job(
    repository: IngestRepository,
    source_type: str,
    external_id: str,
    *,
    success: bool,
    error: str | None = None,
    request_id: str,
) -> None:
    """ingest_jobs への state 記録を best-effort で行う（記録失敗で取り込みは止めない）。"""
    try:
        repository.record_ingest_job(
            source_type,
            external_id,
            state="COMMITTED" if success else "FAILED_TRANSIENT",
            batch_id=request_id,
            success=success,
            error=error,
        )
    except Exception:
        logger.exception(
            "ingest_job_record_failed",
            external_id_ref=_external_id_ref(external_id),
        )


def _process_one_gdrive_file(
    f: Any,
    spec: GDriveFolderSpec,
    *,
    client: Any,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
    skipped: list[str],
    classifier: DocClassifier | None = None,
    contextualizer: ChunkContextualizer | None = None,
    content_registry: set[tuple[str, str]] | None = None,
    warning_collector: _IngestWarningCollector | None = None,
    warning_source_kind: str = "gdrive",
    warning_source_id: str = "gdrive",
    durable_retry: bool = False,
    durability_tracker: _DurabilityTracker | None = None,
    lease_heartbeat: Callable[[bool], None] | None = None,
    retry_lease_owner: str | None = None,
    retry_lease_token: str | None = None,
) -> tuple[int, int]:
    """1 ファイル分の処理を切り出し（_ingest_gdrive_folder から呼ばれる）。

    Day 7 (2026-05-27): 1 ファイル失敗で全体停止しないように、
    呼び出し側を try/except でラップ可能に。

    classifier が非 None（USE_DOC_CLASSIFY=1）の時は本文抜粋を分類し、
    案件 / 業界 / 資料種別 / 商談フェーズを documents.metadata に付与する（fail-open）。

    contextualizer が非 None（USE_CONTEXTUAL_INGEST=1）の時は抽出ページを結合した全文を
    full_text に各 chunk へ文脈前置詞を付与し contextualized + embedding を差し替える
    （fail-open）。None（既定）なら chunks はそのまま＝従来挙動・完全後方互換。
    """
    from teamagent.adapters.gdrive_client import GDriveDownloadContentError
    from teamagent.ingest.office_extract import (
        GDOC_NATIVE_MIME,
        OFFICE_BINARY_MIMES,
        OfficePayloadError,
        extract_office_pages,
    )
    from teamagent.ingest.pdf_extract import extract_pdf_pages

    if lease_heartbeat is not None:
        lease_heartbeat(True)

    if _should_skip_unchanged_gdrive_file(repository, f, request_id=request_id):
        # claimed retryが残っている場合だけlease fence付きで解消する。
        # document/chunksは触らない。
        if retry_lease_owner is not None or retry_lease_token is not None:
            _resolve_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                request_id=request_id,
                dry_run=dry_run,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
                durability_tracker=durability_tracker,
            )
        skipped.append(f.id)
        return 0, 0

    # INGEST_RICH_EXTRACT=1 のときだけ rich 抽出（gslide/gsheet/plain-text 本文化・抽出器の
    # rich 引数）を有効化。OFF（既定）は現行と 1 バイトも挙動を変えない（後方互換）。
    rich = _rich_extract_enabled()
    # rich 引数は kwargs dict で渡す（OFF のときは空 dict ＝引数なし呼び出しと同じ）。
    # 別 agent が extract_pdf_pages / extract_office_pages に同名・既定値=現行挙動で
    # これらを実装する。OFF（既定）では空 dict なので両関数の現行シグネチャと完全互換。
    pdf_kwargs: dict[str, Any] = {"min_chars": 40} if rich else {}
    office_kwargs: dict[str, Any] = (
        {
            "include_notes": True,
            "include_tables": True,
            "formula_fallback": True,
            "min_chars": 40,
        }
        if rich
        else {}
    )

    if f.mime_type in OFFICE_BINARY_MIMES:
        known_reason = _known_invalid_office_reason(repository, f)
        if known_reason is not None:
            _record_office_warning(
                repository=repository,
                f=f,
                category=known_reason,
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_payload_invalid",
                actual_bytes=None,
                expected_bytes=f.size,
                known_invalid=True,
            )
            _resolve_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                request_id=request_id,
                dry_run=dry_run,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
                durability_tracker=durability_tracker,
            )
            skipped.append(f.id)
            return 0, 0

    # ACL を permissions.list で解決
    file_owner_email, acl_emails, acl_groups = _resolve_drive_file_acl(
        client=client,
        file_id=f.id,
        request_id=request_id,
        fallback_owner_email=f.owners_email[0] if f.owners_email else owner_email,
    )

    # PDF / Office (docx/pptx/xlsx) / Google native gdoc は chunk 化、他は title のみ
    chunks: list[ChunkUpsert] = []
    if f.mime_type in _PDF_MIME_TYPES:
        try:
            data = client.download_file_bytes(file_id=f.id, request_id=request_id)
        except Exception as exc:
            _record_pdf_warning(
                f=f,
                category="pdf_download_failed",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_pdf_download_failed",
                error_type=type(exc).__name__,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="pdf_download_failed",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        try:
            pages = extract_pdf_pages(data, **pdf_kwargs)
            page_chunks = _bounded_chunk_pages(pages)
            if page_chunks:
                chunks.extend(
                    _embed_page_chunks(
                        page_chunks,
                        embedder=embedder,
                        lease_heartbeat=lease_heartbeat,
                    )
                )
        except _RetryLeaseLostError:
            raise
        except _IngestContentVolumeError:
            _record_pdf_warning(
                f=f,
                category="pdf_content_too_large",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_pdf_content_too_large",
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="pdf_content_too_large",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        except Exception as exc:
            _record_pdf_warning(
                f=f,
                category="pdf_extract_failed",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_pdf_extract_failed",
                error_type=type(exc).__name__,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="pdf_extract_failed",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        if not page_chunks:
            _record_pdf_warning(
                f=f,
                category="pdf_empty_text",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_pdf_empty_text",
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="pdf_empty_text",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
    elif f.mime_type in OFFICE_BINARY_MIMES:
        # docx / pptx / xlsx: download → extract → chunk_pages（PDF と同じ I/F）
        try:
            data = client.download_file_bytes(file_id=f.id, request_id=request_id)
        except GDriveDownloadContentError as exc:
            _record_office_warning(
                repository=repository,
                f=f,
                category=exc.category,
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_payload_invalid",
                actual_bytes=exc.actual_bytes,
                expected_bytes=f.size,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason=exc.category,
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        except Exception as exc:
            _record_office_warning(
                repository=repository,
                f=f,
                category="office_download_failed",
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_download_failed",
                actual_bytes=None,
                expected_bytes=f.size,
                error_type=type(exc).__name__,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="office_download_failed",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        try:
            pages = extract_office_pages(
                data,
                mime_type=f.mime_type,
                expected_size=f.size,
                expected_md5=getattr(f, "md5_checksum", None),
                progress_callback=(
                    (lambda: lease_heartbeat(False)) if lease_heartbeat is not None else None
                ),
                **office_kwargs,
            )
            page_chunks = _bounded_chunk_pages(pages)
            if page_chunks:
                chunks.extend(
                    _embed_page_chunks(
                        page_chunks,
                        embedder=embedder,
                        lease_heartbeat=lease_heartbeat,
                    )
                )
        except _RetryLeaseLostError:
            raise
        except _IngestContentVolumeError:
            _record_office_warning(
                repository=repository,
                f=f,
                category="unsafe_content_volume",
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_payload_invalid",
                actual_bytes=len(data),
                expected_bytes=f.size,
            )
            _resolve_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                request_id=request_id,
                dry_run=dry_run,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
                durability_tracker=durability_tracker,
            )
            skipped.append(f.id)
            return 0, 0
        except OfficePayloadError as exc:
            # invalid payloadではDBを書かない。既存document/chunksがあればそのまま保持され、
            # observed_gdrive_idsには列挙時点で入るためstale誤判定もしない。
            _record_office_warning(
                repository=repository,
                f=f,
                category=exc.category,
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_payload_invalid",
                actual_bytes=exc.actual_bytes,
                expected_bytes=exc.expected_bytes,
            )
            if exc.category in _PERSISTENT_OFFICE_INVALID_REASONS:
                _resolve_source_retry(
                    repository=repository,
                    f=f,
                    source_kind=warning_source_kind,
                    source_id=warning_source_id,
                    request_id=request_id,
                    dry_run=dry_run,
                    expected_lease_owner=retry_lease_owner,
                    expected_lease_token=retry_lease_token,
                    durability_tracker=durability_tracker,
                )
            else:
                _record_source_retry(
                    repository=repository,
                    f=f,
                    source_kind=warning_source_kind,
                    source_id=warning_source_id,
                    reason=exc.category,
                    request_id=request_id,
                    dry_run=dry_run,
                    enabled=durable_retry,
                    durability_tracker=durability_tracker,
                    expected_lease_owner=retry_lease_owner,
                    expected_lease_token=retry_lease_token,
                )
            skipped.append(f.id)
            return 0, 0
        except Exception as exc:
            _record_office_warning(
                repository=repository,
                f=f,
                category="office_extract_failed",
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_extract_failed",
                actual_bytes=len(data),
                expected_bytes=f.size,
                error_type=type(exc).__name__,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="office_extract_failed",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        if not page_chunks:
            _record_office_warning(
                repository=repository,
                f=f,
                category="office_empty_text",
                request_id=request_id,
                dry_run=dry_run,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_office_empty_text",
                actual_bytes=len(data),
                expected_bytes=f.size,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="office_empty_text",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
    elif f.mime_type == GDOC_NATIVE_MIME:
        # Google native gdoc: Docs API で plain text 抽出（download_file_bytes は使えない）
        try:
            from teamagent.adapters.gdocs_client import GDocsClient

            gdocs = GDocsClient.from_env()
            doc_content = gdocs.get_document_text(document_id=f.id, request_id=request_id)
            text = doc_content.text or ""
            page_chunks = _bounded_chunk_pages([(1, text)]) if text.strip() else []
            if page_chunks:
                chunks.extend(
                    _embed_page_chunks(
                        page_chunks,
                        embedder=embedder,
                        lease_heartbeat=lease_heartbeat,
                    )
                )
        except _RetryLeaseLostError:
            raise
        except _IngestContentVolumeError:
            _record_generic_drive_warning(
                f=f,
                category="native_content_too_large",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_native_content_too_large",
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="native_content_too_large",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        except Exception as exc:
            _record_generic_drive_warning(
                f=f,
                category="gdoc_extract_failed",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_gdoc_extract_failed",
                error_type=type(exc).__name__,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="gdoc_extract_failed",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        if not page_chunks:
            _record_generic_drive_warning(
                f=f,
                category="gdoc_empty_text",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_gdoc_empty_text",
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="gdoc_empty_text",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
    elif rich and (
        f.mime_type in (_GSLIDE_NATIVE_MIME, _GSHEET_NATIVE_MIME)
        or f.mime_type in _PLAIN_TEXT_MIMES
    ):
        # INGEST_RICH_EXTRACT=1: gslide/gsheet/plain-text を本文 chunk 化（gdoc は上で処理済）。
        # 抽出失敗を title-only で成功扱いすると、既存本文の退行とcursor取りこぼしを招く。
        # warning + durable retry として保持し、既存document/chunksには触れない。
        try:
            native_pages = _rich_native_pages(f, client=client, request_id=request_id)
            if native_pages is None:
                _record_generic_drive_warning(
                    f=f,
                    category="native_extract_failed_or_empty",
                    request_id=request_id,
                    warning_collector=warning_collector,
                    warning_source_kind=warning_source_kind,
                    warning_source_id=warning_source_id,
                    event="gdrive_native_extract_failed_or_empty",
                )
                _record_source_retry(
                    repository=repository,
                    f=f,
                    source_kind=warning_source_kind,
                    source_id=warning_source_id,
                    reason="native_extract_failed_or_empty",
                    request_id=request_id,
                    dry_run=dry_run,
                    enabled=durable_retry,
                    durability_tracker=durability_tracker,
                    expected_lease_owner=retry_lease_owner,
                    expected_lease_token=retry_lease_token,
                )
                skipped.append(f.id)
                return 0, 0
            page_chunks = _bounded_chunk_pages(native_pages)
            if not page_chunks:
                _record_generic_drive_warning(
                    f=f,
                    category="native_empty_text",
                    request_id=request_id,
                    warning_collector=warning_collector,
                    warning_source_kind=warning_source_kind,
                    warning_source_id=warning_source_id,
                    event="gdrive_native_empty_text",
                )
                _record_source_retry(
                    repository=repository,
                    f=f,
                    source_kind=warning_source_kind,
                    source_id=warning_source_id,
                    reason="native_empty_text",
                    request_id=request_id,
                    dry_run=dry_run,
                    enabled=durable_retry,
                    durability_tracker=durability_tracker,
                    expected_lease_owner=retry_lease_owner,
                    expected_lease_token=retry_lease_token,
                )
                skipped.append(f.id)
                return 0, 0
            chunks.extend(
                _embed_page_chunks(
                    page_chunks,
                    embedder=embedder,
                    lease_heartbeat=lease_heartbeat,
                )
            )
        except _RetryLeaseLostError:
            raise
        except _IngestContentVolumeError:
            _record_generic_drive_warning(
                f=f,
                category="native_content_too_large",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_native_content_too_large",
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="native_content_too_large",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        except Exception as exc:
            _record_generic_drive_warning(
                f=f,
                category="native_extract_failed",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_native_extract_failed",
                error_type=type(exc).__name__,
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="native_extract_failed",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
    else:
        # 未対応 mime_type: title + mime のフォールバック（検索ヒットだけは可能にする）。
        # §M: 個人ドライブ folder 経路でも title-only に落ちたことを可視化する
        # （crawl 経路 shared_drive_title_only と対称・無音落ちを防ぐ）。
        logger.info(
            "gdrive_folder_title_only",
            file_ref=_external_id_ref(f.id),
            mime_type=f.mime_type,
            folder_ref=_external_id_ref(spec.folder_id),
        )
        text = f"{f.name} ({f.mime_type})"
        if lease_heartbeat is not None:
            lease_heartbeat(False)
        chunks.append(
            ChunkUpsert(
                chunk_idx=0,
                content=text,
                embedding=embedder.embed_passage(text),
                metadata={"mime_type": f.mime_type, "title_only": True},
            )
        )

    # ナレッジ自動分類（案件 / 業界 / 資料種別 / 商談フェーズ）。
    # USE_DOC_CLASSIFY=1 のときだけ classifier が非 None。失敗しても取り込みは継続（fail-open）。
    cls_metadata: dict[str, str] = {}
    if classifier is not None and chunks:
        if lease_heartbeat is not None:
            lease_heartbeat(False)
        sample = "\n".join(c.content for c in chunks[:8])
        try:
            # 2026-07-06: フォルダ置き位置を分類に注入する。gdrive_folders 経路で確実に
            # 取れるフォルダ文脈は spec.folder_name（yaml の起点フォルダ名）のみ:
            # walk_files_recursive はサブフォルダ名を保持しない（DriveFile.parents は
            # ID のみ）ため、サブフォルダ配下のファイルにも起点フォルダ名を渡す
            # （直上フォルダ名が取れないのは正直なスコープ制限）。
            # 効き方は classify() 側: Haiku ヒント（格納フォルダ: XX 行）＋
            # USE_DOC_KIND_RULES gate 配下の決定論ルール（テンプレ/定期報告キーワード）。
            classification = classifier.classify(
                title=f.name or "",
                text=sample,
                request_id=request_id,
                folder_name=spec.folder_name or "",
            )
        except Exception:
            logger.exception("gdrive_classify_unexpected", file_id=f.id, file_name=f.name)
            classification = None
        if classification is not None:
            cls_metadata = classification.as_metadata()
        if lease_heartbeat is not None:
            lease_heartbeat(False)

    # Contextual Retrieval: 抽出ページを結合した全文を full_text に文脈前置詞を付与する。
    # contextualizer が None（既定）なら chunks はそのまま＝従来挙動・完全後方互換（fail-open）。
    if contextualizer is not None and chunks:
        if lease_heartbeat is not None:
            lease_heartbeat(False)
        full_text = "\n\n".join(c.content for c in chunks)
        chunks = contextualizer.contextualize_chunks(f.name or f.id, full_text, chunks, request_id)
        if len(chunks) > MAX_INGEST_EMBEDDINGS_PER_FILE:
            _record_generic_drive_warning(
                f=f,
                category="contextualized_content_too_large",
                request_id=request_id,
                warning_collector=warning_collector,
                warning_source_kind=warning_source_kind,
                warning_source_id=warning_source_id,
                event="gdrive_contextualized_content_too_large",
            )
            _record_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                reason="contextualized_content_too_large",
                request_id=request_id,
                dry_run=dry_run,
                enabled=durable_retry,
                durability_tracker=durability_tracker,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
            )
            skipped.append(f.id)
            return 0, 0
        if lease_heartbeat is not None:
            lease_heartbeat(False)

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
            "md5_checksum": getattr(f, "md5_checksum", None),
            "drive_folder_id": spec.folder_id,
            "drive_folder_name": spec.folder_name,
            **cls_metadata,
        },
        modified_at=f.modified_time,
    )
    if not dry_run:
        if lease_heartbeat is not None:
            lease_heartbeat(True)
        # §2 ガード: 本文版を title_only 版で上書きしない（folder→crawl 順の退行を塞ぐ）。
        claimed_retry = retry_lease_owner is not None or retry_lease_token is not None
        if claimed_retry:
            if not retry_lease_owner or not retry_lease_token:
                if durability_tracker is not None:
                    durability_tracker.add("retry_claim_fence_invalid")
                raise _RetryResolutionDurabilityError("claimed retry fence is incomplete")
            wrote = _guarded_claimed_retry_upsert(
                repository,
                doc,
                chunks,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                request_id=request_id,
                lease_owner=retry_lease_owner,
                lease_token=retry_lease_token,
                durability_tracker=durability_tracker,
                content_registry=content_registry,
            )
        else:
            wrote = _guarded_upsert(
                repository,
                doc,
                chunks,
                request_id=request_id,
                content_registry=content_registry,
            )
            _resolve_source_retry(
                repository=repository,
                f=f,
                source_kind=warning_source_kind,
                source_id=warning_source_id,
                request_id=request_id,
                dry_run=dry_run,
                expected_lease_owner=retry_lease_owner,
                expected_lease_token=retry_lease_token,
                durability_tracker=durability_tracker,
            )
        if not wrote:
            _resolve_reconciliation_gap(
                repository=repository,
                f=f,
                request_id=request_id,
                dry_run=dry_run,
            )
            return 0, 0
        if any(chunk.content.strip() and not chunk.metadata.get("title_only") for chunk in chunks):
            _resolve_reconciliation_gap(
                repository=repository,
                f=f,
                request_id=request_id,
                dry_run=dry_run,
            )
    return 1, len(chunks)


def _ingest_shared_drives_crawl(
    spec: SharedDriveCrawlSpec,
    *,
    embedder: _EmbedderProto,
    repository: IngestRepository,
    owner_email: str,
    dry_run: bool,
    request_id: str,
    content_registry: set[tuple[str, str]] | None = None,
    exclude_folder_name_re: str | None = None,
    observed_gdrive_ids: set[str] | None = None,
    truncated_walk_roots: set[str] | None = None,
    warning_collector: _IngestWarningCollector | None = None,
) -> tuple[int, int]:
    """共有ドライブ全件 crawl + 営業資料フィルタで取り込む (Day 7, 2026-05-27)。

    対象: s-komata がメンバーになっている全共有ドライブ。
    - spec.name_filter が指定されてれば substring match で絞る
    - 各ドライブを再帰 walk
    - spec.sales_relevance_filter=True なら _is_sales_relevant で営業価値判定
    - ACL は permissions.list で解決して acl_emails / acl_groups に写像
    - PDF / Office (docx/pptx/xlsx) はテキスト抽出 + chunk 化、それ以外は title のみで 1 chunk

    入れ込み v2 (2026-07-10): exclude_folder_name_re / observed_gdrive_ids /
    truncated_walk_roots は gdrive_folders 経路（_ingest_gdrive_folder）と同義
    （crawl 経路にも同じ除外と stale 観測・打ち切り検知を配線し、99_ 系フォルダの
    取り込みを両経路で保証して塞ぐ）。crawl の walk 上限は spec.max_files_per_drive。
    """
    from teamagent.adapters.gdrive_client import (
        DEFAULT_EXCLUDE_FOLDER_NAME_RE,
        GDriveClient,
        GDriveDownloadContentError,
    )
    from teamagent.ingest.classify import build_classifier_from_env
    from teamagent.ingest.contextualize import build_contextualizer_from_env
    from teamagent.ingest.office_extract import (
        GDOC_NATIVE_MIME,
        OFFICE_BINARY_MIMES,
        OFFICE_VALIDATOR_SCHEMA_VERSION,
        OfficePayloadError,
        extract_office_pages,
    )
    from teamagent.ingest.pdf_extract import extract_pdf_pages

    client = GDriveClient.from_env(readonly=True)
    # ナレッジ自動分類（USE_DOC_CLASSIFY=1 のときだけ非 None。crawl 単位で 1 回構築）。
    classifier = build_classifier_from_env()
    # Contextual Retrieval（USE_CONTEXTUAL_INGEST=1 のときだけ非 None。crawl 単位で 1 回構築）。
    contextualizer = build_contextualizer_from_env()
    # INGEST_RICH_EXTRACT=1 のときだけ rich 抽出（gdoc/gslide/gsheet/plain-text 本文化・抽出器の
    # rich 引数）を有効化。OFF（既定）は現行と 1 バイトも挙動を変えない（crawl 単位で 1 回読む）。
    rich = _rich_extract_enabled()
    pdf_kwargs: dict[str, Any] = {"min_chars": 40} if rich else {}
    office_kwargs: dict[str, Any] = (
        {
            "include_notes": True,
            "include_tables": True,
            "formula_fallback": True,
            "min_chars": 40,
        }
        if rich
        else {}
    )
    docs_n = 0
    chunks_n = 0
    skipped_count = 0
    filtered_count = 0
    warning_source_kind = "shared_drives"
    warning_source_id = "shared_drives"
    warning_before = (
        warning_collector.snapshot(warning_source_kind, warning_source_id)
        if warning_collector is not None
        else _WarningSnapshot(reasons={}, suppressed=0)
    )

    # migration 0012 の契約どおり、共有ドライブはdrive_idごとにcursorを保持する。
    incremental = _envflag("USE_INCREMENTAL_SYNC")
    changes_cache: dict[str, tuple[set[str], str | None] | None] = {}
    full_scan_cursor_attempted = False
    full_scan_cursor: str | None = None

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

    # yaml キー未記載（None）→ コード既定の 99_一次倉庫系除外。空文字 "" → 除外なし。
    effective_exclude_re = (
        exclude_folder_name_re
        if exclude_folder_name_re is not None
        else DEFAULT_EXCLUDE_FOLDER_NAME_RE
    )

    for drive in drives:
        drive_warning_before = (
            warning_collector.snapshot(warning_source_kind, warning_source_id)
            if warning_collector is not None
            else _WarningSnapshot(reasons={}, suppressed=0)
        )
        changed_ids: set[str] | None = None
        next_cursor: str | None = None
        if incremental:
            state: Any | None = None
            prior_cursor: str | None = None
            try:
                state = repository.load_connector_state(warning_source_kind, drive.id)
                prior_cursor = state.cursor if state else None
            except Exception:
                logger.exception(
                    "connector_state_load_failed",
                    source_kind=warning_source_kind,
                    source_id_ref=_external_id_ref(drive.id),
                )

            prior_validator_version = (
                str(
                    (getattr(state, "metadata", {}) or {}).get(
                        _CONNECTOR_VALIDATOR_METADATA_KEY
                    )
                    or ""
                )
                if state is not None
                else ""
            )
            validator_generation_changed = bool(prior_cursor) and (
                prior_validator_version != OFFICE_VALIDATOR_SCHEMA_VERSION
            )
            if validator_generation_changed:
                logger.warning(
                    "shared_drives_validator_generation_changed",
                    request_id=request_id,
                    drive_ref=_external_id_ref(drive.id),
                    previous_validator=prior_validator_version or "unrecorded",
                    current_validator=OFFICE_VALIDATOR_SCHEMA_VERSION,
                    action="full_revalidation",
                )
            if prior_cursor and not validator_generation_changed:
                try:
                    if prior_cursor not in changes_cache:
                        try:
                            changes_cache[prior_cursor] = _drain_changes(
                                client,
                                prior_cursor,
                                request_id,
                            )
                        except Exception:
                            changes_cache[prior_cursor] = None
                            raise
                    cached = changes_cache[prior_cursor]
                    if cached is not None:
                        changed_ids, next_cursor = cached
                    logger.info(
                        "shared_drives_incremental_changes",
                        request_id=request_id,
                        drive_ref=_external_id_ref(drive.id),
                        changed=len(changed_ids or ()),
                        fail_open=cached is None,
                    )
                except Exception:
                    logger.exception(
                        "shared_drives_get_changes_failed",
                        request_id=request_id,
                        drive_ref=_external_id_ref(drive.id),
                    )
                    changed_ids = None
            if changed_ids is None:
                # full走査開始前にtokenを確保し、走査中の変更を次回changesで拾う。
                if not full_scan_cursor_attempted:
                    full_scan_cursor_attempted = True
                    try:
                        full_scan_cursor = client.get_start_page_token(request_id)
                    except Exception:
                        logger.exception(
                            "shared_drives_start_page_token_failed",
                            request_id=request_id,
                            drive_ref=_external_id_ref(drive.id),
                        )
                next_cursor = full_scan_cursor

        # 2. 各ドライブを再帰 walk
        files = client.walk_files_recursive(
            root_id=drive.id,
            request_id=request_id,
            drive_id=drive.id,
            max_files=spec.max_files_per_drive,
            exclude_folder_name_re=effective_exclude_re,
        )
        # stale/cursor堅牢化: max_files到達は完走と区別できないためfail-closedする。
        if len(files) >= spec.max_files_per_drive:
            if truncated_walk_roots is not None:
                truncated_walk_roots.add(drive.id)
            raise GDrivePaginationIncompleteError(
                "Shared Drive recursive listing reached "
                f"{spec.max_files_per_drive} file safety limit"
            )
        # stale 差集合用: 営業価値フィルタの**前**（Drive 上に存在が確認できた全 file）で
        # 観測済みを記録する（フィルタ落ちした存在中の file が stale 誤爆しないよう安全側）。
        if observed_gdrive_ids is not None:
            observed_gdrive_ids.update(f.id for f in files)
        logger.info(
            "ingest_shared_drive_walked",
            request_id=request_id,
            drive_id=drive.id,
            drive_name=drive.name,
            files_found=len(files),
        )
        if changed_ids is not None:
            files = [f for f in files if f.id in changed_ids]

        for f in files:
            # M4: file ループ本体を file 単位 try/except で囲む。folder 経路（~838）は
            # file 単位 try/except があるが crawl 経路には無く、embed / DB upsert の例外で
            # crawl spec 全体（残り全ファイル）が死んでいた。1 ファイル失敗は skip して継続する。
            try:
                # 3. 営業関連判定
                if spec.sales_relevance_filter:
                    relevant, _reason = _is_sales_relevant(
                        f, modified_within_days=spec.modified_within_days
                    )
                    if not relevant:
                        filtered_count += 1
                        continue

                if _should_skip_unchanged_gdrive_file(
                    repository,
                    f,
                    request_id=request_id,
                ):
                    skipped_count += 1
                    continue

                if f.mime_type in OFFICE_BINARY_MIMES:
                    known_reason = _known_invalid_office_reason(repository, f)
                    if known_reason is not None:
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category=known_reason,
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_payload_invalid",
                            actual_bytes=None,
                            expected_bytes=f.size,
                            known_invalid=True,
                        )
                        skipped_count += 1
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
                    except Exception as exc:
                        _record_pdf_warning(
                            f=f,
                            category="pdf_download_failed",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_pdf_download_failed",
                            error_type=type(exc).__name__,
                        )
                        skipped_count += 1
                        continue
                    try:
                        pages = extract_pdf_pages(data, **pdf_kwargs)
                        page_chunks = _bounded_chunk_pages(pages)
                        if page_chunks:
                            chunks.extend(
                                _embed_page_chunks(
                                    page_chunks,
                                    embedder=embedder,
                                )
                            )
                    except _IngestContentVolumeError:
                        _record_pdf_warning(
                            f=f,
                            category="pdf_content_too_large",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_pdf_content_too_large",
                        )
                        skipped_count += 1
                        continue
                    except Exception as exc:
                        _record_pdf_warning(
                            f=f,
                            category="pdf_extract_failed",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_pdf_extract_failed",
                            error_type=type(exc).__name__,
                        )
                        skipped_count += 1
                        continue
                    if not page_chunks:
                        _record_pdf_warning(
                            f=f,
                            category="pdf_empty_text",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_pdf_empty_text",
                        )
                        skipped_count += 1
                        continue
                elif f.mime_type in OFFICE_BINARY_MIMES:
                    # docx / pptx / xlsx: download → extract → chunk_pages（PDF と同じ I/F）。
                    # 営業提案書は大半が pptx なので、ここで本文を index 化することが肝。
                    # 壊れた pptx は extract_office_pages が zipfile.BadZipFile を投げるが、
                    # fail-open でその 1 ファイルを skip して crawl は継続する。
                    try:
                        data = client.download_file_bytes(file_id=f.id, request_id=request_id)
                    except GDriveDownloadContentError as exc:
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category=exc.category,
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_payload_invalid",
                            actual_bytes=exc.actual_bytes,
                            expected_bytes=f.size,
                        )
                        skipped_count += 1
                        continue
                    except Exception as exc:
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category="office_download_failed",
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_download_failed",
                            actual_bytes=None,
                            expected_bytes=f.size,
                            error_type=type(exc).__name__,
                        )
                        skipped_count += 1
                        continue
                    try:
                        pages = extract_office_pages(
                            data,
                            mime_type=f.mime_type,
                            expected_size=f.size,
                            expected_md5=getattr(f, "md5_checksum", None),
                            **office_kwargs,
                        )
                        page_chunks = _bounded_chunk_pages(pages)
                        if page_chunks:
                            chunks.extend(
                                _embed_page_chunks(
                                    page_chunks,
                                    embedder=embedder,
                                )
                            )
                    except _IngestContentVolumeError:
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category="unsafe_content_volume",
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_payload_invalid",
                            actual_bytes=len(data),
                            expected_bytes=f.size,
                        )
                        skipped_count += 1
                        continue
                    except OfficePayloadError as exc:
                        # invalid payloadは既存document/chunksを上書きせず保持する。file自体は
                        # observed済みなのでstaleにも落とさず、category付きWARNで運用検知する。
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category=exc.category,
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_payload_invalid",
                            actual_bytes=exc.actual_bytes,
                            expected_bytes=exc.expected_bytes,
                        )
                        skipped_count += 1
                        continue
                    except Exception as exc:
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category="office_extract_failed",
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_extract_failed",
                            actual_bytes=len(data),
                            expected_bytes=f.size,
                            error_type=type(exc).__name__,
                        )
                        skipped_count += 1
                        continue
                    if not page_chunks:
                        # 抽出 0 も分類warningとして可視化し、connectorを完全successにしない。
                        _record_office_warning(
                            repository=repository,
                            f=f,
                            category="office_empty_text",
                            request_id=request_id,
                            dry_run=dry_run,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_office_empty_text",
                            actual_bytes=len(data),
                            expected_bytes=f.size,
                        )
                        skipped_count += 1
                        continue
                elif rich and (
                    f.mime_type in (_GSLIDE_NATIVE_MIME, _GSHEET_NATIVE_MIME)
                    or f.mime_type == GDOC_NATIVE_MIME
                    or f.mime_type in _PLAIN_TEXT_MIMES
                ):
                    # INGEST_RICH_EXTRACT=1: crawl 経路でも gdoc/gslide/gsheet/plain-text を
                    # 本文 chunk 化（folder 経路の gdoc 実装と同作法・gslide/gsheet/plain を追加）。
                    # 抽出失敗時は既存本文をtitle-onlyへ格下げせず、warningとしてskipする。
                    try:
                        native_pages = _rich_native_pages(f, client=client, request_id=request_id)
                        if native_pages is None:
                            _record_generic_drive_warning(
                                f=f,
                                category="native_extract_failed_or_empty",
                                request_id=request_id,
                                warning_collector=warning_collector,
                                warning_source_kind=warning_source_kind,
                                warning_source_id=warning_source_id,
                                event="shared_drive_native_extract_failed_or_empty",
                            )
                            skipped_count += 1
                            continue
                        page_chunks = _bounded_chunk_pages(native_pages)
                        if not page_chunks:
                            _record_generic_drive_warning(
                                f=f,
                                category="native_empty_text",
                                request_id=request_id,
                                warning_collector=warning_collector,
                                warning_source_kind=warning_source_kind,
                                warning_source_id=warning_source_id,
                                event="shared_drive_native_empty_text",
                            )
                            skipped_count += 1
                            continue
                        chunks.extend(
                            _embed_page_chunks(
                                page_chunks,
                                embedder=embedder,
                            )
                        )
                    except _IngestContentVolumeError:
                        _record_generic_drive_warning(
                            f=f,
                            category="native_content_too_large",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_native_content_too_large",
                        )
                        skipped_count += 1
                        continue
                    except Exception as exc:
                        _record_generic_drive_warning(
                            f=f,
                            category="native_extract_failed",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_native_extract_failed",
                            error_type=type(exc).__name__,
                        )
                        skipped_count += 1
                        continue
                else:
                    # 未対応 mime: title + mime のみ。レガシー Office(.ppt/.doc/.xls＝旧バイナリ)や
                    # Google native はここに来る（標準ライブラリが旧形式を読めず本文抽出不可）。
                    # 無音で本文が落ちると気づけないため、title-only フォールバックを必ずログする。
                    logger.info(
                        "shared_drive_title_only",
                        request_id=request_id,
                        file_id=f.id,
                        file_name=f.name,
                        mime_type=f.mime_type,
                        drive_id=drive.id,
                    )
                    text = f"{f.name} ({f.mime_type})"
                    chunks.append(
                        ChunkUpsert(
                            chunk_idx=0,
                            content=text,
                            embedding=embedder.embed_passage(text),
                            metadata={"mime_type": f.mime_type, "title_only": True},
                        )
                    )

                # ナレッジ自動分類（案件 / 業界 / 資料種別 / 商談フェーズ）。
                # USE_DOC_CLASSIFY=1 のときだけ classifier が非 None。
                # 失敗しても取り込みは継続（fail-open）。元の chunk 本文で分類するため、
                # 文脈前置詞を付与する contextualize より前に実行する。
                # 2026-07-06: フォルダ文脈（classify の folder_name）は crawl 経路では
                # 渡さない＝対象外。walk_files_recursive は親フォルダ名を保持せず
                # （DriveFile.parents は ID のみ）、drive.name はドライブ名であって
                # フォルダ名ではない（広すぎて配下全ファイルへの誤爆リスクが勝る）。
                # 親フォルダ名が取れる gdrive_folders 経路のみ実装（正直なスコープ）。
                cls_metadata: dict[str, str] = {}
                if classifier is not None and chunks:
                    sample = "\n".join(c.content for c in chunks[:8])
                    try:
                        classification = classifier.classify(
                            title=f.name or "", text=sample, request_id=request_id
                        )
                    except Exception:
                        logger.exception(
                            "shared_drive_classify_unexpected",
                            file_id=f.id,
                            file_name=f.name,
                            drive_id=drive.id,
                        )
                        classification = None
                    if classification is not None:
                        cls_metadata = classification.as_metadata()

                # Contextual Retrieval: 抽出ページ結合の全文を full_text に文脈前置詞を付与する。
                # contextualizer が None（既定）なら chunks はそのまま＝従来挙動・完全後方互換。
                if contextualizer is not None and chunks:
                    full_text = "\n\n".join(c.content for c in chunks)
                    chunks = contextualizer.contextualize_chunks(
                        f.name or f.id, full_text, chunks, request_id
                    )
                    if len(chunks) > MAX_INGEST_EMBEDDINGS_PER_FILE:
                        _record_generic_drive_warning(
                            f=f,
                            category="contextualized_content_too_large",
                            request_id=request_id,
                            warning_collector=warning_collector,
                            warning_source_kind=warning_source_kind,
                            warning_source_id=warning_source_id,
                            event="shared_drive_contextualized_content_too_large",
                        )
                        skipped_count += 1
                        continue

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
                        "md5_checksum": getattr(f, "md5_checksum", None),
                        "shared_drive_id": drive.id,
                        "shared_drive_name": drive.name,
                        "via": "shared_drive_crawl",
                        **cls_metadata,
                    },
                    modified_at=f.modified_time,
                )
                if not dry_run:
                    # §2 ガード: 本文版を title_only 版で上書きしない（退行を塞ぐ）。
                    wrote = _guarded_upsert(
                        repository,
                        doc,
                        chunks,
                        request_id=request_id,
                        content_registry=content_registry,
                    )
                    if not wrote:
                        skipped_count += 1
                        continue
                    if any(
                        chunk.content.strip() and not chunk.metadata.get("title_only")
                        for chunk in chunks
                    ):
                        _resolve_reconciliation_gap(
                            repository=repository,
                            f=f,
                            request_id=request_id,
                            dry_run=dry_run,
                        )
                docs_n += 1
                chunks_n += len(chunks)
            except Exception:
                # fail-open: 1 ファイルの想定外例外（embed / upsert 等）で crawl 全体を
                # 落とさない。folder 経路と対称に exception ログ＋skip カウント＋次へ。
                logger.exception(
                    "shared_drive_file_unexpected_error",
                    request_id=request_id,
                    file_ref=_external_id_ref(f.id),
                    drive_ref=_external_id_ref(drive.id),
                )
                skipped_count += 1
                continue

        drive_warning_delta = (
            warning_collector.delta(warning_source_kind, warning_source_id, drive_warning_before)
            if warning_collector is not None
            else _WarningSnapshot(reasons={}, suppressed=0)
        )
        if incremental and not dry_run:
            try:
                repository.save_connector_state(
                    warning_source_kind,
                    drive.id,
                    cursor=next_cursor,
                    success=True,
                    metadata={
                        "outcome": (
                            "success_with_warnings" if drive_warning_delta.reasons else "success"
                        ),
                        "warning_count": sum(drive_warning_delta.reasons.values()),
                        "warning_reasons": drive_warning_delta.reasons,
                        "known_invalid_suppressed": drive_warning_delta.suppressed,
                        _CONNECTOR_VALIDATOR_METADATA_KEY: OFFICE_VALIDATOR_SCHEMA_VERSION,
                    },
                )
            except Exception as exc:
                logger.exception(
                    "connector_state_save_failed",
                    source_kind=warning_source_kind,
                    source_id_ref=_external_id_ref(drive.id),
                )
                raise IngestDurabilityError("connector cursor state could not be saved") from exc

    warning_delta = (
        warning_collector.delta(warning_source_kind, warning_source_id, warning_before)
        if warning_collector is not None
        else _WarningSnapshot(reasons={}, suppressed=0)
    )
    logger.info(
        "ingest_shared_drives_crawl_done",
        request_id=request_id,
        drives_processed=len(drives),
        documents=docs_n,
        chunks=chunks_n,
        skipped=skipped_count,
        filtered_out=filtered_count,
        warning_count=sum(warning_delta.reasons.values()),
        warning_reasons=warning_delta.reasons,
        known_invalid_suppressed=warning_delta.suppressed,
        outcome="success_with_warnings" if warning_delta.reasons else "success",
        incremental=incremental,
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
    from teamagent.ingest.classify import build_classifier_from_env

    # 2026-07-06: ナレッジ共有フォーム回答シートの構造化 (FB と同設計・form_mappings 参照)。
    # こちらも「このシート固有のコアヘッダ閾値」判定なので非対象シートには空 dict ＝副作用ゼロ。
    from teamagent.ingest.form_mappings import derive_knowledge_client_name, map_knowledge_fields

    # 2026-07-03: 営業 FB フォーム回答シートの構造化。
    # 行のヘッダ → 値 を Slack FB 経路 (slack_fb_parser) と同じ写像でメタ化する。
    # 非 FB シート (コアヘッダ < 閾値) は map_fb_fields が空 dict を返すので副作用ゼロ。
    from teamagent.ingest.slack_fb_parser import extract_client_name, map_fb_fields

    client = GSheetsClient.from_env()
    # ナレッジ自動分類（USE_DOC_CLASSIFY=1 のときだけ非 None。sheet 単位で 1 回構築）。
    # gsheet は row_unit=True（1 行 = 1 document = 1 chunk）なので contextualizer は付けない。
    classifier = build_classifier_from_env()
    docs_n = 0
    chunks_n = 0

    # 2026-07-06: タブ名は運用でリネームされ得る（命名ルール導入時に実際に発生し
    # "Unable to parse range" で全シート取り込み失敗した）。gid は不変なので、取り込み時に
    # gid → 現在のタブ名を解決し、yaml の tab_name は解決失敗時の fallback に降格する。
    # external_id は従来どおり gid ベース＝リネーム後の再取り込みも冪等 upsert のまま。
    try:
        _meta = client.get_sheet_metadata(spec.sheet_id, request_id)
        _gid_to_title = {t.gid: t.title for t in _meta.tabs if t.title}
    except Exception:
        logger.exception(
            "gsheet_metadata_failed_fallback_tab_name",
            sheet_id=spec.sheet_id,
        )
        _gid_to_title = {}

    for tab in spec.tabs:
        tab_title = _gid_to_title.get(tab.gid) or tab.tab_name
        if tab_title != tab.tab_name:
            logger.info(
                "gsheet_tab_renamed_resolved_by_gid",
                sheet_id=spec.sheet_id,
                gid=tab.gid,
                yaml_tab_name=tab.tab_name,
                resolved_title=tab_title,
            )
        tab_rows = client.get_tab_rows(
            sheet_id=spec.sheet_id, tab_name=tab_title, request_id=request_id
        )
        if not tab_rows.headers:
            continue
        for row_idx, row in enumerate(tab_rows.rows, start=2):  # 1=headers, 2 から data
            text = format_row_as_document(tab_rows.headers, row)
            if not text.strip():
                continue
            external_id = build_external_id(spec.sheet_id, tab.gid, row_idx)
            row_title = f"{spec.sheet_name} - {tab.tab_name} - row {row_idx}"

            # 営業 FB シート行の構造化メタ (Slack FB 経路 pipeline.py の
            # _ingest_slack_channel と同品質・同キー)。row が headers より短い分は
            # 空値扱い (format_row_as_document と同じ) なので strict=False で zip する。
            row_fields = dict(zip(tab_rows.headers, row, strict=False))
            fb_metadata = map_fb_fields(row_fields)
            fb_doc_metadata: dict[str, Any] = {}
            if fb_metadata:
                fb_doc_metadata["is_sales_fb"] = True
                fb_doc_metadata.update(fb_metadata)
                derived_client_name = extract_client_name(fb_metadata)
                if derived_client_name:
                    fb_doc_metadata["client_name"] = derived_client_name

            # ナレッジ共有フォーム回答シート行の構造化メタ (2026-07-06・form_mappings)。
            # FB とはコアヘッダが交差しないため相互排他 (テストで固定)。is_sales_fb は
            # 立てない (これは FB ではなくナレッジ共有) 代わりに is_knowledge_share を立て、
            # client_name は 正式社名 から FB と同品質 (法人格/敬称/注記なし) で導出する。
            knowledge_metadata = map_knowledge_fields(row_fields)
            knowledge_doc_metadata: dict[str, Any] = {}
            derived_knowledge_client: str | None = None
            if knowledge_metadata:
                knowledge_doc_metadata["is_knowledge_share"] = True
                knowledge_doc_metadata.update(knowledge_metadata)
                derived_knowledge_client = derive_knowledge_client_name(
                    knowledge_metadata.get("client_company", "")
                )
                if derived_knowledge_client:
                    knowledge_doc_metadata["client_name"] = derived_knowledge_client

            # 営業FB/ナレッジ共有フォームは実列「タイムスタンプ」を持つ。従来は本文から
            # 運用列として外したうえ modified_at=None にしていたため、正しい日時が DB へ
            # 一度も届かなかった。対象フォームと判定できた行だけを決定論的に採用し、
            # 無関係な任意シートの同名列には意味を与えない。
            row_modified_at = (
                _gsheet_row_modified_at(row_fields) if fb_metadata or knowledge_metadata else None
            )

            # ナレッジ共有シート行(フォーム回答)の本文/タイトル正規化:
            if knowledge_metadata:
                # (a) 本文から運用列を外す。先頭列の Slack file URL(100字超)が export_vault の
                #     160 字抜粋を食い潰し、知見が書かれた ポイント/なぜ/フリーコメント が
                #     抜粋＝/app のタグ源から落ちる（実測: 施策手法 6→1）。ファイル名を持つ
                #     「保存ファイル（リンク付き）」は各行唯一の検索キーなので**残す**。
                _ops = frozenset(
                    h for h in tab_rows.headers if _normalize_form_label(h) in _KNOWLEDGE_OPS_NORM
                )
                _body = format_row_as_document(tab_rows.headers, row, exclude_headers=_ops)
                if _body.strip():
                    text = _body
                # (b) title は "row N" でなく「正式社名 案件名」に。Vault のファイル名/H1/検索の
                #     d.title ILIKE//app の表示名を兼ねるため意味のある値にする。正式社名が
                #     プレースホルダ(なし/その他 等)の行は従来タイトルを維持する。
                _company = derive_knowledge_client_name(
                    knowledge_metadata.get("client_company", "")
                )
                _case = str(knowledge_metadata.get("client_case", "") or "").strip()
                _title_parts = [x for x in (_company, _case) if x]
                if _title_parts:
                    row_title = " ".join(_title_parts)

            # ナレッジ自動分類（案件 / 業界 / 資料種別 / 商談フェーズ）。
            # USE_DOC_CLASSIFY=1 のときだけ classifier が非 None。
            # 失敗しても取り込みは継続（fail-open）。
            cls_metadata: dict[str, str] = {}
            if classifier is not None:
                try:
                    classification = classifier.classify(
                        title=row_title, text=text, request_id=request_id
                    )
                except Exception:
                    logger.exception(
                        "gsheet_classify_unexpected",
                        sheet_id=spec.sheet_id,
                        tab_name=tab.tab_name,
                        row_idx=row_idx,
                    )
                    classification = None
                if classification is not None:
                    cls_metadata = classification.as_metadata()
                # Haiku の再分類で監査済みの公開業種が揺れないよう、exact external_id と
                # 人間入力由来 client_name の二重一致で 3 行だけ決定論的に固定する。
                # classifier 無効時は従来どおり cls_* を付けない（feature gate を維持）。
                cls_metadata = apply_gsheet_industry_override(
                    external_id,
                    client_name=derived_knowledge_client,
                    classification_metadata=cls_metadata,
                )

            doc = DocumentUpsert(
                source_type="gsheets",  # migration 0004 で ENUM に追加済
                external_id=external_id,
                source_uri=f"https://docs.google.com/spreadsheets/d/{spec.sheet_id}/edit?gid={tab.gid}#gid={tab.gid}&range={row_idx}:{row_idx}",
                title=row_title,
                owner_email=owner_email,
                acl_emails=[owner_email],
                acl_groups=_company_acl_groups(),  # §G 会社共有（未設定なら []）
                metadata={
                    **spec.extra_metadata,
                    "tab_name": tab.tab_name,
                    "row_idx": row_idx,
                    # Slack 経路と同じ合成順: 固定キー → fb → knowledge → cls。
                    # fb と knowledge はコアヘッダが交差せず同一シートで両方立つことはない。
                    # 人間入力 (fb/knowledge) と Haiku (cls_*) はキーが交差しない設計
                    # (client_type/proposed_menu/knowledge_kind ≠ cls_industry/cls_solution/
                    # cls_doc_type・根拠は form_mappings の module docstring) なので、
                    # cls を後置しても人間入力が Haiku に上書きされることはない＝併存。
                    **fb_doc_metadata,
                    **knowledge_doc_metadata,
                    **cls_metadata,
                },
                modified_at=row_modified_at,
            )
            chunks = [
                ChunkUpsert(
                    chunk_idx=0,
                    content=text,
                    embedding=embedder.embed_passage(text),
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
# ルート検査 preflight（入れ込み v2 2026-07-10）
# -----------------------------------------------------------
_NN_PREFIX_RE = re.compile(r"^\s*(\d{2})[_＿]")
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _check_rulebook_root(
    sources: IngestSources,
    *,
    request_id: str,
    client: Any | None = None,
) -> None:
    """gdrive kind 実行冒頭の preflight: ルールブック ルート直下のカバレッジ検査。

    yaml グローバルキー ``gdrive_rulebook_root_folder_id`` 設定時のみ呼ばれる。
    ルート直下フォルダを列挙し:
    (a) ``NN_`` 接頭フォルダのうち 99_ 系以外で yaml の gdrive_folders に folder_id が
        載っていないものがあれば **exit 1**（silent 未取込の防止・不足フォルダ名一覧を表示）
    (b) 99_ 系（99_ 接頭 or 除外 regex マッチ）が yaml に載っていたら **exit 1**
        （一次倉庫の誤取込防止）

    env ``INGEST_ROOT_CHECK_WARN_ONLY=true`` で exit を WARNING に降格できる。
    ルート列挙自体の失敗も fail-loud（検査できない状態で黙って進まない）。
    client はテスト用に注入可（None なら GDriveClient.from_env）。
    """
    from teamagent.adapters.gdrive_client import (
        DEFAULT_EXCLUDE_FOLDER_NAME_RE,
        GDriveClient,
    )

    root_id = sources.gdrive_rulebook_root_folder_id
    warn_only = _envflag("INGEST_ROOT_CHECK_WARN_ONLY")
    if client is None:
        client = GDriveClient.from_env(readonly=True)

    exclude_pattern = (
        sources.gdrive_exclude_folder_name_re
        if sources.gdrive_exclude_folder_name_re is not None
        else DEFAULT_EXCLUDE_FOLDER_NAME_RE
    )
    exclude_re = re.compile(exclude_pattern) if exclude_pattern else None

    def _fail(event: str, message: str, **kwargs: Any) -> None:
        if warn_only:
            logger.warning(event + "_warn_only", message=message, **kwargs)
            return
        logger.error(event, message=message, **kwargs)
        import sys as _sys

        print(f"[ERROR] {message}", file=_sys.stderr)
        raise SystemExit(1)

    try:
        subfolders = _list_all_gdrive_files(
            client=client,
            folder_id=root_id,
            request_id=request_id,
            mime_type_filter=_FOLDER_MIME,
        )
    except Exception as exc:
        _fail(
            "rulebook_root_list_failed",
            (
                f"ルールブック ルート ({root_id}) 直下の列挙に失敗しました: {exc}。"
                "検査できない状態で黙って進みません"
                "（run_ingest_task.sh --root-check-warn-only で警告降格可）"
            ),
            root_folder_id=root_id,
        )
        return  # warn_only のときだけ到達

    yaml_folder_ids = {s.folder_id for s in sources.gdrive_folders}
    missing: list[str] = []  # NN_ フォルダなのに yaml 未登録（silent 未取込）
    banned: list[str] = []  # 99_ 系なのに yaml に登録されている（誤取込）
    for folder in subfolders:
        name = folder.name or ""
        m = _NN_PREFIX_RE.match(name)
        is_excluded_kind = (m is not None and m.group(1) == "99") or (
            exclude_re is not None and exclude_re.search(name)
        )
        if is_excluded_kind:
            if folder.id in yaml_folder_ids:
                banned.append(name)
            continue
        if m is None:
            continue  # NN_ 接頭でないフォルダは検査対象外
        if folder.id not in yaml_folder_ids:
            missing.append(name)

    logger.info(
        "rulebook_root_check",
        request_id=request_id,
        root_folder_id=root_id,
        subfolders=len(subfolders),
        missing=missing,
        banned=banned,
    )
    if banned:
        _fail(
            "rulebook_root_banned_folder_in_yaml",
            (
                f"99_ 系（検索対象外）フォルダが yaml の gdrive_folders に登録されています: "
                f"{banned}。エントリを削除してください"
                "（run_ingest_task.sh --root-check-warn-only で警告降格可）"
            ),
            banned=banned,
        )
    if missing:
        _fail(
            "rulebook_root_missing_folders",
            (
                f"ルールブック ルート直下の NN_ フォルダが yaml の gdrive_folders に不足しています"
                f"（silent 未取込の防止）: {missing}。yaml に folder_id を追加してください"
                "（run_ingest_task.sh --root-check-warn-only で警告降格可）"
            ),
            missing=missing,
        )


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
        alerter: IngestOpsAlerter | None = None,
    ) -> None:
        self._repo = repository
        self._embedder = embedder
        self._owner_email = owner_email
        self._dry_run = dry_run
        # webhook 未設定なら alerter は no-op（from_env 内で webhook_url=None）
        self._alerter = alerter if alerter is not None else IngestOpsAlerter.from_env()

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

        # §2 dedup ガード用レジストリ。この run の gdrive folder 経路と shared_drives crawl 経路で
        # 1 個を共有し、folder→crawl の順で本文版を title_only 版が上書きするのを塞ぐ。
        # run ごとに新しい set ＝ run 間で漏れない（プロセス内の別 run やテストに影響しない）。
        content_registry: set[tuple[str, str]] = set()

        # 入れ込み v2: run 中に Drive 上で観測した gdrive file_id 全集合
        # （INGEST_MARK_STALE の stale 差集合用。folder / crawl 両経路で 1 個を共有）。
        observed_gdrive_ids: set[str] = set()
        # stale 堅牢化: walk が max_files 上限で打ち切られた root（folder/drive の ID）集合。
        # 打ち切りがあった run は観測集合が不完全＝mark を skip する（両経路で 1 個を共有）。
        truncated_walk_roots: set[str] = set()
        warning_collector = _IngestWarningCollector()
        # フォルダ名除外 regex（yaml グローバルキー。None ならコード既定を handler 側で解決）。
        gdrive_extra_kwargs: dict[str, Any] = {
            "content_registry": content_registry,
            "exclude_folder_name_re": sources.gdrive_exclude_folder_name_re,
            "observed_gdrive_ids": observed_gdrive_ids,
            "truncated_walk_roots": truncated_walk_roots,
            "warning_collector": warning_collector,
        }

        logger.info(
            "ingest_runner_start",
            request_id=request_id,
            kinds=kinds,
            dry_run=self._dry_run,
            owner_email=self._owner_email,
        )

        # ルート検査 preflight: gdrive kind 実行の冒頭・yaml でルート指定時のみ。
        # NN_ フォルダの yaml 不足 / 99_ 系の誤登録を fail-loud で止める（exit 1）。
        if "gdrive" in kinds and sources.gdrive_rulebook_root_folder_id:
            _check_rulebook_root(sources, request_id=request_id)

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
                extra_kwargs=gdrive_extra_kwargs,
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
                    extra_kwargs=gdrive_extra_kwargs,
                )
            else:
                logger.info(
                    "ingest_shared_drives_skipped",
                    request_id=request_id,
                    reason=("shared_drives_crawl が yaml に未定義 or enabled=false の場合 skip"),
                )
                result.by_kind["shared_drives"] = IngestStats(source_kind="shared_drives")

        self._apply_reconciliation_warnings(
            result,
            warning_collector=warning_collector,
            kinds=kinds,
            request_id=request_id,
        )

        # M3: 実行順は dedup（docdedup）→ boilerplate。boilerplate の指紋集計は
        # suppressed（非正本）doc を除外して数えるため、同一 run 内で先に docdedup が
        # 確定させた suppressed 印を参照できるよう、docdedup を先に走らせる。
        #
        # 全 upsert 完了後にコーパス横断の資料まるごと重複排除を 1 回だけ走らせる
        # （DOC_DEDUP_DETECT=1 のときだけ・dry-run では走らせない・fail-open）。
        # OFF（既定）では呼ばない＝現行と完全一致。
        self._maybe_mark_duplicate_documents(request_id=request_id)

        # 重複排除の直後に、コーパス横断のテンプレ検出を 1 回だけ走らせる
        # （BOILERPLATE_DETECT=1 のときだけ・dry-run では走らせない・fail-open）。
        # OFF（既定）では呼ばない＝現行と完全一致。
        self._maybe_mark_boilerplate(request_id=request_id)

        # 入れ込み v2: run 末尾の stale soft-delete（INGEST_MARK_STALE=true のときだけ・
        # 量的ブレーキ付き）。OFF（既定）では呼ばない＝現行と完全一致。
        # stale 堅牢化: result（gdrive 系列挙の部分失敗の検知）と truncated_walk_roots
        # （walk 打ち切りの検知）を渡し、観測集合が不完全な run では mark を skip させる。
        self._maybe_mark_stale_documents(
            observed_gdrive_ids,
            kinds=kinds,
            request_id=request_id,
            result=result,
            truncated_walk_roots=truncated_walk_roots,
        )

        # 鮮度監視: 各 source_type の最新取り込みが古すぎ（or 未取り込み）なら ops 通知。
        # 2026-07-13 の「Slack が 6 週間サイレント停止」の再発防止。read-only・fail-open。
        self._maybe_check_freshness(request_id=request_id)

        logger.info(
            "ingest_runner_done",
            request_id=request_id,
            total_documents=result.total_documents(),
            total_errors=result.total_errors(),
            total_warnings=result.total_warnings(),
            outcome=result.outcome,
            dry_run=self._dry_run,
        )
        return result

    def _apply_reconciliation_warnings(
        self,
        result: IngestResult,
        *,
        warning_collector: _IngestWarningCollector,
        kinds: list[str],
        request_id: str,
    ) -> None:
        """未解消の監査coverage gapを件数だけrun結果・connector run・opsへ反映する。"""
        if "gdrive" not in kinds and "shared_drives" not in kinds:
            return
        loader = getattr(self._repo, "unresolved_reconciliation_counts", None)
        if not callable(loader):
            return
        try:
            counts = {
                str(reason): int(count)
                for reason, count in loader("gdrive").items()
                if int(count) > 0
            }
        except Exception as exc:
            logger.warning(
                "ingest_reconciliation_count_failed",
                request_id=request_id,
                error_type=type(exc).__name__,
            )
            return
        if not counts:
            return

        target_kind = "gdrive" if "gdrive" in kinds else "shared_drives"
        stats = result.by_kind.setdefault(target_kind, IngestStats(source_kind=target_kind))
        for reason, count in counts.items():
            warning_collector.add_count(
                target_kind,
                "__reconciliation__",
                reason,
                count,
            )
            stats.warning_reasons[reason] = stats.warning_reasons.get(reason, 0) + count

        if not self._dry_run:
            recorder = getattr(self._repo, "record_connector_run", None)
            if callable(recorder):
                try:
                    recorder(
                        request_id=request_id,
                        source_kind=target_kind,
                        source_id="__reconciliation__",
                        outcome="success_with_warnings",
                        documents_upserted=0,
                        chunks_inserted=0,
                        warning_reasons=counts,
                        suppressed_retry_count=0,
                        error=None,
                    )
                except Exception as exc:
                    logger.warning(
                        "ingest_reconciliation_connector_run_failed",
                        request_id=request_id,
                        error_type=type(exc).__name__,
                    )
        _send_ops_warning_summary(
            self._alerter,
            kind=target_kind,
            warning_reasons=counts,
            suppressed_retry_count=0,
            request_id=request_id,
            dry_run=self._dry_run,
        )

    def _maybe_mark_boilerplate(self, *, request_id: str) -> None:
        """全 upsert 完了後、env ゲートが ON ならコーパス横断でテンプレ印を付け直す。

        ゲート: ``BOILERPLATE_DETECT``（既定 OFF）。dry-run では DB を書かないので skip。
        既存の書込み接続（repository が持つ pgvector client の admin role 接続）を
        再利用し、新規接続を増やさない。失敗は fail-open（WARN ログ＋取り込みは成功扱い）。

        ``mark_boilerplate`` は admin role で chunks を UPDATE する必要があるため、
        ``upsert_document_with_chunks`` / ``_ops_connection`` と同条件
        （app_role + ``user_role='admin'``）の接続を取得する。
        """
        if self._dry_run or not _envflag("BOILERPLATE_DETECT"):
            return
        min_docs = _envint("BOILERPLATE_MIN_DOCS", 3)
        # M2: 正規化長がこの文字数未満の短い chunk はテンプレ判定の対象外（既定 40）。
        min_chars = _envint("BOILERPLATE_MIN_CHARS", 40)
        try:
            # repository が ingest_jobs / connector_state 用に持つ admin role 接続を再利用。
            # chunks の UPDATE policy（migration 0003）は user_role='admin' で bypass される。
            with self._repo._ops_connection() as conn:
                # SQL走査中とPython比較中（idle-in-tx）の双方を守る。SET LOCALなので
                # transaction終了時に自動失効し、検索接続や次transactionへ漏れない。
                _disable_corpus_scan_timeouts(conn)
                affected = mark_boilerplate(conn, min_docs=min_docs, min_chars=min_chars)
            logger.info(
                "ingest_boilerplate_done",
                request_id=request_id,
                min_docs=min_docs,
                min_chars=min_chars,
                affected=affected,
            )
        except Exception:
            # fail-open: テンプレ検出が落ちても取り込み自体は成功扱いにする。
            logger.warning(
                "ingest_boilerplate_failed",
                request_id=request_id,
                min_docs=min_docs,
                exc_info=True,
            )

    def _maybe_check_freshness(self, *, request_id: str) -> None:
        """run 末尾で source_type 別の取り込み鮮度を検査し、stale を ops 通知する。

        「Slack が 6 週間サイレント停止していたのに誰も気づかない」（2026-07-13）の
        再発防止。read-only（SELECT のみ）・fail-open（検査が落ちても取り込みは成功扱い）。
        通知は OPS_SLACK_WEBHOOK_URL 未設定なら no-op。dry-run では skip。
        """
        if self._dry_run:
            return
        try:
            import datetime as _dt

            from teamagent.ingest.freshness import find_stale_sources, max_age_days_from_env

            now = _dt.datetime.now(_dt.UTC)
            max_age = max_age_days_from_env()
            with self._repo._ops_connection() as conn:
                with conn.cursor() as cur:
                    stale = find_stale_sources(cur, now=now, max_age_days=max_age)
            if stale:
                logger.warning(
                    "ingest_freshness_stale",
                    request_id=request_id,
                    stale=[s.source_type for s in stale],
                    max_age_days=max_age,
                )
                self._alerter.send_freshness_warning(
                    stale=stale, request_id=request_id, dry_run=self._dry_run
                )
            else:
                logger.info("ingest_freshness_ok", request_id=request_id, max_age_days=max_age)
        except Exception:
            logger.warning("ingest_freshness_check_failed", request_id=request_id, exc_info=True)

    def _maybe_mark_duplicate_documents(self, *, request_id: str) -> None:
        """テンプレ検出の直後、env ゲートが ON なら資料まるごと重複排除を実行する。

        ゲート: ``DOC_DEDUP_DETECT``（既定 OFF）。dry-run では DB を書かないので skip。
        しきい値は ``DOC_DEDUP_JACCARD``（既定 0.7・MinHash Jaccard 推定）。
        ``mark_boilerplate`` と同じく既存の admin role 接続（``_ops_connection``）を
        再利用し、新規接続を増やさない。失敗は fail-open（WARN ログ＋取り込みは成功扱い）。
        """
        if self._dry_run or not _envflag("DOC_DEDUP_DETECT"):
            return
        jaccard_threshold = _envfloat("DOC_DEDUP_JACCARD", 0.7)
        # H2: OOM/timeout 回避の上限（doc 数・per-doc 文字数）。env で上書き可。
        max_docs = _envint("DOC_DEDUP_MAX_DOCS", 5000)
        max_chars = _envint("DOC_DEDUP_MAX_CHARS", 500_000)
        try:
            # repository が ingest_jobs / connector_state 用に持つ admin role 接続を再利用。
            # documents の UPDATE policy は user_role='admin' で bypass される（boilerplate 同様）。
            with self._repo._ops_connection() as conn:
                # 全文ロード後のPython比較中は接続がidle-in-txになるため、boilerplateと同じ
                # transaction限定helperでstatement/idle-in-txの双方を無制限にする。
                _disable_corpus_scan_timeouts(conn)
                affected = mark_duplicate_documents(
                    conn,
                    jaccard_threshold=jaccard_threshold,
                    max_docs=max_docs,
                    max_chars=max_chars,
                )
            logger.info(
                "ingest_docdedup_done",
                request_id=request_id,
                jaccard_threshold=jaccard_threshold,
                max_docs=max_docs,
                max_chars=max_chars,
                affected=affected,
            )
        except Exception:
            # fail-open: 重複排除が落ちても取り込み自体は成功扱いにする。
            logger.warning(
                "ingest_docdedup_failed",
                request_id=request_id,
                jaccard_threshold=jaccard_threshold,
                exc_info=True,
            )

    def _maybe_mark_stale_documents(
        self,
        observed_gdrive_ids: set[str],
        *,
        kinds: list[str],
        request_id: str,
        result: IngestResult | None = None,
        truncated_walk_roots: set[str] | None = None,
    ) -> None:
        """run 末尾の stale soft-delete（入れ込み v2・env ゲート ``INGEST_MARK_STALE``）。

        run 中に Drive 上で観測できなかった source_type='gdrive' の documents に
        metadata.stale='true' + stale_marked_at を jsonb 付与し（物理 DELETE はしない）、
        観測できた documents からは stale 印を除去する（復活対応）。

        観測完全性ガード（stale 堅牢化 2026-07-10）:
        - ``result`` の gdrive / shared_drives kind に stats.errors > 0 または
          sources_skipped > 0（＝walk 例外等で source ごと fail-open skip）がある run、
        - または ``truncated_walk_roots`` 非空（＝walk の max_files 打ち切り）の run では、
          失敗フォルダ/上限超過分の生存 file が「未観測」に見えて誤 stale されるため
          **mark/clear の両方を skip** して WARNING
          （``ingest_mark_stale_skipped``・reason で区別）を出す。
          incomplete traversal と同じ run では stale cleanup を一切 commit しない。
          どちらの引数も None（旧呼び出し）ならガード無し＝従来挙動。

        量的ブレーキ:
        - 新規 stale 候補が既存 gdrive documents 総数の **50% 超** → 中止して exit 1
          （``INGEST_STALE_ALLOW_MASS=true`` で明示続行可）
        - **30% 超 50% 以下** → WARNING を出して実行

        boilerplate / docdedup と違い **fail-open にしない**（operator が明示的に
        依頼した掃除が黙って劣化しないよう、DB エラー等はそのまま伝播＝非0終了）。
        """
        if not _envflag("INGEST_MARK_STALE"):
            return
        if self._dry_run:
            logger.info("ingest_mark_stale_skipped", request_id=request_id, reason="dry_run")
            return
        if "gdrive" not in kinds:
            # gdrive を走らせていない run では観測集合が意味を持たない（全 doc が
            # 未観測に見えて大量 stale になる）。誤爆を防いで skip を明示する。
            logger.warning(
                "ingest_mark_stale_skipped",
                request_id=request_id,
                reason="kinds に gdrive が無い run では stale 判定しない（観測集合が空になるため）",
                kinds=kinds,
            )
            return

        # 観測完全性ガード: 列挙の部分失敗 / walk 打ち切りがあった run では mark しない。
        # shared_drives は gdrive と同じ observed_gdrive_ids を共有するため両 kind を見る。
        failed_kinds: dict[str, dict[str, int]] = {}
        if result is not None:
            for k in ("gdrive", "shared_drives"):
                stats = result.by_kind.get(k)
                if stats is not None and (stats.errors or stats.sources_skipped):
                    failed_kinds[k] = {
                        "errors": len(stats.errors),
                        "sources_skipped": stats.sources_skipped,
                    }
        incomplete_reasons: list[str] = []
        if failed_kinds:
            incomplete_reasons.append("source_failure（gdrive 系列挙に部分失敗）")
        if truncated_walk_roots:
            incomplete_reasons.append("walk_truncated（max_files 打ち切りで列挙が不完全）")
        if incomplete_reasons:
            logger.warning(
                "ingest_mark_stale_skipped",
                request_id=request_id,
                reason=(
                    "観測集合が不完全なため stale cleanup（mark/clear）を skip: "
                    + " / ".join(incomplete_reasons)
                ),
                failed_kinds=failed_kinds,
                truncated_walk_roots=sorted(truncated_walk_roots or set()),
                observed=len(observed_gdrive_ids),
            )
            return

        existing = self._repo.list_gdrive_external_ids_with_stale()
        total = len(existing)
        if total == 0:
            logger.info(
                "ingest_mark_stale_skipped",
                request_id=request_id,
                reason="gdrive documents が 0 件",
            )
            return

        # 新規 stale 候補 = 未観測 かつ まだ stale でない doc（既 stale の再マークはしない＝
        # 初回 stale_marked_at を保持し、ブレーキの分子も「今回新たに消える量」に限定する）。
        new_candidates = [
            eid for eid, is_stale in existing if eid not in observed_gdrive_ids and not is_stale
        ]
        ratio = len(new_candidates) / total
        allow_mass = _envflag("INGEST_STALE_ALLOW_MASS")
        if ratio > 0.5 and not allow_mass:
            message = (
                f"stale 候補 {len(new_candidates)} 件が既存 gdrive documents 総数 {total} 件の "
                f"50% を超えています（{ratio:.0%}）。誤設定（yaml 縮小 / walk 失敗）の疑いが"
                "あるため中止します。意図的な大量掃除なら INGEST_STALE_ALLOW_MASS=true で"
                "明示続行してください"
            )
            logger.error(
                "ingest_mark_stale_aborted",
                request_id=request_id,
                candidates=len(new_candidates),
                total=total,
                ratio=round(ratio, 3),
                message=message,
            )
            import sys as _sys

            print(f"[ERROR] {message}", file=_sys.stderr)
            raise SystemExit(1)
        if ratio > 0.3:
            logger.warning(
                "ingest_mark_stale_mass_warning",
                request_id=request_id,
                candidates=len(new_candidates),
                total=total,
                ratio=round(ratio, 3),
                allow_mass=allow_mass,
            )

        import datetime as _dt

        marked_at = _dt.datetime.now(_dt.UTC).isoformat()
        marked = self._repo.mark_documents_stale(new_candidates, marked_at_iso=marked_at)
        cleared = self._repo.clear_documents_stale(sorted(observed_gdrive_ids))
        logger.info(
            "ingest_mark_stale_done",
            request_id=request_id,
            observed=len(observed_gdrive_ids),
            total=total,
            marked=marked,
            cleared=cleared,
            marked_at=marked_at,
        )

    def _run_kind(
        self,
        kind: str,
        specs: tuple[Any, ...],
        handler: Any,
        *,
        request_id: str,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> IngestStats:
        # gdrive / shared_drives 経路にだけ §2 dedup ガード用レジストリを渡す
        # （folder→crawl で本文版を title_only 版が上書きしないよう、run 内で 1 個共有）。
        handler_kwargs = extra_kwargs or {}
        warning_collector_obj = handler_kwargs.get("warning_collector")
        warning_collector = (
            warning_collector_obj
            if isinstance(warning_collector_obj, _IngestWarningCollector)
            else None
        )
        stats = IngestStats(source_kind=kind)
        for spec in specs:
            source_id = _spec_source_id(spec) or kind
            warning_before = (
                warning_collector.snapshot(kind, source_id)
                if warning_collector is not None
                else _WarningSnapshot(reasons={}, suppressed=0)
            )
            docs_n = 0
            chunks_n = 0
            error_message: str | None = None
            try:
                docs_n, chunks_n = handler(
                    spec,
                    embedder=self._embedder,
                    repository=self._repo,
                    owner_email=self._owner_email,
                    dry_run=self._dry_run,
                    request_id=request_id,
                    **handler_kwargs,
                )
                stats.documents_upserted += docs_n
                stats.chunks_inserted += chunks_n
                stats.sources_processed += 1
            except Exception as e:
                error_message = f"{type(e).__name__}: {e}"
                logger.exception(
                    "ingest_source_failed",
                    request_id=request_id,
                    kind=kind,
                    source_id_ref=_external_id_ref(source_id),
                )
                stats.sources_skipped += 1
                stats.errors.append(error_message)
                # #ops 通知（webhook 未設定 / dry-run なら no-op・失敗しても続行）。
                self._alerter.send_ingest_failure(
                    kind=kind,
                    exc=e,
                    request_id=request_id,
                    spec_repr="",
                    dry_run=self._dry_run,
                )
                # 増分同期 ON のとき source 単位の連続失敗を connector_state に刻む
                # （attempt_count++・last_error＝backoff/#ops しきい値判断の根拠）。
                # 既定 OFF なので従来挙動・既存テストの fake repo には影響しない。
                if not self._dry_run and _envflag("USE_INCREMENTAL_SYNC"):
                    incremental_source_id = _spec_source_id(spec)
                    if incremental_source_id is not None:
                        try:
                            self._repo.save_connector_state(
                                kind,
                                incremental_source_id,
                                success=False,
                                error=error_message,
                            )
                        except Exception as state_exc:
                            logger.warning(
                                "connector_state_failure_record_failed",
                                kind=kind,
                                source_id_ref=_external_id_ref(incremental_source_id),
                                error_type=type(state_exc).__name__,
                            )
            warning_delta = (
                warning_collector.delta(kind, source_id, warning_before)
                if warning_collector is not None
                else _WarningSnapshot(reasons={}, suppressed=0)
            )
            for reason, count in warning_delta.reasons.items():
                stats.warning_reasons[reason] = stats.warning_reasons.get(reason, 0) + count
            stats.known_invalid_suppressed += warning_delta.suppressed

            outcome = (
                "failed"
                if error_message is not None
                else ("success_with_warnings" if warning_delta.reasons else "success")
            )
            if not self._dry_run:
                recorder = getattr(self._repo, "record_connector_run", None)
                if callable(recorder):
                    try:
                        recorder(
                            request_id=request_id,
                            source_kind=kind,
                            source_id=source_id,
                            outcome=outcome,
                            documents_upserted=docs_n,
                            chunks_inserted=chunks_n,
                            warning_reasons=warning_delta.reasons,
                            suppressed_retry_count=warning_delta.suppressed,
                            error=(
                                error_message.split(":", 1)[0]
                                if error_message is not None
                                else None
                            ),
                        )
                    except Exception as record_exc:
                        logger.warning(
                            "ingest_connector_run_record_failed",
                            request_id=request_id,
                            kind=kind,
                            source_id_ref=_external_id_ref(source_id),
                            error_type=type(record_exc).__name__,
                        )
            if error_message is None and warning_delta.reasons:
                _send_ops_warning_summary(
                    self._alerter,
                    kind=kind,
                    warning_reasons=warning_delta.reasons,
                    suppressed_retry_count=warning_delta.suppressed,
                    request_id=request_id,
                    dry_run=self._dry_run,
                )
        return stats
