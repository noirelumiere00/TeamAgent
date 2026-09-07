"""submit 前の対話受付（preflight）。

ローカルSkill の受付の型（references/intake-response-schema.md）に忠実:
- 必須 = 対象ブランド / 競合(1つ以上) / 一般KW(1つ以上)。不足があれば **着手せず**、
  受領済み情報（営業が書いた名前を原文表示）・不足の必須情報だけ・補完候補・
  営業がそのまま埋めて返せる回答欄 + 最後の作成指示文を1回で返す。
- 補完はカルテ・金庫から決定論に引けた候補だけ（無理に作らない）。注入可能な
  completion source が無ければ候補なしで不足リストだけ返す。
- 不足ゼロなら追加の「OK確認」を求めず作成へ進む（2ラリー設計の正本どおり）。

判定は純関数（client_name_guard と同じ決定論型）で、LLM を使わない。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from teamagent.skills.omiyage_report.schema import (
    MissingField,
    OmiyageReportSubmitInput,
    OmiyageSuggestion,
)

_FIELD_LABELS: dict[MissingField, str] = {
    "brand": "対象ブランド",
    "competitors": "競合ブランド（1社以上）",
    "keywords": "一般検索キーワード（1つ以上）",
}

_REPLY_FIELD_LABELS: dict[MissingField, str] = {
    "brand": "対象ブランド",
    "competitors": "競合ブランド",
    "keywords": "一般検索キーワード",
}

# needs_input の末尾に必ず付ける「逃げ道」1 行。3 項目を埋めて数十分待った末に望まない
# 検索面資料が届く事故（設計反証: 青木さん「提案資料作成して」）を、依頼者が一言で
# 骨子（文章）側へ切り替えられる形で塞ぐ。決定論文言（LLM が言い換えない）。
OUTLINE_FALLBACK_LINE = (
    "TikTok検索データの資料ではなく、提案書の骨子（文章）や過去提案をもとにした"
    "構成案が必要なら『骨子で』とだけ返信してください。"
)

# 所要目安のモデル（分）。実測に合わせる:
# - TikTok 取得: PR #377 の clamp（1軸 ≤30 本）後は 1 軸 ≈2 分（取得＋集計）。軸は逐次。
# - 動画分析: 1 本 ≈2.5 分（media worker 取得＋視覚AI・09-03 E2E 実測）÷ 並列度。
# - PPTX レンダ＋添付: ≈1 分。
# 「10〜30 分」と固定で言い切っていた受付文（実測 41 分と食い違い）をやめ、依頼内容
# （軸数）と環境（分析本数・並列度）から毎回算出して 5 分刻みで切り上げる。
FETCH_MINUTES_PER_AXIS = 2.0
ANALYSIS_MINUTES_PER_VIDEO = 2.5
RENDER_MINUTES = 1.0
_ESTIMATE_STEP_MINUTES = 5


@dataclass(frozen=True)
class DurationEstimate:
    """1 ジョブの所要目安（決定論・LLM 不使用）。"""

    axes: int
    analysis_videos: int
    analysis_concurrency: int

    @property
    def minutes(self) -> float:
        concurrency = max(1, self.analysis_concurrency)
        fetch = max(0, self.axes) * FETCH_MINUTES_PER_AXIS
        analysis = (
            math.ceil(max(0, self.analysis_videos) / concurrency) * ANALYSIS_MINUTES_PER_VIDEO
        )
        return fetch + analysis + RENDER_MINUTES

    @property
    def rounded_minutes(self) -> int:
        """利用者向けの丸め（5 分刻み切り上げ・最低 5 分）。"""
        step = _ESTIMATE_STEP_MINUTES
        return max(step, math.ceil(self.minutes / step) * step)


def estimate_duration(
    *,
    axes: int,
    analysis_videos: int,
    analysis_concurrency: int,
) -> DurationEstimate:
    return DurationEstimate(
        axes=axes,
        analysis_videos=analysis_videos,
        analysis_concurrency=analysis_concurrency,
    )


@dataclass(frozen=True)
class OmiyageSuggestions:
    """completion source が返す補完候補（全て任意・空でよい）。"""

    competitors: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    source: str = ""


# brand（空なら None）→ 候補。カルテ/金庫を引けない環境では None を注入する。
CompletionSource = Callable[[str], OmiyageSuggestions | None]


@dataclass(frozen=True)
class PreflightResult:
    missing: tuple[MissingField, ...]
    suggestions: tuple[OmiyageSuggestion, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class _Received:
    label: str
    value: str


def run_preflight(
    input: OmiyageReportSubmitInput,
    completion_source: CompletionSource | None = None,
) -> PreflightResult:
    """不足検出と補完候補の収集（決定論・副作用なし）。"""
    missing: list[MissingField] = []
    if not input.brand:
        missing.append("brand")
    if not input.competitors:
        missing.append("competitors")
    if not input.keywords:
        missing.append("keywords")
    if not missing:
        return PreflightResult(missing=())

    suggestions: list[OmiyageSuggestion] = []
    if completion_source is not None:
        try:
            candidates = completion_source(input.brand)
        except Exception:
            candidates = None  # 補完は best-effort。失敗しても受付は止めない。
        if candidates is not None:
            if "competitors" in missing and candidates.competitors:
                suggestions.append(
                    OmiyageSuggestion(
                        field="competitors",
                        candidates=list(candidates.competitors)[:8],
                        source=candidates.source,
                    )
                )
            if "keywords" in missing and candidates.keywords:
                suggestions.append(
                    OmiyageSuggestion(
                        field="keywords",
                        candidates=list(candidates.keywords)[:8],
                        source=candidates.source,
                    )
                )
    return PreflightResult(missing=tuple(missing), suggestions=tuple(suggestions))


def build_needs_input_message(
    input: OmiyageReportSubmitInput,
    result: PreflightResult,
) -> str:
    """intake-response-schema の初回応答構造に沿った決定論文言。"""
    lines: list[str] = [
        "判定：お土産資料（TikTok検索データ確認資料）を作成します。",
        "不足している必須情報があるため、まだ着手していません。",
        "",
    ]

    received: list[_Received] = []
    if input.brand:
        received.append(_Received("対象ブランド", input.brand))
    if input.competitors:
        received.append(_Received("競合ブランド", "、".join(input.competitors)))
    if input.keywords:
        received.append(_Received("一般検索キーワード", "、".join(input.keywords)))
    if input.official_tiktok_account:
        received.append(_Received("公式TikTokアカウント", input.official_tiktok_account))
    if received:
        lines.append("受領済み：")
        lines.extend(f"- {item.label}：{item.value}" for item in received)
        lines.append("")

    lines.append("不足している必須情報：")
    lines.extend(f"- {_FIELD_LABELS[name]}" for name in result.missing)
    lines.append("")

    if result.suggestions:
        lines.append("補完候補（カルテ・金庫から）：")
        for suggestion in result.suggestions:
            label = _REPLY_FIELD_LABELS[suggestion.field]
            lines.append(f"- {label}候補：{'、'.join(suggestion.candidates)}")
        lines.append("")

    if not input.official_tiktok_account:
        lines.append("あると精度が上がる情報（なくても作成可能）：")
        lines.append("- 公式TikTokアカウントURL（公式投稿の露出判定に使います）")
        lines.append("")

    lines.append("以下をコピーしてご返信ください。")
    lines.extend(f"{_REPLY_FIELD_LABELS[name]}：" for name in result.missing)
    if not input.official_tiktok_account:
        lines.append("公式TikTokアカウントURL：（任意）")
    lines.append("指示：この内容で資料を作成してください")
    lines.append("")
    lines.append(OUTLINE_FALLBACK_LINE)
    return "\n".join(lines)


def build_accepted_message(
    input: OmiyageReportSubmitInput,
    estimate: DurationEstimate,
) -> str:
    """受付の定型文（LLM を通さない決定論文言）。

    所要は依頼内容と環境から算出した ``estimate`` を「目安 約 M 分」と言い切る
    （固定の「10〜30 分」は実測 41 分と食い違っていた）。retry_after_seconds は
    status 再照会の間隔であって完成予定ではないので、秒単位の見込みはここに書かない。
    ツール名は出さない（進み具合は『まだ？』で聞けばよい）。
    """

    return (
        f"お土産資料（対象: {input.brand} / 競合: {'、'.join(input.competitors)} / "
        f"一般KW: {'、'.join(input.keywords)}）の作成を受け付けました。"
        f"目安 約 {estimate.rounded_minutes} 分"
        f"（TikTok 取得 {estimate.axes} 軸＋動画分析 最大 {estimate.analysis_videos} 本）。"
        "途中経過は『まだ？』で確認できます。"
        "完成したPPTXは依頼元のスレッド（DM ならこの DM）へ添付します。"
    )


def build_busy_message(*, running: int, position: int, wait_minutes: int) -> str:
    """同時実行の上限で受け付けられなかったときの定型文（LLM を通さない決定論文言）。

    「失敗」ではなく「順番待ち」であること・何番目か・順番が来るまでの目安（分）・
    まだ着手していないことを、営業がそのまま読める形で言い切る。ツール名や
    「60 秒後に再送」のような実態と合わない秒数は書かない。
    """

    wait = max(1, wait_minutes)
    return (
        f"いまお土産資料を{running}件作成中のため、この依頼は順番待ち {position} 番目です"
        f"（順番が来るまで目安あと約 {wait} 分・まだ着手していません）。"
        f"約 {wait} 分後に同じ内容でもう一度お申し付けください。"
        "作成中のぶんの進み具合は『まだ？』で確認できます。"
    )
