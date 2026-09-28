"""検索上位チェックの 2 段目: 上位の動画を選び、中身の分析を決定的に数え、LLM の読みを照合する。

- 選ぶ: 1 段目の面（SurfacePost）から順位順に。検索し直さない（同じ上位を見る）。
- 数える: video_algorithm の 1 本ずつの分析（VideoVSEOAnalysis）を横断で数える（LLM を通さない）。
  分母は「動画を見て分析できた本数」。サムネだけの分析は集計に入れない（テロップ・構成・音を
  静止画から判定できないため）。n=5 の観測なので、保存率との関係は「保存率の高い 2 本に共通する
  こと」に留め、相関は出さない。
- 読む: 集計と 1 本ずつの一覧を LLM に渡し、入力に無い数字・実在しない順位を含む項目は捨てる
  （共通部品 _shared/grounding.py の NumberGrounder）。2 段目の本数は 0〜N（N≤10）に収まるので、
  1 段目のように小さい数（0〜10・90・100）を無条件に通すと「5/5本」「100%」がそのまま通って照合が
  効かない。2 段目は**常に許す数を使わず**、入力の値（項目名は除く）に現れる数字だけを許す。
  それでも「順位」1〜N と分母 N が必ず入力に入るので、本数の主張（「X/Y本」「N本中M本」「M本」
  「すべて」）は数字の照合だけでは止まらない（B5 レビュー指摘: 作り話の「5/5本」が通った）。
  そこで本数は ``CountGrounder`` が別に見る: 分母は分析できた本数、分子は**近くに書かれた項目**
  （冒頭テロップ・CTA・フックの型…）の集計の本数と一致するときだけ通す。項目が近くに無ければ
  いずれかの集計の本数と一致すること。「すべて」は近くの項目の本数が分母と同じときだけ通す。
  誇張語は言い換える。
"""

from __future__ import annotations

import json
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from teamagent.skills._shared.grounding import DropSink, NumberGrounder, tone_down
from teamagent.skills._shared.text_safety import safe_href, sanitize_llm_text
from teamagent.skills.search_surface_check.display import fmt_count
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    KwSurface,
    LabelCount,
    SearchSurfaceCheckOutput,
    SurfacePost,
    VideoDigest,
    VideoDigestConclusion,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo, VideoMeta, VideoVSEOAnalysis

# video_algorithm のレポート（report._HOOK_JP）と同じ呼び名にそろえる。
HOOK_LABEL: dict[str, str] = {
    "question": "問いかけ",
    "number": "数字",
    "shock": "衝撃",
    "visual": "ビジュアル",
    "pov": "POV",
    "dialogue": "会話",
    "problem": "問題提起",
    "other": "その他",
}
PACING_LABEL: dict[str, str] = {
    "slow": "ゆっくり",
    "moderate": "ふつう",
    "fast": "速い",
    "very_fast": "とても速い",
    "unknown": "不明",
}
# video_algorithm のシステムプロンプトが出す cta_type の語彙。
CTA_LABEL: dict[str, str] = {
    "save": "保存",
    "follow": "フォロー",
    "comment": "コメント",
    "visit": "来店",
    "buy": "購入",
    "link_bio": "プロフィールのリンク",
    "share": "シェア",
    "like": "いいね",
}
# 分析の失敗文言（video_algorithm の AnalyzedVideo.error）。サムネだけの縮退はこれで見分ける。
COVER_ONLY_ERROR = "動画取得失敗・サムネのみ軽量分析"
# 冒頭（フック）とみなす秒。video_algorithm のフック定義（0〜3 秒）と同じ。
OPENING_SEC = 3.0

_HEADLINE_MAX = 60
_TEXT_MAX = 180
_ROW_TEXT_MAX = 60
# 2 段目の照合で無条件に通す数（無し）。本数がすべて 0〜N に収まるため、小さい数を通すと
# 作り話の「5/5本」「上位5本すべて」「100%」を止められない（B5 レビュー指摘）。
STRICT_ALWAYS_ALLOWED: frozenset[str] = frozenset()
_TIKTOK_VIDEO_PATH = re.compile(r"/video/\d+")


def hook_label(hook_type: str) -> str:
    return HOOK_LABEL.get(hook_type, HOOK_LABEL["other"])


def cta_label(cta_type: str) -> str:
    return CTA_LABEL.get(cta_type, cta_type)


# ── 選ぶ ─────────────────────────────────────────────────────────────


def followup_surface(out: SearchSurfaceCheckOutput) -> KwSurface | None:
    """2 段目で見る面＝最初の KW の TikTok 面（無ければ None＝2 段目はしない）。"""
    if not out.keywords:
        return None
    first = out.keywords[0]
    return next(
        (s for s in out.surfaces if s.keyword == first and s.platform == "tiktok"),
        None,
    )


def is_analyzable(post: SurfacePost) -> bool:
    """動画として取得・分析できる投稿か（尺あり・URL あり・カルーセル/画像投稿でない）。

    カルーセル/画像投稿は TikTok 側に video が無く尺 0 で届き、URL も /photo/ になる
    （video_algorithm が深掘り対象から外す条件と同じ考え方）。
    """
    if post.platform != "tiktok" or post.duration_sec <= 0:
        return False
    href = safe_href(post.url)
    return bool(href) and _TIKTOK_VIDEO_PATH.search(post.url) is not None


def select_followup_videos(out: SearchSurfaceCheckOutput, max_videos: int) -> list[SurfacePost]:
    """最初の KW の TikTok 面から、順位順に分析できる動画を最大 ``max_videos`` 本選ぶ。"""
    surface = followup_surface(out)
    if surface is None or max_videos <= 0:
        return []
    ranked = sorted(surface.posts, key=lambda p: p.rank)
    return [p for p in ranked if is_analyzable(p)][:max_videos]


def post_to_meta(post: SurfacePost) -> VideoMeta:
    """1 段目の投稿から video_algorithm の VideoMeta を作る（検索し直さない）。"""
    plays = post.play_count
    engagement = (
        (post.like_count + post.comment_count + post.share_count + post.save_count) / plays * 100
        if plays
        else 0.0
    )
    return VideoMeta(
        rank=post.rank,
        url=post.url,
        author=post.author,
        follower_count=post.author_followers,
        desc=post.desc,
        play_count=plays,
        digg_count=post.like_count,
        comment_count=post.comment_count,
        share_count=post.share_count,
        collect_count=post.save_count,
        engagement_rate=round(engagement, 2),
        cover_url=post.thumb_url or None,
        duration_sec=float(post.duration_sec),
    )


# ── 数える ───────────────────────────────────────────────────────────


def is_watched(video: AnalyzedVideo) -> bool:
    """動画そのものを見て分析できたか（サムネだけの分析・失敗は False）。"""
    return video.analysis is not None and video.error is None


def is_cover_only(video: AnalyzedVideo) -> bool:
    return video.analysis is not None and video.error is not None


def has_opening_telop(a: VideoVSEOAnalysis) -> bool:
    return a.hook_has_caption or any(t.sec <= OPENING_SEC and t.text.strip() for t in a.telops)


def has_telop_kw(a: VideoVSEOAnalysis) -> bool:
    return a.kw_in_telop() or any(k.matched and k.layer == "telop" for k in a.keyword_matches)


def has_spoken_kw(a: VideoVSEOAnalysis) -> bool:
    return any(k.matched for k in a.spoken_keywords) or any(
        k.matched and k.layer in ("narration", "dialogue") for k in a.keyword_matches
    )


def duration_of(video: AnalyzedVideo) -> float:
    a = video.analysis
    if a is not None and a.duration_sec > 0:
        return a.duration_sec
    return video.meta.duration_sec


def _counts(labels: list[str]) -> list[LabelCount]:
    counter = Counter(labels)
    order = {label: i for i, label in enumerate(labels)}  # 同数は先に出た順（＝上位の順）
    return [
        LabelCount(label=label, count=count)
        for label, count in sorted(counter.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    ]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    m = statistics.median(values)
    return round(float(m), 1)


def _save_rate(video: AnalyzedVideo) -> float:
    return video.meta.save_rate() if video.meta.collect_count > 0 else 0.0


def _common_traits(a: VideoVSEOAnalysis, b: VideoVSEOAnalysis) -> list[str]:
    traits: list[str] = []
    if a.hook_type == b.hook_type:
        traits.append(f"フックが{hook_label(a.hook_type)}")
    if has_opening_telop(a) and has_opening_telop(b):
        traits.append("冒頭にテロップ")
    if has_telop_kw(a) and has_telop_kw(b):
        traits.append("テロップに KW")
    if has_spoken_kw(a) and has_spoken_kw(b):
        traits.append("発話に KW")
    if a.pacing == b.pacing and a.pacing != "unknown":
        traits.append(f"テンポが{PACING_LABEL.get(a.pacing, a.pacing)}")
    shared_cta = [c for c in a.cta_type if c in b.cta_type]
    if shared_cta:
        traits.append("CTA: " + "・".join(cta_label(c) for c in dict.fromkeys(shared_cta)))
    if a.has_narration and b.has_narration:
        traits.append("ナレーションあり")
    return traits


def digest_videos(
    videos: list[AnalyzedVideo], *, keyword: str, requested: int, reserved: int
) -> VideoDigest:
    """横断で数える（LLM を通さない）。videos は順位順。"""
    watched = [v for v in videos if is_watched(v)]
    analyses = [v.analysis for v in watched if v.analysis is not None]
    cta_types: list[str] = []
    for a in analyses:
        cta_types.extend(dict.fromkeys(a.cta_type))  # 1 本の中の重複は 1 回
    digest = VideoDigest(
        keyword=keyword,
        requested=requested,
        reserved=reserved,
        watched=len(watched),
        watched_ranks=[v.meta.rank for v in watched],
        cover_only_ranks=[v.meta.rank for v in videos if is_cover_only(v)],
        failed_ranks=[v.meta.rank for v in videos if v.analysis is None],
        hook_types=_counts([hook_label(a.hook_type) for a in analyses]),
        opening_telop=sum(1 for a in analyses if has_opening_telop(a)),
        telop_kw=sum(1 for a in analyses if has_telop_kw(a)),
        spoken_kw=sum(1 for a in analyses if has_spoken_kw(a)),
        median_duration_sec=_median([duration_of(v) for v in watched if duration_of(v) > 0]),
        median_cut_count=_median([float(a.cut_count) for a in analyses if a.cut_count is not None]),
        pacing=_counts(
            [PACING_LABEL.get(a.pacing, a.pacing) for a in analyses if a.pacing != "unknown"]
        ),
        cta=sum(1 for a in analyses if a.has_cta()),
        cta_types=_counts([cta_label(c) for c in cta_types]),
        narration=sum(1 for a in analyses if a.has_narration),
        trending_sound=sum(1 for a in analyses if a.is_trending_sound == "yes"),
        median_coherence=_median(
            [float(a.message_coherence) for a in analyses if a.message_coherence is not None]
        ),
    )
    by_save = sorted(
        (v for v in watched if _save_rate(v) > 0), key=lambda v: (-_save_rate(v), v.meta.rank)
    )
    if len(by_save) >= 2 and by_save[0].analysis is not None and by_save[1].analysis is not None:
        top = sorted(by_save[:2], key=lambda v: v.meta.rank)
        digest.save_top_ranks = [v.meta.rank for v in top]
        digest.save_top_common = _common_traits(by_save[0].analysis, by_save[1].analysis)
    return digest


def rule_digest_conclusion(digest: VideoDigest) -> VideoDigestConclusion | None:
    """LLM が使えないときの見出し（集計だけで言えること）。"""
    if digest.watched <= 0 or not digest.hook_types:
        return None
    top = digest.hook_types[0]
    headline = (
        f"上位{digest.watched}本のフックは{top.label}が最多（{top.count}本）。"
        f"冒頭テロップは{digest.opening_telop}/{digest.watched}本"
    )
    return VideoDigestConclusion(headline=headline, generated_by="rule")


# ── 読む（LLM）と照合 ───────────────────────────────────────────────────


def digest_payload(digest: VideoDigest) -> dict[str, Any]:
    """LLM に渡す集計（日本語の項目名・割り算をさせない）。"""
    n = digest.watched
    payload: dict[str, Any] = {
        "動画を見て分析できた本数": n,
        "フックの型": [{"型": h.label, "本数": h.count} for h in digest.hook_types],
        "冒頭3秒にテロップがある本数": digest.opening_telop,
        "テロップに検索KWが出る本数": digest.telop_kw,
        "発話に検索KWが出る本数": digest.spoken_kw,
        "テンポ": [{"テンポ": p.label, "本数": p.count} for p in digest.pacing],
        "CTAがある本数": digest.cta,
        "CTAの種類": [{"種類": c.label, "本数": c.count} for c in digest.cta_types],
        "ナレーションがある本数": digest.narration,
        "流行の音源の本数": digest.trending_sound,
    }
    optional: dict[str, Any] = {
        "尺の中央値（秒）": digest.median_duration_sec,
        "カット数の中央値": digest.median_cut_count,
        "テロップ・本文・映像の一致度の中央値（0〜100）": digest.median_coherence,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    if digest.save_top_ranks:
        payload["保存率の高い2本の順位"] = digest.save_top_ranks
        payload["その2本に共通すること"] = digest.save_top_common
    return payload


def _clip(text: str, n: int = _ROW_TEXT_MAX) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:n]


def videos_payload(videos: list[AnalyzedVideo]) -> list[dict[str, Any]]:
    """1 本ずつの一覧（動画を見て分析できたものだけ）。"""
    rows: list[dict[str, Any]] = []
    for v in videos:
        a = v.analysis
        if a is None or not is_watched(v):
            continue
        row: dict[str, Any] = {
            "順位": v.meta.rank,
            "アカウント": v.meta.author,
            "フックの型": hook_label(a.hook_type),
            "フックの要旨": _clip(a.hook_summary),
            "主なメッセージ": _clip(a.main_message),
            "冒頭テロップ": has_opening_telop(a),
            "テロップにKW": has_telop_kw(a),
            "発話にKW": has_spoken_kw(a),
            "テンポ": PACING_LABEL.get(a.pacing, a.pacing),
            "CTA": [cta_label(c) for c in dict.fromkeys(a.cta_type)],
            "勝因": [_clip(w, 40) for w in a.win_factors[:3]],
            "保存・シェアの動機": _clip(a.save_share_motivation),
        }
        if duration_of(v) > 0:
            row["尺（秒）"] = round(duration_of(v))
        if a.cut_count is not None:
            row["カット数"] = a.cut_count
        if v.meta.play_count > 0 and v.meta.collect_count > 0:
            row["保存率%"] = round(v.meta.save_rate(), 1)
            row["再生"] = fmt_count(v.meta.play_count)
        rows.append(row)
    return rows


def _leaf_values(obj: Any) -> list[str]:
    """JSON にした入力の値だけ（項目名は除く）。項目名の数字（「冒頭3秒」など）を許さないため。"""
    if isinstance(obj, dict):
        return [v for value in obj.values() for v in _leaf_values(value)]
    if isinstance(obj, list):
        return [v for value in obj for v in _leaf_values(value)]
    if obj is None or isinstance(obj, bool):
        return []
    return [str(obj)]


# ── 本数の照合 ───────────────────────────────────────────────────────

# 「3/5本」「3/5」。日付（9/28）はこの読みの文に出てこない前提。
_FRACTION_RE = re.compile(r"(?<![\d.])(\d+)\s*/\s*(\d+)(?![\d.])(?:\s*本)?")
# 「5本中3本」「5本のうち3本」。
_OF_RE = re.compile(r"(?<![\d.])(\d+)\s*本\s*(?:中|のうち)\s*(\d+)\s*本")
# 「3本」（本文・本数などの熟語は除く）。
_COUNT_RE = re.compile(r"(?<![\d./])(\d+)\s*本(?![文数日当来格])")
# 「すべて」の主張。
_ALL_RE = re.compile(r"すべて|全て|全部|どの動画も|全\s*\d+\s*本")
# 「上位5本」「5本の」「5本とも」のように、分析した本数そのもの（範囲）を指す書き方。
_SCOPE_BEFORE = ("上位", "全", "計", "合計")
_SCOPE_AFTER = re.compile(r"\s*(?:の|中|とも|すべて|全て|全部|を分析|を視聴)")
# 数字の近くを見る幅（文字数）。句点・読点・改行で切る。
_WINDOW_BEFORE = 24
_WINDOW_AFTER = 16
_CLAUSE_BREAK = re.compile(r"[。！？!?\n、，,／]")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


@dataclass(frozen=True)
class CountTerm:
    """本数の主張の近くに書かれる項目（冒頭テロップ・CTA など）と、その集計の本数。"""

    pattern: re.Pattern[str]
    counts: frozenset[int]


def _terms(digest: VideoDigest) -> tuple[CountTerm, ...]:
    def t(pattern: str, counts: Iterable[int]) -> CountTerm:
        return CountTerm(re.compile(pattern), frozenset(counts))

    hook_counts = [h.count for h in digest.hook_types]
    cta_counts = [digest.cta, *(c.count for c in digest.cta_types)]
    terms = [
        t(r"冒頭.{0,8}テロップ|テロップ.{0,4}冒頭", [digest.opening_telop]),
        t(
            r"テロップ.{0,6}(?:KW|キーワード|検索語)|(?:KW|キーワード|検索語).{0,6}テロップ",
            [digest.telop_kw],
        ),
        t(
            r"(?:発話|話し|声|ナレーション).{0,6}(?:KW|キーワード|検索語)"
            r"|(?:KW|キーワード|検索語).{0,8}(?:発話|話し|声)",
            [digest.spoken_kw],
        ),
        t(r"テロップ", [digest.opening_telop, digest.telop_kw]),
        t(r"CTA|呼びかけ|促", cta_counts),
        t(r"ナレーション", [digest.narration]),
        t(r"(?:流行|トレンド|人気).{0,4}(?:音源|音楽|曲|サウンド)", [digest.trending_sound]),
        t(r"フック", hook_counts),
        t(r"テンポ", [p.count for p in digest.pacing]),
        t(r"保存率の高い|保存率が高い", [len(digest.save_top_ranks)]),
        t(r"サムネ", [len(digest.cover_only_ranks)]),
    ]
    for h in digest.hook_types:
        if h.label != HOOK_LABEL["other"]:
            terms.append(t(re.escape(h.label), [h.count]))
    for label in CTA_LABEL.values():
        n = next((c.count for c in digest.cta_types if c.label == label), 0)
        terms.append(t(re.escape(label) + r"を(?:促|呼びかけ|誘)", [n]))
    return tuple(terms)


@dataclass(frozen=True)
class CountGrounder:
    """数字の照合（NumberGrounder）に、本数の主張の照合を重ねる（2 段目の読み専用）。"""

    base: NumberGrounder
    watched: int
    denominators: frozenset[int]
    scope: frozenset[int]
    terms: tuple[CountTerm, ...]

    @property
    def always_allowed(self) -> frozenset[str]:
        return self.base.always_allowed

    @property
    def valid_ranks(self) -> frozenset[int] | None:
        return self.base.valid_ranks

    def filter_ranks(self, values: Any) -> list[int]:
        return self.base.filter_ranks(values)

    def stray(self, text: str) -> set[str]:
        return self.base.stray(text)

    @property
    def all_counts(self) -> frozenset[int]:
        out: set[int] = {self.watched}
        for term in self.terms:
            out |= term.counts
        return frozenset(out)

    def _near(self, text: str, start: int, end: int) -> frozenset[int] | None:
        """数字の近く（同じ節）に書かれた項目の本数。項目が無ければ None。"""
        before = text[max(0, start - _WINDOW_BEFORE) : start]
        after = text[end : end + _WINDOW_AFTER]
        before = _CLAUSE_BREAK.split(before)[-1]
        after = _CLAUSE_BREAK.split(after)[0]
        window = before + " " + after
        found: set[int] = set()
        hit = False
        for term in self.terms:
            if term.pattern.search(window):
                hit = True
                found |= term.counts
        return frozenset(found) if hit else None

    def _numerator_ok(self, text: str, value: int, start: int, end: int) -> bool:
        near = self._near(text, start, end)
        return value in (near if near is not None else self.all_counts)

    def count_issues(self, text: str) -> list[str]:
        """本数の主張のうち、集計と合わないもの（理由の文字列・本文は含めない）。"""
        norm = _norm(text)
        issues: list[str] = []
        taken: list[tuple[int, int]] = []
        for m in _OF_RE.finditer(norm):
            total, part = int(m.group(1)), int(m.group(2))
            taken.append(m.span())
            if total not in self.denominators or part > total:
                issues.append(f"{total}本中{part}本")
            elif not self._numerator_ok(norm, part, *m.span()):
                issues.append(f"{total}本中{part}本")
        for m in _FRACTION_RE.finditer(norm):
            if any(s <= m.start() < e for s, e in taken):
                continue
            part, total = int(m.group(1)), int(m.group(2))
            taken.append(m.span())
            if total not in self.denominators or part > total:
                issues.append(f"{part}/{total}")
            elif not self._numerator_ok(norm, part, *m.span()):
                issues.append(f"{part}/{total}")
        for m in _COUNT_RE.finditer(norm):
            if any(s <= m.start() < e for s, e in taken):
                continue
            value = int(m.group(1))
            near = self._near(norm, *m.span())
            if near is not None and value in near:
                continue
            head = norm[: m.start()]
            is_scope = value in self.scope and (
                head.rstrip().endswith(_SCOPE_BEFORE) or _SCOPE_AFTER.match(norm, m.end())
            )
            if is_scope or (near is None and value in self.all_counts):
                continue
            issues.append(f"{value}本")
        for m in _ALL_RE.finditer(norm):
            near = self._near(norm, *m.span())
            pool = near if near is not None else self.all_counts - {self.watched}
            if self.watched not in pool:
                issues.append("すべて")
        return issues

    def reason(self, text: str, *, deny: Iterable[str] = ()) -> str | None:
        parts: list[str] = []
        base = self.base.reason(text, deny=deny)
        if base:
            parts.append(base)
        issues = self.count_issues(text)
        if issues:
            parts.append("count:" + ",".join(dict.fromkeys(issues)))
        return ";".join(parts) or None

    def ok(self, text: str) -> bool:
        return self.reason(text) is None


def digest_grounder(
    digest: VideoDigest, videos: list[AnalyzedVideo], *, keyword: str
) -> CountGrounder:
    """2 段目の照合器: 入力の値に現れる数字・動画を見て分析できた順位・集計と合う本数だけを許す。"""
    values = _leaf_values(digest_payload(digest)) + _leaf_values(videos_payload(videos))
    base = NumberGrounder.from_inputs(
        *values,
        keyword,
        valid_ranks=digest.watched_ranks,
        always_allowed=STRICT_ALWAYS_ALLOWED,
    )
    denominators = {digest.watched}
    if digest.save_top_ranks:
        denominators.add(len(digest.save_top_ranks))
    return CountGrounder(
        base=base,
        watched=digest.watched,
        denominators=frozenset(denominators),
        scope=frozenset({digest.watched, digest.requested, digest.reserved}),
        terms=_terms(digest),
    )


def build_digest_prompt(
    template: str,
    *,
    keyword: str,
    client_name: str | None,
    digest: VideoDigest,
    videos: list[AnalyzedVideo],
) -> tuple[str, CountGrounder]:
    """プロンプトと、照合器（入力の値に現れる数字・実在する順位・集計と合う本数）を返す。"""
    facts_json = json.dumps(digest_payload(digest), ensure_ascii=False)
    videos_json = json.dumps(videos_payload(videos), ensure_ascii=False)
    prompt = template.format(
        keyword=keyword,
        client_name=client_name or "（指定なし）",
        facts_json=facts_json,
        videos_json=videos_json,
    )
    return prompt, digest_grounder(digest, videos, keyword=keyword)


def _parse(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def ground_digest_conclusion(
    raw: dict[str, Any],
    *,
    grounder: CountGrounder,
    on_drop: DropSink | None = None,
) -> VideoDigestConclusion | None:
    """LLM の出力を検査して採用する。

    入力に無い数字・実在しない順位（本文中の「N位」「#N」も）・集計と合わない本数を含む項目は
    丸ごと捨て、欄名と理由
    （数字・順位だけ・本文は渡さない）を ``on_drop`` へ渡す。ranks 欄は実在する順位だけに絞る。
    誇張語は言い換える。
    """

    def grounded(field: str, text: str) -> bool:
        why = grounder.reason(text)
        if why is not None:
            if on_drop is not None:
                on_drop(field, why)
            return False
        return True

    def point(field: str, value: Any) -> ConclusionPoint | None:
        if not isinstance(value, dict):
            return None
        text = tone_down(sanitize_llm_text(str(value.get("text") or "").strip(), max_len=_TEXT_MAX))
        if not text or not grounded(field, text):
            return None
        return ConclusionPoint(text=text, ranks=grounder.filter_ranks(value.get("ranks")))

    headline = tone_down(
        sanitize_llm_text(str(raw.get("headline") or "").strip(), max_len=_HEADLINE_MAX)
    )
    if headline and not grounded("headline", headline):
        headline = ""
    winning = point("winning", raw.get("winning"))
    save_reason = point("save_reason", raw.get("save_reason"))
    if not (headline or winning or save_reason):
        return None
    return VideoDigestConclusion(
        headline=headline,
        winning=winning,
        save_reason=save_reason,
        # 照合が実際に効いている（常に許す数が無く、順位も検査した）ときだけ「照合済み」と書く。
        grounded=not grounder.always_allowed and grounder.valid_ranks is not None,
    )


def conclude_digest(
    converse: Callable[[str], tuple[str, float]],
    template: str,
    *,
    keyword: str,
    client_name: str | None,
    digest: VideoDigest,
    videos: list[AnalyzedVideo],
    on_drop: DropSink | None = None,
) -> tuple[VideoDigestConclusion | None, float]:
    """LLM で読みを作る。使えなければ集計だけの見出しに縮退する（例外は呼び出し側）。"""
    if digest.watched <= 0:
        return None, 0.0
    prompt, grounder = build_digest_prompt(
        template, keyword=keyword, client_name=client_name, digest=digest, videos=videos
    )
    text, cost = converse(prompt)
    raw = _parse(text)
    conclusion = (
        ground_digest_conclusion(raw, grounder=grounder, on_drop=on_drop)
        if raw is not None
        else None
    )
    if conclusion is None:
        if on_drop is not None and raw is None:
            on_drop("all", "unparseable")
        return rule_digest_conclusion(digest), cost
    if not conclusion.headline:
        fallback = rule_digest_conclusion(digest)
        conclusion.headline = fallback.headline if fallback else ""
    return conclusion, cost


__all__ = [
    "COVER_ONLY_ERROR",
    "CTA_LABEL",
    "HOOK_LABEL",
    "PACING_LABEL",
    "STRICT_ALWAYS_ALLOWED",
    "CountGrounder",
    "build_digest_prompt",
    "conclude_digest",
    "cta_label",
    "digest_grounder",
    "digest_payload",
    "digest_videos",
    "duration_of",
    "followup_surface",
    "ground_digest_conclusion",
    "has_opening_telop",
    "has_spoken_kw",
    "has_telop_kw",
    "hook_label",
    "is_analyzable",
    "is_cover_only",
    "is_watched",
    "post_to_meta",
    "rule_digest_conclusion",
    "select_followup_videos",
    "videos_payload",
]
