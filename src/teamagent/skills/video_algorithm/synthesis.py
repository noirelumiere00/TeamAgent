"""複数動画の横断シンセシス（Gemini 2nd pass・概念の関連性/勝ちパターン仮説）。

1本ずつ構造分析済みの結果を要約してテキストプロンプト化し、generate_text で
CrossSynthesis を JSON 生成させる。決定的統計(stats)とは別の「解釈層」。
1本では横断概念にならないため n<2 はスキップ。失敗は graceful（None）でレポート続行。

統合の版（env VIDEO_ALGO_SYNTHESIS_VERSION・既定 v3）:
- v3（既定・仕様 v3 §3）: 入力は synthesis_input（コードが数えた特徴の表・分布・全テロップの
  個票・上位ボードの見出し・絵コンテのカットの秒）。出力は synthesis_checks.finalize で常に検査する
  （本数タグ・ρ の除去、段階名はコード、refs の照合、数の食い違い、御社・避けたい訴求・未計測
  指標・画角・タイアップの注記）。数の食い違いがあれば 1 回だけ作り直させる。
- v2 / v1（戻し先）: 旧 build_prompt_v2（テロップ要旨とキャプション 220 字の個票）と、下の数字の
  照合（ground_synthesis）だけ。常時の検査は掛からない。
  戻し方: ECS タスク定義の環境変数に VIDEO_ALGO_SYNTHESIS_VERSION=v2 を足して差し替える
  （再ビルド不要）。結果キャッシュのキーに版が入るので、版を変えると同じ KW でも作り直す。
1 本ずつの動画分析のプロンプト（system.md）の版は別の env VIDEO_ALGO_PROMPT_VERSION（既定 v2）。

数字の照合（ground_synthesis）: Gemini の自由記述はそのままレポート HTML と編集用スライドに
出る。入力（build_prompt の本文＋extra_context。system は入れない）に無い数字・実在しない
順位・相関係数（ρ）を含む文や項目を欄ごとに捨てる。既定は shadow（捨てずにログ
grounding_dropped だけ）で、env GROUNDING_MODE_VIDEO_ALGORITHM=enforce で捨てる。
shadow では出力（確信度を含む）を照合前と同じに保つ。確信度の天井も enforce のときだけ
新しい数え方（実在する順位・重複なし）にし、shadow では従来の len(supported_by) で決めて、
新しい数え方との差はログ（欄 win_hypotheses.confidence）にだけ出す。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.grounding import (
    RHO_TERMS,
    DropLedger,
    DropSink,
    NumberGrounder,
    grounding_mode,
)
from teamagent.skills.video_algorithm.evidence import KW_LAYER_LABEL, Roster, ranks_text
from teamagent.skills.video_algorithm.facts import brand_facts, detect_pr, duration_of
from teamagent.skills.video_algorithm.schema import (
    CODE_ONLY_ITEM,
    CODE_ONLY_TOP,
    SYNTHESIS_V3_FIELDS,
    AnalyzedVideo,
    CrossSynthesis,
    KwTermLayer,
    StatsAnalysis,
    VideoMeta,
)
from teamagent.skills.video_algorithm.synthesis_checks import (
    CheckLog,
    conflict_note,
    conflict_probe,
    finalize,
)
from teamagent.skills.video_algorithm.synthesis_input import (
    CORR_MIN_N,
    SynthesisContext,
    build_grounders,
    render_prompt,
)

logger = structlog.get_logger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_DESC_MAX = 220
_SKILL = "video_algorithm"
# 倍・%・万が付く数字は、0〜10 でも入力に無ければ通さない（「保存率が3倍」を作らせない）。
_STRICT_SUFFIXES = frozenset({"倍", "%", "万"})
# 反例の本文を捨てても「反例あり」の印は残す（空にすると _enforce_confidence の 1 段下げが
# 外れて確信度が上がってしまう）。
COUNTER_EXAMPLE_MARK = "反例あり（本文は入力に無い数値を含むため省略）"
Confidence = Literal["高", "中", "低"]
_CODE_ONLY_FIELDS = ("grounding_mode", "grounding_dropped")

SYNTHESIS_VERSION_ENV = "VIDEO_ALGO_SYNTHESIS_VERSION"
SYNTHESIS_VERSION = "v3"  # 既定（仕様 v3 §3）
LEGACY_SYNTHESIS_VERSIONS: tuple[str, ...] = ("v1", "v2")  # 旧 build_prompt と数字の照合だけ
_KNOWN_VERSIONS = (*LEGACY_SYNTHESIS_VERSIONS, SYNTHESIS_VERSION)
_BRACES_RE = re.compile(r"\{.*\}", re.DOTALL)


def synthesis_version_from_env() -> str:
    """env VIDEO_ALGO_SYNTHESIS_VERSION（未設定・空・未知の値なら既定の v3）。v2 で旧版に戻す。"""
    raw = os.environ.get(SYNTHESIS_VERSION_ENV, "").strip().lower()
    if raw in _KNOWN_VERSIONS:
        return raw
    if raw:
        logger.warning("video_synthesis_version_unknown", value=raw[:16])
    return SYNTHESIS_VERSION


def _video_brief(v: AnalyzedVideo, roster: Roster | None = None) -> str:
    a = v.analysis
    if a is None:
        return ""
    lm = a.layer_messages
    telop_gist = lm.telop if lm and lm.telop else " / ".join(t.text for t in a.telops[:4])
    # 区分は名簿でコードが決める（Gemini の brand_relation は渡さない。クライアント名が無いのに
    # client と推測した例があった）。名簿が無ければ「区分未指定」。
    brands = (
        "、".join(
            f"{b.name}({b.relation_label}・{b.prominence_label or '目立ち方不明'})"
            for b in brand_facts(v.meta, a, roster)
        )
        or "なし"
    )
    pr, pr_evidence = detect_pr(v.meta, a)
    pr_line = f"  タイアップ表記: あり（{pr_evidence}）\n" if pr else ""
    thumb = f"{v.thumb.tone_jp()}/{v.thumb.bright_jp()}" if v.thumb else "—"
    desc = (v.meta.desc or "")[:_DESC_MAX]
    coh = a.message_coherence if a.message_coherence is not None else "—"
    return (
        f"#{v.meta.rank}（保存率{v.meta.save_rate():.2f}% / 尺{duration_of(v.meta, a):.0f}s）\n"
        f"  主訴求: {a.main_message or '—'}\n"
        f"  訴求軸: {', '.join(a.value_propositions) or '—'}\n"
        f"  フック: {a.hook_type} / {a.hook_summary}\n"
        f"  テロップ要旨: {telop_gist or '—'}\n"
        f"  キャプション: {desc or '—'}\n"
        f"  CTA: {', '.join(a.cta_type) or 'なし'}\n"
        f"  ブランド: {brands}\n"
        f"{pr_line}"
        f"  サムネ色: {thumb}\n"
        f"  メッセージ一貫性: {coh}"
    )


def _stats_block_text(stats: StatsAnalysis | None) -> str:
    """計算済み統計を「## 横断統計」テキスト化（プランナーが根拠に書くため・読む順に並べる）。"""
    if stats is None or stats.sample_size == 0:
        return ""
    lines: list[str] = [
        f"## 横断統計（n={stats.sample_size}・有意性検定なし。各主張はこの数字を根拠に書け）"
    ]
    kc = stats.kw_coverage
    if kc.layer_fill:  # ① 検索面の穴（最優先）
        lines.append(
            "・KWカバレッジ層別: "
            + " / ".join(f"{k}{v}" for k, v in kc.layer_fill)
            + f"（重み付き平均{kc.avg_score_0_100:.0f}/100）※全員充足の層=前提条件、0の層=不要かも"
        )
        if kc.per_video:
            lines.append("    動画別: " + " / ".join(kc.per_video))
    if stats.hook_counts:  # ② フック
        lines.append(
            "・フック分布: "
            + " ".join(f"{h}×{c}" for h, c in stats.hook_counts)
            + f"（強フック {stats.strong_hook_ratio}）※過半数未満の型は第一指定にしない"
        )
    if kc.per_term:  # ①' 語ごと×層ごと（テロップは本文に実在するものだけ数えた値）
        lines.append(
            "・KW 語ごと（テロップは本文に実在するものだけ／発話はAI聞き取りで未照合）: "
            + " ／ ".join(_term_text(kc.per_term, term) for term in _terms_of(kc.per_term))
        )
    # ③ 上位帯の最小〜最大（旧「勝ち筋レンジ」）は渡さない。分布は全 n 本（⑤）だけ。
    cr: list[str] = []  # ④ 相関（方向の裏取り専用）
    for c in stats.correlations:
        if c.rho is None:
            cr.append(f"{c.feature}=判定不能(n<3 or 全員同値)")
        else:
            mono = f"{c.monotonic_hits}/{c.monotonic_total}"
            cr.append(f"{c.feature} ρ{c.rho:+.2f}({c.direction_label}・単調{mono})")
    if cr:
        lines.append("・相関(特徴×順位／方向のヒントのみ・結論や指示に使わない): " + " / ".join(cr))
    for d in stats.distributions:  # ⑤ 分布・外れ値
        ol = f"／外れ値#{d.outlier_rank}({d.outlier_note})" if d.outlier_rank else ""
        lines.append(f"・分布[{d.feature}]: 中央値{d.median} 範囲{d.min}–{d.max}{ol}")
    if stats.caveats:
        lines.append("・前提(必ず caveat に反映): " + " ".join(stats.caveats))
    return "\n".join(lines) + "\n\n"


def _terms_of(rows: list[KwTermLayer]) -> list[str]:
    return list(dict.fromkeys(r.term for r in rows))


def _term_text(rows: list[KwTermLayer], term: str) -> str:
    parts: list[str] = []
    for r in rows:
        if r.term != term:
            continue
        label = KW_LAYER_LABEL.get(r.layer, r.layer)
        text = f"{label}{len(r.exact_ranks)}/{r.n}"
        if r.synonym_ranks:
            text += f"・言い換え{len(r.synonym_ranks)}/{r.n}（{ranks_text(r.synonym_ranks)}）"
        if r.board_hits is not None and r.board_size:
            text += f"（上位{r.board_size}本では{r.board_hits}/{r.board_size}）"
        parts.append(text)
    return f"「{term}」" + "・".join(parts)


def build_prompt(
    analyzed: Sequence[AnalyzedVideo],
    query: str,
    stats: StatsAnalysis | None = None,
    roster: Roster | None = None,
    *,
    board: Sequence[VideoMeta] | None = None,
    avoid_terms: Sequence[str] | None = None,
) -> str:
    """v3 の本文（synthesis_input.render_prompt）。相関は n≥8 だけ・旧 win_ranges は渡さない。"""
    ctx = SynthesisContext.build(
        analyzed, query, board=board, roster=roster, avoid_terms=avoid_terms, stats=stats
    )
    return render_prompt(ctx)


def build_prompt_v2(
    analyzed: list[AnalyzedVideo],
    query: str,
    stats: StatsAnalysis | None = None,
    roster: Roster | None = None,
) -> str:
    """旧版（v1/v2 の統合プロンプト用）の本文。VIDEO_ALGO_SYNTHESIS_VERSION=v2 のときだけ使う。"""
    briefs = "\n".join(b for v in analyzed if (b := _video_brief(v, roster)))
    n = sum(1 for v in analyzed if v.analysis)
    return (
        f"# 検索KW: {query}\n"
        f"{_stats_block_text(stats)}"
        f"# 上位 {n} 本の構造分析（個票・rank紐付けのエビデンス源）:\n\n"
        f"{briefs}\n\n"
        "まず上の『横断統計』を読み、システム指示の統計ガードレールに従って、"
        "各主張に統計の裏付けを角括弧で併記しながら JSON を出力してください。"
    )


def parse_synthesis(text: str) -> CrossSynthesis | None:
    """所見＋JSONブロックを CrossSynthesis にパース（防御的）。"""
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # 照合の記録欄はコードだけが書く（LLM が JSON に書いた値は使わない）。v3 の欄は v3 の検査を
    # 通さないと出せないので、旧版の経路では LLM が書いても捨てる。
    for key in (*_CODE_ONLY_FIELDS, *SYNTHESIS_V3_FIELDS):
        data.pop(key, None)
    try:
        return CrossSynthesis.model_validate(data)
    except ValidationError:
        logger.warning("video_synthesis_validation_failed")
        return None


def _json_object(text: str) -> dict[str, Any] | None:
    """```json ブロック、無ければ最初の { から最後の } まで（v3 は出力が長いので両方読む）。"""
    for m in (_JSON_BLOCK_RE.search(text), _BRACES_RE.search(text)):
        if m is None:
            continue
        try:
            data = json.loads(m.group(1) if m.re is _JSON_BLOCK_RE else m.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _strip_code_only(data: dict[str, Any]) -> None:
    """コードだけが書く欄（段階・順位・照合の結果・カットの秒など）を LLM の JSON から捨てる。"""
    for key in CODE_ONLY_TOP:
        data.pop(key, None)

    def items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    def strip(obj: dict[str, Any], name: str) -> None:
        for key in CODE_ONLY_ITEM.get(name, ()):
            obj.pop(key, None)
        for child in ("refs", "cuts"):
            for item in items(obj.get(child)):
                strip(item, child)

    for name in ("summary_lines", "per_video", "directives", "avoid", "storyboards"):
        for obj in items(data.get(name)):
            strip(obj, name)
    for name in ("board_angles", "hypotheses"):
        for obj in items(data.get(name)):
            strip(obj, name)


def parse_synthesis_v3(text: str) -> CrossSynthesis | None:
    """v3 の出力を読む（v2 の欄が壊れていても v3 の欄は読む・コードだけの欄は捨てる）。"""
    data = _json_object(text)
    if data is None:
        return None
    _strip_code_only(data)
    try:
        return CrossSynthesis.model_validate(data)
    except ValidationError:
        v3 = {k: data[k] for k in SYNTHESIS_V3_FIELDS if k in data}
        try:
            syn = CrossSynthesis.model_validate(v3)
        except ValidationError:
            logger.warning("video_synthesis_validation_failed", version=SYNTHESIS_VERSION)
            return None
        logger.warning("video_synthesis_v2_fields_dropped", version=SYNTHESIS_VERSION)
        return syn


def _capped_confidences(
    syn: CrossSynthesis, n: int, valid_ranks: Iterable[int] | None = None
) -> list[Confidence]:
    """確信度の天井を n・支持本数・反例に機械的に連動（n小の誠実さ・敵対レビュー反映）。

    - n<3: 全仮説『低』（相関すら出ない領域＝共通点メモ扱い）
    - 『高』は「全数支持 ∧ 反例なし ∧ n≥5」を全て満たす時のみ。それ以外の高→中
    - 全数支持でない（過半数止まり）は中止まり
    - 反例ありは更に1段下げる
    - 支持本数: valid_ranks を渡すと実在する順位の重複なしで数える（[1,1,1,1,1] や実在しない
      順位で全数支持に見せかけない）。渡さないときは従来どおり len(supported_by)。
    """
    order = {"高": 2, "中": 1, "低": 0}
    real = set(valid_ranks) if valid_ranks is not None else None
    out: list[Confidence] = []
    for h in syn.win_hypotheses:
        if real is None:
            supported = len(h.supported_by)
        else:
            supported = len({r for r in h.supported_by if r in real})
        full = supported >= n
        lvl = order.get(h.confidence, 1)
        if h.counter_example and lvl > 0:
            lvl -= 1  # 反例ありは1段下げる（先に適用）
        if n < 3:
            lvl = 0  # 相関すら出ない領域＝全仮説「低」
        else:
            if not full and lvl > 1:
                lvl = 1  # 部分支持(過半数止まり)は中止まり
            if lvl == 2 and not (full and n >= 5):
                lvl = 1  # 高は全数支持かつn≥5のときのみ
        out.append("高" if lvl == 2 else "中" if lvl == 1 else "低")
    return out


def _enforce_confidence(
    syn: CrossSynthesis, n: int, valid_ranks: Iterable[int] | None = None
) -> None:
    """_capped_confidences の結果で確信度を上書きする。"""
    for h, level in zip(syn.win_hypotheses, _capped_confidences(syn, n, valid_ranks), strict=True):
        h.confidence = level


def _apply_confidence(syn: CrossSynthesis, n: int, ranks: list[int], ledger: DropLedger) -> None:
    """enforce は新しい数え方で確信度を決める。shadow は従来の数え方で決め、差をログだけに出す。"""
    if ledger.enforce:
        _enforce_confidence(syn, n, valid_ranks=ranks)
        return
    strict = _capped_confidences(syn, n, valid_ranks=ranks)
    _enforce_confidence(syn, n)  # 従来の数え方（len(supported_by)）＝照合前と同じ表示
    for h, level in zip(syn.win_hypotheses, strict, strict=True):
        if level != h.confidence:
            ledger("win_hypotheses.confidence", "confidence_capped")


def build_grounder(prompt: str, valid_ranks: Iterable[int]) -> NumberGrounder:
    """照合に使う入力＝Gemini に渡した本文（build_prompt＋extra_context）。system は入れない。

    system の例文の数字（旧 v1 の「0.5秒」など）を入力扱いにすると、例文どおりに書いた
    作り話の数字が通ってしまうため。統計ブロックの値を丸めた文は通す（rounding=True）。
    """
    return NumberGrounder.from_inputs(
        prompt,
        valid_ranks=valid_ranks,
        rounding=True,
        strict_suffixes=_STRICT_SUFFIXES,
    )


def _rank_reason(values: list[int], valid: frozenset[int] | None) -> str:
    """順位を絞ったときの理由（実在しない順位・重複）。"""
    parts: list[str] = []
    missing = sorted({v for v in values if valid is not None and v not in valid})
    if missing:
        parts.append("rank:" + ",".join(str(v) for v in missing))
    if len(set(values)) != len(values):
        parts.append("rank_dup")
    return ";".join(parts) or "rank_invalid"


def ground_synthesis(
    syn: CrossSynthesis, grounder: NumberGrounder, n: int, ledger: DropLedger
) -> CrossSynthesis:
    """欄ごとに照合する。enforce なら捨てた版を、shadow なら元のまま返す（どちらもログは出す）。

    記録欄 grounding_mode / grounding_dropped は enforce のときだけ書く（どちらも
    model_dump に載らない＝MCP の返却 JSON・キャッシュには出ない。件数はログで測る）。

    - headline: 入力に無い数字・実在しない順位・ρ があれば空（report の既存の代替で埋まる）
    - strategy / posting_design / client_pitch / shared_funnel: 文単位で捨てる（ρ の文も）
    - creative_brief: 項目単位で捨てる（全部落ちたら report の _next_actions に代わる）
    - win_hypotheses: supported_by を実在順位に絞り 0 本なら仮説ごと捨てる。仮説文に入力に
      無い数字があれば捨てる。so_what は空に、counter_example は印を残す
    - common_concepts / angle_clusters: videos を実在順位に絞り 0 本・入力に無い数字なら捨てる。
      prevalence は LLM の値を使わず「videos の本数/n」で作り直す
    - differentiators: rank が実在しない・edge に入力に無い数字なら捨てる
    - caveat: 表示していないので対象外（フッタは stats.caveats を出す）
    """
    g = syn.model_copy(deep=True)

    def text_ok(field_name: str, text: str, deny: Iterable[str] = ()) -> bool:
        why = grounder.reason(text, deny=deny)
        if why is None:
            return True
        ledger(field_name, why)
        return False

    def sentences(field_name: str, text: str) -> str:
        if not text:
            return text
        kept, reasons = grounder.keep_sentences(text, deny=RHO_TERMS)
        for why in reasons:
            ledger(field_name, why)
        return kept if reasons else text

    def ranks(field_name: str, values: list[int]) -> list[int]:
        kept = grounder.filter_ranks(list(values))
        if kept != list(values):
            ledger(field_name, _rank_reason(list(values), grounder.valid_ranks))
        return kept

    if g.headline and not text_ok("headline", g.headline, RHO_TERMS):
        g.headline = ""
    g.strategy = sentences("strategy", g.strategy)
    g.posting_design = sentences("posting_design", g.posting_design)
    g.client_pitch = sentences("client_pitch", g.client_pitch)
    g.creative_brief = [
        item for item in g.creative_brief if text_ok("creative_brief", item, RHO_TERMS)
    ]

    concepts = []
    for cc in g.common_concepts:
        cc.videos = ranks("common_concepts.videos", cc.videos)
        if not cc.videos:
            ledger("common_concepts", "no_valid_rank")
            continue
        if not text_ok("common_concepts", f"{cc.concept}\n{cc.gist}"):
            continue
        prevalence = f"{len(cc.videos)}/{n}"
        if cc.prevalence != prevalence:
            ledger("common_concepts.prevalence", "prevalence_rebuilt")
            cc.prevalence = prevalence
        concepts.append(cc)
    g.common_concepts = concepts

    angles = []
    for ac in g.angle_clusters:
        ac.videos = ranks("angle_clusters.videos", ac.videos)
        if not ac.videos:
            ledger("angle_clusters", "no_valid_rank")
            continue
        if not text_ok("angle_clusters", f"{ac.label_jp}\n{ac.why_works}"):
            continue
        angles.append(ac)
    g.angle_clusters = angles

    if g.shared_funnel is not None:
        g.shared_funnel.pattern = sentences("shared_funnel", g.shared_funnel.pattern)
        g.shared_funnel.save_logic = sentences("shared_funnel", g.shared_funnel.save_logic)

    diffs = []
    for d in g.differentiators:
        if grounder.valid_ranks is not None and d.rank not in grounder.valid_ranks:
            ledger("differentiators", f"rank:{d.rank}")
            continue
        if not text_ok("differentiators", d.edge):
            continue
        diffs.append(d)
    g.differentiators = diffs

    hyps = []
    for h in g.win_hypotheses:
        h.supported_by = ranks("win_hypotheses.supported_by", h.supported_by)
        if not h.supported_by:
            ledger("win_hypotheses", "no_valid_rank")
            continue
        if not text_ok("win_hypotheses", h.hypothesis):
            continue
        if h.so_what and not text_ok("win_hypotheses.so_what", h.so_what):
            h.so_what = ""
        if h.counter_example and not text_ok("win_hypotheses.counter_example", h.counter_example):
            h.counter_example = COUNTER_EXAMPLE_MARK
        hyps.append(h)
    g.win_hypotheses = hyps

    if not ledger.enforce:
        return syn  # shadow: 照合前と同じオブジェクト（記録欄も書かない。件数はログだけ）
    g.grounding_mode = ledger.mode
    g.grounding_dropped = ledger.count
    return g


def synthesize(
    gemini: GeminiClient,
    analyzed: list[AnalyzedVideo],
    query: str,
    *,
    request_id: str,
    prompt_version: str = SYNTHESIS_VERSION,
    stats: StatsAnalysis | None = None,
    extra_context: str = "",
    on_drop: DropSink | None = None,
    roster: Roster | None = None,
    board: Sequence[VideoMeta] | None = None,
    avoid_terms: Sequence[str] | None = None,
) -> tuple[CrossSynthesis | None, float]:
    """横断シンセシスを生成。stats を渡すと統計を根拠に推論させる。失敗で (None, 0.0)。

    prompt_version: 統合の版（synthesis.md の版）。既定 v3。v1/v2 は旧版（戻し先）。
    extra_context: 任意の追加文脈（カタログ⑥: 兄弟KW群・月間検索量）。prompt 末尾に足す。
    on_drop: 照合・検査で捨てた欄と理由を受け取る（テスト・計測用。ログは常に出る）。
    board / avoid_terms: v3 だけが使う（上位ボードの見出し・避けたい訴求）。
    """
    ok = [v for v in analyzed if v.analysis]
    if len(ok) < 2:  # 1本では「横断」概念にならない
        return None, 0.0
    if prompt_version in LEGACY_SYNTHESIS_VERSIONS:
        return _synthesize_legacy(
            gemini,
            ok,
            query,
            request_id=request_id,
            prompt_version=prompt_version,
            stats=stats,
            extra_context=extra_context,
            on_drop=on_drop,
            roster=roster,
        )
    return _synthesize_v3(
        gemini,
        ok,
        query,
        request_id=request_id,
        prompt_version=prompt_version,
        stats=stats,
        extra_context=extra_context,
        on_drop=on_drop,
        roster=roster,
        board=board,
        avoid_terms=avoid_terms,
    )


def _with_extra(prompt: str, extra_context: str) -> str:
    return f"{prompt}\n\n# 追加コンテキスト\n{extra_context}" if extra_context else prompt


def _synthesize_v3(
    gemini: GeminiClient,
    ok: list[AnalyzedVideo],
    query: str,
    *,
    request_id: str,
    prompt_version: str,
    stats: StatsAnalysis | None,
    extra_context: str,
    on_drop: DropSink | None,
    roster: Roster | None,
    board: Sequence[VideoMeta] | None,
    avoid_terms: Sequence[str] | None,
) -> tuple[CrossSynthesis | None, float]:
    """v3: 本文を作って 1 回呼ぶ。数の食い違いがあれば 1 回だけ作り直させ、常に検査する。"""
    try:
        system = load_prompt("video_algorithm", prompt_version, "synthesis")
        ctx = SynthesisContext.build(
            ok, query, board=board, roster=roster, avoid_terms=avoid_terms, stats=stats
        )
        if ctx.n < 2:  # 動画を見て分析できた本が 1 本以下（サムネだけの縮退）は横断にならない
            return None, 0.0
        prompt = _with_extra(render_prompt(ctx), extra_context)
        resp = gemini.generate_text(prompt, request_id, system=system)
        cost = float(resp.cost_usd)
        syn = parse_synthesis_v3(resp.text)
    except Exception as e:  # load/生成/パース/型 どこで失敗してもレポートは続行
        logger.warning("video_synthesis_failed", request_id=request_id, error=type(e).__name__)
        return None, 0.0
    if syn is None:
        logger.warning("video_synthesis_unparsed", request_id=request_id, version=prompt_version)
        return None, cost
    log = CheckLog(request_id=request_id, sink=on_drop)
    ledger = DropLedger(
        skill=_SKILL, mode=grounding_mode(_SKILL), request_id=request_id, sink=on_drop
    )
    try:
        conflicts = conflict_probe(syn, ctx)
        if conflicts:  # R6: 1 回だけ作り直させる（駄目なら finalize が後の方の文を落とす）
            for c in conflicts:
                log(c.field, f"conflict_retry:{c.group}")
            try:
                retry = gemini.generate_text(
                    f"{prompt}\n\n{conflict_note(conflicts)}", request_id, system=system
                )
                cost += float(retry.cost_usd)
                again = parse_synthesis_v3(retry.text)
            except Exception as e:
                logger.warning(
                    "video_synthesis_retry_failed", request_id=request_id, error=type(e).__name__
                )
                again = None
            if again is not None:
                syn = again
        # 照合の入力は最初の本文（作り直しの依頼文の数字は入力に数えない）。
        final = finalize(syn, ctx, log=log, grounders=build_grounders(ctx, prompt), ledger=ledger)
    except Exception as e:  # 検査できない文は出さない（レポートはコードの事実だけで描く）
        logger.warning(
            "video_synthesis_checks_failed", request_id=request_id, error=type(e).__name__
        )
        return None, cost
    if log.count or ledger.count:
        logger.info(
            "video_synthesis_checks_summary",
            request_id=request_id,
            version=prompt_version,
            checked=log.count,
            dropped=ledger.count,
            mode=ledger.mode,
            fields=sorted(set(log.fields) | set(ledger.fields)),
        )
    return final, cost


def recheck_synthesis(
    syn: CrossSynthesis,
    analyzed: Sequence[AnalyzedVideo],
    query: str,
    *,
    stats: StatsAnalysis | None = None,
    roster: Roster | None = None,
    board: Sequence[VideoMeta] | None = None,
    avoid_terms: Sequence[str] | None = None,
    request_id: str = "recheck",
    on_drop: DropSink | None = None,
) -> CrossSynthesis | None:
    """LLM を呼ばずに v3 の検査を掛け直す（キャッシュ済みの synthesis・名簿だけ違う再描画用）。"""
    ok = [v for v in analyzed if v.analysis]
    if len(ok) < 2:
        return None
    ctx = SynthesisContext.build(
        ok, query, board=board, roster=roster, avoid_terms=avoid_terms, stats=stats
    )
    ledger = DropLedger(
        skill=_SKILL, mode=grounding_mode(_SKILL), request_id=request_id, sink=on_drop
    )
    return finalize(
        syn,
        ctx,
        log=CheckLog(request_id=request_id, sink=on_drop),
        grounders=build_grounders(ctx, render_prompt(ctx)),
        ledger=ledger,
    )


def _synthesize_legacy(
    gemini: GeminiClient,
    ok: list[AnalyzedVideo],
    query: str,
    *,
    request_id: str,
    prompt_version: str,
    stats: StatsAnalysis | None,
    extra_context: str,
    on_drop: DropSink | None,
    roster: Roster | None,
) -> tuple[CrossSynthesis | None, float]:
    """旧版（v1/v2）: build_prompt_v2 と数字の照合（ground_synthesis）だけ。"""
    try:
        system = load_prompt("video_algorithm", prompt_version, "synthesis")
        prompt = _with_extra(build_prompt_v2(ok, query, stats, roster), extra_context)
        resp = gemini.generate_text(prompt, request_id, system=system)
        syn = parse_synthesis(resp.text)
        cost = float(resp.cost_usd)
    except Exception as e:  # load/生成/パース/型 どこで失敗してもレポートは続行
        logger.warning("video_synthesis_failed", request_id=request_id, error=type(e).__name__)
        return None, 0.0
    if syn is None:
        return None, cost
    ranks = [v.meta.rank for v in ok]
    ledger = DropLedger(
        skill=_SKILL, mode=grounding_mode(_SKILL), request_id=request_id, sink=on_drop
    )
    try:
        syn = ground_synthesis(syn, build_grounder(prompt, ranks), len(ok), ledger)
    except Exception as e:  # 照合の不具合でレポートを止めない。enforce では照合前の文を出さない
        logger.warning(
            "video_synthesis_grounding_failed",
            request_id=request_id,
            error=type(e).__name__,
            mode=ledger.mode,
        )
        if ledger.enforce:
            return None, cost
    _apply_confidence(syn, len(ok), ranks, ledger)
    if ledger.count:
        logger.info(
            "video_synthesis_grounding_summary",
            request_id=request_id,
            mode=ledger.mode,
            dropped=ledger.count,
            fields=sorted(set(ledger.fields)),
        )
    return syn, cost


__all__ = [
    "CORR_MIN_N",
    "COUNTER_EXAMPLE_MARK",
    "LEGACY_SYNTHESIS_VERSIONS",
    "SYNTHESIS_VERSION",
    "SYNTHESIS_VERSION_ENV",
    "build_grounder",
    "build_prompt",
    "build_prompt_v2",
    "ground_synthesis",
    "parse_synthesis",
    "parse_synthesis_v3",
    "recheck_synthesis",
    "synthesis_version_from_env",
    "synthesize",
]
