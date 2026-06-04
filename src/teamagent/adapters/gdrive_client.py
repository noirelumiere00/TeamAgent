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

import os
import socket
import time
from dataclasses import dataclass
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
                "id, name, mimeType, modifiedTime, size, parents, webViewLink, owners(emailAddress)"
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
    ) -> list[DrivePermission]:
        """ファイルの ACL を取得する。documents.acl_emails に写像するのが目的。

        Drive API 仕様で permissions.list は 100 件/ページなので、
        100 件超のケースは pageToken 追加対応が必要（Sprint 4 で）。
        """
        service = self._ensure_service()
        start = time.perf_counter()
        resp = (
            service.permissions()
            .list(
                fileId=file_id,
                fields="permissions(id, type, role, emailAddress, domain, deleted)",
                supportsAllDrives=include_shared_drives,
            )
            .execute()
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        raw = resp.get("permissions", [])
        perms = [
            DrivePermission(
                id=str(p.get("id", "")),
                type=str(p.get("type", "")),
                role=str(p.get("role", "")),
                email_address=p.get("emailAddress"),
                domain=p.get("domain"),
                deleted=bool(p.get("deleted", False)),
            )
            for p in raw
        ]
        logger.info(
            "gdrive_list_permissions",
            request_id=request_id,
            file_id=file_id,
            count=len(perms),
            latency_ms=latency_ms,
        )
        return perms

    def download_file_bytes(self, file_id: str, request_id: str) -> bytes:
        """ファイル本体をバイナリで取得する（PDF / バイナリファイル用）。

        Google Doc / Sheet / Slide は `export_file()` を使う必要がある（別メソッド予定）。
        """
        service = self._ensure_service()
        # 遅延 import: googleapiclient.http が大きい
        from io import BytesIO

        from googleapiclient.http import MediaIoBaseDownload

        start = time.perf_counter()
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = BytesIO()
        downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        data = buf.getvalue()
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "gdrive_download_file",
            request_id=request_id,
            file_id=file_id,
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
        service = self._ensure_service()
        out: list[SharedDrive] = []
        page_token: str | None = None
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
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        logger.info("gdrive_list_shared_drives", request_id=request_id, count=len(out))
        return out

    def walk_files_recursive(
        self,
        root_id: str,
        request_id: str,
        *,
        drive_id: str | None = None,
        max_files: int = 5000,
        max_depth: int = 10,
    ) -> list[DriveFile]:
        """指定 root_id (フォルダ or 共有ドライブ root) 配下を BFS で全件 walk する。

        Day 7 (2026-05-27) で追加: 共有ドライブのサブフォルダ含む全 file を回収する。

        Args:
            root_id: 起点フォルダ ID（共有ドライブ root の場合は driveId と同じ）
            drive_id: 共有ドライブの ID（指定すると corpora="drive" + driveId で絞る、
                       共有ドライブ専用クエリで効率化）
            max_files: 安全装置（暴走防止、1 共有ドライブで 5000 ファイルが上限）
            max_depth: フォルダ階層の最大深度

        Returns:
            files (フォルダ自身は除外、通常ファイルのみ)
        """
        folder_mime = "application/vnd.google-apps.folder"
        service = self._ensure_service()
        out: list[DriveFile] = []
        queue: list[tuple[str, int]] = [(root_id, 0)]  # (folder_id, depth)
        visited: set[str] = set()

        while queue and len(out) < max_files:
            folder_id, depth = queue.pop(0)
            if folder_id in visited or depth > max_depth:
                continue
            visited.add(folder_id)

            # この folder 直下の files / sub-folders を取得（全 page）
            page_token: str | None = None
            for _ in range(20):  # 1 folder の最大ページ数（page_size=1000 × 20 = 20k 上限）
                kwargs: dict[str, Any] = {
                    "q": f"'{folder_id}' in parents and trashed = false",
                    "pageSize": 1000,
                    "fields": (
                        "nextPageToken, files("
                        "id, name, mimeType, modifiedTime, size, parents, "
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
                page_token = resp.get("nextPageToken")
                if not page_token or len(out) >= max_files:
                    break

        logger.info(
            "gdrive_walk_files_recursive",
            request_id=request_id,
            root_id=root_id,
            drive_id=drive_id,
            folders_visited=len(visited),
            files_collected=len(out),
            hit_max=len(out) >= max_files,
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
def extract_acl_emails(perms: list[DrivePermission]) -> tuple[list[str], list[str]]:
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
    workspace_domain = os.environ.get("WORKSPACE_DOMAIN", "vectorinc.co.jp")

    emails: list[str] = []
    groups: list[str] = []
    for p in perms:
        if p.deleted:
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
