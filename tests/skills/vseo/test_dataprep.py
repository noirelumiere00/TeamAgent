"""VSEO データ準備 (dataprep.py) の単体テスト。

tiktok_search の TikTokVideo から VSEO スキルが食う JSON 構造への変換を検証。
ブラウザ / ネットワーク不要 (合成 TikTokVideo を使う)。
"""

from __future__ import annotations

from teamagent.adapters.tiktok_scraper import TikTokAuthor, TikTokVideo
from teamagent.skills.vseo.dataprep import (
    KwResult,
    build_multi_kw,
    build_top10,
    compute_stats,
    stats_to_dict,
)

_NOW = 1_780_000_000  # 固定基準時刻 (Unix秒)


def _v(
    vid: str,
    *,
    author: str,
    plays: int,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    followers: int = 10000,
    created: int = _NOW,
    desc: str = "テスト動画の説明",
    cover: str = "",
) -> TikTokVideo:
    return TikTokVideo(
        id=vid,
        url=f"https://www.tiktok.com/@{author}/video/{vid}",
        desc=desc,
        create_time=created,
        duration=20,
        cover_url=cover,
        author=TikTokAuthor(unique_id=author, nickname=f"{author}店", follower_count=followers),
        play_count=plays,
        digg_count=likes,
        comment_count=comments,
        share_count=shares,
        collect_count=saves,
        hashtags=(),
        music_title="",
    )


def test_build_top10_structure() -> None:
    results = [
        KwResult(
            "新宿ランチ",
            [_v(str(i), author=f"acc{i}", plays=100000 - i, likes=1000) for i in range(15)],
        )
    ]
    top10 = build_top10(results, top_n=10)
    assert "新宿ランチ" in top10
    assert len(top10["新宿ランチ"]) == 10  # 15本中上位10
    e = top10["新宿ランチ"][0]
    assert e["rank"] == 1
    assert e["account_id"] == "acc0"
    assert e["account_url"] == "https://www.tiktok.com/@acc0"
    assert e["plays"] == 100000
    # eng_rate は % (engagement_rate × 100)
    assert e["eng_rate"] == 1.0  # likes1000/plays100000 = 1%


def test_multi_kw_detects_crossover() -> None:
    """複数KWで同一動画(同一URL)が出たら multi に入る。"""
    # 同じ動画 (vid=X, author=star) が3KWに登場
    star = _v("X", author="star", plays=500000, followers=47)
    r1 = KwResult("グルメ", [star, _v("a", author="a", plays=1000)])
    r2 = KwResult("ランチ", [_v("b", author="b", plays=2000), star])
    r3 = KwResult("ディナー", [star])
    multi = build_multi_kw([r1, r2, r3], top_n=30)

    assert len(multi) == 1  # star のみ複数KW
    m = multi[0]
    assert m["n_kws"] == 3
    assert m["account_id"] == "star"
    assert m["followers"] == 47  # フォロワー47人の3KW入賞 (方法論④)
    kws = {k for k, _ in m["kws"]}
    assert kws == {"グルメ", "ランチ", "ディナー"}


def test_multi_kw_single_kw_excluded() -> None:
    """1KWのみの動画は multi に入らない。"""
    r1 = KwResult("グルメ", [_v("only", author="o", plays=1000)])
    r2 = KwResult("ランチ", [_v("other", author="x", plays=2000)])
    assert build_multi_kw([r1, r2]) == []


def test_multi_kw_sorted_by_nkws_then_plays() -> None:
    s3 = _v("s3", author="s3", plays=100)  # 3KW・低再生
    s2 = _v("s2", author="s2", plays=999999)  # 2KW・高再生
    results = [
        KwResult("k1", [s3, s2]),
        KwResult("k2", [s3, s2]),
        KwResult("k3", [s3]),
    ]
    multi = build_multi_kw(results)
    # n_kws 降順が最優先 → 3KWのs3が先頭 (再生は低くても)
    assert multi[0]["account_id"] == "s3"
    assert multi[0]["n_kws"] == 3
    assert multi[1]["account_id"] == "s2"
    assert multi[1]["n_kws"] == 2


def test_compute_stats() -> None:
    results = [
        KwResult(
            "新宿ランチ",
            [
                _v("1", author="a", plays=200000, likes=4000, followers=39600),  # ENG2%
                _v("2", author="b", plays=600000, likes=6000, followers=100000),  # 外れ値(50万超)
                _v("3", author="c", plays=120000, likes=1500, followers=40000),  # breakout候補
                _v("4", author="d", plays=0, likes=0),  # 再生0 → 除外
            ],
        )
    ]
    stats = compute_stats(results, now_ts=_NOW)
    s = stats[0]
    assert s.n == 3  # 再生0を除く
    # breakout: フォロワー<5万 & 再生>=10万 → 動画1(F39600,20万)と動画3(F40000,12万)
    assert len(s.breakouts) == 2
    # 花王仮説: 10万再生 & ENG>=1% → 動画1(2%)・動画2(1%)・動画3(1.25%) = 3本
    assert s.kao_hypothesis_hits == 3
    # avg_plays_clean は 50万超(動画2)を除外
    assert s.avg_plays_clean == int((200000 + 120000) / 2)


def test_compute_stats_recent_count() -> None:
    """直近60日公開数を create_time で判定。"""
    old = _NOW - 100 * 86400  # 100日前 → 範囲外
    recent = _NOW - 10 * 86400  # 10日前 → 範囲内
    results = [
        KwResult(
            "k",
            [
                _v("1", author="a", plays=1000, created=old),
                _v("2", author="b", plays=2000, created=recent),
                _v("3", author="c", plays=3000, created=_NOW),
            ],
        )
    ]
    s = compute_stats(results, now_ts=_NOW, recent_days=60)[0]
    assert s.recent_count == 2  # recent と now の2本


def test_stats_to_dict() -> None:
    results = [KwResult("k", [_v("1", author="a", plays=1000)])]
    d = stats_to_dict(compute_stats(results, now_ts=_NOW))
    assert "k" in d
    assert d["k"]["n"] == 1
    assert "median_eng" in d["k"]
