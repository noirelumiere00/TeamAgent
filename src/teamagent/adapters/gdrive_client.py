"""Google Drive クライアント（取り込み用）。

CLAUDE.md 6-bis Adapter 層。Skill から googleapiclient を直接呼ばない。
Sprint 3 / PR-2 で雛形を導入。S3-05 / S3-06 / S3-07 で本実装する。

設計判断（2026-05-26 セキュリティ Agent 調査 + ユーザー確認）:
- **OAuth スコープ**: `drive.file` + Picker UI（CASA 監査不要 / Non-sensitive）
  または `drive.readonly`（要 CASA Tier 2、上層承認時のみ）
- **認証**: 個人 OAuth リフレッシュトークン優先（DWD は 16 名規模では避ける）
- **ACL**: `permissions.list` の結果を documents.acl_emails に写像
- **idempotency**: Drive `fileId` を documents.external_id (UNIQUE) に使用
- **changes.list**: `get_start_page_token()` → 定期 cron で `get_changes()`

Usage:
    client = GDriveClient.from_env()
    for file in client.list_files(folder_id="0AB...", request_id="req-1"):
        print(file.name, file.modified_time)

    perms = client.list_permissions(file_id="1XY...", request_id="req-1")
    acl_emails = [p.email_address for p in perms if p.email_address]

テストでは FakeDriveService を inject:
    fake = FakeDriveService(file_list=[...])
    client = GDriveClient(credentials=None, service=fake)
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import structlog

from teamagent.adapters.google_auth import (
    build_oauth_credentials,
    force_oauth_enabled,
)
from teamagent.adapters.oauth_token_store import OAuthToken

# Day 7 (2026-05-27): SSL socket が無期限ハングする問題への対策。
# 大 PDF download (8-10MB) で httplib2 のデフォルト無限 timeout が原因で固まることがある。
# 60 秒で諦めて TimeoutError を上げさせ、上位の try/except でスキップさせる。
socket.setdefaulttimeout(60)

logger = structlog.get_logger(__name__)

# 入れ込み v2 (2026-07-10): walk_files_recursive の既定フォルダ名除外 regex。
# 「99_一次倉庫」系（検索対象外の生データ置き場）をコードで保証して取り込まない。
# yaml のグローバルキー ``gdrive_exclude_folder_name_re`` で上書き可（pipeline 側で配線）。
DEFAULT_EXCLUDE_FOLDER_NAME_RE = r"^\s*99[_＿]|一次倉庫|検索対象外"

# walk_files_recursive の既定 max_files。定数に切り出す理由: pipeline 側の打ち切り検知
# （len(files) >= 上限 → stale mark を skip する run 単位フラグ）が同じ値を参照するため。
# シグネチャに 5000 を直書きすると両者が乖離した時に打ち切りが無検知になる。
DEFAULT_WALK_MAX_FILES = 5000

# permissions.list は 1 ページ最大 100 件。ACL 同期で途中ページを「全件」と誤認すると
# 権限を過小評価してしまうため、全ページを取得しつつ無限 token loop には上限で fail-closed。
DEFAULT_PERMISSIONS_MAX_PAGES = 100
DEFAULT_GOOGLE_API_RETRIES = 3
DEFAULT_GDRIVE_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024


def _file_ref(file_id: str) -> str:
    """ログ用の非可逆な短いDrive参照。"""
    return hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:12]


class GDrivePermissionsPaginationError(RuntimeError):
    """permissions.list を最後のページまで安全に列挙できなかった。"""


class GDriveTraversalIncompleteError(RuntimeError):
    """Drive traversal が安全上限・pagination異常により完走できなかった。"""

    def __init__(self, operation: str, reason: str, **diagnostics: Any) -> None:
        self.operation = operation
        self.reason = reason
        self.diagnostics = diagnostics
        details = ", ".join(f"{key}={value}" for key, value in sorted(diagnostics.items()))
        suffix = f" ({details})" if details else ""
        super().__init__(f"{operation} incomplete: {reason}{suffix}")


class GDriveDownloadContentError(RuntimeError):
    """Drive API がファイル本体以外の内容を返したときの分類可能な例外。"""

    def __init__(self, category: str, *, actual_bytes: int) -> None:
        self.category = category
        self.actual_bytes = actual_bytes
        super().__init__(f"gdrive download returned invalid content: {category}")


class _BoundedBytesIO(BytesIO):
    """MediaIoBaseDownloadがhard capを越えてbufferを拡張する前に中断する。"""

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self._max_bytes = max_bytes

    def write(self, data: Any) -> int:
        view = self.getbuffer()
        try:
            current_size = view.nbytes
        finally:
            view.release()
        projected_size = max(current_size, self.tell() + len(data))
        if projected_size > self._max_bytes:
            raise GDriveDownloadContentError(
                "download_too_large",
                actual_bytes=projected_size,
            )
        return super().write(data)


# -----------------------------------------------------------
# データ型
# -----------------------------------------------------------
@dataclass(frozen=True)
class DriveFile:
    """Drive API files.list() 1 件分。

    フィールドは Drive API v3 のレスポンスにマップ：
    https://developers.google.com/drive/api/reference/rest/v3/files#File
    """

    id: str  # Drive fileId（documents.external_id に使う）
    name: str
    mime_type: str
    modified_time: str | None  # ISO8601, changes.list の since 比較用
    size: int | None  # bytes、フォルダや Google Doc は None
    parents: tuple[str, ...] = ()
    web_view_link: str | None = None  # documents.source_uri / Drive ボタン URL
    owners_email: tuple[str, ...] = ()  # documents.owner_email 候補
    md5_checksum: str | None = None  # binary file の Drive advertised MD5


@dataclass(frozen=True)
class DrivePermission:
    """Drive API permissions.list() 1 件分。

    https://developers.google.com/drive/api/reference/rest/v3/permissions#Permission
    type: "user" | "group" | "domain" | "anyone"
    role: "owner" | "organizer" | "fileOrganizer" | "writer" | "commenter" | "reader"
    """

    id: str
    type: str
    role: str
    email_address: str | None = None  # user / group なら入る、anyone / domain は None
    domain: str | None = None  # domain / anyone-with-link は domain あり
    deleted: bool = False
    # ``published`` / ``metadata`` は本文閲覧権ではない。ACL 同期では除外する。
    view: str | None = None


@dataclass(frozen=True)
class ChangeBatch:
    """changes.list() の 1 ページ分。

    next_page_token があれば追加ページあり。
    new_start_page_token は最後のページにのみ含まれ、次回 cron 起動時の since として保存。
    """

    changes: tuple[DriveChange, ...]
    next_page_token: str | None = None
    new_start_page_token: str | None = None


@dataclass(frozen=True)
class DriveChange:
    """changes.list() の 1 件分。"""

    change_type: str  # "file" | "drive"
    file_id: str | None
    removed: bool
    time: str | None  # ISO8601
    drive_id: str | None = None  # 共有ドライブ ID（マイドライブなら None）


@dataclass(frozen=True)
class SharedDrive:
    """drives.list() の 1 件分（共有ドライブ）。"""

    id: str
    name: str
    created_time: str | None = None


# -----------------------------------------------------------
# クライアント本体
# -----------------------------------------------------------
class GDriveClient:
    """Google Drive API v3 の薄ラッパー。

    認証パターン：
      1. OAuth リフレッシュトークン（推奨）: TEAMAGENT_GDRIVE_REFRESH_TOKEN_SECRET
         + GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET（OAuth クライアント）
      2. Service Account（DWD 使う時のみ）: GOOGLE_APPLICATION_CREDENTIALS=path/to/sa.json

    Skill 層からは psycopg と同じく `from_env()` でインスタンス化、
    `list_files()` 等を呼ぶ。実 API 呼び出しは `_ensure_service()` で遅延初期化。
    """

    # Drive API のスコープ。CASA 監査回避のため drive.file を既定にする。
    # 全フォルダ走査が必要な場合のみ呼び出し側で SCOPES_READONLY を渡す。
    SCOPES_FILE: tuple[str, ...] = (
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.metadata.readonly",  # ACL 取得用
    )
    # Day 7 (2026-05-27): folder bulk ingest 用。Internal OAuth なら CASA 不要。
    # drive.metadata.readonly も同梱で permissions.list 等も問題なく動作する。
    SCOPES_READONLY: tuple[str, ...] = (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    )

    def __init__(
        self,
        credentials: Any | None = None,
        *,
        service: Any | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> None:
        """credentials が None なら from_env() で組み立てる。

        service を直接渡すとテスト用の Fake service を inject できる
        （googleapiclient.discovery.build() の戻り値互換）。
        """
        self._credentials = credentials
        self._service = service
        self._scopes = scopes or self.SCOPES_FILE

    @classmethod
    def from_env(cls, *, readonly: bool = False) -> GDriveClient:
        """環境変数から credentials を組み立てる。

        readonly=True にすると drive.readonly スコープ（CASA 必須）。
        """
        scopes = cls.SCOPES_READONLY if readonly else cls.SCOPES_FILE
        # 実 credentials 構築は _ensure_service() 内で遅延、ここでは存在チェックのみ
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gdrive_credentials_missing",
                hint=(
                    "GOOGLE_CLIENT_ID + refresh token、または "
                    "GOOGLE_APPLICATION_CREDENTIALS を設定してください"
                ),
            )
        return cls(credentials=None, scopes=scopes)

    @classmethod
    def from_user_token(cls, token: OAuthToken, *, readonly: bool = True) -> GDriveClient:
        """per-user: 本人の refresh token から構築（本人の Drive のみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        scopes = cls.SCOPES_READONLY if readonly else cls.SCOPES_FILE
        return cls(credentials=build_user_credentials(token), scopes=scopes)

    # -------------------------------------------------------
    # API 呼び出し
    # -------------------------------------------------------
    def list_files(
        self,
        folder_id: str | None,
        request_id: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
        include_shared_drives: bool = True,
        mime_type_filter: str | None = None,
    ) -> tuple[list[DriveFile], str | None]:
        """指定フォルダ内のファイルを 1 ページ取得する。

        Args:
            folder_id: 親フォルダ ID。None ならルート（全体検索になりがちなので推奨しない）
            request_id: トレース ID
            page_size: 1 ページの件数（最大 1000、Drive API 仕様）
            page_token: 次ページの token（前回返り値の 2 要素目）
            include_shared_drives: 共有ドライブも含めるか
            mime_type_filter: 例 'application/pdf' で PDF だけ

        Returns:
            (files, next_page_token) のタプル
        """
        service = self._ensure_service()
        # クエリ組み立て（外部入力はエスケープしないため、folder_id は文字列限定）
        clauses: list[str] = ["trashed = false"]
        if folder_id:
            clauses.append(f"'{folder_id}' in parents")
        if mime_type_filter:
            clauses.append(f"mimeType = '{mime_type_filter}'")
        q = " and ".join(clauses)

        kwargs: dict[str, Any] = {
            "q": q,
            "pageSize": page_size,
            "fields": (
                "nextPageToken, files("
                "id, name, mimeType, modifiedTime, size, md5Checksum, parents, "
                "webViewLink, owners(emailAddress)"
                ")"
            ),
            "supportsAllDrives": include_shared_drives,
            "includeItemsFromAllDrives": include_shared_drives,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        start = time.perf_counter()
        resp = service.files().list(**kwargs).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        raw_files = resp.get("files", [])
        files = [
            DriveFile(
                id=str(f.get("id", "")),
                name=str(f.get("name", "")),
                mime_type=str(f.get("mimeType", "")),
                modified_time=f.get("modifiedTime"),
                size=int(f["size"]) if f.get("size") else None,
                md5_checksum=str(f["md5Checksum"]).lower() if f.get("md5Checksum") else None,
                parents=tuple(f.get("parents", ()) or ()),
                web_view_link=f.get("webViewLink"),
                owners_email=tuple(
                    o.get("emailAddress") for o in (f.get("owners") or []) if o.get("emailAddress")
                ),
            )
            for f in raw_files
        ]
        next_token = resp.get("nextPageToken")
        logger.info(
            "gdrive_list_files",
            request_id=request_id,
            folder_id=folder_id,
            page_size=page_size,
            returned=len(files),
            has_next=bool(next_token),
            latency_ms=latency_ms,
        )
        return files, next_token

    def list_permissions(
        self,
        file_id: str,
        request_id: str,
        *,
        include_shared_drives: bool = True,
        page_size: int = 100,
        max_pages: int = DEFAULT_PERMISSIONS_MAX_PAGES,
        api_retries: int = DEFAULT_GOOGLE_API_RETRIES,
    ) -> list[DrivePermission]:
        """ファイル ACL を最終ページまで取得する（打ち切り時は部分結果を返さない）。

        ``max_pages`` 到達時に ``nextPageToken`` が残る、または token が循環する場合は
        :class:`GDrivePermissionsPaginationError` を送出する。ACL の部分取得を完全取得として
        扱うとアクセス権を誤って縮小するため、ここは fail-closed とする。

        ``api_retries`` は googleapiclient の ``execute(num_retries=...)`` に渡し、429/5xx
        など同ライブラリが一過性と判定する失敗をページ単位で再試行する。
        """
        if not 1 <= page_size <= 100:
            raise ValueError("permissions page_size must be between 1 and 100")
        if max_pages < 1:
            raise ValueError("permissions max_pages must be at least 1")
        if api_retries < 0:
            raise ValueError("permissions api_retries must be non-negative")

        service = self._ensure_service()
        start = time.perf_counter()
        raw_permissions: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        pages = 0

        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "fileId": file_id,
                "pageSize": page_size,
                "fields": (
                    "nextPageToken, permissions("
                    "id, type, role, emailAddress, domain, deleted, view)"
                ),
                "supportsAllDrives": include_shared_drives,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.permissions().list(**kwargs).execute(num_retries=api_retries)
            pages += 1
            raw_permissions.extend(resp.get("permissions", []) or [])

            next_token_raw = resp.get("nextPageToken")
            next_token = str(next_token_raw) if next_token_raw else None
            if not next_token:
                page_token = None
                break
            if next_token in seen_tokens or next_token == page_token:
                raise GDrivePermissionsPaginationError(
                    "permissions pagination token did not advance"
                )
            seen_tokens.add(next_token)
            page_token = next_token

        if page_token:
            raise GDrivePermissionsPaginationError(
                "permissions pagination reached max_pages with a remaining token"
            )

        latency_ms = int((time.perf_counter() - start) * 1000)
        perms = [
            DrivePermission(
                id=str(p.get("id", "")),
                type=str(p.get("type", "")),
                role=str(p.get("role", "")),
                email_address=p.get("emailAddress"),
                domain=p.get("domain"),
                deleted=bool(p.get("deleted", False)),
                view=p.get("view"),
            )
            for p in raw_permissions
        ]
        logger.info(
            "gdrive_list_permissions",
            request_id=request_id,
            count=len(perms),
            pages=pages,
            latency_ms=latency_ms,
        )
        return perms

    def download_file_bytes(
        self,
        file_id: str,
        request_id: str,
        *,
        max_bytes: int = DEFAULT_GDRIVE_DOWNLOAD_MAX_BYTES,
    ) -> bytes:
        """ファイル本体をバイナリで取得する（PDF / バイナリファイル用）。

        Google Doc / Sheet / Slide は `export_file()` を使う必要がある（別メソッド予定）。
        ``max_bytes``を越える応答はbuffer拡張前に分類例外で中断する。
        """
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        service = self._ensure_service()
        # 遅延 import: googleapiclient.http が大きい
        from googleapiclient.http import MediaIoBaseDownload

        start = time.perf_counter()
        # acknowledgeAbuse=True: 大容量/スキャン未確認ファイルで Google が本体の代わりに
        # 「ウイルススキャンできませんでした」確認応答を返し、結果として後段の zip/pdf 解析が
        # "File is not a zip file" 等で無音失敗するのを防ぐ（本体バイトを返させる）。非該当
        # ファイルには無害。num_retries で httplib2 の一過性失敗を吸収（大容量DLの途中切れ対策）。
        request = service.files().get_media(
            fileId=file_id, supportsAllDrives=True, acknowledgeAbuse=True
        )
        buf = _BoundedBytesIO(max_bytes)
        downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk(num_retries=3)
        data = buf.getvalue()
        latency_ms = int((time.perf_counter() - start) * 1000)
        # office(PK..)/pdf(%PDF) は決して HTML で始まらない。先頭が HTML マーカーなら本体取得に
        # 失敗して確認/エラーページが降ってきた証拠 → 無音 BadZipFile より前に分類例外にして
        # 原因を可視化（呼び出し側は既存documentを保持してskip＝fail-openは維持）。
        head = data[:512].lstrip()
        if head.startswith(b"\xef\xbb\xbf"):
            head = head[3:].lstrip()
        lowered = head.lower()
        if (
            lowered.startswith(b"<!doctype html")
            or lowered.startswith(b"<html")
            or (lowered.startswith(b"<?xml") and b"<html" in lowered)
        ):
            logger.warning(
                "gdrive_download_not_binary",
                request_id=request_id,
                file_ref=_file_ref(file_id),
                bytes=len(data),
                category="html_response",
            )
            raise GDriveDownloadContentError("html_response", actual_bytes=len(data))
        logger.info(
            "gdrive_download_file",
            request_id=request_id,
            file_ref=_file_ref(file_id),
            bytes=len(data),
            latency_ms=latency_ms,
        )
        return data

    def list_shared_drives(
        self,
        request_id: str,
        *,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> list[SharedDrive]:
        """ユーザーがメンバーになっている共有ドライブを全件取得する。

        Day 7 (2026-05-27) で追加: 共有ドライブ全自動 crawl 用の起点 API。
        マイドライブ や「Shared with me」 は含まれない。
        """
        if page_size < 1 or page_size > 100:
            raise ValueError("shared drives page_size must be between 1 and 100")
        if max_pages < 1:
            raise ValueError("shared drives max_pages must be at least 1")
        service = self._ensure_service()
        out: list[SharedDrive] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "pageSize": page_size,
                "fields": "nextPageToken, drives(id, name, createdTime)",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.drives().list(**kwargs).execute()
            for d in resp.get("drives", []):
                out.append(
                    SharedDrive(
                        id=str(d.get("id", "")),
                        name=str(d.get("name", "")),
                        created_time=d.get("createdTime"),
                    )
                )
            next_token = resp.get("nextPageToken")
            if not next_token:
                page_token = None
                break
            if next_token == page_token or next_token in seen_tokens:
                logger.error(
                    "gdrive_list_shared_drives_incomplete",
                    request_id=request_id,
                    reason="pagination_token_cycle",
                    drives_collected=len(out),
                    max_pages=max_pages,
                )
                raise GDriveTraversalIncompleteError(
                    "list_shared_drives",
                    "pagination token cycle",
                    drives_collected=len(out),
                    max_pages=max_pages,
                )
            seen_tokens.add(next_token)
            page_token = next_token
        if page_token:
            logger.error(
                "gdrive_list_shared_drives_incomplete",
                request_id=request_id,
                reason="page_limit_with_remaining_token",
                drives_collected=len(out),
                max_pages=max_pages,
            )
            raise GDriveTraversalIncompleteError(
                "list_shared_drives",
                "page limit reached with remaining token",
                drives_collected=len(out),
                max_pages=max_pages,
            )
        logger.info("gdrive_list_shared_drives", request_id=request_id, count=len(out))
        return out

    def walk_files_recursive(
        self,
        root_id: str,
        request_id: str,
        *,
        drive_id: str | None = None,
        max_files: int = DEFAULT_WALK_MAX_FILES,
        max_depth: int = 10,
        exclude_folder_name_re: str | None = None,
    ) -> list[DriveFile]:
        """指定 root_id (フォルダ or 共有ドライブ root) 配下を BFS で全件 walk する。

        Day 7 (2026-05-27) で追加: 共有ドライブのサブフォルダ含む全 file を回収する。

        Args:
            root_id: 起点フォルダ ID（共有ドライブ root の場合は driveId と同じ）
            drive_id: 共有ドライブの ID（指定すると corpora="drive" + driveId で絞る、
                       共有ドライブ専用クエリで効率化）
            max_files: 安全装置（暴走防止、1 共有ドライブで 5000 ファイルが上限）
            max_depth: フォルダ階層の最大深度
            exclude_folder_name_re: サブフォルダ名がこの regex に search マッチしたら
                配下ごと skip する（入れ込み v2・99_一次倉庫系の除外保証）。
                None / 空文字なら除外しない（後方互換）。既定値は
                ``DEFAULT_EXCLUDE_FOLDER_NAME_RE`` を呼び出し側（pipeline）が解決して渡す。

        Returns:
            files (フォルダ自身は除外、通常ファイルのみ)
        """
        import re as _re

        if max_files < 1:
            raise ValueError("walk max_files must be at least 1")
        if max_depth < 0:
            raise ValueError("walk max_depth must be non-negative")

        folder_mime = "application/vnd.google-apps.folder"
        service = self._ensure_service()
        # 除外 regex は fail-loud: 不正な regex は即 re.error で落とす（黙って全取込しない）。
        exclude_re = _re.compile(exclude_folder_name_re) if exclude_folder_name_re else None
        out: list[DriveFile] = []
        queue: list[tuple[str, int]] = [(root_id, 0)]  # (folder_id, depth)
        visited: set[str] = set()

        while queue and len(out) < max_files:
            folder_id, depth = queue.pop(0)
            if folder_id in visited:
                continue
            visited.add(folder_id)

            # この folder 直下の files / sub-folders を取得（全 page）
            page_token: str | None = None
            seen_page_tokens: set[str] = set()
            for _ in range(20):  # 1 folder の最大ページ数（page_size=1000 × 20 = 20k 上限）
                kwargs: dict[str, Any] = {
                    "q": f"'{folder_id}' in parents and trashed = false",
                    "pageSize": 1000,
                    "fields": (
                        "nextPageToken, files("
                        "id, name, mimeType, modifiedTime, size, md5Checksum, parents, "
                        "webViewLink, owners(emailAddress))"
                    ),
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                }
                if drive_id:
                    kwargs["corpora"] = "drive"
                    kwargs["driveId"] = drive_id
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = service.files().list(**kwargs).execute()
                for f in resp.get("files", []):
                    mime = str(f.get("mimeType", ""))
                    if mime == folder_mime:
                        sub_name = str(f.get("name", ""))
                        # 入れ込み v2: 除外 regex にマッチするサブフォルダは配下ごと skip
                        # （99_一次倉庫等の検索対象外フォルダをコードで保証して取り込まない）。
                        if exclude_re is not None and exclude_re.search(sub_name):
                            logger.info(
                                "skipped_folder",
                                request_id=request_id,
                                folder_id=str(f.get("id", "")),
                                folder_name=sub_name,
                                pattern=exclude_folder_name_re,
                            )
                            continue
                        if depth >= max_depth:
                            logger.error(
                                "gdrive_walk_files_recursive_incomplete",
                                request_id=request_id,
                                root_id=root_id,
                                drive_id=drive_id,
                                reason="max_depth_with_child_folder",
                                max_depth=max_depth,
                                folders_visited=len(visited),
                                files_collected=len(out),
                            )
                            raise GDriveTraversalIncompleteError(
                                "walk_files_recursive",
                                "max depth reached with child folders remaining",
                                root_id=root_id,
                                max_depth=max_depth,
                                folders_visited=len(visited),
                                files_collected=len(out),
                            )
                        # sub-folder → queue に追加
                        queue.append((str(f.get("id", "")), depth + 1))
                    else:
                        out.append(
                            DriveFile(
                                id=str(f.get("id", "")),
                                name=str(f.get("name", "")),
                                mime_type=mime,
                                modified_time=f.get("modifiedTime"),
                                size=int(f["size"]) if f.get("size") else None,
                                md5_checksum=(
                                    str(f["md5Checksum"]).lower() if f.get("md5Checksum") else None
                                ),
                                parents=tuple(f.get("parents", ()) or ()),
                                web_view_link=f.get("webViewLink"),
                                owners_email=tuple(
                                    o.get("emailAddress")
                                    for o in (f.get("owners") or [])
                                    if o.get("emailAddress")
                                ),
                            )
                        )
                        if len(out) >= max_files:
                            break
                next_token = resp.get("nextPageToken")
                if len(out) >= max_files:
                    page_token = next_token
                    break
                if not next_token:
                    page_token = None
                    break
                if next_token == page_token or next_token in seen_page_tokens:
                    logger.error(
                        "gdrive_walk_files_recursive_incomplete",
                        request_id=request_id,
                        root_id=root_id,
                        drive_id=drive_id,
                        folder_id=folder_id,
                        reason="pagination_token_cycle",
                        folders_visited=len(visited),
                        files_collected=len(out),
                    )
                    raise GDriveTraversalIncompleteError(
                        "walk_files_recursive",
                        "pagination token cycle",
                        root_id=root_id,
                        folder_id=folder_id,
                        folders_visited=len(visited),
                        files_collected=len(out),
                    )
                seen_page_tokens.add(next_token)
                page_token = next_token
            if page_token and len(out) < max_files:
                logger.error(
                    "gdrive_walk_files_recursive_incomplete",
                    request_id=request_id,
                    root_id=root_id,
                    drive_id=drive_id,
                    folder_id=folder_id,
                    reason="page_limit_with_remaining_token",
                    max_pages=20,
                    folders_visited=len(visited),
                    files_collected=len(out),
                )
                raise GDriveTraversalIncompleteError(
                    "walk_files_recursive",
                    "page limit reached with remaining token",
                    root_id=root_id,
                    folder_id=folder_id,
                    max_pages=20,
                    folders_visited=len(visited),
                    files_collected=len(out),
                )

        hit_max = len(out) >= max_files
        # stale 堅牢化 (2026-07-10): max_files 打ち切りは列挙の不完全＝INGEST_MARK_STALE の
        # 観測集合の欠損に直結するため、無警告で流さず WARNING に昇格する（イベント名・
        # 既存キーは維持し、max_files キーを追加して件数と上限を突き合わせ可能にする）。
        log_fn = logger.warning if hit_max else logger.info
        log_fn(
            "gdrive_walk_files_recursive",
            request_id=request_id,
            root_id=root_id,
            drive_id=drive_id,
            folders_visited=len(visited),
            files_collected=len(out),
            hit_max=hit_max,
            max_files=max_files,
        )
        return out

    def get_start_page_token(self, request_id: str) -> str:
        """changes.list の since 起点となる token を取得する。

        ingest パイプライン初回起動時に呼んで、得た token を S3 / DynamoDB に保存。
        以降は get_changes() のループ後の new_start_page_token を毎回上書き保存。
        """
        service = self._ensure_service()
        resp = service.changes().getStartPageToken(supportsAllDrives=True).execute()
        token = str(resp.get("startPageToken", ""))
        logger.info("gdrive_get_start_page_token", request_id=request_id, token=token)
        return token

    def get_changes(
        self,
        page_token: str,
        request_id: str,
        *,
        include_removed: bool = True,
        page_size: int = 100,
    ) -> ChangeBatch:
        """前回 token 以降の変更を 1 ページ取得する。

        15min cron で呼んで、changes.file_id ごとに ingest を再実行する想定。
        """
        service = self._ensure_service()
        start = time.perf_counter()
        resp = (
            service.changes()
            .list(
                pageToken=page_token,
                pageSize=page_size,
                includeRemoved=include_removed,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields=(
                    "nextPageToken, newStartPageToken, "
                    "changes(changeType, removed, time, fileId, driveId)"
                ),
            )
            .execute()
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        raw_changes = resp.get("changes", [])
        changes = tuple(
            DriveChange(
                change_type=str(c.get("changeType", "")),
                file_id=c.get("fileId"),
                removed=bool(c.get("removed", False)),
                time=c.get("time"),
                drive_id=c.get("driveId"),
            )
            for c in raw_changes
        )
        batch = ChangeBatch(
            changes=changes,
            next_page_token=resp.get("nextPageToken"),
            new_start_page_token=resp.get("newStartPageToken"),
        )
        logger.info(
            "gdrive_get_changes",
            request_id=request_id,
            page_token=page_token[:8] + "…" if len(page_token) > 8 else page_token,
            changes=len(changes),
            has_next=bool(batch.next_page_token),
            latency_ms=latency_ms,
        )
        return batch

    # -------------------------------------------------------
    # 内部
    # -------------------------------------------------------
    def _ensure_service(self) -> Any:
        """googleapiclient service を遅延初期化。"""
        if self._service is not None:
            return self._service
        # 遅延 import: googleapiclient はビルドが重い
        from googleapiclient.discovery import build

        if self._credentials is None:
            self._credentials = self._build_credentials()
        self._service = build("drive", "v3", credentials=self._credentials, cache_discovery=False)
        return self._service

    def _build_credentials(self) -> Any:
        """環境変数から credentials を構築する（OAuth / Service Account 両対応）。

        現状は雛形で NotImplementedError。S3-01〜S3-03 で GCP プロジェクトと OAuth
        同意画面・Service Account が用意できたら実装する。
        """
        # 1. SA 鍵があり、かつ OAuth 強制でなければ Service Account
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_path and not force_oauth_enabled():
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                sa_path, scopes=list(self._scopes)
            )
        # 2. 個人 OAuth（refresh token）。Drive のスコープは共有トークンの許可内なので
        #    self._scopes をそのまま要求してよい（GOOGLE_OAUTH_SCOPES で上書き可）。
        creds = build_oauth_credentials(self._scopes)
        if creds is not None:
            return creds
        raise NotImplementedError(
            "GCP credentials が未設定です。Service Account (GOOGLE_APPLICATION_CREDENTIALS) "
            "または OAuth (GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET) "
            "を設定してください。テストでは service=Fake service を渡してください。"
        )


# -----------------------------------------------------------
# ヘルパー: permissions → acl_emails 抽出
# -----------------------------------------------------------
def extract_acl_emails(
    perms: list[DrivePermission], *, workspace_domain: str | None = None
) -> tuple[list[str], list[str]]:
    """permissions.list の結果を documents.acl_emails / acl_groups に分解する。

    会社思想 (Day 7, 2026-05-27 ユーザー確認): 「資料は全て共有物」原則。
    Drive で domain / anyone 共有されているファイルは、ワークスペース内 (vectorinc.co.jp)
    全員に見せる方針。WORKSPACE_DOMAIN env で workspace を指定（既定 'vectorinc.co.jp'）。

    マッピング:
        type='user',  email=alice@... , not deleted → acl_emails に追加
        type='group', email=sales@...,  not deleted → acl_groups に追加
        type='domain', domain='vectorinc.co.jp'     → acl_groups に domain 追加
        type='anyone'                                → acl_groups に WORKSPACE_DOMAIN 追加
        role='owner'                                 → 別途 documents.owner_email に
    """
    workspace_domain = workspace_domain or os.environ.get("WORKSPACE_DOMAIN", "vectorinc.co.jp")

    emails: list[str] = []
    groups: list[str] = []
    for p in perms:
        # Drive API の published / metadata view は、公開表示またはメタデータ
        # 参照のための限定 permission。本文を閲覧できる ACL として扱わない。
        if p.deleted or p.view in {"published", "metadata"}:
            continue
        if p.type == "user" and p.email_address:
            emails.append(p.email_address)
        elif p.type == "group" and p.email_address:
            groups.append(p.email_address)
        elif p.type == "domain" and p.domain:
            # ドメイン共有: domain 名そのものを group key として扱う
            groups.append(p.domain)
        elif p.type == "anyone":
            # 「リンクを知ってる全員に公開」: 会社思想に従い workspace 全員に見せる
            groups.append(workspace_domain)

    # 重複除去（同じ domain が複数 permission で出ても 1 件に）
    return emails, sorted(set(groups))
