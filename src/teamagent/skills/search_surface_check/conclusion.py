"""面の読み（結論）: 集計と投稿一覧を LLM に渡し、根拠の数字を照合して採用する。

LLM が作った数字（集計に無い「再生の71%」など）は採用しない。照合は、LLM に渡した
入力の文字列に同じ数字が現れるかどうかで行う（計算して作った数字は入力に現れない）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from teamagent.skills._shared.text_safety import sanitize_llm_text
from teamagent.skills.search_surface_check.display import (
    PLATFORM_LABEL,
    category_label,
    fmt_count,
    fmt_pct,
)
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    SurfaceConclusion,
    SurfaceFacts,
    SurfacePost,
)

_HEADLINE_MAX = 60
_TEXT_MAX = 140
_ANGLE_MAX = 24
_MAX_ACTIONS = 2
_MAX_ANGLES = 4
_POST_TEXT_MAX = 80
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# 数えなくても書ける小さい数（「2つ」「1万人未満」の 1 など）と、帯・期間の境目の数。
_ALWAYS_ALLOWED = frozenset({str(i) for i in range(11)} | {"90", "100"})


def _numbers(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).replace(",", "")
    out: set[str] = set()
    for raw in _NUM_RE.findall(normalized):
        out.add(raw)
        if "." in raw:
            out.add(raw.rstrip("0").rstrip("."))
    return out


def facts_payload(facts: SurfaceFacts) -> dict[str, Any]:
    """LLM に渡す集計。割合は整数 % にして渡す（LLM に割り算をさせない）。"""
    payload: dict[str, Any] = {
        "上位の本数": facts.n,
        "アカウント数": facts.unique_authors,
        "投稿者タイプ": [
            {
                "タイプ": category_label(c.category),
                "本数": c.count,
                "本数の割合%": round(c.count_share * 100),
                "再生の割合%": round(c.play_share * 100),
                "再生の中央値": fmt_count(c.median_plays),
            }
            for c in facts.categories
        ],
        "常連（2枠以上のアカウント）": [
            {"アカウント": h.author, "表示名": h.author_name, "順位": h.ranks}
            for h in facts.holders
        ],
        "フォロワー帯": [
            {"帯": t.tier, "本数": t.count, "再生の割合%": round(t.play_share * 100)}
            for t in facts.tiers
        ],
        "再生の中央値": fmt_count(facts.median_plays),
    }
    optional: dict[str, Any] = {
        f"上位{facts.top10_n}本のうちフォロワー1万人未満の本数": facts.small_in_top10,
        "再生÷フォロワーの中央値": facts.reach_ratio_median,
        "最多再生の順位": facts.most_played_rank,
        "順位と再生数の一致度（1=再生の多い順・0=無関係）": facts.rank_play_rho,
        "保存率の中央値%": facts.median_save_rate_pct,
        "投稿からの日数の中央値": facts.median_age_days,
        "直近90日以内の投稿の本数": facts.recent_90d,
        "尺の中央値（秒）": facts.median_duration_sec,
        "本文かタグに検索KWの語をすべて含む本数": facts.kw_in_text,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    if facts.save_leaders:
        payload["保存率の上位"] = [
            {"順位": s.rank, "アカウント": s.author, "保存率%": s.save_rate_pct}
            for s in facts.save_leaders
        ]
    if facts.top_tags:
        payload["よく付くタグ"] = [{"タグ": f"#{t.tag}", "本数": t.count} for t in facts.top_tags]
    payload["PR表記の順位"] = facts.pr_ranks
    payload["クライアント投稿の順位"] = facts.client_ranks
    payload["クライアント名に触れた投稿の順位"] = facts.mention_ranks
    return payload


def posts_payload(posts: list[SurfacePost], *, now_epoch: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in posts:
        row: dict[str, Any] = {
            "rank": p.rank,
            "account": p.author,
            "name": p.author_name,
            "type": category_label(p.category),
            "plays": fmt_count(p.play_count),
        }
        if p.author_followers > 0:
            row["followers"] = fmt_count(p.author_followers)
        if p.play_count > 0 and p.save_count > 0:
            row["save_rate%"] = round(p.save_count / p.play_count * 100, 1)
        if p.posted_at > 0:
            row["days_ago"] = max(0, (now_epoch - p.posted_at) // 86_400)
        if p.duration_sec > 0:
            row["sec"] = p.duration_sec
        if p.appearances > 1:
            row["appearances"] = p.appearances
        if p.is_pr:
            row["pr"] = True
        if p.is_client:
            row["client"] = True
        row["text"] = re.sub(r"\s+", " ", p.desc)[:_POST_TEXT_MAX]
        out.append(row)
    return out


def rule_conclusion(facts: SurfaceFacts) -> SurfaceConclusion | None:
    """LLM が使えないときの見出し（集計だけで言えること）。"""
    if not facts.categories:
        return None
    top = facts.categories[0]
    headline = (
        f"上位{facts.n}本の最多は{category_label(top.category)}の{top.count}本"
        f"（再生の{fmt_pct(top.play_share)}）"
    )
    if facts.holders:
        h = facts.holders[0]
        headline += f"。常連は@{h.author}（{len(h.ranks)}枠）"
    return SurfaceConclusion(headline=headline, generated_by="rule")


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


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def ground_conclusion(
    raw: dict[str, Any],
    *,
    allowed_numbers: set[str],
    valid_ranks: set[int],
    on_drop: Callable[[str, str], None] | None = None,
) -> SurfaceConclusion | None:
    """LLM の出力を検査して採用する。入力に無い数字・実在しない順位を含む項目は捨てる。"""

    def drop(field: str, reason: str) -> None:
        if on_drop is not None:
            on_drop(field, reason)

    def grounded(field: str, text: str) -> bool:
        stray = _numbers(text) - allowed_numbers - _ALWAYS_ALLOWED
        if stray:
            drop(field, "number:" + ",".join(sorted(stray)))
            return False
        return True

    def ranks_of(value: Any) -> list[int]:
        ranks: list[int] = []
        for r in _as_list(value):
            if isinstance(r, int) and not isinstance(r, bool) and r in valid_ranks:
                if r not in ranks:
                    ranks.append(r)
        return ranks

    def point(field: str, value: Any, key: str = "text", max_len: int = _TEXT_MAX) -> Any:
        if not isinstance(value, dict):
            return None
        text = sanitize_llm_text(str(value.get(key) or "").strip(), max_len=max_len)
        if not text or not grounded(field, text):
            return None
        return ConclusionPoint(text=text, ranks=ranks_of(value.get("ranks")))

    headline = sanitize_llm_text(str(raw.get("headline") or "").strip(), max_len=_HEADLINE_MAX)
    if headline and not grounded("headline", headline):
        headline = ""  # 見出しだけ捨てる（呼び出し側が集計の見出しで埋める）
    actions = [
        a
        for a in (point("actions", v) for v in _as_list(raw.get("actions"))[:_MAX_ACTIONS])
        if a is not None
    ]
    angles: list[ConclusionPoint] = []
    for v in _as_list(raw.get("angles"))[:_MAX_ANGLES]:
        angle = point("angles", v, key="label", max_len=_ANGLE_MAX)
        if angle is None:
            continue
        if len(angle.ranks) < 2:  # 1 本だけなら「共通する切り口」ではない
            drop("angles", "ranks<2")
            continue
        angles.append(angle)
    conclusion = SurfaceConclusion(
        headline=headline,
        winning=point("winning", raw.get("winning")),
        gap=point("gap", raw.get("gap")),
        actions=actions,
        angles=angles,
    )
    if not (headline or conclusion.winning or conclusion.gap or actions or angles):
        return None
    return conclusion


def build_prompt(
    template: str,
    *,
    keyword: str,
    platform: str,
    client_name: str | None,
    facts: SurfaceFacts,
    posts: list[SurfacePost],
    now_epoch: int,
) -> tuple[str, set[str]]:
    """プロンプトと、照合に使う「入力に現れる数字」の集合を返す。"""
    facts_json = json.dumps(facts_payload(facts), ensure_ascii=False)
    posts_json = json.dumps(posts_payload(posts, now_epoch=now_epoch), ensure_ascii=False)
    prompt = template.format(
        keyword=keyword,
        platform=PLATFORM_LABEL.get(platform, platform),
        client_name=client_name or "（指定なし）",
        facts_json=facts_json,
        posts_json=posts_json,
    )
    return prompt, _numbers(facts_json) | _numbers(posts_json) | _numbers(keyword)


def conclude(
    converse: Callable[[str], tuple[str, float]],
    template: str,
    *,
    keyword: str,
    platform: str,
    client_name: str | None,
    facts: SurfaceFacts,
    posts: list[SurfacePost],
    now_epoch: int,
    on_drop: Callable[[str, str], None] | None = None,
) -> tuple[SurfaceConclusion | None, float]:
    """LLM で結論を作る。使えなければ集計だけの見出しに縮退する（例外は呼び出し側）。"""
    prompt, allowed = build_prompt(
        template,
        keyword=keyword,
        platform=platform,
        client_name=client_name,
        facts=facts,
        posts=posts,
        now_epoch=now_epoch,
    )
    text, cost = converse(prompt)
    raw = _parse(text)
    conclusion = (
        ground_conclusion(
            raw,
            allowed_numbers=allowed,
            valid_ranks={p.rank for p in posts},
            on_drop=on_drop,
        )
        if raw is not None
        else None
    )
    if conclusion is None:
        if on_drop is not None and raw is None:
            on_drop("all", "unparseable")
        return rule_conclusion(facts), cost
    if not conclusion.headline:
        fallback = rule_conclusion(facts)
        conclusion.headline = fallback.headline if fallback else ""
    return conclusion, cost


__all__ = [
    "build_prompt",
    "conclude",
    "facts_payload",
    "ground_conclusion",
    "posts_payload",
    "rule_conclusion",
]
