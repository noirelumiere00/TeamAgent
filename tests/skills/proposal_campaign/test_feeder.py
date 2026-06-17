"""feeder の純粋関数テスト（ネット非依存）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from teamagent.adapters.tiktok_scraper import TikTokScrapeError, TikTokVideo
from teamagent.skills.proposal_campaign.feeder import (
    assign_placeholder_ids,
    build_evidence_images,
    extract_keywords_from_dr,
    fetch_one,
    resolve_keywords,
)
from teamagent.skills.proposal_deck.contract import EvidenceImage


def test_resolve_direct_keywords() -> None:
    kws = resolve_keywords(
        keywords=["a", "a", " b "], gemini_dr_json_path=None, composer_output_json_path=None
    )
    assert kws == ["a", "b"]


def test_extract_keywords_from_dr_dedupe() -> None:
    dr = {
        "D_publicity": [{"trend_word": "ご褒美デパ地下"}],
        "E_community": [{"tiktok_tags": ["#作業用BGM", "#深夜作業"]}],
        "C_tiktok": [{"tag": "#集中"}, {"tag": "作業用BGM"}],
    }
    assert extract_keywords_from_dr(dr) == ["ご褒美デパ地下", "作業用BGM", "深夜作業", "集中"]


def test_resolve_from_dr(tmp_path: Path) -> None:
    dr = {"D_publicity": [{"trend_word": "ご褒美"}], "E_community": [{"tiktok_tags": ["#BGM"]}]}
    p = tmp_path / "dr.json"
    p.write_text(json.dumps(dr), encoding="utf-8")
    kws = resolve_keywords(keywords=[], gemini_dr_json_path=str(p), composer_output_json_path=None)
    assert kws == ["ご褒美", "BGM"]


def test_resolve_from_composer(tmp_path: Path) -> None:
    data = {"placeholders": {"58": "メディアA", "60": "トレンドX", "72": "界隈Y"}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    kws = resolve_keywords(keywords=[], gemini_dr_json_path=None, composer_output_json_path=str(p))
    assert set(kws) == {"メディアA", "トレンドX", "界隈Y"}


def test_assign_placeholder_ids() -> None:
    assert assign_placeholder_ids(["a", "b"]) == [58, 60]
    assert assign_placeholder_ids(["a"]) == [58]


def test_fetch_one_success(
    tmp_path: Path, make_video: Callable[..., TikTokVideo], tiny_jpeg: bytes
) -> None:
    def searcher(query: str, n: int, rid: str) -> list[TikTokVideo]:
        return [make_video(cover_url="https://cdn.example/x.jpg")]

    result, ev = fetch_one(
        keyword="集中",
        placeholder_id=58,
        searcher=searcher,
        fetcher=lambda url, rid: tiny_jpeg,
        normalizer=lambda b: b,
        fallback_bytes=None,
        cache_dir=tmp_path,
        request_id="t",
    )
    assert result.success and result.source == "tiktok_1st"
    assert ev is not None and ev.placeholder_id == 58 and ev.keyword == "集中"
    assert ev.image_path is not None and Path(ev.image_path).read_bytes() == tiny_jpeg


def test_fetch_one_search_error_uses_fallback(tmp_path: Path, tiny_jpeg: bytes) -> None:
    def searcher(query: str, n: int, rid: str) -> list[TikTokVideo]:
        raise TikTokScrapeError("captcha")

    result, ev = fetch_one(
        keyword="集中",
        placeholder_id=58,
        searcher=searcher,
        fetcher=lambda url, rid: None,
        normalizer=lambda b: b,
        fallback_bytes=tiny_jpeg,
        cache_dir=tmp_path,
        request_id="t",
    )
    assert result.success and result.source == "fallback"
    assert ev is not None and ev.image_path is not None and Path(ev.image_path).exists()


def test_fetch_one_error_no_fallback(tmp_path: Path) -> None:
    def searcher(query: str, n: int, rid: str) -> list[TikTokVideo]:
        raise TikTokScrapeError("captcha")

    result, ev = fetch_one(
        keyword="集中",
        placeholder_id=58,
        searcher=searcher,
        fetcher=lambda url, rid: None,
        normalizer=lambda b: b,
        fallback_bytes=None,
        cache_dir=tmp_path,
        request_id="t",
    )
    assert not result.success and result.source == "error" and ev is None


def test_build_evidence_images() -> None:
    e1 = EvidenceImage(placeholder_id=58, rank=1, keyword="a", image_path="/p1")
    e2 = EvidenceImage(placeholder_id=60, rank=1, keyword="b", image_path="/p2")
    out = build_evidence_images([e1, e2])
    assert set(out) == {58, 60} and out[58][0].keyword == "a"
