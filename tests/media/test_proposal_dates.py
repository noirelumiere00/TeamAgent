"""Proposal schedule checks inspect body text without changing dates or layout."""

from datetime import UTC, date, datetime, tzinfo
from typing import Self

import pytest
from pptx import Presentation
from pptx.presentation import Presentation as PresentationType
from pptx.util import Inches

from teamagent.media import proposal_dates
from teamagent.media.proposal_dates import proposal_today, schedule_warnings


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
