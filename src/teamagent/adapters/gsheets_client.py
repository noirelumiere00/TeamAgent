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
import re
import time
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.adapters.google_auth import (
    build_oauth_credentials,
    force_oauth_enabled,
)

logger = structlog.get_logger(__name__)

# 単一セル A1（例 "AB5"）だけを許す。範囲（":"）や複数列を弾く＝既存データ削除防止の要。
_SINGLE_CELL_RE = re.compile(r"^[A-Z]+[1-9][0-9]*$")


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

    # --- 個人 OAuth で読む場合のスコープ ---------------------------------
    # 共有リフレッシュトークンは spreadsheets.* スコープを持たないため、
    # 代わりに drive.readonly で読む（Sheets API v4 は drive.readonly でも読取可）。
    # 詳細は adapters/google_auth.py を参照。
    SCOPES_OAUTH_READONLY: tuple[str, ...] = (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    )
    # 個人 OAuth で書く場合（AI一次チェック列への追記）。
    # spreadsheets スコープが refresh_token に含まれている必要がある
    # （scripts/get_google_refresh_token.py で再認証して付与）。
    SCOPES_OAUTH_WRITE: tuple[str, ...] = ("https://www.googleapis.com/auth/spreadsheets",)

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

    def update_single_cell(
        self,
        *,
        sheet_id: str,
        tab_name: str,
        cell: str,
        value: str,
        request_id: str,
    ) -> None:
        """**1 セルだけ**に値を書き込む（AI一次チェック列への追記用）。

        「既存データを絶対に削除しない」ための設計上の砦:
        - 受け付けるのは単一セル A1（例 ``"AB5"``）のみ。範囲指定（``A1:B2``）や
          複数列・複数行は ``ValueError`` で拒否する。万一バグっても 1 セルしか触れない。
        - 使う API は ``values.update`` だけ。``values.clear`` / ``batchClear`` /
          ``batchUpdate(deleteDimension/deleteRange)`` 等の**削除系は本クラスに実装しない**。
        - ``valueInputOption=RAW``: 文字列をそのまま格納（先頭 ``=`` を数式解釈させない＝
          数式インジェクション防止。他セルへ波及する関数を書かせない）。

        呼び出し側（sheet_writeback）は必ず「既存データの無い列」を選んで書くこと。
        """
        if not _SINGLE_CELL_RE.match(cell):
            raise ValueError(
                f"update_single_cell は単一セル A1 のみ許可（受領: {cell!r}）。"
                "範囲・複数列は既存データ削除防止のため禁止。"
            )
        service = self._ensure_service()
        full_range = f"'{tab_name}'!{cell}"
        start = time.perf_counter()
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=sheet_id,
                range=full_range,
                valueInputOption="RAW",
                body={"values": [[value]]},
            )
            .execute()
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "gsheets_update_single_cell",
            request_id=request_id,
            sheet_id=sheet_id,
            tab_name=tab_name,
            cell=cell,
            value_len=len(value),
            latency_ms=latency_ms,
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
        # 1. SA 鍵があり、かつ OAuth 強制でなければ Service Account
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_path and not force_oauth_enabled():
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                sa_path, scopes=list(self._scopes)
            )

        # 2. 個人 OAuth。
        #    - 読取: 共有トークンは spreadsheets.* を持たないため drive.readonly で読む
        #      （Sheets API v4 は drive.readonly でも読取可）。
        #    - 書込: spreadsheets スコープを要求（refresh_token に付与済みが前提。
        #      未付与なら invalid_scope。scripts/get_google_refresh_token.py で再認証）。
        oauth_scopes = (
            self.SCOPES_OAUTH_WRITE
            if self._scopes == self.SCOPES_WRITE
            else self.SCOPES_OAUTH_READONLY
        )
        creds = build_oauth_credentials(oauth_scopes)
        if creds is not None:
            return creds

        raise NotImplementedError(
            "Google credentials が未設定です。Service Account (GOOGLE_APPLICATION_CREDENTIALS) "
            "または OAuth (GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET) "
            "を設定してください。テストでは service=Fake service を渡してください。"
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
