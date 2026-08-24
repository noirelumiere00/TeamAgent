"""お土産資料の決定論集計エンジン（LLM不使用）。

ローカルSkill tiktok-competitive-analysis の
``scripts/common.py`` / ``scripts/measure_keywords.py`` から、便1で使う
caption / hashtag 2経路のロジックを同じ定義で移植した。

指標定義（references/metric-definitions.md 準拠・便1範囲）:
- 分母は「取得できた全投稿」。キーワード0回の投稿も分母に含める。
- caption経路: caption本文（タグ除去後）の表記ゆれ込み非重複出現。
- hashtag経路: caption内タグと hashtags 列を統合し、同じタグの重複計上はしない
  （caption内に2回書かれたタグは2回のまま）。
- 照合は NFKC + 小文字化 + CJK間空白除去 + カタカナ→ひらがな統合。
  複数の表記ゆれは左端・最長一致で重複加算しない。英数字語へ連結した部分一致は除外。
- #PR は正規化後の完全一致タグ ``#PR`` のみ。「#PR表記なし」をオーガニックと呼ばない。
- 露出シェア: 同一動画に複数ブランド → 各ブランドに1本ずつ計上。
  取得順位は検索順位スナップショットとして扱い、並び替えない。
- 有効動画0件の率は 0% ではなく N/A（None）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

_KATAKANA_START = ord("ァ")
_HIRAGANA_START = ord("ぁ")
_KATAKANA_END = ord("ヶ")

_CJK_INTERNAL_SPACE_RE = re.compile(r"(?<=[　-鿿])\s+(?=[　-鿿])")
_LATIN_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_HASHTAG_RE = re.compile(r"[#＃]([\w一-龠々〆ヵヶぁ-んァ-ヶー]+)")


def katakana_to_hiragana(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if _KATAKANA_START <= code <= _KATAKANA_END:
            out.append(chr(code - (_KATAKANA_START - _HIRAGANA_START)))
        else:
            out.append(ch)
    return "".join(out)


def normalize_text(text: str | None, *, kana_fold: bool = False) -> str:
    """照合専用の正規化（表示には使わない）。ローカルSkill common.py と同一規則。"""
    if text is None:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = _CJK_INTERNAL_SPACE_RE.sub("", t)
    if kana_fold:
        t = katakana_to_hiragana(t)
    return t


@dataclass(frozen=True)
class KeywordVariant:
    display: str
    normalized: str


def keyword_variants(values: Sequence[str]) -> tuple[KeywordVariant, ...]:
    """表示名と正規化形のペア群（正規化空・重複は除外）。"""
    seen: set[str] = set()
    result: list[KeywordVariant] = []
    for value in values:
        normalized = normalize_text(value, kana_fold=True)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(KeywordVariant(display=value, normalized=normalized))
    return tuple(result)


def count_occurrences(text: str, variants: Sequence[KeywordVariant]) -> int:
    """左端・最長一致の非重複出現数（英数字語へ連結した部分一致は除外）。"""
    normalized = normalize_text(text, kana_fold=True)
    candidates: list[tuple[int, int]] = []
    for variant in variants:
        term = variant.normalized
        start = 0
        while term and start < len(normalized):
            idx = normalized.find(term, start)
            if idx < 0:
                break
            end = idx + len(term)
            before = normalized[idx - 1] if idx else ""
            after = normalized[end] if end < len(normalized) else ""
            if not _LATIN_ALNUM_RE.match(before) and not _LATIN_ALNUM_RE.match(after):
                candidates.append((idx, end))
            start = idx + 1
    candidates.sort(key=lambda pair: (pair[0], -(pair[1] - pair[0])))
    occupied_until = -1
    count = 0
    for start, end in candidates:
        if start >= occupied_until:
            count += 1
            occupied_until = end
    return count


def contains_term(text: str, variants: Sequence[KeywordVariant]) -> bool:
    return count_occurrences(text, variants) > 0


def caption_hashtags(caption: str) -> list[str]:
    return _HASHTAG_RE.findall(caption or "")


def caption_without_hashtags(caption: str) -> str:
    return _HASHTAG_RE.sub(" ", caption or "")


def _strip_tag(raw_tag: str) -> str:
    return re.sub(r"^[#＃]+", "", str(raw_tag or "")).strip()


def caption_route_hits(caption: str, variants: Sequence[KeywordVariant]) -> int:
    """caption経路: タグを除いた本文の非重複出現数。"""
    return count_occurrences(caption_without_hashtags(caption), variants)


def hashtag_route_hits(
    caption: str,
    hashtags: Sequence[str],
    variants: Sequence[KeywordVariant],
) -> int:
    """hashtag経路: caption内タグ + hashtags列。エクスポーター重複は1回だけ数える。

    同じ見た目のタグが caption と hashtags 列の両方にある場合は caption 側だけを
    数える（エクスポーターの二重掲載）。caption 内に同じタグが2回書かれていれば
    2回のまま（利用者に2回見えている）。
    """
    caption_tag_counts: dict[str, int] = {}
    total = 0
    for raw_tag in caption_hashtags(caption):
        tag = _strip_tag(raw_tag)
        norm = normalize_text(tag, kana_fold=True)
        caption_tag_counts[norm] = caption_tag_counts.get(norm, 0) + 1
        total += count_occurrences(tag, variants)
    seen_external: set[str] = set()
    for raw_tag in hashtags or ():
        tag = _strip_tag(raw_tag)
        norm = normalize_text(tag, kana_fold=True)
        if norm in caption_tag_counts or norm in seen_external:
            continue
        seen_external.add(norm)
        total += count_occurrences(tag, variants)
    return total


def has_pr_tag(caption: str, hashtags: Sequence[str]) -> bool:
    """正規化後の完全一致タグ ``#PR`` のみ True。``#brand_pr`` 等は対象外。"""
    candidates = [_strip_tag(tag) for tag in hashtags or ()]
    candidates += caption_hashtags(caption or "")
    return any(normalize_text(candidate) == "pr" for candidate in candidates)


def normalize_handle(value: str | None) -> str:
    """公式TikTokアカウント入力（URL / @handle / handle）→ 照合用ハンドル。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.search(r"tiktok\.com/@([\w.\-]+)", raw, flags=re.IGNORECASE)
    if match:
        raw = match.group(1)
    raw = raw.lstrip("@").split("?")[0].split("/")[0]
    return normalize_text(raw)


# ---------------------------------------------------------------------------
# 入力データ形
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostRecord:
    """検索結果1投稿（tiktok_search 実取得フィールドの決定論サブセット）。"""

    video_id: str
    url: str
    author: str
    caption: str
    hashtags: tuple[str, ...]
    rank: int  # 取得順位（1始まり・並び替えない）
    plays: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    followers: int = 0
    nickname: str = ""
    cover_url: str = ""
    duration_sec: int = 0

    @property
    def eg_rate(self) -> float:
        """EG率 = (いいね+コメント+シェア+保存)/再生。再生0は0（spec決定論式）。"""
        if self.plays <= 0:
            return 0.0
        return (self.likes + self.comments + self.shares + self.saves) / self.plays

    @property
    def eg_rate_pct(self) -> float:
        return round(self.eg_rate * 100, 2)


AxisRole = Literal["general", "brand", "competitor"]


@dataclass(frozen=True)
class AxisData:
    """1検索軸（=1クエリ）の取得結果。"""

    role: AxisRole
    label: str
    query: str
    requested: int
    posts: tuple[PostRecord, ...]
    failed: bool = False
    failure_code: str = ""


# ---------------------------------------------------------------------------
# ② キーワード登場率（caption / hashtag 2経路）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteMetrics:
    videos_with: int
    total_mentions: int


@dataclass(frozen=True)
class KeywordMetrics:
    denominator: int
    caption: RouteMetrics
    hashtag: RouteMetrics
    combined_videos_with: int
    combined_total_mentions: int

    def rate_pct(self, videos_with: int) -> float | None:
        if self.denominator == 0:
            return None
        return round(videos_with / self.denominator * 100, 1)

    def avg(self, total: int) -> float | None:
        if self.denominator == 0:
            return None
        return round(total / self.denominator, 2)

    @property
    def combined_rate_pct(self) -> float | None:
        return self.rate_pct(self.combined_videos_with)

    @property
    def combined_avg(self) -> float | None:
        return self.avg(self.combined_total_mentions)


def measure_keyword_axis(
    posts: Sequence[PostRecord],
    variants: Sequence[KeywordVariant],
) -> KeywordMetrics:
    """分母は取得できた全投稿（0回の投稿も含む）。"""
    caption_counts: list[int] = []
    hashtag_counts: list[int] = []
    for post in posts:
        caption_counts.append(caption_route_hits(post.caption, variants))
        hashtag_counts.append(hashtag_route_hits(post.caption, post.hashtags, variants))
    combined = [c + h for c, h in zip(caption_counts, hashtag_counts, strict=True)]
    return KeywordMetrics(
        denominator=len(posts),
        caption=RouteMetrics(
            videos_with=sum(1 for count in caption_counts if count > 0),
            total_mentions=sum(caption_counts),
        ),
        hashtag=RouteMetrics(
            videos_with=sum(1 for count in hashtag_counts if count > 0),
            total_mentions=sum(hashtag_counts),
        ),
        combined_videos_with=sum(1 for count in combined if count > 0),
        combined_total_mentions=sum(combined),
    )


@dataclass(frozen=True)
class PrComparison:
    pr_videos: int
    no_pr_videos: int
    pr: KeywordMetrics
    no_pr: KeywordMetrics


def measure_pr_comparison(
    posts: Sequence[PostRecord],
    variants: Sequence[KeywordVariant],
) -> PrComparison:
    """#PR表記あり／なしの両群で同じ指標を計算する（群の合計=分母）。"""
    pr_posts = [post for post in posts if has_pr_tag(post.caption, post.hashtags)]
    no_pr_posts = [post for post in posts if not has_pr_tag(post.caption, post.hashtags)]
    return PrComparison(
        pr_videos=len(pr_posts),
        no_pr_videos=len(no_pr_posts),
        pr=measure_keyword_axis(pr_posts, variants),
        no_pr=measure_keyword_axis(no_pr_posts, variants),
    )


# ---------------------------------------------------------------------------
# ① 露出シェア・公式アカウント露出・投稿者内訳
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrandExposure:
    brand: str
    videos: int
    share_pct: float | None
    best_rank: int | None


def measure_brand_exposure(
    posts: Sequence[PostRecord],
    brand_terms: Mapping[str, Sequence[KeywordVariant]],
    official_handles: Mapping[str, str] | None = None,
) -> tuple[BrandExposure, ...]:
    """各ブランドの露出本数。同一動画に複数ブランド→各1本計上。

    帰属判定はブランド名（表記ゆれ込み）の caption/hashtag 登場、または
    投稿者ハンドルがそのブランドの公式ハンドルと一致した場合。
    """
    handles = {
        brand: normalize_handle(handle)
        for brand, handle in (official_handles or {}).items()
        if normalize_handle(handle)
    }
    denominator = len(posts)
    results: list[BrandExposure] = []
    for brand, variants in brand_terms.items():
        official = handles.get(brand, "")
        videos = 0
        best_rank: int | None = None
        for post in posts:
            hit = (
                contains_term(post.caption, variants)
                or any(contains_term(tag, variants) for tag in post.hashtags)
                or (bool(official) and normalize_handle(post.author) == official)
            )
            if hit:
                videos += 1
                if best_rank is None or post.rank < best_rank:
                    best_rank = post.rank
        share = round(videos / denominator * 100, 1) if denominator else None
        results.append(
            BrandExposure(brand=brand, videos=videos, share_pct=share, best_rank=best_rank)
        )
    return tuple(results)


@dataclass(frozen=True)
class OfficialExposure:
    handle: str
    videos: int
    share_pct: float | None
    best_rank: int | None
    ranks: tuple[int, ...]


def measure_official_exposure(
    posts: Sequence[PostRecord],
    official_account: str,
) -> OfficialExposure:
    handle = normalize_handle(official_account)
    matched = [post for post in posts if handle and normalize_handle(post.author) == handle]
    denominator = len(posts)
    ranks = tuple(sorted(post.rank for post in matched))
    return OfficialExposure(
        handle=handle,
        videos=len(matched),
        share_pct=round(len(matched) / denominator * 100, 1) if denominator else None,
        best_rank=ranks[0] if ranks else None,
        ranks=ranks,
    )


@dataclass(frozen=True)
class PosterBreakdown:
    official_videos: int
    third_party_videos: int


def measure_poster_breakdown(
    posts: Sequence[PostRecord],
    official_account: str,
) -> PosterBreakdown:
    """公式／第三者の投稿者内訳（公式ハンドル確認済みの軸で使う）。"""
    handle = normalize_handle(official_account)
    official = sum(1 for post in posts if handle and normalize_handle(post.author) == handle)
    return PosterBreakdown(
        official_videos=official,
        third_party_videos=len(posts) - official,
    )


# ---------------------------------------------------------------------------
# EG率・フォロワー階層・頻出タグ・TOP5（FMT化裁定 2026-08-24 追加分）
# ---------------------------------------------------------------------------


def avg_plays(posts: Sequence[PostRecord]) -> float | None:
    if not posts:
        return None
    return round(sum(post.plays for post in posts) / len(posts), 1)


def avg_eg_rate_pct(posts: Sequence[PostRecord]) -> float | None:
    """1本ごとのEG率の単純平均（%）。0件は 0% ではなく N/A（None）。"""
    if not posts:
        return None
    return round(sum(post.eg_rate for post in posts) / len(posts) * 100, 2)


# ユーザー裁定（2026-08-24）のフォロワー帯: ナノ〜1万 / マイクロ1-10万 / ミドル10-50万 / メガ50万〜
FOLLOWER_TIERS: tuple[tuple[str, int, int | None], ...] = (
    ("ナノ（〜1万）", 0, 10_000),
    ("マイクロ（1〜10万）", 10_000, 100_000),
    ("ミドル（10〜50万）", 100_000, 500_000),
    ("メガ（50万〜）", 500_000, None),
)


@dataclass(frozen=True)
class TierMetrics:
    label: str
    videos: int
    avg_plays: float | None
    avg_eg_rate_pct: float | None


def measure_follower_tiers(posts: Sequence[PostRecord]) -> tuple[TierMetrics, ...]:
    """フォロワー階層別の本数・平均再生・平均EG率。帯は全帯を返す（0本も明示）。

    follower_count はスクレイパ側で欠損時0埋めのため、0 はナノ帯へ入る
    （spec follower_zero_caveat）。
    """
    result: list[TierMetrics] = []
    for label, low, high in FOLLOWER_TIERS:
        member = [
            post
            for post in posts
            if post.followers >= low and (high is None or post.followers < high)
        ]
        result.append(
            TierMetrics(
                label=label,
                videos=len(member),
                avg_plays=avg_plays(member),
                avg_eg_rate_pct=avg_eg_rate_pct(member),
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class HashtagCount:
    display: str
    videos: int


def top_hashtags(posts: Sequence[PostRecord], *, limit: int = 5) -> tuple[HashtagCount, ...]:
    """頻出ハッシュタグ（そのタグを含む動画本数・同数は初出順）。"""
    order: list[str] = []
    display: dict[str, str] = {}
    counts: dict[str, int] = {}
    for post in posts:
        tags: dict[str, str] = {}
        for raw in (*caption_hashtags(post.caption), *post.hashtags):
            tag = _strip_tag(raw)
            norm = normalize_text(tag, kana_fold=True)
            if norm:
                tags.setdefault(norm, tag)
        for norm, shown in tags.items():
            if norm not in counts:
                counts[norm] = 0
                display[norm] = shown
                order.append(norm)
            counts[norm] += 1
    ranked = sorted(order, key=lambda norm: (-counts[norm], order.index(norm)))
    return tuple(
        HashtagCount(display=f"#{display[norm]}", videos=counts[norm]) for norm in ranked[:limit]
    )


def top_posts(posts: Sequence[PostRecord], *, limit: int = 5) -> tuple[PostRecord, ...]:
    """再生数TOP-N（同数は取得順位の上位を先に）。並びの元データは改変しない。"""
    return tuple(sorted(posts, key=lambda post: (-post.plays, post.rank))[:limit])


# ---------------------------------------------------------------------------
# テロップ経路（動画解析由来）とクラスタ集計
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelopMetrics:
    """telop経路の登場率。分母は「テロップを解析できた本数」（未解析を0回にしない）。"""

    analyzed: int
    videos_with: int
    total_mentions: int

    @property
    def rate_pct(self) -> float | None:
        if self.analyzed == 0:
            return None
        return round(self.videos_with / self.analyzed * 100, 1)

    @property
    def avg(self) -> float | None:
        if self.analyzed == 0:
            return None
        return round(self.total_mentions / self.analyzed, 2)


def measure_telop_route(
    posts: Sequence[PostRecord],
    telops: Mapping[str, str],
    variants: Sequence[KeywordVariant],
) -> TelopMetrics:
    """視覚AIが読み取ったテロップ文字列（video_id→text）でのキーワード登場。"""
    analyzed = [post for post in posts if post.video_id in telops]
    counts = [count_occurrences(telops[post.video_id], variants) for post in analyzed]
    return TelopMetrics(
        analyzed=len(analyzed),
        videos_with=sum(1 for count in counts if count > 0),
        total_mentions=sum(counts),
    )


@dataclass(frozen=True)
class ClusterAggregate:
    label: str
    videos: int
    avg_eg_rate_pct: float | None


def aggregate_clusters(
    posts: Sequence[PostRecord],
    assignments: Mapping[str, str],
    vocabulary: Sequence[str],
) -> tuple[ClusterAggregate, ...]:
    """クラスタ表（件数+平均EG率）。分母=解析できた本数（assignmentsにある投稿のみ）。

    取得・解析に失敗した投稿は assignments に居ないため自動的に分母から外れる
    （未取得をクラスタへ混ぜない）。語彙外ラベルは無視する（視覚AI出力の検証は
    video_analysis 側の責務で、ここは決定論集計だけを行う）。
    """
    by_label: dict[str, list[PostRecord]] = {label: [] for label in vocabulary}
    for post in posts:
        label = assignments.get(post.video_id)
        if label in by_label:
            by_label[label].append(post)
    return tuple(
        ClusterAggregate(
            label=label,
            videos=len(members),
            avg_eg_rate_pct=avg_eg_rate_pct(members),
        )
        for label, members in by_label.items()
    )


# ---------------------------------------------------------------------------
# 統合計測
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AxisMeasurement:
    axis: AxisData
    keyword: KeywordMetrics
    pr: PrComparison
    brand_exposure: tuple[BrandExposure, ...]
    official: OfficialExposure | None
    poster: PosterBreakdown | None


@dataclass(frozen=True)
class OmiyageMeasurement:
    brand: str
    competitors: tuple[str, ...]
    keywords: tuple[str, ...]
    official_account: str
    axes: tuple[AxisMeasurement, ...]

    @property
    def general_axes(self) -> tuple[AxisMeasurement, ...]:
        return tuple(m for m in self.axes if m.axis.role == "general" and not m.axis.failed)

    @property
    def brand_axis(self) -> AxisMeasurement | None:
        for m in self.axes:
            if m.axis.role == "brand" and not m.axis.failed:
                return m
        return None

    @property
    def competitor_axes(self) -> tuple[AxisMeasurement, ...]:
        return tuple(m for m in self.axes if m.axis.role == "competitor" and not m.axis.failed)

    @property
    def failed_axes(self) -> tuple[AxisData, ...]:
        return tuple(m.axis for m in self.axes if m.axis.failed)

    @property
    def official_handle_confirmed(self) -> bool:
        return bool(normalize_handle(self.official_account))


def measure(
    axes: Sequence[AxisData],
    *,
    brand: str,
    competitors: Sequence[str],
    keywords: Sequence[str],
    official_account: str = "",
) -> OmiyageMeasurement:
    """全軸の決定論計測。一般KW軸では自社+競合の露出シェアも計測する。"""
    kw_variants = keyword_variants(keywords)
    brand_terms: dict[str, Sequence[KeywordVariant]] = {brand: keyword_variants([brand])}
    for competitor in competitors:
        brand_terms[competitor] = keyword_variants([competitor])
    official_handles = {brand: official_account} if official_account else {}
    handle_confirmed = bool(normalize_handle(official_account))

    measurements: list[AxisMeasurement] = []
    for axis in axes:
        posts = axis.posts if not axis.failed else ()
        exposure: tuple[BrandExposure, ...] = ()
        official: OfficialExposure | None = None
        poster: PosterBreakdown | None = None
        if axis.role == "general" and not axis.failed:
            exposure = measure_brand_exposure(posts, brand_terms, official_handles)
            if handle_confirmed:
                official = measure_official_exposure(posts, official_account)
        if axis.role == "brand" and not axis.failed and handle_confirmed:
            official = measure_official_exposure(posts, official_account)
            poster = measure_poster_breakdown(posts, official_account)
        measurements.append(
            AxisMeasurement(
                axis=axis,
                keyword=measure_keyword_axis(posts, kw_variants),
                pr=measure_pr_comparison(posts, kw_variants),
                brand_exposure=exposure,
                official=official,
                poster=poster,
            )
        )
    return OmiyageMeasurement(
        brand=brand,
        competitors=tuple(competitors),
        keywords=tuple(keywords),
        official_account=official_account,
        axes=tuple(measurements),
    )
