"""2 段目: 上位の動画 1 本ずつの「学べること・弱点」と、クライアント向けの絵コンテ案（LLM）。

入力は決定的に組んだ構成（video_structure: 場面の役割・テロップ・発話・狙い・主要な数字・評価）。
LLM（Bedrock）は 1 回だけ呼び、5 本分のメモと絵コンテ案をまとめて書かせる。

照合（共通部品 _shared/grounding.py の NumberGrounder・常に許す数なし）:
- 1 本ずつのメモは**その動画の入力の値**に現れる数字と、動画を見て分析できた順位だけを許す
  （ほかの動画の数字を混ぜた文を落とす）。
- 絵コンテ案は全部の動画の入力と共通点に現れる数字だけを許す。
- 合わない項目は 1 つずつ捨て、欄名と理由（数字・順位だけ・本文は渡さない）を記録する。
評価の記号（◎○△—）は LLM に付けさせない（コードが決めたものを入力に渡すだけ）。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from teamagent.skills._shared.grounding import DropSink, NumberGrounder, tone_down
from teamagent.skills._shared.text_safety import sanitize_llm_text
from teamagent.skills.search_surface_check.display import fmt_count
from teamagent.skills.search_surface_check.video_digest import (
    STRICT_ALWAYS_ALLOWED,
    duration_of,
    hook_label,
    is_watched,
)
from teamagent.skills.search_surface_check.video_structure import (
    grade_video,
    others_text,
    prominence_label,
    relation_label,
    scene_rows,
    video_keys,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo

# 絵コンテ案の段（コードが決める。LLM は段ごとに映すものとテロップを埋める）。
STORYBOARD_STAGES: tuple[str, ...] = (
    "0〜3秒（フック）",
    "3〜10秒",
    "10秒〜終盤",
    "最後の数秒（CTA）",
)
_ITEM_MAX = 120
_SHOW_MAX = 80
_TELOP_MAX = 40
_MAX_LEARN = 2
_MAX_WEAK = 1
# 出力の上限（notes_max_tokens）の見積もり: 日本語 1 字あたりのトークン（多めに）と、JSON の骨組み
# （キー・順位・括弧）の分。途中で切れると JSON が壊れ、メモと絵コンテ案が丸ごと消えるので、受け取る
# 最大の量より小さくしない（Haiku なので増える費用はわずか）。
_TOKENS_PER_CHAR = 1.5
_JSON_TOKENS_PER_VIDEO = 200
_JSON_TOKENS_BASE = 500
_CELL_MAX = 60
_MAX_TELOPS = 12


@dataclass
class VideoNote:
    rank: int
    learn: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)


@dataclass
class StoryboardStep:
    stage: str
    show: str = ""
    telop: str = ""


@dataclass
class StructureNotes:
    videos: dict[int, VideoNote] = field(default_factory=dict)
    storyboard: list[StoryboardStep] = field(default_factory=list)


def notes_max_tokens(n_videos: int) -> int:
    """メモの出力の上限（本数に応じる）。受け取る最大の字数（本数 × 3 項目 × 120 字＋絵コンテ
    4 段 × 120 字）に 1 字 1.5 トークンと JSON の骨組みを足す。

    5 本で約 2,300 字（4,920 トークン）・10 本（上限）で約 4,100 字（8,620 トークン）。
    字下げつきの JSON（Haiku がよく返す形）でも収まる。
    """
    n = max(1, n_videos)
    chars = n * (_MAX_LEARN + _MAX_WEAK) * _ITEM_MAX + len(STORYBOARD_STAGES) * (
        _SHOW_MAX + _TELOP_MAX
    )
    return math.ceil(chars * _TOKENS_PER_CHAR) + n * _JSON_TOKENS_PER_VIDEO + _JSON_TOKENS_BASE


def _clip(text: str, n: int = _CELL_MAX) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def _sec(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def structure_payload(video: AnalyzedVideo) -> dict[str, Any] | None:
    """1 本の構成（LLM に渡す・日本語の項目名・割り算をさせない）。

    動画を見て分析できていなければ None。
    """
    a = video.analysis
    keys = video_keys(video)
    if a is None or keys is None or not is_watched(video):
        return None
    row: dict[str, Any] = {
        "順位": video.meta.rank,
        "アカウント": video.meta.author,
        "尺（秒）": _sec(duration_of(video)),
        "フック": {"型": hook_label(a.hook_type), "要旨": _clip(a.hook_summary)},
        "主なメッセージ": _clip(a.main_message),
        "カット数": keys.cut_count,
        "平均カット秒": keys.avg_cut_sec,
        "最初のテロップの秒": _sec(keys.first_telop_sec),
        "検索KWの初出": (
            {"秒": _sec(keys.kw_first_sec), "場所": keys.kw_first_layer}
            if keys.kw_first_sec is not None
            else "出ない"
        ),
        "CTA": (
            {"秒": _sec(keys.cta_sec), "種類": keys.cta_types, "文言": _clip(keys.cta_text)}
            if keys.has_cta
            else "なし"
        ),
        "テロップ": [
            {"秒": _sec(t.sec), "文言": _clip(t.text, _TELOP_MAX)}
            for t in sorted(a.telops, key=lambda t: t.sec)[:_MAX_TELOPS]
            if t.text.strip()
        ],
        "ナレーション": keys.narration,
        "保存・シェアの動機": _clip(a.save_share_motivation),
        "評価": [{"軸": g.axis, "記号": g.mark, "理由": g.reason} for g in grade_video(video)],
        "場面": [
            {
                "秒": f"{r.start:g}〜{r.end:g}",
                "役割": r.role_label,
                "画面": _clip(r.desc),
                "テロップ": _clip(r.telop),
                "発話": _clip(r.speech),
                "狙い": _clip(r.intent),
            }
            for r in scene_rows(video)
        ],
    }
    if keys.has_brand:
        row["商品"] = {
            "名前": keys.brand_name,
            "初出の秒": _sec(keys.brand_first_sec),
            "合計秒": keys.brand_total_sec,
            "目立ち方": prominence_label(keys.brand_prominence),
            "関係": relation_label(keys.brand_relation),
        }
        if keys.brand_others:
            row["商品"]["ほかに映るブランド"] = others_text(keys.brand_others)
    if video.meta.play_count > 0 and video.meta.collect_count > 0:
        row["保存率%"] = round(video.meta.save_rate(), 1)
        row["再生"] = fmt_count(video.meta.play_count)
    return row


def _leaf_values(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [v for value in obj.values() for v in _leaf_values(value)]
    if isinstance(obj, list):
        return [v for value in obj for v in _leaf_values(value)]
    if obj is None or isinstance(obj, bool):
        return []
    return [str(obj)]


def _grounder(values: list[str], keyword: str, ranks: list[int]) -> NumberGrounder:
    return NumberGrounder.from_inputs(
        *values, keyword, valid_ranks=ranks, always_allowed=STRICT_ALWAYS_ALLOWED
    )


@dataclass(frozen=True)
class NotesPrompt:
    text: str
    per_video: dict[int, NumberGrounder]
    storyboard: NumberGrounder


def build_notes_prompt(
    template: str,
    *,
    keyword: str,
    client_name: str | None,
    videos: list[AnalyzedVideo],
    common: list[str],
) -> NotesPrompt | None:
    payloads = [p for p in (structure_payload(v) for v in videos) if p is not None]
    if not payloads:
        return None
    ranks = [int(p["順位"]) for p in payloads]
    text = template.format(
        keyword=keyword,
        client_name=client_name or "（指定なし）",
        stages_json=json.dumps(list(STORYBOARD_STAGES), ensure_ascii=False),
        common_json=json.dumps(common, ensure_ascii=False),
        videos_json=json.dumps(payloads, ensure_ascii=False),
    )
    per_video = {int(p["順位"]): _grounder(_leaf_values(p), keyword, ranks) for p in payloads}
    all_values = _leaf_values(payloads) + common + list(STORYBOARD_STAGES)
    return NotesPrompt(
        text=text, per_video=per_video, storyboard=_grounder(all_values, keyword, ranks)
    )


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


def _clean(value: Any, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return tone_down(sanitize_llm_text(" ".join(value.split()), max_len=max_len))


def ground_notes(
    raw: dict[str, Any], *, prompt: NotesPrompt, on_drop: DropSink | None = None
) -> StructureNotes:
    """LLM の出力を 1 項目ずつ照合して採用する（合わない項目だけ捨てる）。"""

    def keep(field_name: str, text: str, grounder: NumberGrounder) -> bool:
        why = grounder.reason(text)
        if why is not None:
            if on_drop is not None:
                on_drop(field_name, why)
            return False
        return True

    notes = StructureNotes()
    items = raw.get("videos")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        rank = item.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank not in prompt.per_video:
            continue
        grounder = prompt.per_video[rank]
        note = notes.videos.setdefault(rank, VideoNote(rank=rank))
        for key, limit, bucket in (
            ("learn", _MAX_LEARN, note.learn),
            ("weak", _MAX_WEAK, note.weak),
        ):
            values = item.get(key)
            for value in values if isinstance(values, list) else []:
                text = _clean(value, _ITEM_MAX)
                if text and len(bucket) < limit and keep(f"{key}:{rank}", text, grounder):
                    bucket.append(text)
    steps = raw.get("storyboard")
    steps = steps if isinstance(steps, list) else []
    for i, stage in enumerate(STORYBOARD_STAGES):
        item = steps[i] if i < len(steps) and isinstance(steps[i], dict) else {}
        show = _clean(item.get("show"), _SHOW_MAX)
        telop = _clean(item.get("telop"), _TELOP_MAX)
        if show and not keep(f"storyboard:{i}:show", show, prompt.storyboard):
            show = ""
        if telop and not keep(f"storyboard:{i}:telop", telop, prompt.storyboard):
            telop = ""
        if show or telop:
            notes.storyboard.append(StoryboardStep(stage=stage, show=show, telop=telop))
    return notes


def conclude_notes(
    converse: Callable[[str], tuple[str, float]],
    template: str,
    *,
    keyword: str,
    client_name: str | None,
    videos: list[AnalyzedVideo],
    common: list[str],
    on_drop: DropSink | None = None,
) -> tuple[StructureNotes | None, float]:
    """LLM でメモと絵コンテ案を作る。使えなければ None（章はメモ無しで出す・例外は呼び出し側）。"""
    prompt = build_notes_prompt(
        template, keyword=keyword, client_name=client_name, videos=videos, common=common
    )
    if prompt is None:
        return None, 0.0
    text, cost = converse(prompt.text)
    raw = _parse(text)
    if raw is None:
        if on_drop is not None:
            on_drop("all", "unparseable")
        return None, cost
    return ground_notes(raw, prompt=prompt, on_drop=on_drop), cost


__all__ = [
    "STORYBOARD_STAGES",
    "NotesPrompt",
    "StoryboardStep",
    "StructureNotes",
    "VideoNote",
    "build_notes_prompt",
    "conclude_notes",
    "ground_notes",
    "notes_max_tokens",
    "structure_payload",
]
