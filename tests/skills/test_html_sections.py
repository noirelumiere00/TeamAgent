"""固定フォーマット本文の分割（構造を拾うだけ・解釈は足さない）のテスト。"""

from __future__ import annotations

from teamagent.skills._html.sections import split_sections


def test_numbered_headings() -> None:
    assert split_sections("### 1. A\nあ\n### 2) B\nい") == [("A", "あ"), ("B", "い")]


def test_plain_headings() -> None:
    assert split_sections("## タイトル\n本文") == [("タイトル", "本文")]


def test_text_before_first_heading_is_dropped_from_sections() -> None:
    # 見出し前の前置きはセクションに属さない（呼び出し側が本文として扱う）。
    assert split_sections("前置き\n### 1. A\nあ") == [("A", "あ")]


def test_no_heading_returns_empty() -> None:
    assert split_sections("見出しのない本文") == []


def test_empty_input() -> None:
    assert split_sections("") == []
    assert split_sections("   ") == []


def test_body_keeps_inner_markdown() -> None:
    assert split_sections("### 1. A\n- 一つ\n- 二つ")[0][1] == "- 一つ\n- 二つ"
