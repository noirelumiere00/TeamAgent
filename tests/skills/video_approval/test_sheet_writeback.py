"""sheet_writeback.py のテスト。

最重要: write_ai_check が **単一セルへの update だけ** を行い、既存データに触れない
（削除系を一切呼ばない）ことを、書込を記録するフェイクで検証する。
"""

from __future__ import annotations

import re

import pytest

from teamagent.adapters.gsheets_client import SheetMetadata, SheetTab, TabRows
from teamagent.skills.video_approval.schema import ApprovalIssue, VideoApprovalOutput
from teamagent.skills.video_approval.sheet_writeback import (
    DEFAULT_AI_HEADER,
    format_check_cell,
    format_check_slack,
    write_ai_check,
    write_ai_review,
)

_SINGLE_CELL = re.compile(r"^[A-Z]+[1-9][0-9]*$")

# 投稿管理: バナー2行 → ヘッダ → データ2行（管理番号 B 列）
_POSTING = [
    ["CL名", "", "", "伊藤園"],
    ["案件名", "", "", "発表会"],
    ["通し番号", "管理番号", "商材", "クリエイティブ名"],
    ["1", "E01-01", "O", "日本茶の未来"],
    ["2", "E01-02", "O", "純・新・進"],
]


def _out(verdict: str, issues: list[ApprovalIssue], summary: str = "") -> VideoApprovalOutput:
    return VideoApprovalOutput(
        verdict=verdict, summary=summary, issues=issues, feedback_text="本文"
    )


class _RecordingGS:
    """get_sheet_metadata / get_tab_rows / update_single_cell を模し、書込を記録。

    update_single_cell は本物と同じ単一セルガードを持たせ、範囲書込を物理的に拒否。
    削除系メソッド（clear/batchClear/delete）は **存在しない** ＝呼べばAttributeError。
    """

    def __init__(self, posting: list[list[str]]) -> None:
        self._posting = posting
        self.writes: list[tuple[str, str, str]] = []  # (tab, cell, value)

    def get_sheet_metadata(self, *, sheet_id: str, request_id: str) -> SheetMetadata:
        tab = SheetTab(
            sheet_id=sheet_id,
            gid=0,
            title="投稿管理シート_NTV管理用（92）",
            row_count=len(self._posting),
            col_count=4,
        )
        return SheetMetadata(sheet_id=sheet_id, title="伊藤園", tabs=(tab,))

    def get_tab_rows(
        self, *, sheet_id: str, tab_name: str, request_id: str, range_a1: str | None = None
    ) -> TabRows:
        headers = tuple(self._posting[0])
        body = tuple(tuple(r) for r in self._posting[1:])
        return TabRows(
            sheet_id=sheet_id, tab_name=tab_name, headers=headers, rows=body, row_count=len(body)
        )

    def update_single_cell(
        self, *, sheet_id: str, tab_name: str, cell: str, value: str, request_id: str
    ) -> None:
        if not _SINGLE_CELL.match(cell):
            raise ValueError(f"range write forbidden: {cell!r}")
        self.writes.append((tab_name, cell, value))


# -----------------------------------------------------------
# フォーマッタ
# -----------------------------------------------------------
def test_format_check_cell_has_verdict_issues_and_disclaimer() -> None:
    out = _out(
        "要修正",
        [
            ApprovalIssue(
                category="必須要素", severity="must_fix", timecode="0:05", detail="テロップ未確認"
            ),
            ApprovalIssue(category="NG事項", severity="suggestion", detail="BGM要検討"),
        ],
        summary="必須テロップ欠落",
    )
    text = format_check_cell(out)
    assert text.startswith("【AI一次】要修正")
    assert "必須テロップ欠落" in text
    assert "必須要素: テロップ未確認(0:05)" in text
    assert "人の確認" in text  # disclaimer


def test_format_check_cell_orders_must_fix_first_and_caps() -> None:
    issues = [
        ApprovalIssue(category="NG事項", severity="suggestion", detail=f"s{i}") for i in range(10)
    ]
    issues.append(ApprovalIssue(category="必須要素", severity="must_fix", detail="重大"))
    text = format_check_cell(_out("要修正", issues))
    lines = text.splitlines()
    # 最初の指摘行は must_fix（重大）
    first_issue = next(line for line in lines if line.startswith("・"))
    assert "重大" in first_issue
    assert "…他" in text  # 8件で打ち切り＋残数表記


def test_format_check_slack_marks_severity_and_links_video() -> None:
    out = _out(
        "要修正",
        [ApprovalIssue(category="NG事項", severity="must_fix", timecode="0:12", detail="薬機NG")],
    )
    text = format_check_slack(
        out, management_no="E01-01", creative_name="日本茶", video_url="https://drive/x"
    )
    assert "AI一次チェック" in text and "E01-01" in text
    assert "🔴 *NG事項*: 薬機NG (0:12)" in text
    assert "<https://drive/x|納品動画>" in text


# -----------------------------------------------------------
# write_ai_check（削除ゼロ保証）
# -----------------------------------------------------------
def test_write_ai_check_creates_column_and_writes_single_cells() -> None:
    gs = _RecordingGS(_POSTING)
    res = write_ai_check(
        gs,  # type: ignore[arg-type]
        sheet_id="sid",
        management_no="E01-01",
        cell_text="【AI一次】OK",
    )
    # 既存4列(A-D)の右隣 = E 列に新規作成
    assert res.created_column is True
    assert res.ai_column == "E"
    assert res.header_row == 3  # ヘッダは物理3行目
    assert res.data_row == 4  # E01-01 は物理4行目
    # 書込は2セルのみ（ヘッダ + 値）、どちらも単一セル、既存列(A-D)には触れない
    assert gs.writes == [
        ("投稿管理シート_NTV管理用（92）", "E3", DEFAULT_AI_HEADER),
        ("投稿管理シート_NTV管理用（92）", "E4", "【AI一次】OK"),
    ]
    for _tab, cell, _val in gs.writes:
        assert _SINGLE_CELL.match(cell)
        assert cell[0] == "E"  # 既存データ列ではない


def test_write_ai_check_reuses_existing_ai_column() -> None:
    posting = [list(r) for r in _POSTING]
    posting[2].append(DEFAULT_AI_HEADER)  # ヘッダ行に既に AI 列(E)がある
    gs = _RecordingGS(posting)
    res = write_ai_check(
        gs,  # type: ignore[arg-type]
        sheet_id="sid",
        management_no="E01-02",
        cell_text="要修正",
    )
    assert res.created_column is False
    assert res.ai_column == "E"
    assert res.data_row == 5  # E01-02 は物理5行目
    # ヘッダは書き直さず、値1セルのみ
    assert gs.writes == [("投稿管理シート_NTV管理用（92）", "E5", "要修正")]


def test_write_ai_check_unknown_management_no_raises() -> None:
    gs = _RecordingGS(_POSTING)
    with pytest.raises(ValueError, match="見つかりません"):
        write_ai_check(
            gs,  # type: ignore[arg-type]
            sheet_id="sid",
            management_no="ZZZ-99",
            cell_text="x",
        )
    assert gs.writes == []  # 行が無ければ一切書かない


# -----------------------------------------------------------
# write_ai_review: ユーザー指定の2列(AIチェック/AI FB内容)へ書く（2行ヘッダ対応）
# -----------------------------------------------------------
# 実シート再現: row0 バナー / row1 グループ見出し(Q-V) / row2 本ヘッダ / data。
# AIチェック=R(17), AI FB内容=S(18) はグループ見出し行(row1)にあり、管理番号=B(1) は本ヘッダ(row2)。
_GROUP = [""] * 16 + ["動画ステ", "AIチェック", "AI　FB内容"]  # idx16,17,18 = Q,R,S
_POSTING_RS = [
    ["CL名", "", "", "伊藤園"],  # row0 バナー
    [
        "案件名",
        *_GROUP[1:],
    ],  # row1 グループ見出し（先頭=案件名, 16-18=動画ステ/AIチェック/AI FB内容）
    [
        "通し番号",
        "管理番号",
        "商材",
        "クリエイティブ名",
        "投稿アカウント名",
        "種別",
    ],  # row2 本ヘッダ
    ["1", "E01-01", "O", "日本茶の未来", "★既存データ(消えない)", "TikTok"],  # row3 = E01-01
    ["2", "E01-02", "O", "純・新・進", "★既存2", "YouTube"],  # row4 = E01-02
]


def test_write_ai_review_writes_to_designated_two_columns() -> None:
    gs = _RecordingGS(_POSTING_RS)
    res = write_ai_review(
        gs,  # type: ignore[arg-type]
        sheet_id="sid",
        management_no="E01-01",
        verdict="要修正",
        fb_body="必須テロップ欠落。",
    )
    assert res.verdict_cell == "R4"  # R列(AIチェック) × 物理4行目
    assert res.fb_cell == "S4"  # S列(AI FB内容)
    # 書込は R4, S4 の2セルだけ。既存データ列(E=★既存)には触れない
    assert gs.writes == [
        ("投稿管理シート_NTV管理用（92）", "R4", "要修正"),
        ("投稿管理シート_NTV管理用（92）", "S4", "必須テロップ欠落。"),
    ]
    for _tab, cell, _val in gs.writes:
        assert _SINGLE_CELL.match(cell)


def test_write_ai_review_second_row_targets_correct_row() -> None:
    gs = _RecordingGS(_POSTING_RS)
    res = write_ai_review(
        gs,  # type: ignore[arg-type]
        sheet_id="sid",
        management_no="E01-02",
        verdict="OK",
        fb_body="問題なし。",
    )
    assert (res.verdict_cell, res.fb_cell) == ("R5", "S5")  # E01-02 は物理5行目


def test_write_ai_review_creates_columns_when_missing() -> None:
    """AI 列が無いシート（同構造の新シート）→ 右端に2列を新設して書く（削除ゼロ）。"""
    gs = _RecordingGS(_POSTING)  # 幅4列(A-D)、AIチェック/AI FB内容 は無い
    res = write_ai_review(
        gs,  # type: ignore[arg-type]
        sheet_id="sid",
        management_no="E01-01",
        verdict="要修正",
        fb_body="NG事項あり。",
    )
    # 既存4列(A-D)の外＝E,F を新設。ヘッダは本ヘッダ行(物理3)、値は E01-01 行(物理4)
    assert res.created_columns == ("E", "F")
    assert (res.verdict_cell, res.fb_cell) == ("E4", "F4")
    assert gs.writes == [
        ("投稿管理シート_NTV管理用（92）", "E3", "AIチェック"),
        ("投稿管理シート_NTV管理用（92）", "F3", "AI FB内容"),
        ("投稿管理シート_NTV管理用（92）", "E4", "要修正"),
        ("投稿管理シート_NTV管理用（92）", "F4", "NG事項あり。"),
    ]
    for _tab, cell, _val in gs.writes:
        assert _SINGLE_CELL.match(cell)


def test_write_ai_review_no_create_raises_when_missing() -> None:
    """create_if_missing=False なら列が無いとき ValueError（誤って別列に書かない）。"""
    gs = _RecordingGS(_POSTING)
    with pytest.raises(ValueError, match="AI 書込先の列"):
        write_ai_review(
            gs,  # type: ignore[arg-type]
            sheet_id="sid",
            management_no="E01-01",
            verdict="OK",
            fb_body="x",
            create_if_missing=False,
        )
    assert gs.writes == []
