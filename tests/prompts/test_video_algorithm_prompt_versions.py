"""video_algorithm の v1 / v2 / v3 プロンプトの関係を固定する。

- v2/system.md（1 本ずつの動画分析）は v1 の複製で、H1 の版表記だけが違う。本番既定は v2、
  env VIDEO_ALGO_PROMPT_VERSION=v1 で戻せるので、片方だけ直すと本番と戻し先で中身がずれる。
  わざと変えるときは、このテストを直して差分を明示すること。
- v2/synthesis.md が「ρ を書いた文はコードで削除される」と約束する欄が、コード
  （synthesis.ground_synthesis で deny=RHO_TERMS を通す欄）と一致すること。
- v3/system.md は v2/system.md と同一（この PR では 1 本ずつの分析プロンプトを変えない。
  env VIDEO_ALGO_PROMPT_VERSION=v3 を選んでも Gemini への入力は v2 と同じ）。v3 で変えるのは
  統合（synthesis.md・既定の版は synthesis.SYNTHESIS_VERSION）だけ。
- v3/synthesis.md は規則 R1〜R17 をすべて持ち、出力の JSON に仕様 §3-2 の欄が全部あること
  （R17 と cover_directives はサムネ＝一覧の表紙の指示）。
  例文に数字を入れない（例文どおりに書いた作り話の数字が照合を通らないため・仕様 §2-5）。
"""

from __future__ import annotations

import re
from pathlib import Path

import teamagent.prompts

_DIR = Path(teamagent.prompts.__file__).parent / "video_algorithm"


def _read(version: str, name: str) -> list[str]:
    return (_DIR / version / f"{name}.md").read_text(encoding="utf-8").splitlines()


def test_v2_system_is_a_copy_of_v1_except_the_title() -> None:
    v1, v2 = _read("v1", "system"), _read("v2", "system")
    assert v1[0].endswith("system prompt v1")
    assert v2[0].endswith("system prompt v2")
    assert v1[0].removesuffix("v1") == v2[0].removesuffix("v2")
    assert v2[1:] == v1[1:]


def test_v2_synthesis_lists_every_field_whose_rho_sentences_are_removed() -> None:
    text = "\n".join(_read("v2", "synthesis"))
    m = re.search(r"（([^（）]*)に ρ を書いた文はコードで削除される）", text)
    assert m is not None
    listed = set(re.findall(r"`(\w+)`", m.group(1)))
    # synthesis.py: headline・creative_brief は項目単位、残りは文単位で ρ を捨てる
    assert listed == {
        "headline",
        "strategy",
        "creative_brief",
        "posting_design",
        "client_pitch",
        "shared_funnel",
    }


def test_v3_system_is_identical_to_v2() -> None:
    assert _read("v3", "system") == _read("v2", "system")


def test_v3_synthesis_has_every_rule_and_field() -> None:
    text = "\n".join(_read("v3", "synthesis"))
    assert text.splitlines()[0].endswith("system prompt v3")
    rules = re.findall(r"^- (R\d+): ", text, re.MULTILINE)
    assert rules == [f"R{i}" for i in range(1, 18)]
    fence = text[text.index("```json") : text.index("```", text.index("```json") + 3)]
    for key in (
        "summary_lines",
        "type_line",
        "feature_ids",
        "best_reason",
        "client_move",
        "per_video",
        "win_line",
        "why_fact",
        "why_guess",
        "steal",
        "not_to_copy",
        "directives",
        "avoid",
        "storyboards",
        "basis_ranks",
        "cuts",
        "board_angles",
        "match_terms",
        "hypotheses",
        "stat_feature",
        "posting",
        "caption_plan",
        "ab_plan",
        "refs",
        "quote",
        "cover_directives",
        "on",
    ):
        assert f'"{key}"' in fence, key
    # コードだけが書く欄は、LLM に書かせない（出力例に出さない）
    for key in ("tier", "ranks", "tag", "found_sec", "start_sec", "basis_note"):
        assert f'"{key}"' not in fence, key


def test_v3_synthesis_examples_have_no_digits() -> None:
    """例文（数字を入れない）と JSON の出力例に、作り話の数字が無いこと（字数の上限は除く）。"""
    text = "\n".join(_read("v3", "synthesis"))
    example = next(line for line in text.splitlines() if line.startswith("- 例（別ジャンル"))
    assert not re.search(r"\d", example)
    fence = text[text.index("```json") : text.index("```", text.index("```json") + 3)]
    assert not re.search(r"\d", re.sub(r"\d+字以内", "", fence))
