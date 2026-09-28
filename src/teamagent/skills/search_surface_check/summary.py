"""Slack に出す文面（ツールが組み立てる。OpenClaw はこれをそのまま返す前提）。

書式は SOUL.md「Slack の書き方」と同じ標準 Markdown（配信側が Slack 記法へ変換する）:
太字は `**語**`（`*語*` は斜体になる）、箇条書きは `- `。表（`|---|` やコードブロックの罫線）は
スマホで崩れるので使わず、1 行 1 事実・1 本 1 行にする。第三者の文字列（本文・表示名）は
記法を無効化してから差し込む（`<!channel>` などのメンション、`*` の強調、`[..](..)` のリンク）。
"""

from __future__ import annotations

import re

from teamagent.skills._shared.text_safety import sanitize_llm_text
from teamagent.skills.search_surface_check.display import (
    PLATFORM_LABEL,
    account_label,
    category_label,
    fmt_age,
    fmt_count,
    fmt_date,
    fmt_duration,
    fmt_pct,
    fmt_ranks,
)
from teamagent.skills.search_surface_check.schema import (
    KwSurface,
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
    SurfaceFacts,
    SurfacePost,
)

_TOP_FULL = 10
_TOP_COMPACT = 5
_TITLE_MAX = 26
_SLACK_TRANSLATE = str.maketrans(
    {
        "&": "＆",
        "<": "＜",
        ">": "＞",
        "*": "＊",
        "~": "〜",
        "`": "'",
        "|": "｜",
        "[": "［",
        "]": "］",
    }
)


def slack_safe(text: str) -> str:
    """第三者/LLM の文字列を Slack の記法として効かない形にする（改行も潰す）。"""
    return re.sub(r"\s+", " ", (text or "").translate(_SLACK_TRANSLATE)).strip()


def rho_words(rho: float) -> str:
    if rho >= 0.5:
        return "再生の多い順にほぼ並ぶ"
    if rho >= 0.2:
        return "再生の多い順に弱く並ぶ"
    return "再生の多い順ではない"


def _title(post: SurfacePost) -> str:
    text = slack_safe(post.desc)
    if not text:
        return ""
    return "「" + (text[:_TITLE_MAX] + "…" if len(text) > _TITLE_MAX else text) + "」"


def _post_line(post: SurfacePost, *, now_epoch: int) -> str:
    who = [category_label(post.category)]
    if post.author_followers > 0:
        who.append(f"{fmt_count(post.author_followers)}人")
    metrics = [f"{fmt_count(post.play_count)}回"]
    if post.appearances > 1:
        metrics.append(f"出現{post.appearances}回")
    if post.play_count > 0 and post.save_count > 0:
        metrics.append(f"保存{post.save_count / post.play_count * 100:.1f}%")
    if post.posted_at > 0:
        metrics.append(fmt_age(max(0, (now_epoch - post.posted_at) // 86_400)))
    marks = ""
    if post.is_client:
        marks += "［クライアント］"
    if post.is_pr:
        marks += "［PR表記］"
    handle = slack_safe(f"@{post.author}") if post.author else "不明"
    return (
        f"- {post.rank}位 {handle}（{'・'.join(who)}）{marks} "
        f"{'・'.join(metrics)} {_title(post)}".rstrip()
    )


def _structure_lines(
    facts: SurfaceFacts, *, keyword: str, input: SearchSurfaceCheckInput, compact: bool
) -> list[str]:
    lines: list[str] = []
    if facts.categories:
        parts = [
            f"{category_label(c.category)} {c.count}本（再生の{fmt_pct(c.play_share)}）"
            for c in facts.categories
        ]
        lines.append("- 投稿者: " + "／".join(parts))
    if facts.holders:
        parts = [
            f"{slack_safe(account_label(h.author, h.author_name))} {len(h.ranks)}枠"
            f"（{fmt_ranks(h.ranks)}）"
            for h in facts.holders[:3]
        ]
        lines.append("- 常連: " + "／".join(parts))
    elif facts.n:
        lines.append(f"- 常連: なし（{facts.n}本すべて別のアカウント）")
    if compact:
        return lines
    reach: list[str] = []
    if facts.small_in_top10 is not None:
        reach.append(f"フォロワー1万人未満が上位{facts.top10_n}本中 {facts.small_in_top10}本")
    if facts.reach_ratio_median is not None:
        reach.append(f"再生÷フォロワーの中央値 {facts.reach_ratio_median:g}倍")
    if reach:
        lines.append("- " + "／".join(reach))
    plays = [f"再生の中央値 {fmt_count(facts.median_plays)}回"]
    if facts.rank_play_rho is not None:
        plays.append(f"順位は{rho_words(facts.rank_play_rho)}（一致度 {facts.rank_play_rho:g}）")
    lines.append("- " + "／".join(plays))
    if facts.median_save_rate_pct is not None:
        line = f"- 保存率の中央値 {facts.median_save_rate_pct:g}%"
        if facts.save_leaders:
            top = facts.save_leaders[0]
            line += f"（最高は{top.rank}位 {slack_safe('@' + top.author)} {top.save_rate_pct:g}%）"
        lines.append(line)
    timing: list[str] = []
    if facts.median_age_days is not None:
        timing.append(f"投稿時期の中央値 {fmt_age(facts.median_age_days)}")
    if facts.recent_90d is not None:
        timing.append(f"直近90日の投稿 {facts.recent_90d}本")
    if facts.median_duration_sec is not None:
        timing.append(f"尺の中央値 {fmt_duration(facts.median_duration_sec)}")
    if timing:
        lines.append("- " + "／".join(timing))
    if facts.kw_in_text is not None:
        lines.append(
            f"- 本文かタグに「{slack_safe(keyword)}」の語をすべて含む: "
            f"{facts.kw_in_text}/{facts.n}本"
        )
    if facts.top_tags:
        tags = "・".join(f"#{slack_safe(t.tag)} {t.count}本" for t in facts.top_tags[:5])
        lines.append(f"- よく付くタグ: {tags}")
    if facts.pr_ranks:
        lines.append(f"- PR表記あり: {fmt_ranks(facts.pr_ranks)}")
    return lines


def _client_line(surface: KwSurface, input: SearchSurfaceCheckInput) -> str | None:
    facts = surface.facts
    parts: list[str] = []
    if input.client_accounts:
        if surface.client_ranks:
            parts.append(f"{fmt_ranks(surface.client_ranks)}に在圏")
        else:
            parts.append("クライアントの投稿は上位に無し")
    if input.client_name and facts is not None:
        name = slack_safe(input.client_name)
        if facts.mention_ranks:
            parts.append(
                f"「{name}」に触れた投稿 {len(facts.mention_ranks)}本"
                f"（{fmt_ranks(facts.mention_ranks)}）"
            )
        else:
            parts.append(f"「{name}」に触れた投稿は無し")
    return "- クライアント: " + "／".join(parts) if parts else None


def _conclusion_lines(surface: KwSurface) -> list[str]:
    c = surface.conclusion
    if c is None:
        return []
    lines = [f"**結論** {slack_safe(c.headline)}"] if c.headline else []

    def with_ranks(text: str, ranks: list[int]) -> str:
        return slack_safe(text) + (f"（{fmt_ranks(ranks)}）" if ranks else "")

    if c.winning:
        lines.append("- 勝ち筋: " + with_ranks(c.winning.text, c.winning.ranks))
    if c.gap:
        lines.append("- 空白: " + with_ranks(c.gap.text, c.gap.ranks))
    for action in c.actions:
        lines.append("- 打ち手: " + with_ranks(action.text, action.ranks))
    if c.angles:
        angles = "／".join(
            f"「{slack_safe(a.text)}」{fmt_ranks(a.ranks, limit=4)}" for a in c.angles
        )
        lines.append("- 上位に共通する切り口: " + angles)
    return lines


def build_slack_summary(
    out: SearchSurfaceCheckOutput,
    input: SearchSurfaceCheckInput,
    *,
    now_epoch: int,
    missing_platforms: list[tuple[str, str]],
) -> str:
    """Slack の文面を組む。

    surfaces は KW 順・媒体順に並んでいる前提。missing_platforms は取得できなかった
    (KW, 媒体) の組。
    """
    compact = len(out.surfaces) > 1
    today = fmt_date(now_epoch)
    lines: list[str] = []
    for i, surface in enumerate(out.surfaces):
        if i:
            lines.append("")
        platform = PLATFORM_LABEL.get(surface.platform, surface.platform)
        lines.append(
            f"**検索上位チェック**「{slack_safe(surface.keyword)}」{platform} "
            f"上位{len(surface.posts)}本・{today} 実測"
        )
        lines.extend(_conclusion_lines(surface))
        if surface.facts is not None:
            lines.append("**上位の顔ぶれ**")
            lines.extend(
                _structure_lines(
                    surface.facts, keyword=surface.keyword, input=input, compact=compact
                )
            )
        client = _client_line(surface, input)
        if client:
            if surface.facts is None:
                lines.append("**上位の顔ぶれ**")
            lines.append(client)
        top_n = _TOP_COMPACT if compact else _TOP_FULL
        if surface.posts:
            unit = "再生・保存率・投稿時期" if surface.platform == "tiktok" else "再生・出現回数"
            lines.append(f"**上位{min(top_n, len(surface.posts))}本**（{unit}）")
            lines.extend(_post_line(p, now_epoch=now_epoch) for p in surface.posts[:top_n])
    lines.append("")
    for keyword, platform in missing_platforms:
        lines.append(
            f"{PLATFORM_LABEL.get(platform, platform)}「{slack_safe(keyword)}」は"
            "データを取得できませんでした（取得できた媒体だけで分析しています）"
        )
    if out.report_url:
        total = sum(len(s.posts) for s in out.surfaces)
        lines.append(f"レポート（全{total}本の一覧つき・7日有効）: {out.report_url}")
    if out.warnings:
        lines.append("注意: " + " / ".join(sanitize_llm_text(w, max_len=120) for w in out.warnings))
    lines.append(f"_概算 ${out.total_cost_usd:.4f}_")
    return "\n".join(lines).strip()


__all__ = ["build_slack_summary", "rho_words", "slack_safe"]
