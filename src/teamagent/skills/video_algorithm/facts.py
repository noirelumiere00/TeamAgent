"""video_algorithm の事実層（純関数・LLM を使わない）。

横断シンセシス（LLM）の入力と、スライド・レポートの全描画が**同じ事実**を使うための層。
本数（c/n）・段階名（必須条件／多数派／事例）・分布・区分・PR・KW の照合は、ここ（コード）だけが
決める。LLM には書かせない（本番で、上位 2 本の幅を「n=5」と書く・別指標の ρ を付ける・
Gemini が推測した「client」をそのまま使う、といった誤りがあった）。

- 尺は TikTok のメタ（meta.duration_sec）を優先し、0 のときだけ Gemini の値を使う。
- KW は語ごと×層ごと×完全一致/言い換え（evidence.kw_hits。テロップは実在を照合済み）。
- ブランドの区分は名簿（client_name / competitors）でコードが決める。名簿が無ければ「未指定」。
- PR はキャプション全文の #PR 等・@ブランド・「提供の可能性」（likely_sponsored）で判定する。
- 向き（縦/横）は抜いたコマの JPEG の SOF から幅と高さを読む（stdlib）。
- CTA は文言も秒も無いものを無効にする（本番の #4 の comment は文言も秒も無かった）。
"""

from __future__ import annotations

import base64
import binascii
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from teamagent.skills.search_surface_check.video_digest import CTA_LABEL, HOOK_LABEL
from teamagent.skills.search_surface_check.video_structure import QTY_RE, ROLE_LABEL, infer_roles
from teamagent.skills.video_algorithm.evidence import (
    KW_LAYER_LABEL,
    RELATION_LABEL,
    TIER_CASE,
    UNIDENTIFIED_LOGO,
    KwHit,
    Relation,
    Roster,
    contains,
    fold,
    kw_hits,
    norm,
    query_terms,
    tier,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    BrandDetection,
    FrameShot,
    VideoMeta,
    VideoVSEOAnalysis,
)

JST = timezone(timedelta(hours=9), "JST")

# 冒頭（0〜3 秒）の秒。video_digest.OPENING_SEC と同じ。
OPENING_SEC = 3.0
# 「最初のテロップが 0 秒」とみなす上限（0 秒台）。
FIRST_TELOP_ZERO_SEC = 1.0
# 構成の型の段（video_notes.STORYBOARD_STAGES の 4 段と 1 対 1 に対応）。
STAGE_LABELS: tuple[str, ...] = ("0〜3秒", "3〜10秒", "10秒〜残り10秒", "最後の10秒")
_STAGE_HEAD_END = 3.0
_STAGE_BUILD_END = 10.0
_STAGE_TAIL = 10.0
# 外れ値の基準（事実だけ・コードが決める）。
OUTLIER_LONG_RATIO = 1.4  # 尺が中央値のこの倍を超える
OUTLIER_LOW_PLAYS_RATIO = 0.2  # 再生が中央値のこの倍未満
OUTLIER_OLD_DAYS = 365  # 最古の投稿が、投稿日の中央値よりこの日数以上古い
_MAX_NUMERIC_CLAIMS = 12
_CAPTION_CONTEXT = 12

# 数の主張（同じ助数詞に付く数が欄の間で食い違うかを見るための原文）。
NUMERIC_CLAIM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:種類|種|つ|分|秒|枚)")
_PR_TAG_RE = re.compile(r"#(pr|タイアップ|提供|プロモーション)(?![0-9a-z_])")
_VIDEO_ID_RE = re.compile(r"/video/(\d{15,20})")
# TikTok のサービス開始より前・未来の日付は、動画 ID からの換算が外れたものとして捨てる。
_ID_EPOCH_MIN = int(datetime(2016, 9, 1, tzinfo=JST).timestamp())

_PROMINENT = ("hero", "prominent")
_PROMINENCE_RANK = {"hero": 0, "prominent": 1, "incidental": 2, "background": 3}
PROMINENCE_LABEL = {
    "hero": "主役",
    "prominent": "目立つ",
    "incidental": "付随",
    "background": "背景",
}
POSITION_LABEL = {
    "top": "上段",
    "center": "中央",
    "bottom": "下段",
    "full": "全面",
    "unknown": "不明",
}
ORIENTATION_LABEL = {"portrait": "縦", "landscape": "横長", "square": "正方形", "unknown": "不明"}
CTA_KIND_LABEL: dict[str, str] = {
    **CTA_LABEL,
    "link_bio": "プロフィール誘導",
    "try": "試してみて",
    "other": "その他",
}
# 文言から CTA の型を決める（Gemini の cta_type より優先。本番では「プロフィールから
# チェックしてね」「ぜひお試しください」が visit＝来店になっていた）。
_CTA_TEXT_KIND: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("link_bio", ("プロフィール", "プロフ", "リンク")),
    ("save", ("保存",)),
    ("follow", ("フォロー",)),
    ("comment", ("コメント",)),
    ("buy", ("購入", "買って", "ポチ")),
    ("try", ("試して", "お試し", "作ってみ", "やってみ", "作って")),
)
_CAPTION_CTA: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("save", re.compile(r"「保存」|保存(?:して|しておいて|しといて|お願い|推奨|必須|必至)")),
    ("follow", re.compile(r"フォロー(?:して|しておいて|お願い|よろしく|で)")),
    ("comment", re.compile(r"コメント(?:で|して|ください|お待ち|欄)")),
    ("link_bio", re.compile(r"プロフ(?:ィール)?(?:から|の|リンク)")),
)
EVENT_LABEL: dict[str, str] = {
    "first_telop": "最初のテロップ",
    "kw_telop": "検索語テロップの初出",
    "qty_telop": "分量テロップ",
    "brand_first": "目立つ商品の初出",
    "result_first": "完成品の初出",
    "cta": "CTA",
}
_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


# ── データ ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrandFact:
    """1 つのブランド（同じ名前の検出はまとめる）。区分は名簿でコードが決める。"""

    name: str
    relation: Relation
    prominence: str
    first_sec: float | None
    last_sec: float | None
    total_sec: float
    in_telop: bool
    in_caption: bool
    sponsored: bool
    category_match: bool | None  # 検索 KW の商材カテゴリの商品か（名簿か v3 の欄。無ければ None）

    @property
    def relation_label(self) -> str:
        return RELATION_LABEL[self.relation]

    @property
    def prominence_label(self) -> str:
        return PROMINENCE_LABEL.get(self.prominence, "")

    @property
    def prominent(self) -> bool:
        return self.prominence in _PROMINENT


@dataclass(frozen=True)
class VideoFacts:
    """1 本の事実（コードが決めた値だけ）。"""

    rank: int
    author: str
    followers: int
    posted_at: date | None
    posted_estimated: bool  # 投稿日を動画 ID から換算した（非公式の方法）
    plays: int
    save_rate: float  # %（2.9% は 2.9）
    shares: int
    duration_sec: float  # meta 優先・0 のときだけ analysis
    orientation: str  # portrait / landscape / square / unknown
    telop_count: int
    telops_per_sec: float
    telop_position_major: str
    first_telop_sec: float | None
    opening_telops: tuple[tuple[float, str], ...]  # 0〜3 秒の全文（秒の順）
    kw: tuple[KwHit, ...]
    qty_telops: tuple[tuple[float, str], ...]  # 分量テロップ（秒の順）
    qty_in_caption: bool
    numeric_claims: tuple[tuple[str, float | None, str], ...]  # (層, 秒, 原文)
    brands: tuple[BrandFact, ...]
    pr: bool
    pr_evidence: str
    cta_in_video: tuple[str, str, float | None] | None  # (型, 文言, 秒)。文言も秒も無ければ None
    cta_dropped: tuple[str, ...]  # 文言も秒も無く無効にした cta_type
    cta_in_caption: tuple[str, ...]  # キャプション内の呼びかけの型
    narration: bool
    music_title: str
    roles_inferred: bool  # 場面の役割がコードの推定（v2 の出力）
    result_first_sec: float | None  # v3 の欄（v2 では None）
    hook_type: str
    desc: str  # キャプション全文（refs の照合に使う）
    watched: bool  # 動画そのものを見て分析できた（サムネだけの縮退・失敗は False）

    @property
    def kw_first_telop_sec(self) -> float | None:
        secs = [s for h in self.kw if h.layer == "telop" and h.match == "exact" for s in h.secs]
        return min(secs) if secs else None

    def has_kw(self, term: str, layer: str, match: str) -> bool:
        return any(h.term == term and h.layer == layer and h.match == match for h in self.kw)

    @property
    def qty_anywhere(self) -> bool:
        return bool(self.qty_telops) or self.qty_in_caption

    @property
    def qty_place(self) -> str:
        """分量の置き場所（テロップ／キャプション／両方／無し）。"""
        if self.qty_telops and self.qty_in_caption:
            return "テロップとキャプション"
        if self.qty_telops:
            return "テロップ"
        if self.qty_in_caption:
            return "キャプション"
        return "無し"


@dataclass(frozen=True)
class Feature:
    """横断の特徴 1 つ（本数・順位・段階はコードが決める）。"""

    id: str
    label: str
    ranks: tuple[int, ...]
    n: int
    tier: str
    board_rate: tuple[int, int] | None = None  # メタで測れる特徴だけ（上位ボード全体の本数/本数）

    @property
    def count(self) -> int:
        return len(self.ranks)


@dataclass(frozen=True)
class KwRow:
    """拾われる条件の表の 1 行（語×層）。"""

    term: str
    layer: str
    exact: tuple[int, ...]
    synonym: tuple[int, ...]
    n: int
    verified: bool
    board: tuple[int, int] | None = None

    @property
    def layer_label(self) -> str:
        return KW_LAYER_LABEL.get(self.layer, self.layer)


@dataclass(frozen=True)
class Distribution:
    median: float
    min: float
    max: float


@dataclass(frozen=True)
class SummaryBand:
    """構成の型の上の帯（尺・1 秒あたりのテロップ・語り）。"""

    n: int
    duration: Distribution | None
    telops_per_sec: Distribution | None
    narration_ranks: tuple[int, ...]


@dataclass(frozen=True)
class StageObs:
    """1 本 × 1 段の観測。"""

    rank: int
    start: float
    end: float
    role: str | None
    role_inferred: bool
    telops: tuple[tuple[float, str], ...]  # 段内のテロップ（先頭 2 つ）
    events: tuple[str, ...]  # EVENT_LABEL のキー


@dataclass(frozen=True)
class StageRow:
    """共通する構成の型の 1 段（本数と段階はコード）。"""

    index: int
    label: str
    per_video: tuple[StageObs, ...]
    roles: tuple[tuple[str, tuple[int, ...], str], ...]  # (役割, 順位, 段階)
    events: tuple[tuple[str, tuple[int, ...], str], ...]  # (出来事, 順位, 段階)
    examples: tuple[tuple[int, float, str], ...]  # (#n, 秒, 原文)
    roles_inferred: bool


@dataclass(frozen=True)
class SurfaceMap:
    """検索面の地図（上位ボード・メタだけ）。"""

    size: int
    creators: tuple[tuple[str, tuple[int, ...]], ...]  # 2 本以上の作り手
    angles: tuple[tuple[str, tuple[int, ...]], ...]  # 切り口の語 → 順位（LLM が語、コードが本数）
    pr_ranks: tuple[int, ...]
    top_save: tuple[tuple[int, float], ...]  # 保存率の高い 3 本 (順位, %)
    median_save_rate: float | None
    years: tuple[tuple[int, int], ...]  # (投稿年, 本数)
    kw_rates: tuple[tuple[str, str, int, int], ...]  # (語, 層, 本数, 母数)


# ── 小さな部品 ──────────────────────────────────────────────────────────


def duration_of(meta: VideoMeta, analysis: VideoVSEOAnalysis | None) -> float:
    """尺: TikTok のメタを優先し、0 のときだけ Gemini の値。"""
    if meta.duration_sec > 0:
        return float(meta.duration_sec)
    if analysis is not None and analysis.duration_sec > 0:
        return float(analysis.duration_sec)
    return 0.0


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """JPEG の SOF から (幅, 高さ)。読めなければ None（stdlib のみ）。"""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # 詰め物
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # 長さの無いマーカー
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # 画像の終わり・スキャンの始まり（SOF はそれより前）
            return None
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2:
            return None
        if marker in _SOF_MARKERS:
            if i + 9 > len(data):
                return None
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height) if width and height else None
        i += 2 + seglen
    return None


def _data_uri_size(uri: str) -> tuple[int, int] | None:
    if not uri.startswith("data:image/") or ";base64," not in uri:
        return None
    try:
        raw = base64.b64decode(uri.split(",", 1)[1], validate=False)
    except (binascii.Error, ValueError):
        return None
    return jpeg_size(raw)


def orientation_of(frames: Iterable[FrameShot], cover_data_uri: str = "") -> str:
    """コマの寸法で縦横を決める。コマで読めなければ表紙、それも無ければ unknown。"""
    for uri in [*(f.data_uri for f in frames), cover_data_uri]:
        size = _data_uri_size(uri or "")
        if size is None:
            continue
        w, h = size
        if w > h * 1.05:
            return "landscape"
        if h > w * 1.05:
            return "portrait"
        return "square"
    return "unknown"


def posted_date(meta: VideoMeta) -> tuple[date | None, bool]:
    """投稿日（JST）と、動画 ID から換算したか。create_time が 0 のときだけ換算する。"""
    if meta.create_time > 0:
        return datetime.fromtimestamp(meta.create_time, JST).date(), False
    m = _VIDEO_ID_RE.search(meta.url or "")
    if m is None:
        return None, False
    ts = int(m.group(1)) >> 32
    if ts < _ID_EPOCH_MIN or ts > int(datetime.now(JST).timestamp()) + 86_400:
        return None, False
    return datetime.fromtimestamp(ts, JST).date(), True


def _brand_display(b: BrandDetection) -> str:
    name = b.brand_name.strip()
    return "ロゴ（不明）" if not name or name == UNIDENTIFIED_LOGO else name


def brand_facts(
    meta: VideoMeta, a: VideoVSEOAnalysis | None, roster: Roster | None = None
) -> tuple[BrandFact, ...]:
    """ブランドごとにまとめる（区分は名簿。Gemini の brand_relation は使わない）。

    並びは 目立ち方（主役→目立つ→付随→背景）→ 合計秒の長い順 → 先に出た順。
    """
    if a is None:
        return ()
    roster = roster or Roster()
    groups: dict[str, list[BrandDetection]] = {}
    for b in a.brand_detections:
        groups.setdefault(norm(_brand_display(b)), []).append(b)
    out: list[tuple[tuple[int, float, float, int], BrandFact]] = []
    for order, dets in enumerate(groups.values()):
        name = _brand_display(dets[0])
        known = name != "ロゴ（不明）"
        relation = (
            roster.relation(name) if known else ("other" if roster.specified else "unspecified")
        )
        prominence = min((d.prominence for d in dets), key=lambda p: _PROMINENCE_RANK.get(p, 4))
        secs = sorted({s for d in dets for s in d.appear_sec})
        mention = known and (
            contains(meta.desc, f"@{name}")
            or any(contains(d.co_occurring_caption, f"@{name}") for d in dets)
        )
        cat_values = [getattr(d, "category_match", None) for d in dets]
        category: bool | None
        if relation in ("client", "competitor"):
            category = True
        elif any(v is True for v in cat_values):
            category = True
        elif any(v is False for v in cat_values):
            category = False
        else:
            category = None
        fact = BrandFact(
            name=name,
            relation=relation,
            prominence=prominence,
            first_sec=secs[0] if secs else None,
            last_sec=secs[-1] if secs else None,
            total_sec=round(sum(d.total_screen_time_sec for d in dets), 1),
            in_telop=known and any(contains(t.text, name) for t in a.telops),
            in_caption=known and contains(meta.desc, name),
            sponsored=any(d.is_intentional == "likely_sponsored" for d in dets) or bool(mention),
            category_match=category,
        )
        key = (
            _PROMINENCE_RANK.get(prominence, 4),
            -fact.total_sec,
            fact.first_sec if fact.first_sec is not None else 1e9,
            order,
        )
        out.append((key, fact))
    return tuple(f for _k, f in sorted(out, key=lambda x: x[0]))


def detect_pr(meta: VideoMeta, a: VideoVSEOAnalysis | None = None) -> tuple[bool, str]:
    """タイアップ表記の有無と根拠（キャプション全文の #PR 等・@ブランド・提供の可能性）。"""
    parts: list[str] = []
    if a is not None:
        for b in a.brand_detections:
            name = _brand_display(b)
            if name != "ロゴ（不明）" and contains(meta.desc, f"@{name}"):
                parts.append(f"@{name}")
    tags = [m.group(1) for m in _PR_TAG_RE.finditer(fold(meta.desc))]
    parts.extend("#PR" if t == "pr" else f"#{t}" for t in dict.fromkeys(tags))
    evidence = f"キャプション {' '.join(dict.fromkeys(parts))}" if parts else ""
    if a is not None:
        sponsored = [
            _brand_display(b) for b in a.brand_detections if b.is_intentional == "likely_sponsored"
        ]
        if sponsored:
            ai = f"AI判定: 提供の可能性（{'・'.join(dict.fromkeys(sponsored))}）"
            evidence = f"{evidence}・{ai}" if evidence else ai
    return bool(evidence), evidence


def cta_kind(types: Sequence[str], text: str) -> str:
    """CTA の型: 文言があれば文言から決め、無ければ Gemini の cta_type の先頭。"""
    body = norm(text)
    for kind, words in _CTA_TEXT_KIND:
        if any(norm(w) in body for w in words):
            return kind
    return types[0] if types else "other"


def valid_cta(a: VideoVSEOAnalysis) -> tuple[str, str, float | None] | None:
    """動画内の CTA（文言か秒があるものだけ）。文言も秒も無ければ無効（None）。"""
    text = (a.cta_text or "").strip()
    if not text and a.cta_sec is None:
        return None
    return cta_kind(a.cta_type, text), text, a.cta_sec


def caption_ctas(desc: str) -> tuple[str, ...]:
    """キャプション内の呼びかけの型（保存・フォロー・コメント・プロフィール誘導）。"""
    body = fold(desc)
    return tuple(kind for kind, pat in _CAPTION_CTA if pat.search(body))


def _numeric_claims(
    a: VideoVSEOAnalysis | None, desc: str
) -> tuple[tuple[str, float | None, str], ...]:
    out: list[tuple[str, float | None, str]] = []
    if a is not None:
        for t in sorted(a.telops, key=lambda x: x.sec):
            if NUMERIC_CLAIM_RE.search(fold(t.text)):
                out.append(("telop", t.sec, t.text.strip()))
    body = unicodedata.normalize("NFKC", desc or "")
    for m in NUMERIC_CLAIM_RE.finditer(body):
        lo = max(0, m.start() - _CAPTION_CONTEXT)
        hi = min(len(body), m.end() + _CAPTION_CONTEXT)
        out.append(("caption", None, " ".join(body[lo:hi].split())))
    return tuple(out[:_MAX_NUMERIC_CLAIMS])


def _major_position(a: VideoVSEOAnalysis) -> str:
    counts = Counter(t.position for t in a.telops if t.text.strip())
    if not counts:
        return "unknown"
    top = counts.most_common()
    return top[0][0]


# ── 1 本の事実 ─────────────────────────────────────────────────────────


def is_watched(video: AnalyzedVideo) -> bool:
    return video.analysis is not None and video.error is None


def video_facts(
    v: AnalyzedVideo,
    query: str,
    roster: Roster | None = None,
    *,
    terms: Sequence[str] | None = None,
) -> VideoFacts:
    """1 本の事実。analysis が無い（失敗）ときもメタの事実（KW のキャプション層・PR）は出す。"""
    meta = v.meta
    a = v.analysis
    words = list(terms) if terms is not None else query_terms(query)
    dur = duration_of(meta, a)
    telops = sorted((t for t in (a.telops if a else []) if t.text.strip()), key=lambda t: t.sec)
    posted, estimated = posted_date(meta)
    pr, pr_evidence = detect_pr(meta, a)
    cta = valid_cta(a) if a is not None else None
    dropped: tuple[str, ...] = ()
    if a is not None and cta is None and a.cta_type:
        dropped = tuple(dict.fromkeys(a.cta_type))
    roles = infer_roles(a) if a is not None else []
    return VideoFacts(
        rank=meta.rank,
        author=meta.author,
        followers=meta.follower_count,
        posted_at=posted,
        posted_estimated=estimated,
        plays=meta.play_count,
        save_rate=round(meta.save_rate(), 2),
        shares=meta.share_count,
        duration_sec=dur,
        orientation=orientation_of(v.frames, v.cover_data_uri),
        telop_count=len(telops),
        telops_per_sec=round(len(telops) / dur, 2) if dur > 0 else 0.0,
        telop_position_major=_major_position(a) if a is not None else "unknown",
        first_telop_sec=telops[0].sec if telops else None,
        opening_telops=tuple((t.sec, t.text.strip()) for t in telops if t.sec <= OPENING_SEC),
        kw=kw_hits(meta, a, words),
        qty_telops=tuple((t.sec, t.text.strip()) for t in telops if QTY_RE.search(fold(t.text))),
        qty_in_caption=QTY_RE.search(fold(meta.desc)) is not None,
        numeric_claims=_numeric_claims(a, meta.desc),
        brands=brand_facts(meta, a, roster),
        pr=pr,
        pr_evidence=pr_evidence,
        cta_in_video=cta,
        cta_dropped=dropped,
        cta_in_caption=caption_ctas(meta.desc),
        narration=bool(a and a.has_narration),
        music_title=meta.music_title,
        roles_inferred=not roles or any(inferred for _role, inferred in roles),
        result_first_sec=getattr(a, "result_first_sec", None) if a is not None else None,
        hook_type=a.hook_type if a is not None else "",
        desc=meta.desc,
        watched=is_watched(v),
    )


def all_facts(
    videos: Sequence[AnalyzedVideo], query: str, roster: Roster | None = None
) -> list[VideoFacts]:
    """順位の順の VideoFacts（失敗した動画も含む。横断の母数は watched だけ）。"""
    return [video_facts(v, query, roster) for v in sorted(videos, key=lambda x: x.meta.rank)]


def _watched(facts: Iterable[VideoFacts]) -> list[VideoFacts]:
    return sorted((f for f in facts if f.watched), key=lambda f: f.rank)


# ── 横断の特徴 ─────────────────────────────────────────────────────────


def _feature(
    fid: str, label: str, ranks: Iterable[int], n: int, board_rate: tuple[int, int] | None = None
) -> Feature:
    uniq = tuple(sorted(set(ranks)))
    return Feature(fid, label, uniq, n, tier(len(uniq), n), board_rate)


def _board_rate(board: Sequence[VideoMeta], hit: Callable[[VideoMeta], bool]) -> tuple[int, int]:
    return sum(1 for m in board if hit(m)), len(board)


def category_known(facts: Iterable[VideoFacts]) -> bool:
    """「カテゴリの商品か」を判定できるか（名簿か v3 の欄がある）。"""
    return any(b.category_match is not None for f in facts for b in f.brands)


def feature_table(
    facts: Sequence[VideoFacts], board: Sequence[VideoMeta], query: str
) -> list[Feature]:
    """観測した特徴の表（1 本以上で観測したものだけ）。段階は tier(c, n)。"""
    watched = _watched(facts)
    n = len(watched)
    if n == 0:
        return []
    terms = query_terms(query)
    out: list[Feature] = []

    def add(
        fid: str, label: str, ranks: Iterable[int], rate: tuple[int, int] | None = None
    ) -> None:
        feature = _feature(fid, label, ranks, n, rate)
        if feature.count:
            out.append(feature)

    add(
        "first_telop_0s",
        "最初のテロップが0秒台",
        [
            f.rank
            for f in watched
            if f.first_telop_sec is not None and f.first_telop_sec < FIRST_TELOP_ZERO_SEC
        ],
    )
    for term in terms:

        def has(f: VideoFacts, layer: str, match: str, t: str = term) -> bool:
            return f.has_kw(t, layer, match)

        add(
            f"kw_telop_3s:{term}",
            f"「{term}」のテロップが3秒以内",
            [
                f.rank
                for f in watched
                if any(
                    s <= OPENING_SEC
                    for h in f.kw
                    if h.term == term and h.layer == "telop" and h.match == "exact"
                    for s in h.secs
                )
            ],
        )
        add(
            f"kw_telop:{term}",
            f"テロップに「{term}」",
            [f.rank for f in watched if has(f, "telop", "exact")],
        )
        add(
            f"kw_telop_syn:{term}",
            f"テロップに「{term}」の言い換え（照合済み）",
            [f.rank for f in watched if has(f, "telop", "synonym")],
        )
        add(
            f"kw_caption:{term}",
            f"キャプションに「{term}」",
            [f.rank for f in watched if has(f, "caption", "exact")],
            _board_rate(board, _desc_has(term)),
        )
        add(
            f"kw_hashtag:{term}",
            f"ハッシュタグに「{term}」",
            [f.rank for f in watched if has(f, "hashtag", "exact")],
            _board_rate(board, _tag_has(term)),
        )
        add(
            f"kw_speech:{term}",
            f"発話に「{term}」（AI聞き取り・未照合）",
            [f.rank for f in watched if any(h.term == term and h.layer == "speech" for h in f.kw)],
        )
    add("qty_telop", "分量をテロップに出す", [f.rank for f in watched if f.qty_telops])
    add("qty_caption", "分量をキャプションに載せる", [f.rank for f in watched if f.qty_in_caption])
    add(
        "qty_anywhere",
        "分量をテロップかキャプションに載せる",
        [f.rank for f in watched if f.qty_anywhere],
    )
    add("narration", "語りあり", [f.rank for f in watched if f.narration])
    for hook in dict.fromkeys(f.hook_type for f in watched):
        add(
            f"hook:{hook}",
            f"フックの型が{HOOK_LABEL.get(hook, HOOK_LABEL['other'])}",
            [f.rank for f in watched if f.hook_type == hook],
        )
    add("cta_video", "動画内のCTA（文言か秒あり）", [f.rank for f in watched if f.cta_in_video])
    for kind in dict.fromkeys(f.cta_in_video[0] for f in watched if f.cta_in_video):
        add(
            f"cta:{kind}",
            f"動画内のCTAが{CTA_KIND_LABEL.get(kind, kind)}",
            [f.rank for f in watched if f.cta_in_video and f.cta_in_video[0] == kind],
        )
    for kind in dict.fromkeys(k for f in watched for k in f.cta_in_caption):
        add(
            f"cta_caption:{kind}",
            f"キャプションで{CTA_KIND_LABEL.get(kind, kind)}の呼びかけ",
            [f.rank for f in watched if kind in f.cta_in_caption],
        )
    add(
        "pr",
        "タイアップ表記",
        [f.rank for f in watched if f.pr],
        _board_rate(board, lambda m: detect_pr(m)[0]),
    )
    if category_known(watched):
        add(
            "brand_category_prominent",
            "商材カテゴリの商品が主役か目立つ大きさで映る",
            [f.rank for f in watched if any(b.prominent and b.category_match for b in f.brands)],
        )
    for orient in ("portrait", "landscape"):
        add(
            f"orientation:{orient}",
            f"{ORIENTATION_LABEL[orient]}の動画",
            [f.rank for f in watched if f.orientation == orient],
        )
    add(
        "result_5s",
        "5秒以内に完成品",
        [f.rank for f in watched if f.result_first_sec is not None and f.result_first_sec <= 5.0],
    )
    return out


def _hashtag_meta_hit(meta: VideoMeta, term: str) -> bool:
    return any(h.layer == "hashtag" for h in kw_hits(meta, None, [term]))


def _desc_has(term: str) -> Callable[[VideoMeta], bool]:
    """キャプション全文に語があるか（上位ボードの率を数える判定）。"""

    def hit(meta: VideoMeta) -> bool:
        return contains(meta.desc, term)

    return hit


def _tag_has(term: str) -> Callable[[VideoMeta], bool]:
    """ハッシュタグに語があるか（取得したハッシュタグ・無ければキャプションの #…語）。"""

    def hit(meta: VideoMeta) -> bool:
        return _hashtag_meta_hit(meta, term)

    return hit


def cta_consensus(facts: Sequence[VideoFacts]) -> list[tuple[str, tuple[int, ...]]]:
    """動画内 CTA の型のうち、多数派（ceil(0.6n) 本以上）のものだけ。無ければ空。"""
    watched = _watched(facts)
    n = len(watched)
    counts: dict[str, list[int]] = {}
    for f in watched:
        if f.cta_in_video:
            counts.setdefault(f.cta_in_video[0], []).append(f.rank)
    return [
        (kind, tuple(ranks))
        for kind, ranks in counts.items()
        if n and tier(len(ranks), n) != TIER_CASE
    ]


def kw_matrix(facts: Sequence[VideoFacts], board: Sequence[VideoMeta], query: str) -> list[KwRow]:
    """拾われる条件（語×層・完全一致と言い換えを分ける・0 本も出す）。"""
    watched = _watched(facts)
    n = len(watched)
    rows: list[KwRow] = []
    for term in query_terms(query):
        for layer in ("telop", "caption", "hashtag", "speech"):
            exact = tuple(f.rank for f in watched if f.has_kw(term, layer, "exact"))
            syn = tuple(f.rank for f in watched if f.has_kw(term, layer, "synonym"))
            rate: tuple[int, int] | None = None
            if layer == "caption":
                rate = _board_rate(board, _desc_has(term)) if board else None
            elif layer == "hashtag":
                rate = _board_rate(board, _tag_has(term)) if board else None
            rows.append(KwRow(term, layer, exact, syn, n, layer != "speech", rate))
    return rows


def _dist(values: Sequence[float]) -> Distribution | None:
    vals = [v for v in values if v > 0]
    if not vals:
        return None
    return Distribution(
        median=round(float(statistics.median(vals)), 2),
        min=round(min(vals), 2),
        max=round(max(vals), 2),
    )


def summary_band(facts: Sequence[VideoFacts]) -> SummaryBand:
    """尺（meta）と 1 秒あたりのテロップの分布（全 n 本）・語りの本数。"""
    watched = _watched(facts)
    return SummaryBand(
        n=len(watched),
        duration=_dist([f.duration_sec for f in watched]),
        telops_per_sec=_dist([f.telops_per_sec for f in watched]),
        narration_ranks=tuple(f.rank for f in watched if f.narration),
    )


# ── 最も見られた 1 本・外れ値 ──────────────────────────────────────────────


def best_video(facts: Sequence[VideoFacts]) -> tuple[int, list[str]]:
    """再生→保存率→シェアの順で最大の 1 本と、それが最大だった指標の名前。無ければ (0, [])。"""
    watched = _watched(facts)
    if not watched:
        return 0, []
    best = max(watched, key=lambda f: (f.plays, f.save_rate, f.shares, -f.rank))
    metrics: dict[str, list[float]] = {
        "再生": [float(f.plays) for f in watched],
        "保存率": [f.save_rate for f in watched],
        "シェア": [float(f.shares) for f in watched],
    }
    i = watched.index(best)
    names = [label for label, vals in metrics.items() if max(vals) > 0 and vals[i] == max(vals)]
    return best.rank, names


def fmt_man(n: float) -> str:
    """2.05万・27.4万・86万 の形（有効数字 3 桁）。1 万未満は 3 桁区切り。"""
    if n >= 10_000:
        return f"{n / 10_000:.3g}万"
    return f"{int(n):,}"


def outliers(facts: Sequence[VideoFacts]) -> list[tuple[int, list[str]]]:
    """事実だけの外れ値（向きが少数派・尺が長い・再生が少ない・投稿が古い）。順位の順。"""
    watched = _watched(facts)
    n = len(watched)
    if n < 3:
        return []
    reasons: dict[int, list[str]] = {}
    known = [f for f in watched if f.orientation != "unknown"]
    counts = Counter(f.orientation for f in known)
    if len(counts) >= 2:
        for f in known:
            c = counts[f.orientation]
            if c * 2 < len(known):
                label = ORIENTATION_LABEL[f.orientation]
                reasons.setdefault(f.rank, []).append(f"{label}（{len(known)}本中{c}本）")
    durs = [f.duration_sec for f in watched if f.duration_sec > 0]
    if durs:
        med = statistics.median(durs)
        for f in watched:
            if med > 0 and f.duration_sec > med * OUTLIER_LONG_RATIO:
                reasons.setdefault(f.rank, []).append(
                    f"{f.duration_sec:.0f}秒（中央値{med:.0f}秒の{f.duration_sec / med:.1f}倍）"
                )
    plays = [f.plays for f in watched if f.plays > 0]
    if plays:
        med_p = statistics.median(plays)
        for f in watched:
            if med_p > 0 and f.plays < med_p * OUTLIER_LOW_PLAYS_RATIO:
                ratio = "1割未満" if f.plays < med_p * 0.1 else f"{f.plays / med_p:.1f}倍"
                reasons.setdefault(f.rank, []).append(
                    f"再生{fmt_man(f.plays)}（{n}本の中央値{fmt_man(med_p)}の{ratio}）"
                )
    dated = [f for f in watched if f.posted_at is not None]
    if len(dated) >= 3:
        ordered = sorted(dated, key=lambda f: f.posted_at or date.max)
        oldest, second = ordered[0], ordered[1]
        days = sorted((f.posted_at - date.min).days for f in dated if f.posted_at)
        med_day = date.min + timedelta(days=int(statistics.median(days)))
        if (
            oldest.posted_at is not None
            and second.posted_at is not None
            and oldest.posted_at < second.posted_at
            and (med_day - oldest.posted_at).days >= OUTLIER_OLD_DAYS
        ):
            mark = "・換算" if oldest.posted_estimated else ""
            reasons.setdefault(oldest.rank, []).append(
                f"{oldest.posted_at.year}年投稿（最古{mark}）"
            )
    return [(rank, reasons[rank]) for rank in sorted(reasons)]


# ── 共通する構成の型 ─────────────────────────────────────────────────────


def stage_bounds(duration: float) -> list[tuple[float, float]]:
    """0〜3秒 / 3〜10秒 / 10秒〜(尺−10秒) / 最後の10秒（短い動画は重ならないよう詰める）。"""
    d = max(0.0, duration)
    b1 = min(_STAGE_HEAD_END, d)
    b2 = min(_STAGE_BUILD_END, d)
    b3 = max(b2, d - _STAGE_TAIL)
    return [(0.0, b1), (b1, b2), (b2, b3), (b3, d)]


def _in_stage(sec: float, i: int, bounds: list[tuple[float, float]]) -> bool:
    """段 i に入るか。最初の段は 0〜3 秒の両端を含み（3 秒ちょうどは冒頭）、以降は (始め, 終わり]。

    最後の段は尺より後の秒（Gemini の秒は実尺より長いことがある）も含める。
    """
    s, e = bounds[i]
    if i == 0:
        return 0.0 <= sec <= e and e > 0
    if i == len(bounds) - 1:
        return sec > s
    return s < sec <= e


def _stage_of(sec: float, bounds: list[tuple[float, float]]) -> int | None:
    if not bounds or bounds[-1][1] <= 0:
        return None
    sec = max(0.0, sec)
    for i in range(len(bounds)):
        if _in_stage(sec, i, bounds):
            return i
    return None


def _main_role(a: VideoVSEOAnalysis, start: float, end: float) -> tuple[str | None, bool]:
    scenes = sorted(a.scenes, key=lambda sc: (sc.start_sec, sc.end_sec))
    secs: dict[str, float] = {}
    inferred_of: dict[str, bool] = {}
    for sc, (role, inferred) in zip(scenes, infer_roles(a), strict=True):
        overlap = min(end, max(sc.end_sec, sc.start_sec)) - max(start, sc.start_sec)
        if overlap > 0:
            secs[role] = secs.get(role, 0.0) + overlap
            inferred_of[role] = inferred_of.get(role, False) or inferred
    if not secs:
        return None, True
    role = max(secs, key=lambda r: secs[r])  # 同じ秒なら先に出た役割（dict の順）
    return role, inferred_of[role]


def _events(
    f: VideoFacts, bounds: list[tuple[float, float]], *, use_category: bool
) -> list[set[str]]:
    """段ごとの出来事。use_category は「カテゴリの商品か」を判定できる（名簿か v3）とき。"""
    per: list[set[str]] = [set() for _ in bounds]

    def mark(key: str, sec: float | None) -> None:
        if sec is None:
            return
        i = _stage_of(sec, bounds)
        if i is not None:
            per[i].add(key)

    mark("first_telop", f.first_telop_sec)
    mark("kw_telop", f.kw_first_telop_sec)
    for sec, _text in f.qty_telops:
        mark("qty_telop", sec)
    prominent = [
        b.first_sec
        for b in f.brands
        if b.prominent and (b.category_match if use_category else True) and b.first_sec is not None
    ]
    mark("brand_first", min(prominent) if prominent else None)
    mark("result_first", f.result_first_sec)
    if f.cta_in_video is not None:
        mark("cta", f.cta_in_video[2])
    return per


def template(videos: Sequence[AnalyzedVideo], facts: Sequence[VideoFacts]) -> list[StageRow]:
    """段ごとに、役割の本数・出来事の本数・代表例を数える（段階は tier）。"""
    by_rank = {f.rank: f for f in _watched(facts)}
    pairs = [
        (v, by_rank[v.meta.rank])
        for v in sorted(videos, key=lambda x: x.meta.rank)
        if v.analysis is not None and v.meta.rank in by_rank
    ]
    n = len(pairs)
    if n == 0:
        return []
    use_category = category_known(f for _v, f in pairs)
    obs: list[list[StageObs]] = [[] for _ in STAGE_LABELS]
    for v, f in pairs:
        a = v.analysis
        assert a is not None
        bounds = stage_bounds(f.duration_sec)
        stage_events = _events(f, bounds, use_category=use_category)
        telops = sorted((t for t in a.telops if t.text.strip()), key=lambda t: t.sec)
        for i, (s, e) in enumerate(bounds):
            inside = [(t.sec, t.text.strip()) for t in telops if _stage_of(t.sec, bounds) == i]
            role, inferred = _main_role(a, s, e) if e > s else (None, True)
            obs[i].append(
                StageObs(
                    rank=f.rank,
                    start=s,
                    end=e,
                    role=role,
                    role_inferred=inferred,
                    telops=tuple(inside[:2]),
                    events=tuple(k for k in EVENT_LABEL if k in stage_events[i]),
                )
            )
    plays = {f.rank: f.plays for _v, f in pairs}
    rows: list[StageRow] = []
    for i, label in enumerate(STAGE_LABELS):
        stage_obs = obs[i]
        role_ranks: dict[str, list[int]] = {}
        for o in stage_obs:
            if o.role:
                role_ranks.setdefault(o.role, []).append(o.rank)
        roles = tuple(
            (role, tuple(ranks), tier(len(ranks), n))
            for role, ranks in sorted(role_ranks.items(), key=lambda kv: (-len(kv[1]), kv[1][0]))
        )
        events = tuple(
            (key, ranks, tier(len(ranks), n))
            for key in EVENT_LABEL
            if (ranks := tuple(o.rank for o in stage_obs if key in o.events))
        )
        with_telop = sorted(
            (o for o in stage_obs if o.telops), key=lambda o: (-plays.get(o.rank, 0), o.rank)
        )
        examples = tuple((o.rank, o.telops[0][0], o.telops[0][1]) for o in with_telop[:2])
        rows.append(
            StageRow(
                index=i,
                label=label,
                per_video=tuple(stage_obs),
                roles=roles,
                events=events,
                examples=examples,
                roles_inferred=any(o.role_inferred for o in stage_obs),
            )
        )
    return rows


def role_label(role: str | None) -> str:
    return ROLE_LABEL.get(role or "other", ROLE_LABEL["other"])


# ── 検索面の地図 ────────────────────────────────────────────────────────


def surface_map(
    board: Sequence[VideoMeta], angle_terms: Iterable[str] = (), *, query: str = ""
) -> SurfaceMap:
    """上位ボード（メタだけ）の集計。切り口は LLM が語を出し、コードが本数を数える。"""
    size = len(board)
    by_author: dict[str, list[int]] = {}
    for m in board:
        if m.author:
            by_author.setdefault(m.author, []).append(m.rank)
    creators = tuple(
        (author, tuple(sorted(ranks)))
        for author, ranks in sorted(by_author.items(), key=lambda kv: (-len(kv[1]), min(kv[1])))
        if len(ranks) >= 2
    )
    angles_raw = [
        (term, tuple(m.rank for m in board if contains(m.desc, term)))
        for term in dict.fromkeys(t.strip() for t in angle_terms if t and t.strip())
    ]
    angles = tuple(
        sorted(((t, r) for t, r in angles_raw if r), key=lambda x: (-len(x[1]), x[1][0]))
    )
    pr_ranks = tuple(m.rank for m in board if detect_pr(m)[0])
    rated = [(m.rank, m.save_rate()) for m in board if m.play_count > 0]
    top_save = tuple(
        (rank, round(rate, 2)) for rank, rate in sorted(rated, key=lambda x: (-x[1], x[0]))[:3]
    )
    median_save = round(float(statistics.median([r for _k, r in rated])), 2) if rated else None
    years = Counter(d.year for d, _est in (posted_date(m) for m in board) if d is not None)
    kw_rates: list[tuple[str, str, int, int]] = []
    for term in query_terms(query):
        cap_hits, _ = _board_rate(board, _desc_has(term))
        tag_hits, _ = _board_rate(board, _tag_has(term))
        kw_rates.append((term, "caption", cap_hits, size))
        kw_rates.append((term, "hashtag", tag_hits, size))
    return SurfaceMap(
        size=size,
        creators=creators,
        angles=angles,
        pr_ranks=pr_ranks,
        top_save=top_save,
        median_save_rate=median_save,
        years=tuple(sorted(years.items())),
        kw_rates=tuple(kw_rates),
    )


__all__ = [
    "CTA_KIND_LABEL",
    "EVENT_LABEL",
    "JST",
    "ORIENTATION_LABEL",
    "POSITION_LABEL",
    "PROMINENCE_LABEL",
    "QTY_RE",
    "STAGE_LABELS",
    "BrandFact",
    "Distribution",
    "Feature",
    "KwHit",
    "KwRow",
    "Roster",
    "StageObs",
    "StageRow",
    "SummaryBand",
    "SurfaceMap",
    "VideoFacts",
    "all_facts",
    "best_video",
    "brand_facts",
    "caption_ctas",
    "category_known",
    "cta_consensus",
    "cta_kind",
    "detect_pr",
    "duration_of",
    "feature_table",
    "fmt_man",
    "jpeg_size",
    "kw_matrix",
    "orientation_of",
    "outliers",
    "posted_date",
    "role_label",
    "stage_bounds",
    "summary_band",
    "surface_map",
    "template",
    "tier",
    "valid_cta",
    "video_facts",
]
