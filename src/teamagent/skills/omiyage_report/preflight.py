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
    return "\n".join(lines)


def build_accepted_message(input: OmiyageReportSubmitInput) -> str:
    return (
        f"お土産資料（対象: {input.brand} / 競合: {'、'.join(input.competitors)} / "
        f"一般KW: {'、'.join(input.keywords)}）の作成を受け付けました。"
        "完了までstatusを照会してください。完成したPPTXは依頼元のスレッドへ添付します。"
    )
