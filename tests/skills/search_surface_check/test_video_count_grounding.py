"""2 段目の読みの「本数」の照合（B5 レビュー指摘の再現）。

本番と同じ形の入力（5 本すべて動画で分析済み）では、入力の値に「順位」1〜5 と分母 5 が必ず入る。
数字の照合だけだと、作り話の「5/5本」「5本中5本」「すべて」と、実際は 3 本なのに書いた「4/5本」が
通り、レポートに「照合済み」と出てしまう。本数は近くに書かれた項目の集計と照合する。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.search_surface_check.video_digest import (
    digest_grounder,
    digest_videos,
    ground_digest_conclusion,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo, VideoMeta, VideoVSEOAnalysis


def _video(rank: int, **analysis: Any) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(rank=rank, author=f"u{rank}", play_count=10000, collect_count=100 * rank),
        analysis=VideoVSEOAnalysis.model_validate(analysis),
    )


def _five() -> list[AnalyzedVideo]:
    """フックは問いかけ 1 本・冒頭テロップ 3 本・CTA 4 本（レビューの偽物と同じ形）。"""
    telop = [{"sec": 0.5, "text": "4つでいい"}]
    return [
        _video(1, hook_type="question", telops=telop, cta_type=["save"], duration_sec=30),
        _video(2, hook_type="number", telops=telop, cta_type=["save"], duration_sec=40),
        _video(3, hook_type="number", telops=telop, cta_type=["follow"], duration_sec=50),
        _video(4, hook_type="visual", cta_type=["save"], duration_sec=60),
        _video(5, hook_type="problem", duration_sec=70),
    ]


@pytest.fixture()
def grounder() -> Any:
    videos = _five()
    digest = digest_videos(videos, keyword="スパイスカレー", requested=5, reserved=5)
    assert (digest.watched, digest.opening_telop, digest.cta) == (5, 3, 4)
    return digest_grounder(digest, videos, keyword="スパイスカレー")


@pytest.mark.parametrize(
    "text",
    [
        "上位5本すべてが問いかけフックで冒頭テロップも5/5本",
        "5本中5本がCTAで保存を促す",
        "冒頭テロップは4/5本",
        "上位5本のうち5本が冒頭テロップ",
        "問いかけフックが3本",
        "冒頭テロップは6/5本",
        "CTAは3/4本",  # 分母が分析できた本数でない
    ],
)
def test_fabricated_counts_are_rejected(grounder: Any, text: str) -> None:
    why = grounder.reason(text)
    assert why is not None and "count:" in why


@pytest.mark.parametrize(
    "text",
    [
        "冒頭テロップは3/5本、CTAは4/5本",
        "上位5本のうち3本が冒頭テロップ",
        "5本中4本がCTAで保存を促す",
        "数字フックが2本、問いかけは1本",
        "保存を促すCTAが3本",
        "上位5本の動画はテンポが揃う",
        "保存率の高い2本に共通する",
    ],
)
def test_counts_that_match_the_digest_pass(grounder: Any, text: str) -> None:
    assert grounder.reason(text) is None


def test_all_claim_passes_only_when_the_near_item_covers_every_video() -> None:
    videos = _five()
    for v in videos:
        assert v.analysis is not None
        v.analysis.cta_type = ["save"]
    digest = digest_videos(videos, keyword="kw", requested=5, reserved=5)
    g = digest_grounder(digest, videos, keyword="kw")
    assert g.reason("CTAは5本すべてにある") is None
    assert g.reason("すべての動画が保存を促す") is None
    assert g.reason("冒頭テロップはすべてにある") is not None  # 冒頭テロップは 3 本


def test_the_conclusion_is_dropped_and_logged_without_the_body(grounder: Any) -> None:
    dropped: list[tuple[str, str]] = []
    raw = {
        "headline": "上位5本すべてが問いかけフック",
        "winning": {"text": "冒頭テロップは3/5本で共通", "ranks": [1, 2]},
        "save_reason": {"text": "5本中5本がCTAで保存を促す", "ranks": [1]},
    }
    c = ground_digest_conclusion(
        raw, grounder=grounder, on_drop=lambda f, r: dropped.append((f, r))
    )
    assert c is not None
    assert c.headline == ""
    assert c.winning is not None and c.save_reason is None
    assert [f for f, _ in dropped] == ["headline", "save_reason"]
    assert all("フック" not in r and "CTA" not in r for _, r in dropped)  # 本文は渡さない
