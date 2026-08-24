"""決定論集計エンジンの正しさ（ローカルSkill指標定義v2の便1範囲と同一定義）。

fixture は実KOLエクスポート相当のデータ形（caption内タグ・hashtags列の
エクスポーター重複・表記ゆれ・全角/半角）で組む。
"""

from __future__ import annotations

from teamagent.skills.omiyage_report.metrics import (
    AxisData,
    PostRecord,
    caption_route_hits,
    count_occurrences,
    has_pr_tag,
    hashtag_route_hits,
    keyword_variants,
    measure,
    measure_brand_exposure,
    measure_keyword_axis,
    measure_official_exposure,
    measure_poster_breakdown,
    measure_pr_comparison,
    normalize_handle,
)


def _post(
    video_id: str,
    caption: str,
    hashtags: tuple[str, ...] = (),
    *,
    author: str = "someone",
    rank: int = 1,
) -> PostRecord:
    return PostRecord(
        video_id=video_id,
        url=f"https://www.tiktok.com/@{author}/video/{video_id}",
        author=author,
        caption=caption,
        hashtags=hashtags,
        rank=rank,
    )


# ---------------------------------------------------------------------------
# 照合規則（表記ゆれ・境界・OCR風スペース）
# ---------------------------------------------------------------------------


def test_matching_folds_width_case_and_kana() -> None:
    variants = keyword_variants(["シャンプー"])
    # カタカナ→ひらがな統合・全角/半角折り畳みで一致する
    assert count_occurrences("新作しゃんぷーの紹介", variants) == 1
    # CJK文字間の空白（OCR風）は除去して照合する
    assert count_occurrences("シャン プー が人気", variants) == 1
    assert count_occurrences("無関係の本文", variants) == 0


def test_latin_alnum_boundary_excludes_glued_tokens() -> None:
    variants = keyword_variants(["ABC"])
    assert count_occurrences("EasyABC を使ってみた", variants) == 0
    assert count_occurrences("ABCの新商品", variants) == 1


def test_overlapping_variants_prefer_longest_and_do_not_double_count() -> None:
    variants = keyword_variants(["ヒアルロン酸", "ヒアルロン酸配合"])
    # 「ヒアルロン酸配合」1回を2変種で二重加算しない
    assert count_occurrences("ヒアルロン酸配合の美容液", variants) == 1
    # 重ならない2出現はどちらも数える
    assert count_occurrences("ヒアルロン酸配合、ヒアルロン酸たっぷり", variants) == 2


# ---------------------------------------------------------------------------
# caption / hashtag 2経路
# ---------------------------------------------------------------------------


def test_caption_route_excludes_hashtags_and_hashtag_route_dedupes_exporter() -> None:
    variants = keyword_variants(["ヘアケア"])
    caption = "毎日のヘアケア習慣 #ヘアケア おすすめ"
    # caption経路はタグを除いた本文のみ（本文1回）
    assert caption_route_hits(caption, variants) == 1
    # hashtags列に同じ #ヘアケア（エクスポーター重複）→ 1回だけ。別タグは加算。
    hits = hashtag_route_hits(caption, ("#ヘアケア", "#ヘアケアグッズ"), variants)
    assert hits == 2  # caption内タグ1 + ヘアケアグッズ内の一致1（重複タグは数えない）


def test_hashtag_typed_twice_in_caption_counts_twice() -> None:
    variants = keyword_variants(["ヘアケア"])
    caption = "#ヘアケア 朝と夜 #ヘアケア"
    assert hashtag_route_hits(caption, ("#ヘアケア",), variants) == 2


def test_denominator_includes_zero_hit_posts() -> None:
    variants = keyword_variants(["シャンプー"])
    posts = [
        _post("1", "シャンプーの選び方 #シャンプー"),
        _post("2", "全く無関係な動画"),
        _post("3", "無関係その2"),
        _post("4", "しゃんぷー比較"),
    ]
    metrics = measure_keyword_axis(posts, variants)
    assert metrics.denominator == 4  # 0回の投稿も分母に含む
    assert metrics.caption.videos_with == 2
    assert metrics.hashtag.videos_with == 1
    assert metrics.combined_videos_with == 2
    assert metrics.rate_pct(metrics.caption.videos_with) == 50.0
    assert metrics.combined_rate_pct == 50.0
    assert metrics.combined_total_mentions == 3
    assert metrics.combined_avg == 0.75


def test_empty_axis_yields_na_not_zero() -> None:
    metrics = measure_keyword_axis([], keyword_variants(["kw"]))
    assert metrics.denominator == 0
    assert metrics.combined_rate_pct is None  # 0% ではなく N/A
    assert metrics.combined_avg is None


# ---------------------------------------------------------------------------
# #PR 判定（完全一致のみ）
# ---------------------------------------------------------------------------


def test_pr_tag_exact_match_only() -> None:
    assert has_pr_tag("", ("#PR",)) is True
    assert has_pr_tag("案件です ＃ＰＲ", ()) is True  # 全角も正規化で一致
    assert has_pr_tag("", ("#pr",)) is True
    assert has_pr_tag("", ("#brand_pr",)) is False  # 部分一致は#PR扱いしない
    assert has_pr_tag("#PRチーム の裏側", ()) is False
    assert has_pr_tag("propose した", ()) is False


def test_pr_comparison_partitions_denominator_and_reports_both_groups() -> None:
    variants = keyword_variants(["シャンプー"])
    posts = [
        _post("1", "シャンプー紹介 #PR"),
        _post("2", "シャンプーレビュー"),
        _post("3", "無関係 #PR"),
        _post("4", "無関係"),
    ]
    comparison = measure_pr_comparison(posts, variants)
    assert comparison.pr_videos == 2
    assert comparison.no_pr_videos == 2
    assert comparison.pr_videos + comparison.no_pr_videos == len(posts)
    assert comparison.pr.combined_rate_pct == 50.0
    assert comparison.no_pr.combined_rate_pct == 50.0


# ---------------------------------------------------------------------------
# ① 露出シェア・公式露出・投稿者内訳
# ---------------------------------------------------------------------------


def test_brand_exposure_counts_each_brand_once_per_video() -> None:
    brand_terms = {
        "エムキュア": keyword_variants(["エムキュア"]),
        "ラサーナ": keyword_variants(["ラサーナ"]),
    }
    posts = [
        _post("1", "エムキュアとラサーナを比較", rank=1),  # 両ブランドに各1本計上
        _post("2", "エムキュア単体レビュー", rank=2),
        _post("3", "無関係", rank=3),
        _post("4", "ラサーナ使ってみた #ラサーナ", rank=4),
    ]
    exposures = {e.brand: e for e in measure_brand_exposure(posts, brand_terms)}
    assert exposures["エムキュア"].videos == 2
    assert exposures["エムキュア"].share_pct == 50.0
    assert exposures["エムキュア"].best_rank == 1
    assert exposures["ラサーナ"].videos == 2
    assert exposures["ラサーナ"].best_rank == 1


def test_brand_exposure_counts_official_author_without_name_mention() -> None:
    brand_terms = {"エムキュア": keyword_variants(["エムキュア"])}
    posts = [
        _post("1", "新商品のご紹介", author="mqure_official", rank=3),
    ]
    exposures = measure_brand_exposure(
        posts,
        brand_terms,
        {"エムキュア": "https://www.tiktok.com/@mqure_official"},
    )
    assert exposures[0].videos == 1
    assert exposures[0].best_rank == 3


def test_official_exposure_and_poster_breakdown() -> None:
    posts = [
        _post("1", "a", author="mqure_official", rank=2),
        _post("2", "b", author="reviewer1", rank=5),
        _post("3", "c", author="MQURE_OFFICIAL", rank=9),  # 大文字も一致
    ]
    official = measure_official_exposure(posts, "@mqure_official")
    assert official.videos == 2
    assert official.share_pct == 66.7
    assert official.best_rank == 2
    assert official.ranks == (2, 9)
    breakdown = measure_poster_breakdown(posts, "@mqure_official")
    assert breakdown.official_videos == 2
    assert breakdown.third_party_videos == 1


def test_normalize_handle_accepts_url_and_at_forms() -> None:
    assert normalize_handle("https://www.tiktok.com/@Brand.Official?lang=ja") == "brand.official"
    assert normalize_handle("@brand.official") == "brand.official"
    assert normalize_handle("brand.official") == "brand.official"
    assert normalize_handle("") == ""


# ---------------------------------------------------------------------------
# 統合計測
# ---------------------------------------------------------------------------


def test_measure_wires_axes_and_skips_failed_axes_from_pools() -> None:
    general = AxisData(
        role="general",
        label="一般KW「ヘアケア」検索",
        query="ヘアケア",
        requested=120,
        posts=(
            _post("1", "エムキュアのヘアケア", rank=1),
            _post("2", "ラサーナ #PR", rank=2),
        ),
    )
    brand = AxisData(
        role="brand",
        label="ブランド名「エムキュア」検索",
        query="エムキュア",
        requested=120,
        posts=(_post("3", "ヘアケアはエムキュア", author="mqure", rank=1),),
    )
    failed = AxisData(
        role="competitor",
        label="競合「ラサーナ」検索",
        query="ラサーナ",
        requested=120,
        posts=(),
        failed=True,
        failure_code="MEDIA_TIKTOK_BOT_WALL",
    )
    measurement = measure(
        [general, brand, failed],
        brand="エムキュア",
        competitors=["ラサーナ"],
        keywords=["ヘアケア"],
        official_account="@mqure",
    )
    assert len(measurement.general_axes) == 1
    assert measurement.brand_axis is not None
    assert measurement.competitor_axes == ()
    assert [axis.label for axis in measurement.failed_axes] == ["競合「ラサーナ」検索"]
    assert measurement.official_handle_confirmed is True

    general_measurement = measurement.general_axes[0]
    exposure = {e.brand: e for e in general_measurement.brand_exposure}
    assert exposure["エムキュア"].videos == 1
    assert exposure["ラサーナ"].videos == 1
    # ブランド軸: 公式露出 + 投稿者内訳が付く
    brand_measurement = measurement.brand_axis
    assert brand_measurement.official is not None
    assert brand_measurement.official.videos == 1
    assert brand_measurement.poster is not None
    assert brand_measurement.poster.third_party_videos == 0


# ---------------------------------------------------------------------------
# FMT化裁定 追加指標（EG率・フォロワー階層・telop経路・クラスタ・頻出タグ・TOP5）
# ---------------------------------------------------------------------------


def _stat_post(
    video_id: str,
    *,
    plays: int,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    followers: int = 0,
    rank: int = 1,
    caption: str = "",
    hashtags: tuple[str, ...] = (),
) -> PostRecord:
    return PostRecord(
        video_id=video_id,
        url=f"https://www.tiktok.com/@a/video/{video_id}",
        author="a",
        caption=caption,
        hashtags=hashtags,
        rank=rank,
        plays=plays,
        likes=likes,
        comments=comments,
        shares=shares,
        saves=saves,
        followers=followers,
    )


def test_eg_rate_uses_spec_formula_and_zero_plays_is_zero() -> None:
    post = _stat_post("1", plays=10_000, likes=300, comments=100, shares=50, saves=50)
    assert post.eg_rate == (300 + 100 + 50 + 50) / 10_000
    assert post.eg_rate_pct == 5.0
    assert _stat_post("2", plays=0, likes=999).eg_rate == 0.0


def test_avg_eg_rate_empty_is_none_not_zero() -> None:
    from teamagent.skills.omiyage_report.metrics import avg_eg_rate_pct

    assert avg_eg_rate_pct([]) is None
    posts = [
        _stat_post("1", plays=100, likes=10),  # 10%
        _stat_post("2", plays=100, likes=30),  # 30%
    ]
    assert avg_eg_rate_pct(posts) == 20.0


def test_follower_tiers_bin_boundaries_match_ruling() -> None:
    from teamagent.skills.omiyage_report.metrics import measure_follower_tiers

    posts = [
        _stat_post("1", plays=100, followers=9_999),
        _stat_post("2", plays=200, followers=10_000),
        _stat_post("3", plays=300, followers=99_999),
        _stat_post("4", plays=400, followers=100_000),
        _stat_post("5", plays=500, followers=499_999),
        _stat_post("6", plays=600, followers=500_000),
    ]
    tiers = {tier.label: tier for tier in measure_follower_tiers(posts)}
    assert tiers["ナノ（〜1万）"].videos == 1
    assert tiers["マイクロ（1〜10万）"].videos == 2
    assert tiers["ミドル（10〜50万）"].videos == 2
    assert tiers["メガ（50万〜）"].videos == 1
    assert tiers["マイクロ（1〜10万）"].avg_plays == 250.0
    # 全帯を返す（0本の帯は N/A で開示）
    empty = {tier.label: tier for tier in measure_follower_tiers([])}
    assert empty["メガ（50万〜）"].videos == 0
    assert empty["メガ（50万〜）"].avg_eg_rate_pct is None


def test_telop_route_denominator_is_analyzed_count_only() -> None:
    from teamagent.skills.omiyage_report.metrics import measure_telop_route

    variants = keyword_variants(["ヘアケア"])
    posts = [
        _stat_post("1", plays=1),
        _stat_post("2", plays=1),
        _stat_post("3", plays=1),  # 未解析（telops に無い）
    ]
    telops = {"1": "ヘアケアの正解はこれ", "2": "無関係のテロップ"}
    metrics = measure_telop_route(posts, telops, variants)
    assert metrics.analyzed == 2  # 分母は解析できた本数（3ではない）
    assert metrics.videos_with == 1
    assert metrics.rate_pct == 50.0
    assert metrics.avg == 0.5
    # 解析0本は 0% ではなく N/A
    empty = measure_telop_route(posts, {}, variants)
    assert empty.analyzed == 0
    assert empty.rate_pct is None


def test_aggregate_clusters_excludes_unanalyzed_and_keeps_full_vocabulary() -> None:
    from teamagent.skills.omiyage_report.metrics import aggregate_clusters

    vocabulary = ("正直レビュー/検証系", "成分オタク系")
    posts = [
        _stat_post("1", plays=100, likes=10),  # EG 10%
        _stat_post("2", plays=100, likes=30),  # EG 30%
        _stat_post("3", plays=100, likes=90),  # 未解析 → 分母に入れない
    ]
    assignments = {"1": "正直レビュー/検証系", "2": "正直レビュー/検証系"}
    clusters = {c.label: c for c in aggregate_clusters(posts, assignments, vocabulary)}
    assert clusters["正直レビュー/検証系"].videos == 2
    assert clusters["正直レビュー/検証系"].avg_eg_rate_pct == 20.0
    assert clusters["成分オタク系"].videos == 0
    assert clusters["成分オタク系"].avg_eg_rate_pct is None
    assert sum(c.videos for c in clusters.values()) == 2  # 分母=解析できた本数


def test_top_hashtags_count_videos_not_occurrences() -> None:
    from teamagent.skills.omiyage_report.metrics import top_hashtags

    posts = [
        _stat_post("1", plays=1, caption="#ヘアケア #ヘアケア #PR", hashtags=("ヘアケア",)),
        _stat_post("2", plays=1, caption="#ヘアケア"),
        _stat_post("3", plays=1, caption="#シャンプー"),
    ]
    tags = top_hashtags(posts, limit=2)
    assert [(tag.display, tag.videos) for tag in tags] == [("#ヘアケア", 2), ("#PR", 1)]


def test_top_posts_sort_by_plays_then_rank() -> None:
    from teamagent.skills.omiyage_report.metrics import top_posts

    posts = [
        _stat_post("1", plays=100, rank=3),
        _stat_post("2", plays=500, rank=2),
        _stat_post("3", plays=100, rank=1),
    ]
    assert [post.video_id for post in top_posts(posts, limit=2)] == ["2", "3"]
