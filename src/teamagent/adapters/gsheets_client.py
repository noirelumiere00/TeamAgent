"""Google Sheets クライアント（フォーム回答 / 一覧データ取り込み用）。

CLAUDE.md 6-bis Adapter 層。Skill から googleapiclient を直接呼ばない。
Sprint 3 / PR-5 で雛形を導入。S3-13 後続で本実装。

設計判断（ユーザー貴重情報源対応 2026-05-26）:
- **対象**: ナレッジ共有フォーム回答 / ショート動画営業 FB フォーム回答
- **取り込み単位**: 1 行 = 1 document（row_unit=true）
  - 行に「タイムスタンプ / 業界 / クライアント / 温度感 / 自由記述」等の column
  - row_unit=false の場合は 1 タブを丸ごと連結（自由記述シート用、Sprint 4 で対応）
- **OAuth スコープ**: `https://www.googleapis.com/auth/spreadsheets.readonly`
  - **Non-sensitive** スコープ（CASA 不要）。読み取り専用
  - または既存 `drive.metadata.readonly` 経由でメタのみ
- **ACL**: Drive permissions.list を流用（Sheet も Drive ファイル）
  → gdrive_client.list_permissions(file_id=sheet_id) を再利用
- **idempotency**: `<sheet_id>:<gid>:<row_idx>` を documents.external_id に
- **フォーム回答の動的性**: 新規行が追加され続けるので、
  最新行 idx を S3 / DynamoDB に保存 → 差分 ingest（Sprint 4）

Usage:
    client = GSheetsClient.from_env()
    sheet_meta = client.get_sheet_metadata(sheet_id="1AbC...", request_id="r")
    rows, headers = client.get_tab_rows(
        sheet_id="1AbC...", tab_name="フォーム回答 1", request_id="r"
    )
    for idx, row in enumerate(rows):
        doc = format_row_as_document(headers, row)  # column名: value\n... の形式

テスト時は FakeSheetsService を inject:
    fake = FakeSheetsService(values=[["col1","col2"], ["v1","v2"]])
    client = GSheetsClient(service=fake)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# データ型
# -----------------------------------------------------------
@dataclass(frozen=True)
class SheetTab:
    """1 つのタブ（worksheet）のメタデータ。"""

    sheet_id: str  # spreadsheet 全体の id
    gid: int  # tab の id
    title: str  # tab 名
    row_count: int
    col_count: int


@dataclass(frozen=True)
class SheetMetadata:
    """spreadsheets.get の取得結果（メタのみ）。"""

    sheet_id: str
    title: str  # spreadsheet 全体の title
    tabs: tuple[SheetTab, ...]


@dataclass(frozen=True)
class TabRows:
    """1 タブ分の行データ。"""

    sheet_id: str
    tab_name: str
    headers: tuple[str, ...]  # 1 行目を見出しとして抽出
    rows: tuple[tuple[str, ...], ...]  # 2 行目以降
    row_count: int


# -----------------------------------------------------------
# クライアント本体
# -----------------------------------------------------------
class GSheetsClient:
    """Google Sheets API v4 の薄ラッパー。

    認証パターン（gdrive_client.py / gmail_client.py と統一）：
      1. OAuth リフレッシュトークン（推奨）
      2. Service Account（DWD 使う時のみ）
    """

    # 読み取り専用スコープ（Non-sensitive、CASA 不要）
    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    # 書き込み必要な場合（Sprint 4 で要件出たら）
    SCOPES_WRITE: tuple[str, ...] = ("https://www.googleapis.com/auth/spreadsheets",)

    def __init__(
        self,
        credentials: Any | None = None,
        *,
        service: Any | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._scopes = scopes or self.SCOPES_READONLY

    @classmethod
    def from_env(cls, *, write: bool = False) -> GSheetsClient:
        """環境変数から credentials を組み立てる。

        write=True で書き込みスコープ（フォーム回答更新等、慎重に）。
        """
        scopes = cls.SCOPES_WRITE if write else cls.SCOPES_READONLY
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gsheets_credentials_missing",
                hint=(
                    "GOOGLE_CLIENT_ID + refresh token、または "
                    "GOOGLE_APPLICATION_CREDENTIALS を設定してください"
                ),
            )
        return cls(credentials=None, scopes=scopes)

    # -------------------------------------------------------
    # メタデータ取得
    # -------------------------------------------------------
    def get_sheet_metadata(self, sheet_id: str, request_id: str) -> SheetMetadata:
        """spreadsheets.get でメタデータ（タブ一覧 + サイズ）を取得。

        実データは取得しない（高速、レート制限に優しい）。
        """
        service = self._ensure_service()
        start = time.perf_counter()
        resp = service.spreadsheets().get(spreadsheetId=sheet_id, includeGridData=False).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        props: dict[str, Any] = resp.get("properties", {}) or {}
        raw_sheets: list[dict[str, Any]] = resp.get("sheets", []) or []
        tabs = tuple(
            SheetTab(
                sheet_id=sheet_id,
                gid=int((s.get("properties") or {}).get("sheetId", 0)),
                title=str((s.get("properties") or {}).get("title", "")),
                row_count=int(
                    ((s.get("properties") or {}).get("gridProperties") or {}).get("rowCount", 0)
                ),
                col_count=int(
                    ((s.get("properties") or {}).get("gridProperties") or {}).get("columnCount", 0)
                ),
            )
            for s in raw_sheets
        )
        meta = SheetMetadata(sheet_id=sheet_id, title=str(props.get("title", "")), tabs=tabs)
        logger.info(
            "gsheets_get_metadata",
            request_id=request_id,
            sheet_id=sheet_id,
            tab_count=len(tabs),
            latency_ms=latency_ms,
        )
        return meta

    # -------------------------------------------------------
    # データ取得
    # -------------------------------------------------------
    def get_tab_rows(
        self,
        sheet_id: str,
        tab_name: str,
        request_id: str,
        *,
        range_a1: str | None = None,
        value_render_option: str = "FORMATTED_VALUE",
    ) -> TabRows:
        """指定タブの行データを取得する。

        range_a1 を渡すとセル範囲を絞れる（例: 'A1:Z1000'）。
        None なら tab 全体（'tab_name' だけ）。

        1 行目を headers として扱い、2 行目以降を rows として返す。
        """
        service = self._ensure_service()
        # range 構築: "'タブ名'!A1:Z1000" 形式（タブ名にスペース / 日本語 OK）
        full_range = f"'{tab_name}'"
        if range_a1:
            full_range = f"{full_range}!{range_a1}"

        start = time.perf_counter()
        resp = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=sheet_id,
                range=full_range,
                valueRenderOption=value_render_option,
            )
            .execute()
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        raw_values: list[list[Any]] = resp.get("values", []) or []
        if not raw_values:
            logger.info(
                "gsheets_get_tab_rows",
                request_id=request_id,
                sheet_id=sheet_id,
                tab_name=tab_name,
                returned=0,
                latency_ms=latency_ms,
            )
            return TabRows(sheet_id=sheet_id, tab_name=tab_name, headers=(), rows=(), row_count=0)

        headers = tuple(str(c) for c in raw_values[0])
        # 2 行目以降を rows として
        rows = tuple(tuple(str(c) if c is not None else "" for c in r) for r in raw_values[1:])
        logger.info(
            "gsheets_get_tab_rows",
            request_id=request_id,
            sheet_id=sheet_id,
            tab_name=tab_name,
            returned=len(rows),
            col_count=len(headers),
            latency_ms=latency_ms,
        )
        return TabRows(
            sheet_id=sheet_id,
            tab_name=tab_name,
            headers=headers,
            rows=rows,
            row_count=len(rows),
        )

    # -------------------------------------------------------
    # 内部
    # -------------------------------------------------------
    def _ensure_service(self) -> Any:
        if self._service is not None:
            return self._service
        from googleapiclient.discovery import build

        if self._credentials is None:
            self._credentials = self._build_credentials()
        self._service = build("sheets", "v4", credentials=self._credentials, cache_discovery=False)
        return self._service

    def _build_credentials(self) -> Any:
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_path:
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                sa_path, scopes=list(self._scopes)
            )
        refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            from google.oauth2.credentials import Credentials

            return Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(self._scopes),
            )
        raise NotImplementedError(
            "Google credentials が未設定です。Sprint 3 の S3-01 で OAuth クライアントを "
            "用意してから本実装。テストでは service=Fake service を渡してください。"
        )


# -----------------------------------------------------------
# ヘルパー: 1 行 → 1 document テキスト
# -----------------------------------------------------------
def format_row_as_document(
    headers: tuple[str, ...] | list[str],
    row: tuple[str, ...] | list[str],
    *,
    skip_empty: bool = True,
) -> str:
    """1 行を `column: value` 形式の document テキストに整形する。

    フォーム回答の場合、長い質問が column 名で、回答が value になる：
        タイムスタンプ: 2026-05-20 10:00
        業界: 飲食
        クライアント: ABC 株式会社
        提案結果: 受注
        温度感: 高
        自由記述: ...

    skip_empty=True なら空値の列は出さない（document の見やすさ重視）。
    """
    lines: list[str] = []
    for i, h in enumerate(headers):
        val = row[i] if i < len(row) else ""
        if skip_empty and not val.strip():
            continue
        lines.append(f"{h}: {val}")
    return "\n".join(lines)


def build_external_id(sheet_id: str, gid: int, row_idx: int) -> str:
    """documents.external_id を組み立てる（`<sheet_id>:<gid>:<row_idx>`）。

    row_idx は 1 始まり（spreadsheet と整合）、1 = headers なのでデータは 2 から。
    """
    return f"{sheet_id}:{gid}:{row_idx}"
