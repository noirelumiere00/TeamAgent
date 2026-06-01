"""VSEO データ準備パイプライン (prepare.py) の単体テスト。

search_tiktok とサムネ DL をモックし、5KW → JSON 群生成の配線を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokScrapeError,
    TikTokSearchResult,
    TikTokVideo,
)
from teamagent.skills.vseo import prepare as prepare_mod
from teamagent.skills.vseo.prepare import prepare_vseo_data

_NOW = 1_780_000_000


def _v(vid: str, author: str, plays: int) -> TikTokVideo:
    return TikTokVideo(
        id=vid,
        url=f"https://www.tiktok.com/@{author}/video/{vid}",
        desc=f"{author}の動画",
        create_time=_NOW,
        duration=20,
        cover_url=f"https://cdn/{vid}.jpg",
        author=TikTokAuthor(unique_id=author, nickname=f"{author}店", follower_count=1000),
        play_count=plays,
        digg_count=plays // 100,
        comment_count=0,
        share_count=0,
        collect_count=0,
        hashtags=(),
        music_title="",
    )


def _fake_searcher_factory(per_kw: dict[str, list[TikTokVideo]]):
    def _search(query, *, search_type="keyword", max_videos=30, request_id=None):  # type: ignore[no-untyped-def]
        vids = per_kw.get(query, [])
        return TikTokSearchResult(query=query, search_type="keyword", videos=tuple(vids))

    return _search


def test_prepare_writes_all_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # サムネ DL はネットワークを使うのでモック
    monkeypatch.setattr(prepare_mod, "download_covers", lambda *a, **k: {})

    star = _v("star", "star", 500000)  # 2KW にまたがる動画
    per_kw = {
        "新宿 ランチ": [star, _v("a", "a", 1000)],
        "新宿 グルメ": [_v("b", "b", 2000), star],
    }
    result = prepare_vseo_data(
        ["新宿 ランチ", "新宿 グルメ"],
        tmp_path,
        now_ts=_NOW,
        searcher=_fake_searcher_factory(per_kw),
        download_thumbnails=False,
    )

    # 3 つの JSON が書かれる
    assert (tmp_path / "top10_with_urls.json").exists()
    assert (tmp_path / "multi_kw_videos.json").exists()
    assert (tmp_path / "kw_stats.json").exists()
    assert (tmp_path / "_meta.json").exists()

    # top10: 2KW 分
    top10 = json.loads((tmp_path / "top10_with_urls.json").read_text())
    assert set(top10.keys()) == {"新宿 ランチ", "新宿 グルメ"}

    # multi: star が 2KW 入賞
    multi = json.loads((tmp_path / "multi_kw_videos.json").read_text())
    assert len(multi) == 1
    assert multi[0]["account_id"] == "star"
    assert multi[0]["n_kws"] == 2

    assert result.multi_kw_count == 1
    assert result.counts == {"新宿 ランチ": 2, "新宿 グルメ": 2}


def test_prepare_partial_failure_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一部KWが失敗しても成功分で JSON を生成する。"""
    monkeypatch.setattr(prepare_mod, "download_covers", lambda *a, **k: {})

    def _search(query, *, search_type="keyword", max_videos=30, request_id=None):  # type: ignore[no-untyped-def]
        if query == "失敗KW":
            raise TikTokScrapeError("TIKTOK_EMPTY_RESULT: captcha")
        return TikTokSearchResult(query=query, search_type="keyword", videos=(_v("x", "x", 1000),))

    result = prepare_vseo_data(
        ["成功KW", "失敗KW"],
        tmp_path,
        now_ts=_NOW,
        searcher=_search,
        download_thumbnails=False,
    )
    assert result.failed_keywords == ["失敗KW"]
    assert "成功KW" in result.counts
    assert (tmp_path / "top10_with_urls.json").exists()


def test_prepare_all_fail_raises(tmp_path: Path) -> None:
    """全KW失敗なら例外。"""

    def _search(query, **k):  # type: ignore[no-untyped-def]
        raise TikTokScrapeError("TIKTOK_EMPTY_RESULT")

    with pytest.raises(TikTokScrapeError):
        prepare_vseo_data(["a", "b"], tmp_path, now_ts=_NOW, searcher=_search)


def test_prepare_calls_cover_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_thumbnails=True ならサムネ DL を呼ぶ。"""
    called = {}

    def _fake_dl(entries, out_dir, **k):  # type: ignore[no-untyped-def]
        called["count"] = called.get("count", 0) + 1
        return {1: out_dir / "rank01.jpeg"}

    monkeypatch.setattr(prepare_mod, "download_covers", _fake_dl)

    per_kw = {"k1": [_v("a", "a", 1000)], "k2": [_v("b", "b", 2000)]}
    result = prepare_vseo_data(
        ["k1", "k2"],
        tmp_path,
        now_ts=_NOW,
        searcher=_fake_searcher_factory(per_kw),
        download_thumbnails=True,
    )
    assert called["count"] == 2  # 2KW 分呼ばれる
    assert result.covers_saved == 2
