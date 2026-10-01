"""横断シンセシス v3 の入力（仕様 v3 §3-1）。LLM を使わない・純関数。

LLM（Gemini）に渡すのは、コードが数えた事実（facts）と 1 本ずつの個票だけ。
本数・段階の名前・分布・最も見られた 1 本・外れ値・絵コンテのカットの秒はコードが決め、
LLM には「文」と「根拠にした実例（refs）」だけを書かせる（本番で、上位 2 本の幅を「n=5」と
書く・別指標の ρ を付ける・テロップ要旨 1 行とキャプション 220 字から結論を書く、といった
誤りがあった）。

渡すもの:
1. 検索 KW と語の分割
2. クライアント欄（名前・競合・避けたい訴求。無ければ「未指定」と、その場合の書き方）
3. 特徴の表（FeatureTable: id・本数・#列挙・段階名・上位ボードの率）
4. 分布（全 n 本・尺は TikTok のメタ）
5. 最も見られ保存された 1 本・外れ値・実績が伴わない 1 本
6. 個票 n 本（structure_payload を土台に、全テロップを秒の順・場面・ブランド・分量の置き場所・
   タイアップ・CTA・語り・音源・投稿日・再生・保存率・シェア・キャプション全文（1000 字まで））
7. 上位ボードの見出し（先頭 60 字・作り手・順位・タイアップ・保存率）
8. 絵コンテのカットの秒（本編を 3 等分するコードの規則）
渡さないもの: 相関（n<8 のとき）・上位帯の幅（旧 win_ranges）・「勝ち筋」の語の入った旧フラグ名。
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from teamagent.skills._shared.grounding import NumberGrounder
from teamagent.skills.search_surface_check.display import fmt_count
from teamagent.skills.search_surface_check.video_digest import HOOK_LABEL
from teamagent.skills.search_surface_check.video_notes import structure_payload
from teamagent.skills.search_surface_check.video_structure import scene_rows
from teamagent.skills.video_algorithm.cover_facts import (
    CLUTTER_LABEL,
    ELEMENT_LABEL,
    EMPTY_VIEW,
    EXPRESSION_LABEL,
    FACE_KIND_LABEL,
    GAZE_LABEL,
    LEGIBILITY_LABEL,
    MATCH_LABEL,
    NUMBER_KIND_LABEL,
    POSITION_JP,
    PRODUCT_LABEL,
    SIZZLE_LABEL,
    STATUS_LABEL,
    STYLE_LABEL,
    CoverFacts,
    CoverView,
    code_cover_directives,
    cover_view,
    dist_text,
)
from teamagent.skills.video_algorithm.evidence import (
    KW_LAYER_LABEL,
    RELATION_LABEL,
    Roster,
    at_least_majority,
    norm,
    query_terms,
    ranks_text,
)
from teamagent.skills.video_algorithm.facts import (
    CTA_KIND_LABEL,
    ORIENTATION_LABEL,
    POSITION_LABEL,
    STAGE_LABELS,
    Feature,
    SummaryBand,
    VideoFacts,
    all_facts,
    best_video,
    cta_consensus,
    feature_table,
    fmt_man,
    kw_matrix,
    outliers,
    pr_marked,
    rank_runs,
    stage_bounds,
    summary_band,
    surface_map,
    unanalyzed_ranks,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    StatsAnalysis,
    VideoMeta,
    VideoVSEOAnalysis,
)

# 相関（特徴×表示順位）を LLM に渡す最小の本数（仕様 C1: n<8 では渡さない）。
CORR_MIN_N = 8
# 個票のキャプションの上限（超えた分だけ末尾を切る）。
DESC_MAX = 1000
# 上位ボードの見出しの字数。
BOARD_HEAD_MAX = 60
# 実績が伴わない 1 本（R9）: 再生が最少で、中央値のこの倍未満。
LOW_PLAYS_RATIO = 0.2
# 倍・%・万が付く数字は、0〜10 でも入力に無ければ通さない（v2 と同じ）。
STRICT_SUFFIXES = frozenset({"倍", "%", "万"})
UNSPECIFIED_CLIENT = "（クライアント商品）"
# コードが事実から作る指示（synthesis_checks.code_directives）の特徴。LLM に重ねて書かせない。
CODE_DIRECTIVE_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("first_telop_0s", "最初のテロップを0秒台に出す", "テロップ"),
    ("kw_telop_3s:{term}", "「{term}」を3秒以内にテロップで出す", "フック"),
    ("qty_anywhere", "材料と分量をテロップかキャプションに載せる", "テロップ"),
    # 名簿（クライアント・競合）かカテゴリ判定があるときだけ出る特徴。
    (
        "brand_category_prominent",
        "{product}を本編で主役か目立つ大きさで映し、名前をテロップかキャプションで出す",
        "商品",
    ),
)


@dataclass(frozen=True)
class CutSlot:
    """絵コンテの 1 カット（秒はコードが決める）。"""

    cut: int
    start: float
    end: float
    stage: str


def cut_plan(target_sec: float) -> tuple[CutSlot, ...]:
    """0〜3秒 / 3〜10秒 / 本編（10 秒〜残り 10 秒）を 3 等分 / 最後の 10 秒。秒は整数に丸める。"""
    if target_sec <= 0:
        return ()
    (h0, h1), (b0, b1), (m0, m1), (t0, t1) = stage_bounds(target_sec)
    raw: list[tuple[float, float, str]] = [(h0, h1, STAGE_LABELS[0]), (b0, b1, STAGE_LABELS[1])]
    if m1 > m0:
        step = (m1 - m0) / 3
        raw += [(m0 + i * step, m0 + (i + 1) * step, STAGE_LABELS[2]) for i in range(3)]
    raw.append((t0, t1, STAGE_LABELS[3]))
    out: list[CutSlot] = []
    for start, end, stage in raw:
        s, e = float(round(start)), float(round(end))
        if e > s:
            out.append(CutSlot(cut=len(out) + 1, start=s, end=e, stage=stage))
    return tuple(out)


def low_performer(facts: Sequence[VideoFacts]) -> int | None:
    """再生が最少で、中央値の 0.2 倍未満の 1 本（R9）。3 本未満・該当なしは None。"""
    rated = [f for f in facts if f.plays > 0]
    if len(rated) < 3:
        return None
    med = statistics.median(f.plays for f in rated)
    low = min(rated, key=lambda f: (f.plays, f.rank))
    return low.rank if low.plays < med * LOW_PLAYS_RATIO else None


def has_framing(videos: Iterable[AnalyzedVideo]) -> bool:
    """場面に画角の欄（v3 の framing）があるか。無ければ寄り・アップ・表情の指示を出さない。"""
    for v in videos:
        a = v.analysis
        if a is None:
            continue
        if any(getattr(sc, "framing", None) for sc in a.scenes):
            return True
    return False


@dataclass(frozen=True)
class SynthesisContext:
    """シンセシスの入力と検査が共有する事実（同じ事実で書かせ、同じ事実で照合する）。"""

    query: str
    videos: tuple[AnalyzedVideo, ...]  # 動画を見て分析できた本（順位の順）
    facts: tuple[VideoFacts, ...]  # 同じ順
    board: tuple[VideoMeta, ...]
    roster: Roster
    avoid_terms: tuple[str, ...]
    features: tuple[Feature, ...]
    band: SummaryBand
    best_rank: int
    best_metrics: tuple[str, ...]
    outliers: tuple[tuple[int, tuple[str, ...]], ...]
    low_rank: int | None
    framing: bool
    cuts: tuple[CutSlot, ...]
    stats: StatsAnalysis | None = None
    # サムネ（一覧の表紙）の読み取りのまとめ（上位ボードの cover_read から・無ければ空）。
    cover: CoverView = EMPTY_VIEW

    @classmethod
    def build(
        cls,
        videos: Sequence[AnalyzedVideo],
        query: str,
        *,
        board: Sequence[VideoMeta] | None = None,
        roster: Roster | None = None,
        avoid_terms: Iterable[str] | None = None,
        stats: StatsAnalysis | None = None,
    ) -> SynthesisContext:
        roster = roster or Roster()
        facts = [f for f in all_facts(list(videos), query, roster) if f.watched]
        ranks = {f.rank for f in facts}
        watched = tuple(
            v
            for v in sorted(videos, key=lambda x: x.meta.rank)
            if v.analysis is not None and v.meta.rank in ranks
        )
        board_t = tuple(board or ())
        band = summary_band(facts)
        best, metrics = best_video(facts)
        return cls(
            query=query,
            videos=watched,
            facts=tuple(facts),
            board=board_t,
            roster=roster,
            avoid_terms=tuple(
                dict.fromkeys(t.strip() for t in (avoid_terms or ()) if t and t.strip())
            ),
            features=tuple(feature_table(facts, board_t, query)),
            band=band,
            best_rank=best,
            best_metrics=tuple(metrics),
            outliers=tuple((r, tuple(why)) for r, why in outliers(facts)),
            low_rank=low_performer(facts),
            framing=has_framing(watched),
            cuts=cut_plan(band.duration.median if band.duration else 0.0),
            stats=stats,
            cover=cover_view(board_t, list(videos), query, roster),
        )

    @property
    def n(self) -> int:
        return len(self.facts)

    @property
    def ranks(self) -> tuple[int, ...]:
        return tuple(f.rank for f in self.facts)

    @property
    def target_sec(self) -> float | None:
        return self.band.duration.median if self.band.duration else None

    @property
    def client_label(self) -> str:
        """提案文でクライアントを呼ぶ名前（名簿の先頭の別名・無ければ「（クライアント商品）」）。"""
        name = (self.roster.client_name or "").split("|")[0].strip()
        return name or UNSPECIFIED_CLIENT

    @property
    def product_subject(self) -> str:
        """指示の主語「GABANの商品」（クライアント名が無ければ「（クライアント商品）」）。"""
        label = self.client_label
        return label if label == UNSPECIFIED_CLIENT else f"{label}の商品"

    def fact(self, rank: int) -> VideoFacts | None:
        return next((f for f in self.facts if f.rank == rank), None)

    def analysis(self, rank: int) -> VideoVSEOAnalysis | None:
        return next((v.analysis for v in self.videos if v.meta.rank == rank), None)

    def feature(self, fid: str) -> Feature | None:
        return next((f for f in self.features if f.id == fid), None)

    @property
    def cover_n(self) -> int:
        """表紙を読めた本数（上位の群）。0 なら表紙の指示を出さない。"""
        return len(self.cover.top_ok)

    def code_directive_features(self) -> list[tuple[Feature, str, str]]:
        """コードが事実から作る指示に使う特徴（多数派以上だけ・固定の順）と、その文・種類。"""
        out: list[tuple[Feature, str, str]] = []
        for fid, text, kind in CODE_DIRECTIVE_FEATURES:
            for term in query_terms(self.query) if "{term}" in fid else [""]:
                f = self.feature(fid.format(term=term))
                if f is not None and at_least_majority(f.tier):
                    out.append((f, text.format(term=term, product=self.product_subject), kind))
                    break
        return out


# ── 個票 ────────────────────────────────────────────────────────────────


def _sec(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _span(start: float | None, end: float | None) -> str:
    if start is None:
        return "—"
    if end is None or end == start:
        return f"{start:g}"
    return f"{start:g}〜{end:g}"


def video_card(v: AnalyzedVideo, f: VideoFacts, ctx: SynthesisContext) -> dict[str, Any]:
    """1 本の個票（日本語の項目名・コードの事実と動画分析 AI の記録）。"""
    a = v.analysis
    assert a is not None
    base = structure_payload(v, keyword=ctx.query, roster=ctx.roster) or {}
    telops = sorted((t for t in a.telops if t.text.strip()), key=lambda t: t.sec)
    desc = f.desc or ""
    card: dict[str, Any] = {
        "順位": f.rank,
        "アカウント": f.author,
        "フォロワー": fmt_count(f.followers) if f.followers else "不明",
        "投稿日": (
            f"{f.posted_at.isoformat()}{'（動画IDから換算）' if f.posted_estimated else ''}"
            if f.posted_at
            else "不明"
        ),
        "再生": f.plays,
        "保存率%": f.save_rate,
        "シェア": f.shares,
        "尺（秒・TikTokのメタ）": _sec(f.duration_sec),
        "向き": ORIENTATION_LABEL.get(f.orientation, "不明"),
        "語り": "あり" if f.narration else "なし",
        "音源": f.music_title or "不明",
        "タイアップ表記": f.pr_evidence if f.pr else "なし",
        "フック": base.get("フック")
        or {"型": HOOK_LABEL.get(a.hook_type, HOOK_LABEL["other"]), "要旨": a.hook_summary},
        "主なメッセージ": a.main_message,
        "最初のテロップの秒": _sec(f.first_telop_sec),
        "0〜3秒のテロップ": [{"秒": _sec(s), "文言": t} for s, t in f.opening_telops],
        "テロップ（全部・秒の順）": [
            {
                "秒": _sec(t.sec),
                "文言": t.text.strip(),
                "位置": POSITION_LABEL.get(t.position, "不明"),
            }
            for t in telops
        ],
        "テロップ枚数": f.telop_count,
        "1秒あたりのテロップ": f.telops_per_sec,
        "分量の置き場所": f.qty_place,
        "分量テロップ": [{"秒": _sec(s), "文言": t} for s, t in f.qty_telops],
        "数の表現": [
            {"層": "テロップ" if layer == "telop" else "キャプション", "秒": _sec(s), "原文": t}
            for layer, s, t in f.numeric_claims
        ],
        "場面": [
            {
                "秒": _span(r.start, r.end),
                "役割": r.role_label + ("（推定）" if r.role_inferred else ""),
                "画面": r.desc,
                "テロップ": r.telop,
                "発話": r.speech,
                "狙い": r.intent,
            }
            for r in scene_rows(v)
        ],
        "ブランド": [
            {
                "名前": b.name,
                "区分": b.relation_label,
                "目立ち方": b.prominence_label or "不明",
                "映る秒（AI推定）": _span(b.first_sec, b.last_sec),
                "合計秒": b.total_sec,
                "テロップに名前": "あり" if b.in_telop else "なし",
                "キャプションに名前": "あり" if b.in_caption else "なし",
                "提供・タイアップの印": "あり" if b.sponsored else "なし",
                "検索KWの商材カテゴリ": (
                    "不明" if b.category_match is None else "該当" if b.category_match else "外"
                ),
            }
            for b in f.brands
        ],
        "検索語の一致（テロップは本文に実在するものだけ・発話はAI聞き取りで未照合）": [
            {
                "語": h.term,
                "層": KW_LAYER_LABEL.get(h.layer, h.layer),
                "一致": "完全一致" if h.match == "exact" else "言い換え",
                "秒": [_sec(s) for s in h.secs],
            }
            for h in f.kw
        ],
        "CTA": {
            "動画内": (
                {
                    "型": CTA_KIND_LABEL.get(f.cta_in_video[0], f.cta_in_video[0]),
                    "文言": f.cta_in_video[1] or "（文言なし）",
                    "秒": _sec(f.cta_in_video[2]),
                }
                if f.cta_in_video
                else "なし"
            ),
            "キャプション内": [CTA_KIND_LABEL.get(k, k) for k in f.cta_in_caption] or "なし",
        },
        "評価": base.get("評価", []),
        "保存・シェアの動機（AI所見）": a.save_share_motivation,
        "カット数": a.cut_count if a.cut_count is not None else "未計測",
        "キャプション（全文）": desc[:DESC_MAX] + ("…（以下略）" if len(desc) > DESC_MAX else ""),
    }
    if f.cta_dropped:
        card["CTA"]["無効（文言も秒も無い）"] = [CTA_KIND_LABEL.get(k, k) for k in f.cta_dropped]
    named = getattr(a, "named_items", None)
    if named:
        card["紹介アイテム"] = [
            item.model_dump() if hasattr(item, "model_dump") else item for item in named
        ]
    if f.result_first_sec is not None:
        card["完成品が最初に映る秒"] = _sec(f.result_first_sec)
    return card


def cards(ctx: SynthesisContext) -> dict[int, dict[str, Any]]:
    return {f.rank: video_card(v, f, ctx) for v, f in zip(ctx.videos, ctx.facts, strict=True)}


def card_json(card: dict[str, Any]) -> str:
    return json.dumps(card, ensure_ascii=False)


# ── 本文 ────────────────────────────────────────────────────────────────


def _alias_text(entry: str) -> str:
    """「S&B|エスビー食品」→「S&B（別名: エスビー食品）」。"""
    names = [x.strip() for x in entry.split("|") if x.strip()]
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{names[0]}（別名: {'・'.join(names[1:])}）"


def _client_lines(ctx: SynthesisContext) -> list[str]:
    r = ctx.roster
    lines: list[str] = []
    if r.client_name:
        lines.append(
            f"- クライアント: {_alias_text(r.client_name)}"
            f"（提案文では「{ctx.client_label}」と書く）"
        )
    else:
        lines.append(
            "- クライアント: 未指定（自社/競合の区分なし）。提案文では「御社」「貴社」「弊社」を"
            f"使わず「{UNSPECIFIED_CLIENT}」と書く"
        )
    if r.competitors:
        names = "、".join(_alias_text(c) for c in r.competitors)
        lines.append(f"- 競合: {names}（競合の商品を勧めない）")
    else:
        lines.append("- 競合: 未指定")
    if ctx.avoid_terms:
        lines.append(
            f"- 避けたい訴求: {'、'.join(ctx.avoid_terms)}"
            "（指示・絵コンテ・クライアントの次の一手に書かない。やらないことには書いてよい）"
        )
    return lines


def _feature_lines(ctx: SynthesisContext) -> list[str]:
    lines: list[str] = []
    for f in ctx.features:
        rate = (
            f"｜上位{f.board_rate[1]}本では{f.board_rate[0]}/{f.board_rate[1]}"
            if f.board_rate
            else ""
        )
        who = "全部" if f.count == f.n else ranks_text(f.ranks)
        lines.append(f"- {f.id}｜{f.label}｜{f.count}/{f.n}（{who}）｜{f.tier}{rate}")
    return lines


def _dist_lines(ctx: SynthesisContext) -> list[str]:
    b = ctx.band
    lines: list[str] = []
    if b.duration:
        d = b.duration
        lines.append(
            f"- 尺（TikTokのメタ・全{b.n}本）: 中央値{d.median:g}秒（{d.min:g}〜{d.max:g}秒）"
        )
    if b.telops_per_sec:
        t = b.telops_per_sec
        lines.append(f"- 1秒あたりのテロップ: 中央値{t.median:g}枚（{t.min:g}〜{t.max:g}）")
    lines.append(f"- 語りあり: {ranks_text(b.narration_ranks) or 'なし'}")
    consensus = cta_consensus(ctx.facts)
    lines.append(
        "- 動画内CTAの多数派: "
        + (
            "、".join(f"{CTA_KIND_LABEL.get(k, k)}（{ranks_text(r)}）" for k, r in consensus)
            if consensus
            else "なし（過半数の型が無い）"
        )
    )
    return lines


def _kw_lines(ctx: SynthesisContext) -> list[str]:
    lines: list[str] = []
    rows = kw_matrix(ctx.facts, ctx.board, ctx.query)
    for term in query_terms(ctx.query):
        parts: list[str] = []
        for r in rows:
            if r.term != term:
                continue
            text = f"{r.layer_label} 完全一致{len(r.exact)}/{r.n}"
            if r.exact:
                text += f"（{ranks_text(r.exact)}）"
            if r.synonym:
                text += f"・言い換え{len(r.synonym)}/{r.n}（{ranks_text(r.synonym)}）"
            if r.board:
                text += f"・上位{r.board[1]}本では{r.board[0]}/{r.board[1]}"
            parts.append(text)
        lines.append(f"- 「{term}」: " + "／".join(parts))
    return lines


def _board_lines(ctx: SynthesisContext) -> list[str]:
    if not ctx.board:
        return []
    sm = surface_map(ctx.board, query=ctx.query)
    lines = [
        "- 作り手の重なり（2本以上）: "
        + (
            "、".join(f"@{a}（{ranks_text(r)}）" for a, r in sm.creators) if sm.creators else "なし"
        ),
        f"- タイアップ表記: {len(sm.pr_ranks)}/{sm.size}（{ranks_text(sm.pr_ranks) or 'なし'}）",
    ]
    if sm.median_save_rate is not None:
        top = "、".join(f"#{r} {v:.2f}%" for r, v in sm.top_save)
        lines.append(f"- 保存率: 中央値{sm.median_save_rate:.2f}%・高い3本 {top}")
    if sm.years:
        lines.append("- 投稿年: " + "、".join(f"{y}年 {c}本" for y, c in sm.years))
    for m in ctx.board:
        head = " ".join((m.desc or "").split())
        head = head[:BOARD_HEAD_MAX] + ("…" if len(head) > BOARD_HEAD_MAX else "")
        pr = " タイアップ表記" if pr_marked(m) else ""
        rate = f"保存率{m.save_rate():.2f}%" if m.play_count else "保存率不明"
        lines.append(
            f"#{m.rank} @{m.author or '不明'} {rate} 再生{fmt_man(m.play_count)}{pr}「{head}」"
        )
    return lines


def _unwatched_text(ctx: SynthesisContext) -> str:
    """動画を見ていない順位（分析の失敗・下位の繰上げがあっても順位の集合から書く）。"""
    missing = unanalyzed_ranks(ctx.ranks, ctx.board)
    return f"{rank_runs(missing)}は動画を見ていない" if missing else "全部の動画を見た"


def _cut_lines(ctx: SynthesisContext) -> list[str]:
    if not ctx.cuts:
        return []
    target = ctx.target_sec or 0.0
    lines = [f"- 目安の尺: {target:g}秒（{ctx.n}本の尺の中央値・固定しない）"]
    lines += [f"- カット{c.cut}: {c.start:g}〜{c.end:g}秒（{c.stage}）" for c in ctx.cuts]
    return lines


def _corr_lines(ctx: SynthesisContext) -> list[str]:
    st = ctx.stats
    if st is None or ctx.n < CORR_MIN_N:
        return []
    parts = [
        f"{c.feature} ρ{c.rho:+.2f}（単調{c.monotonic_hits}/{c.monotonic_total}）"
        for c in st.correlations
        if c.rho is not None
    ]
    if not parts:
        return []
    return [
        "# 特徴×表示順位の相関（ρ<0 はその値が大きい動画ほど順位が上・有意性なし・因果ではない）",
        "・" + " / ".join(parts),
        "文に数値は書かず、仮説の stat_feature に特徴のキー名だけを入れる。",
    ]


# ── サムネ（一覧の表紙）───────────────────────────────────────────────

COVER_SECTION_HEAD = "# サムネ（一覧の表紙）の個票"


def _label(table: dict[str, str], value: str) -> str:
    return table.get(value, "不明")


def cover_card(c: CoverFacts) -> dict[str, Any]:
    """1 枚の表紙の個票（AI の読み取りと、コードの判定を分けて書く）。"""
    if not c.ok:
        return {"順位": c.rank, "表紙": f"分析なし（{STATUS_LABEL.get(c.status, c.status)}）"}
    return {
        "順位": c.rank,
        "群": "上位" if c.group == "top" else "ほか",
        "AIの読み取り": {
            "写っている要素": [ELEMENT_LABEL.get(e, e) for e in (c.elements or ())],
            "主役の説明": c.subject_note,
            "表紙の文字": [t.replace("\n", "／") for t in c.texts],
            "読めない文字": "あり" if c.read.unreadable_text else "なし",
            "顔": _label(FACE_KIND_LABEL, c.face_kind),
            "表情": _label(EXPRESSION_LABEL, c.expression),
            "視線": _label(GAZE_LABEL, c.gaze),
            "寄り": "あり" if c.closeup else "なし" if c.closeup is False else "不明",
            "質感の見せ場": [SIZZLE_LABEL.get(x, x) for x in (c.sizzle or ())],
            "商品": _label(PRODUCT_LABEL, c.product),
            "背景": _label(CLUTTER_LABEL, c.clutter),
            "読みやすさ": _label(LEGIBILITY_LABEL, c.legibility),
            "文字の飾り": [STYLE_LABEL.get(x, x) for x in (c.styles or ())],
        },
        "コードの判定": {
            "大きい文字": c.main_flat,
            "行数": c.lines,
            "位置": POSITION_JP.get(c.position, "不明"),
            "一覧のタイルで読める大きさ": (
                "不明" if c.large_text is None else "はい" if c.large_text else "いいえ"
            ),
            "検索語": list(c.kw_terms),
            "単位つきの数字": [f"{NUMBER_KIND_LABEL.get(k, k)}:{raw}" for k, raw in c.numbers],
            "問いかけ": "はい" if c.question else "いいえ",
            "手間の少なさ": "はい" if c.effortless else "いいえ",
            "失敗や注意": "はい" if c.warning else "いいえ",
            "商品名（照合済みの区分）": [
                f"{name}（{'未照合' if rel == 'unverified' else RELATION_LABEL.get(rel, rel)}）"
                for name, rel in c.brands
            ],
            "冒頭のテロップとの一致（AI同士）": MATCH_LABEL.get(c.opening_match, "—"),
            "キャプション冒頭との一致（実データ）": MATCH_LABEL.get(c.caption_match, "—"),
        },
    }


def _cover_feature_lines(view: CoverView) -> list[str]:
    lines: list[str] = []
    for f in view.features:
        who = "全部" if f.count == f.n else ranks_text(f.ranks)
        lines.append(f"- {f.id}｜{f.label}｜{f.count}/{f.n}（{who}）｜{f.tier}")
    return lines


def _cover_gap_lines(view: CoverView) -> list[str]:
    if view.mode != "board" or not view.gap:
        return [f"- {view.gap_note}"]
    lines = [f"- {view.gap_note}"]
    for g in view.gap:
        mark = f"｜差が大きい（参考・{g.mark_text}）" if g.marked else ""
        lines.append(f"- {g.id}｜{g.label}｜上位 {g.a}/{g.n}・ほか {g.b}/{g.m}{mark}")
    return lines


def cover_prompt_block(ctx: SynthesisContext) -> str:
    """表紙の節（無ければ空）。数字の照合は、この節だけを cover_directives に使う。"""
    view = ctx.cover
    if not view.top:
        return ""
    ok = len(view.top_ok)
    code = code_cover_directives(view, ctx.avoid_terms)
    sections: list[list[str]] = [
        [
            f"{COVER_SECTION_HEAD}（AI が表紙の画像だけから読んだもの・上位{view.n_top}本中"
            f"{ok}本を読めた。1行に1本の JSON。表紙の文字と主役の説明は cover_directives の refs の"
            "引用元）",
            *(json.dumps(cover_card(c), ensure_ascii=False) for c in (*view.top, *view.rest)),
        ],
        [
            "# 表紙の特徴の表（母数は欄ごとの読めた本数・段階はコードの集計。cover_directives の"
            " feature にはこの id を1つ入れる。段階と本数はその特徴の集計から付く）",
            "id｜特徴｜本数｜該当｜段階",
            *_cover_feature_lines(view),
        ],
        [f"# 表紙の文字の分布（上位）: {dist_text(view.dist)}"],
        [f"# 上位{view.n_top}本とほかの表紙（本数・参考・因果ではない）", *_cover_gap_lines(view)],
        [
            "# コードが入れる表紙の指示（重ねて書かない）: "
            + ("、".join(f"{d.text}（{d.tier}）" for d in code) or "なし")
        ],
    ]
    return "\n\n".join("\n".join(x) for x in sections if x)


def render_prompt(ctx: SynthesisContext) -> str:
    """Gemini に渡す本文（system は synthesis.md v3）。照合の入力もこの本文だけ。"""
    terms = "・".join(f"「{t}」" for t in query_terms(ctx.query)) or f"「{ctx.query}」"
    code_dirs = ctx.code_directive_features()
    why = f"（{'・'.join(ctx.best_metrics)}が{ctx.n}本で最大）" if ctx.best_metrics else ""
    best = f"#{ctx.best_rank}{why}" if ctx.best_rank else "なし"
    outs = "／".join(f"#{r} {'・'.join(why)}" for r, why in ctx.outliers) or "なし"
    low = f"#{ctx.low_rank}" if ctx.low_rank is not None else "なし"
    card_list = list(cards(ctx).values())
    sections: list[list[str]] = [
        [f"# 検索KW: {ctx.query}（語: {terms}）"],
        ["# クライアント", *_client_lines(ctx)],
        [
            "# 特徴の表（本数と段階の名前はコードが数えた値。見出しには 必須条件・多数派 の id "
            "だけを使う）",
            "id｜特徴｜本数｜該当｜段階｜上位ボードの率",
            *_feature_lines(ctx),
        ],
        [f"# 分布（全{ctx.n}本）", *_dist_lines(ctx)],
        [
            "# 最も見られ保存された1本・外れ値（事実）",
            f"- 最も見られ保存された1本: {best}",
            f"- 外れ値: {outs}",
            f"- 実績が伴わない1本（この1本だけの特徴は指示にしない）: {low}",
        ],
        ["# 拾われる条件（語×層）", *_kw_lines(ctx)],
        [
            "# 画角の欄: "
            + ("あり" if ctx.framing else "なし（寄り・アップ・表情など画角の指示は書かない）")
        ],
        [
            "# コードが入れる事実の指示（重ねて書かない）: "
            + ("、".join(f"{text}（{f.tier}）" for f, text, _k in code_dirs) or "なし")
        ],
        ["# 絵コンテのカット（秒はコードが決めた。cut にはこの番号を入れる）", *_cut_lines(ctx)],
        _corr_lines(ctx),
        [
            f"# 個票（{ctx.n}本・1行に1本の JSON。テロップ・場面・ブランド・キャプションは refs の"
            "引用元）",
            *(card_json(c) for c in card_list),
        ],
        [
            f"# 上位{len(ctx.board)}本の一覧（メタだけ。{_unwatched_text(ctx)}）",
            *_board_lines(ctx),
        ],
        [cover_prompt_block(ctx)] if ctx.cover.top else ["# サムネ（一覧の表紙）: 分析なし"],
        [
            "システム指示の規則（R1〜R17）に従い、所見を2-3行書いたあと JSON ブロックを1つ"
            "出力してください。"
        ],
    ]
    return "\n\n".join("\n".join(s) for s in sections if s)


@dataclass(frozen=True)
class Grounders:
    """数字の照合: 本文全体（表紙の節を除く）と、1 本ずつ（その動画の個票だけ）と、表紙の節だけ。"""

    all: NumberGrounder
    per_video: dict[int, NumberGrounder]
    cover: NumberGrounder | None = None


def build_grounders(ctx: SynthesisContext, prompt: str) -> Grounders:
    """照合の入力は Gemini に渡した本文だけ（system は入れない。例文の数字を通さないため）。

    表紙の節（AI が表紙から読んだ文字の数字）は、本文全体の照合から外し、cover_directives の
    照合にだけ使う（表紙の文字の数字で、動画の欄の数字が通らないようにする）。
    """
    block = cover_prompt_block(ctx)
    body = prompt.replace(block, "") if block else prompt
    cover = (
        NumberGrounder.from_inputs(
            block,
            ctx.query,
            valid_ranks=[c.rank for c in (*ctx.cover.top, *ctx.cover.rest)],
            rounding=True,
            strict_suffixes=STRICT_SUFFIXES,
        )
        if block
        else None
    )
    per_video = {
        rank: NumberGrounder.from_inputs(
            card_json(card),
            ctx.query,
            valid_ranks=[rank],
            rounding=True,
            strict_suffixes=STRICT_SUFFIXES,
        )
        for rank, card in cards(ctx).items()
    }
    return Grounders(
        all=NumberGrounder.from_inputs(
            body, valid_ranks=ctx.ranks, rounding=True, strict_suffixes=STRICT_SUFFIXES
        ),
        per_video=per_video,
        cover=cover,
    )


def match_text(term: str, text: str) -> bool:
    """語が文にあるか（NFKC・大小・空白を無視）。"""
    return bool(norm(term)) and norm(term) in norm(text)


__all__ = [
    "CODE_DIRECTIVE_FEATURES",
    "CORR_MIN_N",
    "COVER_SECTION_HEAD",
    "DESC_MAX",
    "STRICT_SUFFIXES",
    "UNSPECIFIED_CLIENT",
    "CutSlot",
    "Grounders",
    "SynthesisContext",
    "build_grounders",
    "card_json",
    "cards",
    "cover_card",
    "cover_prompt_block",
    "cut_plan",
    "has_framing",
    "low_performer",
    "match_text",
    "render_prompt",
    "video_card",
]
