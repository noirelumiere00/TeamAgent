"""深掘り検索（n_per_kw>30・お土産資料 便1）の media 契約境界。

実測（2026-08-24・「シャンプー」keyword・--max 120）: 120/120件・47秒・ブロックなし。
上限120は実測で到達済みの深度で、metadata_only の1KWジョブなら締切内に収まる。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from teamagent.media.contracts import (
    TikTokAcquireOperation,
    estimate_tiktok_operation_seconds,
    tiktok_search_timeout_seconds,
)


def test_deep_metadata_only_single_keyword_is_admissible() -> None:
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("シャンプー",),
        n_per_kw=120,
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )
    assert operation.n_per_kw == 120


def test_n_per_kw_above_measured_ceiling_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("シャンプー",),
            n_per_kw=121,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )


def test_deep_search_budget_scales_and_rejects_too_many_keywords() -> None:
    # 深掘りは1検索240s計上: 3KWまでは締切内、4KW以上は却下される
    assert (
        estimate_tiktok_operation_seconds(
            keyword_count=3,
            n_per_kw=120,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )
        == 720
    )
    with pytest.raises(ValidationError, match="immutable job deadline"):
        TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("a", "b", "c", "d"),
            n_per_kw=120,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )


def test_shallow_search_budget_is_unchanged() -> None:
    # 既存の浅い検索（n<=30）は従来どおり120s/KW＝7KW metadata_onlyが通る
    assert tiktok_search_timeout_seconds(30) == 120
    assert tiktok_search_timeout_seconds(31) == 240
    assert tiktok_search_timeout_seconds(120) == 240
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("a", "b", "c", "d", "e", "f", "g"),
        n_per_kw=30,
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )
    assert operation.artifact_mode == "metadata_only"
