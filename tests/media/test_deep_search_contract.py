"""sales25 alone admits one metadata-only search up to 100 results."""

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
        edition="sales25",
        keywords=("synthetic",),
        n_per_kw=100,
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )
    assert operation.n_per_kw == 100


def test_n_per_kw_above_sales25_ceiling_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TikTokAcquireOperation(
            kind="tiktok_acquire",
            edition="sales25",
            keywords=("synthetic",),
            n_per_kw=101,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )


def test_deep_search_budget_scales_and_rejects_too_many_keywords() -> None:
    # The estimate scales, while initial sales25 admission allows one query per child.
    assert (
        estimate_tiktok_operation_seconds(
            keyword_count=3,
            n_per_kw=100,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )
        == 720
    )
    with pytest.raises(ValidationError, match="one keyword"):
        TikTokAcquireOperation(
            kind="tiktok_acquire",
            edition="sales25",
            keywords=("a", "b"),
            n_per_kw=100,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )


def test_shallow_search_budget_is_unchanged() -> None:
    # 既存の浅い検索（n<=30）は従来どおり120s/KW＝7KW metadata_onlyが通る
    assert tiktok_search_timeout_seconds(30) == 120
    assert tiktok_search_timeout_seconds(31) == 240
    assert tiktok_search_timeout_seconds(100) == 240
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("a", "b", "c", "d", "e", "f", "g"),
        n_per_kw=30,
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )
    assert operation.artifact_mode == "metadata_only"


@pytest.mark.parametrize("count", [31, 100])
def test_ordinary_search_stays_at_thirty(count: int) -> None:
    with pytest.raises(ValidationError, match="ordinary TikTok"):
        TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("synthetic",),
            n_per_kw=count,
            videos_per_kw=0,
            artifact_mode="metadata_only",
        )


def test_default_edition_preserves_off_wire_payload() -> None:
    implicit = TikTokAcquireOperation(kind="tiktok_acquire", keywords=("synthetic",))
    explicit = TikTokAcquireOperation(kind="tiktok_acquire", keywords=("synthetic",), edition=None)
    assert implicit.model_dump_json() == explicit.model_dump_json()
    assert set(implicit.model_dump(mode="json")) == {
        "kind",
        "search_type",
        "keywords",
        "n_per_kw",
        "videos_per_kw",
        "sort",
        "artifact_mode",
        "max_video_bytes",
        "client",
    }
