"""資料の文字の共通処理（_deck.text）。お土産 FMT と共用。"""

from __future__ import annotations

from teamagent.skills._deck.text import (
    fit_text,
    organic_without_definition,
    strip_display_symbols,
    wording_problems,
)


def test_fit_text_cuts_at_sentence_end_without_ellipsis() -> None:
    text = "一文目です。二文目はもう少し長いです。三文目。"
    shown, cut = fit_text(text, 15)
    assert (shown, cut) == ("一文目です。", True)
    assert fit_text(text, 100) == (text, False)


def test_fit_text_falls_back_to_soft_break_then_hard_cut() -> None:
    shown, cut = fit_text("あいうえおかきくけこ、さしすせそたちつてと", 15)
    assert cut and shown == "あいうえおかきくけこ"  # 区切りの読点は残さない
    shown, cut = fit_text("あ" * 30, 10)
    assert cut and shown == "あ" * 10 and "…" not in shown


def test_wording_problems_ignore_quotes() -> None:
    assert wording_problems("前回の資料のとおり") == ["前の会を指す言い方「前回」"]
    assert wording_problems("投稿は「前回の動画で…」と話す") == []
    assert any("オーガニック" in p for p in wording_problems("オーガニック投稿が多い"))
    assert any("AI っぽい" in p for p in wording_problems("鍵となるのは保存率"))


def test_shared_helpers_keep_omiyage_behavior() -> None:
    assert strip_display_symbols("A✨B🦄C") == "ABC"
    assert organic_without_definition(["オーガニックが多い"]) is True
    assert organic_without_definition(["オーガニック", "#PR等の表記が確認できない投稿"]) is False
