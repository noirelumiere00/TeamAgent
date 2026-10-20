from __future__ import annotations

import pytest

from teamagent.skills._shared.voice_evidence import VoiceEvidence, aggregate_assignments


def evidence(key: str, text: str = "合成の投稿本文") -> VoiceEvidence:
    return VoiceEvidence(
        id=key,
        platform="tiktok",
        url="https://www.tiktok.com/@sample/video/1",
        text=text,
        fetched_at="2026-10-19T00:00:00+00:00",
        source="fake",
    )


def test_quotes_are_contiguous_raw_spans() -> None:
    item = evidence("a", "  合成の投稿本文。\n続き  ")
    assert item.quote(2, 9) == item.text[2:9]
    assert item.text.startswith("  ")
    with pytest.raises(ValueError):
        item.quote(-1, 999)


def test_counts_use_unique_known_ids_and_single_categories() -> None:
    items = [evidence("a"), evidence("b"), evidence("c")]
    groups, classified = aggregate_assignments(
        items,
        [
            {"category": "好意", "ids": ["a", "a", "missing"], "count": 900},
            {"category": "不満・障壁", "ids": ["a", "b"]},
            {"category": "架空分類", "ids": ["c"]},
        ],
    )
    assert groups == {"好意": ["a"], "不満・障壁": ["b"]}
    assert classified == 2


def test_platform_and_source_url_must_agree() -> None:
    with pytest.raises(ValueError):
        evidence("a").model_copy().model_validate(
            {
                **evidence("a").model_dump(),
                "url": "https://evil.example/tiktok.com",
            }
        )


def test_body_hash_and_timezone_are_checked_on_reload() -> None:
    item = evidence("hash-test")
    saved = item.model_dump()
    assert len(saved["text_sha256"]) == 64
    assert VoiceEvidence.model_validate(saved).text == item.text
    with pytest.raises(ValueError):
        VoiceEvidence.model_validate({**saved, "text": "原文を変更"})
    with pytest.raises(ValueError):
        VoiceEvidence.model_validate({**saved, "fetched_at": "unknown"})
