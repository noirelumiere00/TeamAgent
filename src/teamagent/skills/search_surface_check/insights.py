"""検索面の構造を決定的に数える（LLM を通さない）。

結論（LLM）はここで数えた数字だけを根拠に書かせる。数字の出どころを 1 か所に絞るのは、
「再生の71%」のような数字を LLM が作らないようにするため（skill 側で照合する）。
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Sequence

from teamagent.skills.search_surface_check.schema import (
    CategoryStat,
    HolderStat,
    SaveLeader,
    SurfaceFacts,
    SurfacePost,
    TagStat,
    TierStat,
)

# フォロワー帯（下限, 表示名）。0 人は「不明」として帯に入れない（IG は取得できない）。
FOLLOWER_TIERS: tuple[tuple[int, str], ...] = (
    (1_000_000, "100万人以上"),
    (100_000, "10万〜100万人"),
    (10_000, "1万〜10万人"),
    (1, "1万人未満"),
)
SMALL_ACCOUNT_MAX = 10_000
# 保存率の上位は、再生が少なすぎて率だけ跳ねた投稿を外す。
_SAVE_LEADER_MIN_PLAYS = 1_000
_DAY = 86_400
# 広告表記として扱うタグ（正規化後の完全一致。#brand_pr のような部分一致は数えない）。
_PR_TAGS = frozenset({"pr", "ad", "提供", "タイアップ", "プロモーション", "sponsored", "promotion"})
_CAPTION_TAG_RE = re.compile(r"[#＃]([^\s#＃]+)")


def normalize(text: str) -> str:
    """照合用: NFKC・小文字・空白除去。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower())


def post_tags(post: SurfacePost) -> list[str]:
    """タグ列と本文中の #タグ を合わせて、正規化した 1 投稿 1 回のタグ集合にする。"""
    raw = list(post.hashtags) + _CAPTION_TAG_RE.findall(post.desc or "")
    seen: dict[str, None] = {}
    for tag in raw:
        norm = normalize(tag.lstrip("#＃"))
        if norm:
            seen.setdefault(norm, None)
    return list(seen)


def is_pr_post(post: SurfacePost) -> bool:
    return any(tag in _PR_TAGS for tag in post_tags(post))


def mentions(post: SurfacePost, name: str | None) -> bool:
    """本文・タグにクライアント名が出るか。1 文字の名前は誤爆するので見ない。"""
    target = normalize(name or "")
    if len(target) < 2:
        return False
    return target in normalize(post.desc) or any(target in tag for tag in post_tags(post))


def _kw_terms(keyword: str) -> list[str]:
    return [normalize(t) for t in re.split(r"[\s　]+", keyword) if normalize(t)]


def contains_keyword(post: SurfacePost, keyword: str) -> bool:
    terms = _kw_terms(keyword)
    if not terms:
        return False
    haystack = normalize(post.desc) + "|" + "|".join(post_tags(post))
    return all(term in haystack for term in terms)


def _median_int(values: Sequence[float]) -> int | None:
    return round(statistics.median(values)) if values else None


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """順位相関（同順位は平均順位）。ばらつきが無ければ None。"""

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    try:
        return round(statistics.correlation(rx, ry), 2)
    except statistics.StatisticsError:
        return None


def tier_of(followers: int) -> str | None:
    for floor, label in FOLLOWER_TIERS:
        if followers >= floor:
            return label
    return None


def compute_facts(
    posts: Sequence[SurfacePost],
    *,
    keyword: str,
    client_name: str | None,
    now_epoch: int,
) -> SurfaceFacts:
    """1 つの面（KW×媒体）の構造を数える。posts は面の表示順。"""
    n = len(posts)
    plays_total = sum(p.play_count for p in posts)

    by_cat: dict[str, list[SurfacePost]] = {}
    for p in posts:
        by_cat.setdefault(p.category, []).append(p)
    categories = sorted(
        (
            CategoryStat(
                category=cat,
                count=len(group),
                count_share=round(len(group) / n, 3) if n else 0.0,
                play_share=(
                    round(sum(p.play_count for p in group) / plays_total, 3) if plays_total else 0.0
                ),
                median_plays=_median_int([p.play_count for p in group]) or 0,
            )
            for cat, group in by_cat.items()
        ),
        key=lambda c: (-c.count, -c.play_share, c.category),
    )

    by_author: dict[str, list[SurfacePost]] = {}
    for p in posts:
        if p.author:
            by_author.setdefault(p.author.lower(), []).append(p)
    holders = sorted(
        (
            HolderStat(
                author=group[0].author,
                author_name=group[0].author_name,
                category=group[0].category,
                ranks=[p.rank for p in group],
            )
            for group in by_author.values()
            if len(group) >= 2
        ),
        key=lambda h: (-len(h.ranks), min(h.ranks)),
    )

    with_followers = [p for p in posts if p.author_followers > 0]
    tiers: list[TierStat] = []
    for _, label in FOLLOWER_TIERS:
        group = [p for p in with_followers if tier_of(p.author_followers) == label]
        if group:
            tiers.append(
                TierStat(
                    tier=label,
                    count=len(group),
                    play_share=(
                        round(sum(p.play_count for p in group) / plays_total, 3)
                        if plays_total
                        else 0.0
                    ),
                )
            )
    top10 = [p for p in posts if p.rank <= 10]
    small_in_top10 = (
        sum(1 for p in top10 if 0 < p.author_followers < SMALL_ACCOUNT_MAX)
        if any(p.author_followers > 0 for p in top10)
        else None
    )
    reach = [p.play_count / p.author_followers for p in with_followers if p.play_count > 0]

    played = [p for p in posts if p.play_count > 0]
    most_played = max(played, key=lambda p: p.play_count) if played else None
    rho = (
        _spearman([p.rank for p in played], [-p.play_count for p in played])
        if len(played) >= 8
        else None
    )

    save_rates = [
        (p, p.save_count / p.play_count * 100) for p in posts if p.play_count > 0 and p.save_count
    ]
    has_saves = any(p.save_count > 0 for p in posts)
    median_save = (
        round(statistics.median([p.save_count / p.play_count * 100 for p in played]), 2)
        if has_saves and played
        else None
    )
    save_leaders = [
        SaveLeader(rank=p.rank, author=p.author, save_rate_pct=round(rate, 2), plays=p.play_count)
        for p, rate in sorted(save_rates, key=lambda pr: -pr[1])
        if p.play_count >= _SAVE_LEADER_MIN_PLAYS
    ][:3]

    ages = [max(0, (now_epoch - p.posted_at) // _DAY) for p in posts if p.posted_at > 0]
    durations = [p.duration_sec for p in posts if p.duration_sec > 0]

    tag_counts: Counter[str] = Counter()
    for p in posts:
        tag_counts.update(post_tags(p))
    top_tags = [
        TagStat(tag=tag, count=count)
        for tag, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= 2 and tag not in _PR_TAGS
    ][:8]

    return SurfaceFacts(
        n=n,
        unique_authors=len(by_author),
        categories=categories,
        holders=holders,
        tiers=tiers,
        small_in_top10=small_in_top10,
        top10_n=len(top10),
        median_plays=_median_int([p.play_count for p in posts]) or 0,
        reach_ratio_median=round(statistics.median(reach), 2) if reach else None,
        most_played_rank=most_played.rank if most_played else None,
        rank_play_rho=rho,
        median_save_rate_pct=median_save,
        save_leaders=save_leaders,
        median_age_days=_median_int(ages),
        recent_90d=sum(1 for a in ages if a <= 90) if ages else None,
        median_duration_sec=_median_int(durations),
        kw_in_text=sum(1 for p in posts if contains_keyword(p, keyword)) if n else None,
        top_tags=top_tags,
        pr_ranks=[p.rank for p in posts if is_pr_post(p)],
        client_ranks=[p.rank for p in posts if p.is_client],
        mention_ranks=[p.rank for p in posts if mentions(p, client_name)],
    )


__all__ = [
    "FOLLOWER_TIERS",
    "compute_facts",
    "contains_keyword",
    "is_pr_post",
    "mentions",
    "normalize",
    "post_tags",
    "tier_of",
]
