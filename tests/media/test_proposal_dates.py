"""Proposal schedule checks inspect body text without changing dates or layout."""

from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Self

import pytest
from pptx import Presentation
from pptx.presentation import Presentation as PresentationType
from pptx.util import Inches

from teamagent.media import proposal_dates
from teamagent.media.proposal_bu2 import BU2Plan
from teamagent.media.proposal_dates import proposal_today, schedule_warnings
from tests.skills.proposal_deck.test_bu2_shape import _pin_template, _render, _template


def _deck(*texts: str) -> PresentationType:
    presentation = Presentation()
    for text in texts:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text = text
    return presentation


def test_creation_date_uses_japanese_date_at_utc_midnight_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls(2026, 10, 21, 15, 5, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(proposal_dates, "datetime", FixedDateTime)
    assert proposal_today() == date(2026, 10, 22)


def test_same_phase_month_warns_with_one_based_page() -> None:
    presentation = _deck("表紙", "フェーズ１（2026年10月）／フェーズ２（2026年10月）")
    warnings = schedule_warnings(presentation, date(2026, 10, 22))
    assert len(warnings) == 1
    assert warnings[0][0] == 2
    assert "フェーズ1" in warnings[0][1] and "フェーズ2" in warnings[0][1]
    assert "2026年10月" in warnings[0][1]


def test_phase_range_overlap_inherits_year() -> None:
    presentation = _deck("フェーズ1（2026年10月〜11月）\nフェーズ2（11月〜12月）")
    assert "2026年11月" in schedule_warnings(presentation, date(2026, 9, 1))[0][1]


def test_separate_phases_and_year_crossing_do_not_warn() -> None:
    presentation = _deck("フェーズ1（2026年12月）\nフェーズ2（2027年1月）")
    assert schedule_warnings(presentation, date(2026, 10, 22)) == []


def test_phase_year_rolls_over_after_december() -> None:
    presentation = _deck("フェーズ1（2026年12月〜1月）\nフェーズ2（1月）")
    assert "2027年1月" in schedule_warnings(presentation, date(2026, 10, 22))[0][1]


def test_production_start_before_creation_warns_and_same_day_does_not() -> None:
    presentation = _deck("制作工程\n2026/10/01〜2026/11/01")
    warnings = schedule_warnings(presentation, date(2026, 10, 22))
    assert len(warnings) == 1 and warnings[0][0] == 1
    assert "2026-10-01" in warnings[0][1] and "2026-10-22" in warnings[0][1]
    assert schedule_warnings(presentation, date(2026, 10, 1)) == []


def test_production_axis_uses_posting_year_across_new_year() -> None:
    presentation = _deck("スケジュール：制作進行 → 投稿\n11/06\n12/04\n投稿開始 01/01")
    assert (
        schedule_warnings(presentation, date(2026, 11, 1), posting_start_date=date(2027, 1, 1))
        == []
    )
    warnings = schedule_warnings(
        presentation, date(2026, 11, 10), posting_start_date=date(2027, 1, 1)
    )
    assert "2026-11-06" in warnings[0][1]


def test_notes_and_non_schedule_production_mentions_are_ignored() -> None:
    presentation = _deck("制作事例 2020/01/01")
    presentation.slides[
        0
    ].notes_slide.notes_text_frame.text = "フェーズ1（2026年10月）／フェーズ2（2026年10月）"
    assert schedule_warnings(presentation, date(2026, 10, 22)) == []


@pytest.mark.parametrize("backend", ["local", "worker"])
@pytest.mark.parametrize("posting_date", [None, date(2027, 1, 10)])
def test_undated_plan_prose_preserves_evidence_and_requested_periods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    posting_date: date | None,
) -> None:
    template = tmp_path / "template.pptx"
    _template(template)
    prs = Presentation(str(template))
    slide = prs.slides[5]
    fixed = [
        "フェーズ1（2050年12月）",
        "制作工程\n2050/12/01～2050/12/31",
        "投稿開始 2050-12-31、実績: 2020/01/01に投稿開始",
        "出典: 2024年3月1日発行／提案日: 2026年10月22日",
        "過去の投稿実績: 2020/01/01／フェーズ2（2051年1月）",
        "投稿開始 2050/12/31。出典発行日 2024/03/01。",
        "利用者指定の期間: 2052/01/01～01/31",
        "投稿期間: 2052/01/01～01/31",
        "投稿開始 ２０５０／１２／３１／出典: 2024/03/01発行",
        "公開実績: 2020/01/01",
        "投稿開始 未定、出典URL https://example.com/2024/03/01",
        "出典: 2024/03/01発行\nフェーズ3（2051年1月）",
        "投稿開始\n2050/12/31\n実績: 2020/01/01に投稿開始",
        "投稿開始 2050/12/01、終了 2050/12/31",
    ]
    for index, text in enumerate(fixed):
        slide.shapes.add_textbox(0, Inches(index / 2), Inches(9), Inches(0.5)).text = text
    table = slide.shapes.add_table(2, 2, 0, 0, Inches(6), Inches(1)).table
    table.cell(0, 0).text = "制作スケジュール"
    table.cell(0, 1).text = "実績"
    table.cell(1, 0).text = "2050/12/01"
    table.cell(1, 1).text = "2020/01/01"
    prs.slides[6].shapes.add_textbox(0, 0, Inches(5), Inches(1)).text = "記録: 2023/02/01"
    prs.save(str(template))
    _pin_template(monkeypatch, template)
    plan = {"name": "Trial", "period": "2052/01/01～01/31", "assumption": "依頼文"}
    output = _render(
        tmp_path,
        backend,
        posting_start_date=posting_date,
        placeholders={1: "投稿開始 2050/12/31"},
        plans=[BU2Plan.model_validate(plan)] if backend == "local" else [plan],
    )
    generated = Presentation(str(output))
    texts = [node.text or "" for page in generated.slides for node in page._element.xpath(".//a:t")]
    combined = "\n".join(texts)
    if posting_date is None:
        assert "投稿開始 未定" in combined
        assert "フェーズ1（未定）" in combined
        assert "フェーズ2（未定）" in combined
        assert "未定～未定" in combined
        assert "2050" not in combined and "2051" not in combined
        assert "２０５０" not in combined
    else:
        assert "投稿開始 2050/12/31" in combined
        assert "フェーズ1（2050年12月）" in combined
    expected = fixed.copy()
    if posting_date is None:
        for index in (0, 1, 2, 4, 5, 8, 11, 12, 13):
            expected[index] = (
                expected[index]
                .replace("2050年12月", "未定")
                .replace("2051年1月", "未定")
                .replace("2050-12-31", "未定")
                .replace("2050/12/01", "未定")
                .replace("2050/12/31", "未定")
                .replace("２０５０／１２／３１", "未定")
            )
    shapes = [shape for shape in generated.slides[5].shapes if shape.has_text_frame]
    assert [shape.text for shape in shapes][-len(fixed) :] == expected
    rendered_table = next(shape.table for shape in generated.slides[5].shapes if shape.has_table)
    assert rendered_table.cell(1, 0).text == ("未定" if posting_date is None else "2050/12/01")
    assert rendered_table.cell(1, 1).text == "2020/01/01"
    assert "記録: 2023/02/01" in combined
    assert combined.count("2052/01/01～01/31") == 3


@pytest.mark.parametrize("backend", ["local", "worker"])
def test_undated_cleanup_handles_raw_newline_inside_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    # 本番の事例一覧・PRワード候補は1つの run に生の改行を持つ（10-30 本番の30・33枚目）。
    template = tmp_path / "template.pptx"
    _template(template)
    prs = Presentation(str(template))
    texts = ["1. 事例\n投稿開始 2050/12/31", "出典: 社内RAG\n概要のみ", "動画投稿：イメージ（1/2）"]
    for index, text in enumerate(texts):
        box = prs.slides[5].shapes.add_textbox(0, Inches(index), Inches(9), Inches(0.5))
        box.text_frame.paragraphs[0].add_run().text = text
    prs.save(str(template))
    _pin_template(monkeypatch, template)
    output = _render(tmp_path, backend, posting_start_date=None, placeholders={1: "本文"})
    texts = [
        shape.text for shape in Presentation(str(output)).slides[5].shapes if shape.has_text_frame
    ]
    assert "1. 事例\v投稿開始 未定" in texts
    assert "出典: 社内RAG\n概要のみ" in texts
    assert "動画投稿：イメージ（1/2）" in texts  # ページ番号は日付扱いしない（10-30 本番47枚目）
