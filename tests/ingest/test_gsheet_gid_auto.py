"""gsheets の ``gid: auto``（タブ名から実 gid を引く・2026-10-02 案件決定v2 の取り込み）。

固定すること:
- loader: ``gid: auto`` は印（GID_BY_TITLE）で、tab_name 必須・gid_env とは併用不可
- pipeline: 取り込み時にタブ名から実 gid を引き、external_id / リンクは実 gid で作る
- 引けないとき（タブ名が違う・メタ取得に失敗）はそのタブを取り込まない（推測の gid を使わない）
- 案件決定v2 のエントリが auto で読み込まれる
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gsheets_client import SheetMetadata, SheetTab, TabRows, build_external_id
from teamagent.ingest.loader import GID_BY_TITLE, GSheetSpec, GSheetsTabSpec, _parse_gsheet_tab

from .test_ingest_differential import _CountingEmbedder, _FakeDifferentialRepository

ROOT = Path(__file__).resolve().parents[2]
SHEET = "1UNicjhmAPYzlY4TBTgTn92dqRkVYPqAmYh1c3oxcsYU"
TAB = "フォーム形式の回答"


def test_loader_gid_auto() -> None:
    assert _parse_gsheet_tab({"gid": "auto", "tab_name": TAB}) == GSheetsTabSpec(
        gid=GID_BY_TITLE, tab_name=TAB
    )
    assert _parse_gsheet_tab({"gid": "auto"}) is None  # tab_name が無ければ引けない
    assert _parse_gsheet_tab({"gid": "auto", "tab_name": TAB, "gid_env": "X"}) is None


def test_anken_kettei_v2_entry_uses_gid_auto() -> None:
    from teamagent.ingest.loader import load_ingest_sources

    sources = load_ingest_sources(ROOT / "data" / "ingest_sources.yaml")
    spec = next(g for g in sources.gsheets if g.sheet_id == SHEET)
    assert spec.tabs == (GSheetsTabSpec(gid=GID_BY_TITLE, tab_name=TAB),)
    assert spec.row_unit is True


def _install(monkeypatch: pytest.MonkeyPatch, *, meta: Any) -> MagicMock:
    client = MagicMock()
    if isinstance(meta, Exception):
        client.get_sheet_metadata.side_effect = meta
    else:
        client.get_sheet_metadata.return_value = meta
    client.get_tab_rows.return_value = TabRows(
        sheet_id=SHEET,
        tab_name=TAB,
        headers=("撮影日", "クライアント名／商材名", "種別"),
        rows=(("2026-08-17", "ファミリーマート", "ビデオリリース"),),
        row_count=1,
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )
    monkeypatch.delenv("USE_DOC_CLASSIFY", raising=False)
    return client


def _run(repo: _FakeDifferentialRepository) -> None:
    from teamagent.ingest.pipeline import _ingest_gsheet

    _ingest_gsheet(
        GSheetSpec(
            sheet_id=SHEET,
            sheet_name="案件決定v2",
            description="",
            tabs=(GSheetsTabSpec(gid=GID_BY_TITLE, tab_name=TAB),),
            row_unit=True,
        ),
        embedder=_CountingEmbedder(),  # type: ignore[arg-type]
        repository=repo,  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=False,
        request_id="r",
    )


def _meta(title: str) -> SheetMetadata:
    return SheetMetadata(
        sheet_id=SHEET,
        title="案件決定v2",
        tabs=(SheetTab(sheet_id=SHEET, gid=1234567, title=title, row_count=1089, col_count=20),),
    )


def test_gid_auto_resolves_from_tab_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, meta=_meta(TAB))
    repo = _FakeDifferentialRepository()
    _run(repo)
    assert [c["external_id"] for c in repo.upsert_calls] == [
        build_external_id(SHEET, 1234567, 2)
    ]  # 行番号はシートの行（見出しの次＝2）
    assert "#gid=1234567" in repo.upsert_calls[0]["source_uri"]


@pytest.mark.parametrize(
    "meta", [_meta("別のタブ"), RuntimeError("sheets api down")], ids=["renamed", "meta_failed"]
)
def test_unresolved_gid_auto_skips_the_tab(
    monkeypatch: pytest.MonkeyPatch, meta: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client = _install(monkeypatch, meta=meta)
    repo = _FakeDifferentialRepository()
    with caplog.at_level(logging.ERROR):
        _run(repo)
    assert repo.upsert_calls == []  # 推測の gid（-1 や 0）で取り込まない
    client.get_tab_rows.assert_not_called()
