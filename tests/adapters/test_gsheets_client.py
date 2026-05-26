"""adapters/gsheets_client.py のユニットテスト。

FakeSheetsService で googleapiclient.discovery.build('sheets', 'v4') の戻りをモック。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.gsheets_client import (
    GSheetsClient,
    SheetMetadata,
    SheetTab,
    TabRows,
    build_external_id,
    format_row_as_document,
)


# -----------------------------------------------------------
# Fake Sheets Service
# -----------------------------------------------------------
class _FakeReq:
    def __init__(self, response: Any) -> None:
        self._r = response

    def execute(self) -> Any:
        return self._r


class _FakeValuesResource:
    def __init__(self, get_response: Any) -> None:
        self._get = get_response
        self.last_get_kwargs: dict[str, Any] = {}

    def get(self, **kwargs: Any) -> _FakeReq:
        self.last_get_kwargs = kwargs
        return _FakeReq(self._get)


class _FakeSpreadsheetsResource:
    def __init__(
        self,
        get_response: Any,
        values_get_response: Any,
    ) -> None:
        self._get = get_response
        self._values = _FakeValuesResource(values_get_response)
        self.last_get_kwargs: dict[str, Any] = {}

    def get(self, **kwargs: Any) -> _FakeReq:
        self.last_get_kwargs = kwargs
        return _FakeReq(self._get)

    def values(self) -> _FakeValuesResource:
        return self._values


class FakeSheetsService:
    def __init__(
        self,
        *,
        spreadsheets_get: Any | None = None,
        values_get: Any | None = None,
    ) -> None:
        self._ss = _FakeSpreadsheetsResource(
            spreadsheets_get or {"properties": {"title": ""}, "sheets": []},
            values_get or {"values": []},
        )

    def spreadsheets(self) -> _FakeSpreadsheetsResource:
        return self._ss


# -----------------------------------------------------------
# get_sheet_metadata
# -----------------------------------------------------------
def test_get_sheet_metadata_maps_tabs() -> None:
    fake = FakeSheetsService(
        spreadsheets_get={
            "properties": {"title": "ショート動画営業 FB"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 537831563,
                        "title": "フォーム回答 1",
                        "gridProperties": {"rowCount": 1000, "columnCount": 20},
                    }
                },
                {
                    "properties": {
                        "sheetId": 999,
                        "title": "集計",
                        "gridProperties": {"rowCount": 100, "columnCount": 10},
                    }
                },
            ],
        }
    )
    client = GSheetsClient(service=fake)
    meta = client.get_sheet_metadata(sheet_id="1VukC...", request_id="r")
    assert isinstance(meta, SheetMetadata)
    assert meta.title == "ショート動画営業 FB"
    assert len(meta.tabs) == 2
    assert isinstance(meta.tabs[0], SheetTab)
    assert meta.tabs[0].gid == 537831563
    assert meta.tabs[0].title == "フォーム回答 1"
    assert meta.tabs[0].row_count == 1000
    assert meta.tabs[1].title == "集計"


def test_get_sheet_metadata_handles_no_sheets() -> None:
    fake = FakeSheetsService(spreadsheets_get={"properties": {"title": "empty"}, "sheets": []})
    client = GSheetsClient(service=fake)
    meta = client.get_sheet_metadata(sheet_id="X", request_id="r")
    assert meta.tabs == ()


# -----------------------------------------------------------
# get_tab_rows
# -----------------------------------------------------------
def test_get_tab_rows_separates_headers_and_data() -> None:
    fake = FakeSheetsService(
        values_get={
            "values": [
                ["タイムスタンプ", "業界", "クライアント", "温度感"],
                ["2026-05-20 10:00", "飲食", "ABC", "高"],
                ["2026-05-21 14:00", "コスメ", "DEF", "中"],
            ]
        }
    )
    client = GSheetsClient(service=fake)
    tab = client.get_tab_rows(sheet_id="1VukC...", tab_name="フォーム回答 1", request_id="r")
    assert isinstance(tab, TabRows)
    assert tab.headers == ("タイムスタンプ", "業界", "クライアント", "温度感")
    assert tab.row_count == 2
    assert tab.rows[0] == ("2026-05-20 10:00", "飲食", "ABC", "高")
    assert tab.rows[1][1] == "コスメ"


def test_get_tab_rows_builds_range_with_quoted_tab_name() -> None:
    """日本語 + スペース含むタブ名でも 'name' で正しく escape される。"""
    fake = FakeSheetsService(values_get={"values": []})
    client = GSheetsClient(service=fake)
    client.get_tab_rows(
        sheet_id="X",
        tab_name="フォーム 回答 1",
        request_id="r",
        range_a1="A1:Z100",
    )
    kw = fake.spreadsheets().values().last_get_kwargs
    assert kw["range"] == "'フォーム 回答 1'!A1:Z100"


def test_get_tab_rows_handles_empty_response() -> None:
    fake = FakeSheetsService(values_get={"values": []})
    client = GSheetsClient(service=fake)
    tab = client.get_tab_rows(sheet_id="X", tab_name="t", request_id="r")
    assert tab.headers == ()
    assert tab.rows == ()
    assert tab.row_count == 0


def test_get_tab_rows_handles_missing_cells() -> None:
    """行の末尾セルが省略されていても壊れない（Sheets API は空セル省略する）。"""
    fake = FakeSheetsService(
        values_get={
            "values": [
                ["a", "b", "c"],
                ["1", "2"],  # c 列なし
                ["x"],  # b, c なし
            ]
        }
    )
    client = GSheetsClient(service=fake)
    tab = client.get_tab_rows(sheet_id="X", tab_name="t", request_id="r")
    assert tab.headers == ("a", "b", "c")
    assert tab.rows[0] == ("1", "2")
    assert tab.rows[1] == ("x",)


# -----------------------------------------------------------
# format_row_as_document
# -----------------------------------------------------------
def test_format_row_as_document_combines_header_and_value() -> None:
    headers = ("業界", "クライアント", "温度感")
    row = ("飲食", "ABC 株式会社", "高")
    doc = format_row_as_document(headers, row)
    assert "業界: 飲食" in doc
    assert "クライアント: ABC 株式会社" in doc
    assert "温度感: 高" in doc
    # 行数 = 3
    assert doc.count("\n") == 2


def test_format_row_as_document_skips_empty_by_default() -> None:
    headers = ("a", "b", "c")
    row = ("v1", "", "v3")
    doc = format_row_as_document(headers, row)
    assert "a: v1" in doc
    assert "b:" not in doc  # スキップ
    assert "c: v3" in doc


def test_format_row_as_document_can_include_empty() -> None:
    headers = ("a", "b")
    row = ("", "v")
    doc = format_row_as_document(headers, row, skip_empty=False)
    assert "a: " in doc
    assert "b: v" in doc


def test_format_row_as_document_handles_short_row() -> None:
    """row が headers より短い場合、足りない列は空扱い（skip）。"""
    headers = ("a", "b", "c")
    row = ("v1",)  # b, c なし
    doc = format_row_as_document(headers, row)
    assert "a: v1" in doc
    assert "b:" not in doc
    assert "c:" not in doc


# -----------------------------------------------------------
# build_external_id
# -----------------------------------------------------------
def test_build_external_id_format() -> None:
    assert build_external_id("1VukC", 537831563, 2) == "1VukC:537831563:2"
    assert build_external_id("X", 0, 1) == "X:0:1"


# -----------------------------------------------------------
# scopes / from_env
# -----------------------------------------------------------
def test_from_env_default_scope_is_readonly() -> None:
    client = GSheetsClient.from_env()
    assert "https://www.googleapis.com/auth/spreadsheets.readonly" in client._scopes


def test_from_env_write_scope_when_explicit() -> None:
    client = GSheetsClient.from_env(write=True)
    assert "https://www.googleapis.com/auth/spreadsheets" in client._scopes


def test_ensure_service_uses_injected_service() -> None:
    fake = FakeSheetsService()
    client = GSheetsClient(service=fake)
    assert client._ensure_service() is fake


def test_build_credentials_raises_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    client = GSheetsClient(service=None)
    with pytest.raises(NotImplementedError):
        client._build_credentials()
