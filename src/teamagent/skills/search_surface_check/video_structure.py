"""2 段目: 上位の動画 1 本ずつの構成（場面の役割・時間配分・主要な数字・評価）を決定的に組む。

LLM は使わない。場面の役割（role）は 1 本ずつの分析（Gemini）が出していればそれを使い、無い
（v1/v2 の既定プロンプトの出力・古いキャッシュ）ときはコードで推定する: 最初の場面＝フック、
CTA の秒（cta_sec）を含む場面＝CTA、ほかは手順（展開）。推定した役割は「（推定）」と書く。

評価（◎○△—）は数字の基準でコードが決める（LLM に採点させない）。基準は下の定数で、テストで
境界を固定し、レポートの脚注にそのまま書く（``GRADE_RULES``）。— は判定に必要な値が無いとき。

Gemini の自己申告はそのまま使わない（video_algorithm.evidence で照合する）:
- 検索 KW の初出は、テロップ本文に語が実在するもの（言い換えは秒 ±2 のテロップに実在するもの）
  と発話（AI 聞き取り）から取る。``kw_match`` の ✓ は信じない。
- ブランドの区分（クライアント／競合）は名簿（Roster）でコードが決める。``brand_relation`` は
  使わない。名簿が無ければ区分を書かない。
- 冒頭のテロップは ``hook_has_caption`` ではなく、最初のテロップの秒（3 秒以内か）で決める。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from teamagent.skills.search_surface_check.video_digest import (
    OPENING_SEC,
    cta_label,
    duration_of,
    hook_label,
    is_watched,
)
from teamagent.skills.video_algorithm.evidence import (
    KwHit,
    Roster,
    analysis_terms,
    fold,
    kw_hits,
    query_terms,
)
from teamagent.skills.video_algorithm.frames import (
    MAX_SCENE_FRAMES,
    frame_duration,
    pick_scene_rows,
    scene_time,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    BrandDetection,
    FrameShot,
    Scene,
    VideoVSEOAnalysis,
)

ROLE_LABEL: dict[str, str] = {
    "hook": "フック",
    "problem": "問題提起",
    "steps": "手順",
    "result": "結果・実演",
    "proof": "証拠・比較",
    "cta": "CTA",
    "other": "その他",
}
# 役割の推定: 最初と CTA 以外の場面（「順に展開」）。
INFERRED_MIDDLE_ROLE = "steps"

# ── 評価の基準（定数・テストで境界を固定・脚注にそのまま出す）──────────────────
MARK_GOOD = "◎"
MARK_OK = "○"
MARK_WEAK = "△"
MARK_NONE = "—"
MARK_WORD = {MARK_GOOD: "よい", MARK_OK: "ふつう", MARK_WEAK: "弱い", MARK_NONE: "判定なし"}

HOOK_FIRST_TELOP_MAX_SEC = 1.0  # 最初のテロップがこの秒以内
TEMPO_GOOD_SEC = 3.0  # 平均カット秒がこれ以下で ◎
TEMPO_OK_SEC = 6.0  # これ以下で ○
KW_GOOD_SEC = 3.0  # 検索 KW の初出（テロップか発話）がこれ以内で ◎
KW_OK_SEC = 10.0  # これ以内で ○
SAVE_GOOD_SIGNALS = 2  # 保存の仕掛けがこの数以上で ◎（1 つで ○）
STEPS_MIN_SCENES = 2  # 手順の段とみなす場面数
BRAND_GOOD_SEC = 3.0  # 商品が映る合計秒がこれ以上かつ目立つ（hero/prominent）で ◎
COHERENCE_GOOD = 80  # 一致度がこれ以上で ◎
COHERENCE_OK = 60  # これ以上で ○

# 分量（大さじ・小さじ・数字＋g/ml/cc/個/本/袋/枚/カップ/杯/片・適量・少々）。NFKC＋小文字で見る。
# 「4つ」は数えない（「やりがちNG談を4つ」のような分量でない語を拾うため）。
QTY_RE = re.compile(
    r"大さじ|小さじ|適量|少々|\d+(?:[./]\d+)?\s*(?:g|ml|cc|個|本|袋|枚|カップ|杯|片)(?![a-z])"
)

AXES: tuple[str, ...] = (
    "冒頭3秒の掴み",
    "テンポ",
    "KWの露出",
    "保存の仕掛け",
    "CTA",
    "商品の見せ方",
    "一致度",
)


def _n(value: float) -> str:
    return f"{value:g}"


GRADE_RULES: tuple[tuple[str, str], ...] = (
    (
        "冒頭3秒の掴み",
        f"冒頭（0〜{_n(OPENING_SEC)}秒）のテロップあり・最初のテロップが"
        f"{_n(HOOK_FIRST_TELOP_MAX_SEC)}秒以内・"
        f"フックの型が「その他」でない、の 3 つがそろえば{MARK_GOOD}、1 つ欠けで{MARK_OK}、"
        f"2 つ以上欠けで{MARK_WEAK}（テロップの有無は分析 AI の申告でなく、テロップの秒で見る）",
    ),
    (
        "テンポ",
        f"平均カット秒（尺÷カット数）が{_n(TEMPO_GOOD_SEC)}秒以下で{MARK_GOOD}、"
        f"{_n(TEMPO_OK_SEC)}秒以下で{MARK_OK}、それより長いと{MARK_WEAK}。カット数が不明なら{MARK_NONE}",
    ),
    (
        "KWの露出",
        f"検索 KW がテロップか発話に初めて出る秒が{_n(KW_GOOD_SEC)}秒以内で{MARK_GOOD}、"
        f"{_n(KW_OK_SEC)}秒以内（または発話に出るが秒が不明）で{MARK_OK}、それ以降か出ないと"
        f"{MARK_WEAK}。テロップは本文に語があるもの（言い換えは前後2秒のテロップに実在するもの）"
        "だけ数え、発話は分析 AI の聞き取り",
    ),
    (
        "保存の仕掛け",
        f"手順の段（手順の場面が{STEPS_MIN_SCENES}つ以上か番号つきのテロップ）・保存を促す CTA・"
        "分量を載せている（テロップかキャプションに大さじ・g などの分量）"
        f"のうち{SAVE_GOOD_SIGNALS}つ以上で{MARK_GOOD}、1 つで{MARK_OK}、無しで{MARK_WEAK}",
    ),
    (
        "CTA",
        f"CTA の型と出る秒が分かれば{MARK_GOOD}、CTA はあるが秒が不明なら{MARK_OK}、"
        f"無しで{MARK_WEAK}",
    ),
    (
        "商品の見せ方",
        f"商品・ブランドが合計{_n(BRAND_GOOD_SEC)}秒以上、主役か目立つ大きさで映れば{MARK_GOOD}、"
        f"映るがそれ未満なら{MARK_OK}、映らなければ{MARK_NONE}（評価の対象外）",
    ),
    (
        "一致度",
        f"テロップ・キャプション・映像の一致度（0〜100）が{COHERENCE_GOOD}以上で{MARK_GOOD}、"
        f"{COHERENCE_OK}以上で{MARK_OK}、それ未満で{MARK_WEAK}。分析 AI が食い違い（乖離）を"
        f"指摘していれば 1 段下げる。不明なら{MARK_NONE}",
    ),
)

_NUMBERED_TELOP = re.compile(
    r"^\s*(?:[①-⑳]|\d+\s*[.)．、:：]|STEP|Step|step|手順|ステップ|\d+つ目)"
)
_PROMINENT = ("hero", "prominent")
_PROMINENCE_RANK = {"hero": 0, "prominent": 1, "incidental": 2, "background": 3}
_RELATION_RANK = {"client": 0, "competitor": 1}
_UNIDENTIFIED_LOGO = "unidentified_logo"
_PROMINENCE_LABEL = {
    "hero": "主役",
    "prominent": "目立つ",
    "incidental": "付随",
    "background": "背景",
}
_RELATION_LABEL = {"client": "クライアント", "competitor": "競合"}
_LAYER_TELOP = "テロップ"
_LAYER_SPEECH = "発話"


def fmt_sec(value: float) -> str:
    """秒の表記（0.5秒・12秒）。"""
    return f"{round(value, 1):g}秒"


# ── 場面の行 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SceneRow:
    start: float
    end: float
    role: str
    role_inferred: bool
    desc: str
    telop: str
    speech: str
    intent: str
    frame: FrameShot | None = None

    @property
    def role_label(self) -> str:
        return ROLE_LABEL.get(self.role, ROLE_LABEL["other"])


def _ordered(a: VideoVSEOAnalysis) -> list[Scene]:
    return sorted(a.scenes, key=lambda sc: (sc.start_sec, sc.end_sec))


def _contains(scene: Scene, sec: float, *, last: bool) -> bool:
    end = max(scene.end_sec, scene.start_sec)
    return scene.start_sec <= sec < end or (last and sec == end)


def infer_roles(a: VideoVSEOAnalysis) -> list[tuple[str, bool]]:
    """開始秒の順の場面ごとに (役割, 推定したか)。欄があればそれを使い、無い場面だけ推定する。"""
    scenes = _ordered(a)
    cta_index: int | None = None
    if a.cta_sec is not None:
        cta_index = next(
            (
                i
                for i, sc in enumerate(scenes)
                if _contains(sc, a.cta_sec, last=i == len(scenes) - 1)
            ),
            None,
        )
    out: list[tuple[str, bool]] = []
    for i, sc in enumerate(scenes):
        if sc.role:
            out.append((sc.role, False))
        elif i == 0:
            out.append(("hook", True))
        elif i == cta_index:
            out.append(("cta", True))
        else:
            out.append((INFERRED_MIDDLE_ROLE, True))
    return out


def nearest_frame(frames: list[FrameShot], sec: float) -> FrameShot | None:
    """その秒に最も近い実フレーム（同じ距離なら早い方）。"""
    usable = [f for f in frames if f.data_uri]
    if not usable:
        return None
    return min(usable, key=lambda f: (abs(f.sec - sec), f.sec))


def _telops_in(a: VideoVSEOAnalysis, start: float, end: float) -> str:
    texts = [t.text.strip() for t in a.telops if start <= t.sec < max(end, start + 0.01)]
    return " ／ ".join(dict.fromkeys(t for t in texts if t))


def scene_rows(video: AnalyzedVideo, limit: int = MAX_SCENE_FRAMES) -> list[SceneRow]:
    """構成表の行（最大 ``limit`` 行・開始秒の順）。コマは場面の中央の秒に最も近いフレーム。"""
    a = video.analysis
    if a is None:
        return []
    roles = dict(zip(map(id, _ordered(a)), infer_roles(a), strict=True))
    clip = frame_duration(a.duration_sec, video.meta.duration_sec)  # コマを抜いた秒と同じ収め方
    rows: list[SceneRow] = []
    for sc in pick_scene_rows(a.scenes, limit):
        role, inferred = roles[id(sc)]
        rows.append(
            SceneRow(
                start=sc.start_sec,
                end=max(sc.end_sec, sc.start_sec),
                role=role,
                role_inferred=inferred,
                desc=sc.desc.strip(),
                telop=(sc.telop or _telops_in(a, sc.start_sec, sc.end_sec)).strip(),
                speech=(sc.speech or "").strip(),
                intent=(sc.intent or "").strip(),
                frame=nearest_frame(video.frames, scene_time(sc, clip)),
            )
        )
    return rows


def omitted_scenes(video: AnalyzedVideo, limit: int = MAX_SCENE_FRAMES) -> int:
    a = video.analysis
    return max(0, len(a.scenes) - limit) if a is not None else 0


# ── 時間配分 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoleShare:
    role: str
    sec: float
    pct: int

    @property
    def label(self) -> str:
        return ROLE_LABEL.get(self.role, ROLE_LABEL["other"])


def role_shares(a: VideoVSEOAnalysis) -> list[RoleShare]:
    """役割ごとの合計秒と割合（全場面・役割の固定の順）。割合は場面の合計秒が分母。"""
    secs: dict[str, float] = {}
    for sc, (role, _inferred) in zip(_ordered(a), infer_roles(a), strict=True):
        secs[role] = secs.get(role, 0.0) + max(0.0, sc.end_sec - sc.start_sec)
    total = sum(secs.values())
    if total <= 0:
        return []
    order = {r: i for i, r in enumerate(ROLE_LABEL)}
    return [
        RoleShare(role=r, sec=round(s, 1), pct=round(s / total * 100))
        for r, s in sorted(secs.items(), key=lambda kv: order.get(kv[0], 99))
        if s > 0
    ]


def role_flow(a: VideoVSEOAnalysis) -> list[str]:
    """役割の並び（続く同じ役割はまとめる）。"""
    flow: list[str] = []
    for role, _inferred in infer_roles(a):
        if not flow or flow[-1] != role:
            flow.append(role)
    return flow


# ── 主要な数字 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VideoKeys:
    duration_sec: float
    cut_count: int | None
    avg_cut_sec: float | None
    first_telop_sec: float | None
    kw_first_sec: float | None
    kw_first_layer: str
    kw_matched_no_sec: bool
    kw_in_caption: bool
    has_brand: bool
    brand_name: str
    brand_first_sec: float | None
    brand_total_sec: float
    brand_prominence: str
    brand_relation: str
    cta_sec: float | None
    cta_types: list[str] = field(default_factory=list)
    cta_text: str = ""
    has_cta: bool = False
    narration: bool = False
    trending: str = "unknown"
    brand_others: tuple[str, ...] = ()  # 見出しのブランド以外に映るブランド（名前だけ）


def kw_terms(a: VideoVSEOAnalysis, query: str | None) -> list[str]:
    """照合に使う検索語。検索 KW が渡されなければ、分析 AI が記録した keyword から取る。"""
    return query_terms(query) if query else analysis_terms(a)


def _kw_first(hits_in: tuple[KwHit, ...]) -> tuple[float | None, str, bool]:
    """照合済みの KW（evidence.kw_hits）のうち、テロップか発話で最も早い秒。

    テロップは本文に語があるもの・言い換えは秒 ±2 のテロップに実在するものだけ（Gemini の
    kw_match は使わない）。発話は分析 AI の聞き取りで、秒が無ければ「出るが秒は不明」。
    """
    hits: list[tuple[float, str]] = []
    matched_no_sec = False
    for h in hits_in:
        if h.layer == "telop":
            hits.extend((s, _LAYER_TELOP) for s in h.secs)
        elif h.layer == "speech":
            if h.secs:
                hits.extend((s, _LAYER_SPEECH) for s in h.secs)
            else:
                matched_no_sec = True
    if hits:
        sec, layer = min(hits, key=lambda h: (h[0], h[1]))
        return sec, layer, False
    return None, "", matched_no_sec


@dataclass(frozen=True)
class BrandSummary:
    """1 つのブランドの検出をまとめたもの（名前・関係・目立ち方・秒は同じブランドから取る）。"""

    name: str
    relation: str  # client / competitor / ""（それ以外）
    prominence: str  # そのブランドの検出のうち最も目立つもの
    first_sec: float | None
    total_sec: float


def _brand_name(b: BrandDetection) -> str:
    name = b.brand_name.strip()
    return "ロゴ（不明）" if not name or name == _UNIDENTIFIED_LOGO else name


def brand_summaries(a: VideoVSEOAnalysis, roster: Roster | None = None) -> list[BrandSummary]:
    """ブランドごとにまとめ、見出しにする順（クライアント→競合→目立つ→長く映る→先に出た）に並べる。

    検出はブランドの名前でまとめる（同じブランドが看板とパッケージで 2 つ出ることがある）。
    目立ち方・初出・合計秒はそのブランドの検出だけから取り、ほかのブランドと混ぜない。
    区分（クライアント／競合）は名簿（roster）でコードが決める。分析 AI の brand_relation は
    使わない（クライアント名を渡していないのに client と推測した例があった）。
    """
    roster = roster or Roster()
    groups: dict[str, list[BrandDetection]] = {}
    for b in a.brand_detections:
        groups.setdefault(_brand_name(b).casefold(), []).append(b)
    out: list[tuple[tuple[int, int, float, int], BrandSummary]] = []
    for order, dets in enumerate(groups.values()):
        code_relation = roster.relation(_brand_name(dets[0]))
        relation = code_relation if code_relation in _RELATION_RANK else ""
        prominence = min((d.prominence for d in dets), key=lambda p: _PROMINENCE_RANK.get(p, 4))
        firsts = [s for d in dets for s in d.appear_sec]
        total = round(sum(d.total_screen_time_sec for d in dets), 1)
        summary = BrandSummary(
            name=_brand_name(dets[0]),
            relation=relation,
            prominence=prominence,
            first_sec=min(firsts) if firsts else None,
            total_sec=total,
        )
        rank = (
            _RELATION_RANK.get(relation, len(_RELATION_RANK)),
            _PROMINENCE_RANK.get(prominence, 4),
            -total,
            order,
        )
        out.append((rank, summary))
    return [summary for _rank, summary in sorted(out, key=lambda x: x[0])]


def video_keys(
    video: AnalyzedVideo, *, query: str | None = None, roster: Roster | None = None
) -> VideoKeys | None:
    """主要な数字。query（検索 KW）が無ければ分析 AI が記録した keyword で照合する。"""
    a = video.analysis
    if a is None:
        return None
    dur = duration_of(video)
    avg = round(dur / a.cut_count, 1) if a.cut_count and dur > 0 else None
    telop_secs = [t.sec for t in a.telops if t.text.strip()]
    hits = kw_hits(video.meta, a, kw_terms(a, query))
    kw_sec, kw_layer, kw_no_sec = _kw_first(hits)
    kw_caption = any(h.layer in ("caption", "hashtag") for h in hits)
    brands = brand_summaries(a, roster)
    best = brands[0] if brands else None
    return VideoKeys(
        duration_sec=dur,
        cut_count=a.cut_count,
        avg_cut_sec=avg,
        first_telop_sec=min(telop_secs) if telop_secs else None,
        kw_first_sec=kw_sec,
        kw_first_layer=kw_layer,
        kw_matched_no_sec=kw_no_sec,
        kw_in_caption=kw_caption,
        has_brand=best is not None,
        brand_name=best.name if best is not None else "",
        brand_first_sec=best.first_sec if best is not None else None,
        brand_total_sec=best.total_sec if best is not None else 0.0,
        brand_prominence=best.prominence if best is not None else "",
        brand_relation=best.relation if best is not None else "",
        brand_others=tuple(b.name for b in brands[1:]),
        cta_sec=a.cta_sec,
        cta_types=[cta_label(c) for c in dict.fromkeys(a.cta_type)],
        cta_text=(a.cta_text or "").strip(),
        has_cta=a.has_cta(),
        narration=a.has_narration,
        trending=a.is_trending_sound,
    )


def prominence_label(value: str) -> str:
    return _PROMINENCE_LABEL.get(value, "")


def relation_label(value: str) -> str:
    return _RELATION_LABEL.get(value, "")


_OTHERS_SHOWN = 2


def others_text(names: tuple[str, ...]) -> str:
    """見出し以外のブランドの名前（2 つまで・残りは「ほかN」）。"""
    shown = "・".join(names[:_OTHERS_SHOWN])
    rest = len(names) - _OTHERS_SHOWN
    return f"{shown}ほか{rest}" if rest > 0 else shown


# ── 評価 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Grade:
    axis: str
    mark: str
    reason: str

    @property
    def word(self) -> str:
        return MARK_WORD[self.mark]


def _grade_hook(a: VideoVSEOAnalysis, k: VideoKeys) -> Grade:
    # 冒頭（0〜3 秒）のテロップ: 分析 AI の申告（hook_has_caption）でなく、テロップの秒で見る。
    opening = k.first_telop_sec is not None and k.first_telop_sec <= OPENING_SEC
    early = k.first_telop_sec is not None and k.first_telop_sec <= HOOK_FIRST_TELOP_MAX_SEC
    typed = a.hook_type != "other"
    missing = [opening, early, typed].count(False)
    mark = MARK_GOOD if missing == 0 else MARK_OK if missing == 1 else MARK_WEAK
    parts = [
        "冒頭のテロップあり" if opening else "冒頭のテロップなし",
        f"最初のテロップ {fmt_sec(k.first_telop_sec)}"
        if k.first_telop_sec is not None
        else "テロップなし",
        f"フックは{hook_label(a.hook_type)}",
    ]
    return Grade("冒頭3秒の掴み", mark, "・".join(parts))


def _grade_tempo(k: VideoKeys) -> Grade:
    if k.avg_cut_sec is None or k.cut_count is None:
        return Grade("テンポ", MARK_NONE, "カット数が分からない")
    avg = k.avg_cut_sec
    mark = MARK_GOOD if avg <= TEMPO_GOOD_SEC else MARK_OK if avg <= TEMPO_OK_SEC else MARK_WEAK
    return Grade(
        "テンポ",
        mark,
        f"平均 {fmt_sec(avg)}/カット（{k.cut_count}カット・{fmt_sec(k.duration_sec)}）",
    )


def _grade_kw(k: VideoKeys) -> Grade:
    caption = "・キャプションにもあり" if k.kw_in_caption else ""
    if k.kw_first_sec is not None:
        sec = k.kw_first_sec
        mark = MARK_GOOD if sec <= KW_GOOD_SEC else MARK_OK if sec <= KW_OK_SEC else MARK_WEAK
        return Grade("KWの露出", mark, f"{k.kw_first_layer}に {fmt_sec(sec)} で初出{caption}")
    if k.kw_matched_no_sec:
        return Grade("KWの露出", MARK_OK, f"テロップか発話に出るが秒は不明{caption}")
    return Grade("KWの露出", MARK_WEAK, f"テロップにも発話にも出ない{caption}")


def _steps_signal(a: VideoVSEOAnalysis) -> bool:
    """手順の段があるか。推定の役割（真ん中の場面はすべて手順になる）では数えない。"""
    explicit_steps = sum(1 for sc in a.scenes if sc.role == "steps")
    numbered = sum(1 for t in a.telops if _NUMBERED_TELOP.match(t.text))
    return explicit_steps >= STEPS_MIN_SCENES or numbered >= STEPS_MIN_SCENES


def qty_places(a: VideoVSEOAnalysis, desc: str) -> list[str]:
    """分量を載せている場所（テロップ・キャプション）。"""
    places: list[str] = []
    if any(QTY_RE.search(fold(t.text)) for t in a.telops):
        places.append("テロップ")
    if QTY_RE.search(fold(desc)):
        places.append("キャプション")
    return places


def _grade_save(a: VideoVSEOAnalysis, desc: str) -> Grade:
    """保存の仕掛け。「見返す理由」（保存・シェアの動機の欄）は 5 本とも空でなく差が出ないので
    使わず、分量を載せているか（テロップかキャプション）で見る。"""
    signals: list[str] = []
    if _steps_signal(a):
        signals.append("手順の段")
    if "save" in a.cta_type:
        signals.append("保存を促す CTA")
    places = qty_places(a, desc)
    if places:
        signals.append(f"分量を{'と'.join(places)}に載せている")
    n = len(signals)
    mark = MARK_GOOD if n >= SAVE_GOOD_SIGNALS else MARK_OK if n == 1 else MARK_WEAK
    return Grade("保存の仕掛け", mark, "・".join(signals) if signals else "仕掛けは見当たらない")


def _grade_cta(k: VideoKeys) -> Grade:
    kinds = "・".join(k.cta_types) or "型は不明"
    if k.has_cta and k.cta_sec is not None and k.cta_types:
        return Grade("CTA", MARK_GOOD, f"{fmt_sec(k.cta_sec)} で{kinds}")
    if k.has_cta:
        when = f"{fmt_sec(k.cta_sec)} で" if k.cta_sec is not None else "秒は不明・"
        return Grade("CTA", MARK_OK, f"{when}{kinds}")
    return Grade("CTA", MARK_WEAK, "CTA なし")


def _grade_brand(k: VideoKeys) -> Grade:
    if not k.has_brand:
        return Grade("商品の見せ方", MARK_NONE, "商品・ブランドは映らない")
    good = k.brand_total_sec >= BRAND_GOOD_SEC and k.brand_prominence in _PROMINENT
    parts = [k.brand_name or "商品"]
    if relation_label(k.brand_relation):
        parts[0] += f"（{relation_label(k.brand_relation)}）"
    if k.brand_first_sec is not None:
        parts.append(f"初出 {fmt_sec(k.brand_first_sec)}")
    parts.append(f"合計 {fmt_sec(k.brand_total_sec)}")
    if prominence_label(k.brand_prominence):
        parts.append(prominence_label(k.brand_prominence))
    if k.brand_others:
        parts.append(f"ほかに{others_text(k.brand_others)}も映る")
    return Grade("商品の見せ方", MARK_GOOD if good else MARK_OK, "・".join(parts))


_MARK_DOWN = {MARK_GOOD: MARK_OK, MARK_OK: MARK_WEAK, MARK_WEAK: MARK_WEAK}


def _grade_coherence(a: VideoVSEOAnalysis) -> Grade:
    c = a.message_coherence
    if c is None:
        return Grade("一致度", MARK_NONE, "一致度が分からない")
    mark = MARK_GOOD if c >= COHERENCE_GOOD else MARK_OK if c >= COHERENCE_OK else MARK_WEAK
    if (a.divergence_note or "").strip():  # 食い違いを自分で指摘しているのに高得点のことがある
        return Grade("一致度", _MARK_DOWN[mark], f"{c}（100 が一致）・食い違いの指摘あり")
    return Grade("一致度", mark, f"{c}（100 が一致）")


def grade_video(
    video: AnalyzedVideo, *, query: str | None = None, roster: Roster | None = None
) -> list[Grade]:
    """評価軸ごとの ◎○△—（AXES の順）。動画を見て分析できていなければ空。"""
    a = video.analysis
    k = video_keys(video, query=query, roster=roster)
    if a is None or k is None or not is_watched(video):
        return []
    return [
        _grade_hook(a, k),
        _grade_tempo(k),
        _grade_kw(k),
        _grade_save(a, video.meta.desc),
        _grade_cta(k),
        _grade_brand(k),
        _grade_coherence(a),
    ]


# ── 共通点（上位に多い型・決定的）─────────────────────────────────────────


def common_points(
    videos: list[AnalyzedVideo], *, query: str | None = None, roster: Roster | None = None
) -> list[str]:
    """動画を見て分析できた本を横断して、多い型を本数つきで書く（LLM を通さない）。"""
    watched = [v for v in videos if is_watched(v) and v.analysis is not None]
    n = len(watched)
    if n == 0:
        return []
    analyses = [v.analysis for v in watched if v.analysis is not None]
    keys = [
        k for k in (video_keys(v, query=query, roster=roster) for v in watched) if k is not None
    ]
    points: list[str] = []
    hooks = Counter(hook_label(a.hook_type) for a in analyses).most_common()
    if hooks and hooks[0][1] >= 2:
        points.append(f"フックの型は{hooks[0][0]}が最多（{hooks[0][1]}/{n}本）")
    elif hooks:  # 1 本ずつなら「最多」とは書かない
        points.append("フックの型はそろっていない（" + "・".join(h for h, _ in hooks) + "）")
    early_kw = sum(1 for k in keys if k.kw_first_sec is not None and k.kw_first_sec <= KW_GOOD_SEC)
    points.append(f"検索 KW を{_n(KW_GOOD_SEC)}秒以内にテロップか発話で出す: {early_kw}/{n}本")
    avgs = [k.avg_cut_sec for k in keys if k.avg_cut_sec is not None]
    if avgs:
        points.append(f"平均カット秒の中央値: {fmt_sec(statistics.median(avgs))}")
    with_cta = [k for k in keys if k.has_cta]
    if with_cta:
        kinds = Counter(t for k in with_cta for t in k.cta_types).most_common()
        kind_text = "（" + "・".join(f"{t} {c}本" for t, c in kinds) + "）" if kinds else ""
        points.append(f"CTA あり: {len(with_cta)}/{n}本{kind_text}")
    flows = Counter(
        tuple(role_flow(a)) for a in analyses if a.scenes and any(sc.role for sc in a.scenes)
    ).most_common(1)
    if flows and flows[0][1] >= 2:
        flow = "、".join(ROLE_LABEL.get(r, r) for r in flows[0][0])
        points.append(f"構成の流れで多いもの: {flow} の順（{flows[0][1]}/{n}本）")
    grades = [grade_video(v, query=query, roster=roster) for v in watched]
    goods = Counter(g.axis for gs in grades for g in gs if g.mark == MARK_GOOD)
    if goods:
        top = max(goods.values())
        leaders = [axis for axis in AXES if goods.get(axis) == top]
        if len(leaders) == 1:
            points.append(f"{MARK_GOOD}がいちばん多い評価軸: {leaders[0]}（{top}/{n}本）")
        else:  # 同数なら 1 つを「いちばん」とは書かない（並んでいる軸を全部書く）
            points.append(
                f"{MARK_GOOD}が多い評価軸は並んでいる: {'・'.join(leaders)}（各{top}/{n}本）"
            )
    return points


__all__ = [
    "AXES",
    "BRAND_GOOD_SEC",
    "COHERENCE_GOOD",
    "COHERENCE_OK",
    "GRADE_RULES",
    "HOOK_FIRST_TELOP_MAX_SEC",
    "INFERRED_MIDDLE_ROLE",
    "KW_GOOD_SEC",
    "KW_OK_SEC",
    "MARK_GOOD",
    "MARK_NONE",
    "MARK_OK",
    "MARK_WEAK",
    "MARK_WORD",
    "QTY_RE",
    "ROLE_LABEL",
    "SAVE_GOOD_SIGNALS",
    "STEPS_MIN_SCENES",
    "TEMPO_GOOD_SEC",
    "TEMPO_OK_SEC",
    "BrandSummary",
    "Grade",
    "RoleShare",
    "SceneRow",
    "VideoKeys",
    "brand_summaries",
    "common_points",
    "fmt_sec",
    "grade_video",
    "infer_roles",
    "kw_terms",
    "nearest_frame",
    "omitted_scenes",
    "others_text",
    "prominence_label",
    "qty_places",
    "relation_label",
    "role_flow",
    "role_shares",
    "scene_rows",
    "video_keys",
]
