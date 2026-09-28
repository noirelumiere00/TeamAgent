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
# 付け替えで照合結果が変わっていないことを、同じ文の集合で突き合わせる。
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
