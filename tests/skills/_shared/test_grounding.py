"""_shared/grounding.py（入力に無い数字を捨てる照合）の単体テスト。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import pytest

from teamagent.skills._shared.grounding import (
    ALWAYS_ALLOWED,
    RHO_TERMS,
    DropLedger,
    NumberGrounder,
    extract_numbers,
    grounding_mode,
    tone_down,
)

# 付け替え前の search_surface_check/conclusion.py:54-61 の _numbers を写したもの。
# 付け替えで照合結果が変わっていないことを、同じ文の集合で突き合わせる（読み方の改良 4 点
# ＝先頭ゼロ・画面比・タイムコード・万/億に当たらない文。当たる文の変化は
# tests/skills/search_surface_check/test_grounding_impact.py が固定する）。
_LEGACY_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _legacy_numbers(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).replace(",", "")
    out: set[str] = set()
    for raw in _LEGACY_NUM_RE.findall(normalized):
        out.add(raw)
        if "." in raw:
            out.add(raw.rstrip("0").rstrip("."))
    return out


_LEGACY_INPUT = (
    '{"上位の本数": 15, "投稿者タイプ": [{"本数": 7, "再生の割合%": 68}],'
    ' "保存率の中央値%": 1.4, "尺の中央値（秒）": 32.5, "再生の中央値": "8,200"}'
)
_LEGACY_OUTPUTS = (
    "クリエイター7本で再生の68%",
    "再生の83%を占める",
    "尺は32.5秒前後",
    "保存率1.4%が中央値",
    "８，２００回の再生",
    "37本の投稿で検証する",
    "上位15本のうち2つ",
    "90日以内に100本",
    "1.45%と2.7倍",
)


@pytest.mark.parametrize("text", _LEGACY_OUTPUTS)
def test_stray_matches_the_legacy_surface_check(text: str) -> None:
    """付け替え前の式（_numbers(text) - allowed - _ALWAYS_ALLOWED）と同じ結果になる。"""
    allowed = _legacy_numbers(_LEGACY_INPUT)
    legacy = _legacy_numbers(text) - allowed - ALWAYS_ALLOWED
    grounder = NumberGrounder(allowed=frozenset(allowed))
    assert grounder.stray(text) == legacy


def test_extract_numbers_normalizes_fullwidth_commas_and_trailing_zero() -> None:
    assert extract_numbers("再生の６８％・1,234本・2.50倍") == {"68", "1234", "2.50", "2.5"}


def test_from_inputs_collects_numbers_of_every_text() -> None:
    g = NumberGrounder.from_inputs("尺20秒", "保存率1.2%")
    assert g.ok("尺は20秒で保存率1.2%")
    assert g.stray("尺は25秒") == {"25"}


def test_small_numbers_pass_unless_strict_suffix() -> None:
    loose = NumberGrounder.from_inputs("x")
    assert loose.ok("3カットで2つ")
    assert loose.ok("保存率が3倍")
    strict = NumberGrounder.from_inputs("x", strict_suffixes=frozenset({"倍", "%", "万"}))
    assert strict.ok("3カットで2つ")
    assert strict.stray("保存率が3倍") == {"3"}
    assert strict.stray("保存率5%") == {"5"}


def test_keep_sentences_drops_only_the_bad_sentence() -> None:
    g = NumberGrounder.from_inputs("尺20秒")
    kept, dropped = g.keep_sentences("尺は20秒に収める。再生の71%が保存する。KWを冒頭に置く。")
    assert kept == "尺は20秒に収める。KWを冒頭に置く。"
    assert dropped == ["number:71"]


def test_keep_sentences_denies_terms() -> None:
    g = NumberGrounder.from_inputs("ρ-0.90")
    kept, dropped = g.keep_sentences("尺を伸ばす〔ρ=-0.90〕。KWを冒頭に置く。", deny=RHO_TERMS)
    assert kept == "KWを冒頭に置く。"
    assert dropped == ["deny:ρ"]


def test_rank_refs_must_exist() -> None:
    g = NumberGrounder.from_inputs("#1 #2 #3", valid_ranks={1, 2, 3})
    assert g.rank_refs_ok("#1と#3で観測")
    assert g.bad_rank_refs("#9で観測（rank7も）") == {7, 9}
    assert g.bad_rank_refs("8位の動画") == {8}
    assert g.rank_refs_ok("10位以内の常連")  # 範囲の言い方は順位の参照ではない
    assert g.reason("#9で観測") == "rank:9"


@pytest.mark.parametrize(
    "text",
    [
        "#100均 のタグを添える",  # 数字で始まるハッシュタグ
        "#30代ランチ を付ける",
        "#2025新宿 で投稿する",
        "#3coins の雑貨を映す",
        "#１００均グッズ",  # 全角
        "上位10位に入る",  # 範囲の言い方
        "トップ10位を狙う",
        "10位以内の常連",
    ],
)
def test_hashtags_and_ranges_are_not_rank_refs(text: str) -> None:
    g = NumberGrounder.from_inputs("", valid_ranks={1, 2, 3})
    assert g.bad_rank_refs(text) == set(), text


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("#9の冒頭", {9}),  # 助詞が続く「#N」は順位
        ("#8と#9", {8, 9}),
        ("#7。", {7}),
        ("rank12で観測", {12}),
        ("上位は#1〜#5", {5}),
        ("4位の動画", {4}),
    ],
)
def test_real_rank_refs_are_still_checked(text: str, bad: set[int]) -> None:
    g = NumberGrounder.from_inputs("", valid_ranks={1, 2, 3})
    assert g.bad_rank_refs(text) == bad, text


def test_rank_refs_are_not_checked_without_valid_ranks() -> None:
    assert NumberGrounder.from_inputs("#9").rank_refs_ok("#99")


def test_filter_ranks_keeps_existing_unique_in_order() -> None:
    g = NumberGrounder.from_inputs("", valid_ranks={1, 2, 4})
    assert g.filter_ranks([4, 9, 1, 1, True, "2", 2]) == [4, 1, 2]
    assert g.filter_ranks("1,2") == []
    assert g.filter_ranks(None) == []


def test_tone_down_is_shared() -> None:
    assert tone_down("検索面を支配") == "検索面の中心"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("enforce", "enforce"), ("SHADOW", "shadow"), ("", "shadow"), ("off", "shadow")],
)
def test_grounding_mode_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", value)
    assert grounding_mode("video_algorithm") == expected


def test_grounding_mode_default_can_be_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROUNDING_MODE_X", raising=False)
    assert grounding_mode("x", default="enforce") == "enforce"


def test_drop_ledger_logs_field_and_reason_without_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills._shared import grounding

    events: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(grounding, "logger", _Log())
    seen: list[tuple[str, str]] = []
    ledger = DropLedger(
        skill="video_algorithm",
        mode="shadow",
        request_id="r1",
        sink=lambda f, r: seen.append((f, r)),
    )
    ledger("strategy", "number:71")
    assert ledger.count == 1 and not ledger.enforce
    assert seen == [("strategy", "number:71")]
    assert events == [
        (
            "grounding_dropped",
            {
                "skill": "video_algorithm",
                "field": "strategy",
                "reason": "number:71",
                "mode": "shadow",
                "request_id": "r1",
            },
        )
    ]


# ── 数字の読み方の改良（先頭ゼロ・画面比・タイムコード・万/億・丸め）─────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("冒頭0:05", {"5"}),
        ("1:30で締める", {"90"}),
        ("縦型9:16と16:9", set()),
        ("投稿は18:00", {"18", "00", "0"}),  # 時刻はタイムコードとして読まない
        ("1.2万回", {"12000"}),
        ("3億", {"300000000"}),
        ("05本", {"05", "5"}),
    ],
)
def test_extract_numbers_reads_units(text: str, expected: set[str]) -> None:
    assert extract_numbers(text) == expected


def test_man_unit_does_not_collide_with_percent() -> None:
    g = NumberGrounder.from_inputs("保存率1.2%")
    assert g.stray("検索量1.2万") == {"12000"}
    assert g.ok("保存率1.2%")


def test_rounding_allows_rounded_input_values_only() -> None:
    """実物（新宿 20260617）の「範囲5.2–20.3」→「5秒から20秒」を通す。作った値は通さない。"""
    g = NumberGrounder.from_inputs("範囲5.2–20.3・保存率2.35%・検索量12,345", rounding=True)
    assert g.ok("5秒から20秒に収める")
    assert g.ok("21秒以内")  # 20.3 の切り上げ
    assert g.ok("保存率2.4%前後")  # 小数 1 桁の四捨五入
    assert g.ok("月間約1.2万回の検索")  # 12,345 の万単位の丸め
    assert g.stray("尺は17秒") == {"17"}
    assert g.stray("保存率2.9%") == {"2.9"}
    strict = NumberGrounder.from_inputs("範囲5.2–20.3")
    assert strict.stray("5秒から20秒") == {"20"}  # 丸めを許さない既定（検索上位チェック）
