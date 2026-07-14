"""カタログ成果物 → 永続化用の構造化要約 markdown（Part1）。

生 HTML でなく素の markdown を documents の本文(1 chunk)にする。理由: export_vault が
excerpt を本文 chunk 先頭から作り、@AiLa 検索の対象にもなるため、営業が後から「何を研究/
提案したか」を読める素テキストが最適。要再確認(未検証)の声は ⚠️ 付きで残す（黙って捨てない）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from teamagent.skills._shared.text_safety import sanitize_llm_text

_JST = _dt.timezone(_dt.timedelta(hours=9))
_FOOTER = "\n---\n（@AiLa のカタログツールが自動生成した施策研究の記録）"
_MAX_POSTS = 12  # ノートに載せる代表投稿の上限（肥大抑制）


def _today() -> str:
    return _dt.datetime.now(_JST).strftime("%Y-%m-%d")


def _clean(text: str, *, max_len: int = 280) -> str:
    """LLM/投稿テキストを1行 markdown 用に安全化（制御文字除去・改行畳み・長すぎは省略）。"""
    s = sanitize_llm_text(text or "", max_len=max_len)
    return " ".join(s.split())


def _post_line(p: Any) -> str:
    """1 投稿を『「本文」 — @handle（❤️数）⚠️注記』の1行に。"""
    who = f"@{p.author_handle}" if getattr(p, "author_handle", "") else (
        getattr(p, "author_name", "") or "投稿者不明"
    )
    note = ""
    if not getattr(p, "verified", False) and getattr(p, "verify_note", ""):
        note = f" ⚠️{_clean(p.verify_note, max_len=40)}"
    return f"- 「{_clean(p.text)}」 — {who}（❤️{int(getattr(p, 'like_count', 0)):,}）{note}"


def build_voice_summary_md(out: Any) -> str:
    """① 世の中の声集め の要約 markdown。"""
    lines = [
        f"# {out.product_name} Xの声集め（X（旧Twitter））",
        "",
        f"取得 {out.searched}件 → 厳選 {out.selected}件"
        f"（実在検証 {out.verified_count} / 要再確認 {out.unverified_count}）・{_today()}",
    ]
    if getattr(out, "noise_note", ""):
        lines += ["", f"検索メモ: {_clean(out.noise_note)}"]
    lines += ["", "## 主要な声"]
    lines += [_post_line(p) for p in (out.posts or [])[:_MAX_POSTS]]
    lines.append(_FOOTER)
    return "\n".join(lines)


def build_needs_summary_md(out: Any) -> str:
    """② ニーズ発掘 の要約 markdown（分類軸＋インサイト仮説＋代表投稿）。"""
    lines = [f"# {out.theme} ニーズ発掘（X（旧Twitter））", "", f"作成 {_today()}"]
    if getattr(out, "hypothesis_summary", ""):
        lines += ["", "## インサイト仮説", _clean(out.hypothesis_summary, max_len=600)]
    if getattr(out, "clusters", None):
        lines += ["", "## ニーズ分類"]
        for c in out.clusters:
            lines.append(f"- **{_clean(c.label, max_len=60)}**: {_clean(c.insight, max_len=300)}")
    posts = out.posts or []
    if posts:
        lines += ["", "## 代表投稿"]
        lines += [_post_line(p) for p in posts[:_MAX_POSTS]]
    lines.append(_FOOTER)
    return "\n".join(lines)


def build_buzz_summary_md(
    *,
    keyword: str,
    start_date: str,
    end_date: str,
    daily_counts: list[dict[str, Any]],
    top_posts: list[Any],
    spike_analysis: str,
) -> str:
    """④ 効果測定 の要約 markdown（期間・総発話・山の読み方・バズ投稿TOP）。"""
    total = sum(int(d.get("count", 0) or 0) for d in (daily_counts or []))
    lines = [
        f"# {keyword} X発話量 効果測定（X（旧Twitter））",
        "",
        f"{start_date} 〜 {end_date}・総発話 {total:,}件・作成 {_today()}",
    ]
    if spike_analysis:
        lines += ["", "## 読み方", _clean(spike_analysis, max_len=800)]
    if top_posts:
        lines += ["", "## バズ投稿 TOP"]
        lines += [_post_line(p) for p in top_posts[:_MAX_POSTS]]
    lines.append(_FOOTER)
    return "\n".join(lines)


def build_comment_summary_md(out: Any, *, client_name: str) -> str:
    """⑤ コメント欄マイニング の要約 markdown（テーマ・語彙・ペイン/欲求）。"""
    lines = [f"# {client_name} コメント欄マイニング", "", f"作成 {_today()}"]
    if getattr(out, "cross_vocabulary", None):
        vocab = "・".join(_clean(v, max_len=30) for v in out.cross_vocabulary[:20])
        lines += ["", f"横断語彙: {vocab}"]
    for v in getattr(out, "videos", []) or []:
        if getattr(v, "key_themes", None):
            themes = "・".join(_clean(t, max_len=40) for t in v.key_themes[:8])
            lines += ["", f"## 主要テーマ: {themes}"]
        for label, items in (
            ("ペイン", getattr(v, "pain_points", None)),
            ("欲求", getattr(v, "desires", None)),
            ("購買シグナル", getattr(v, "purchase_signals", None)),
        ):
            if items:
                lines.append(f"- {label}: " + " / ".join(_clean(x, max_len=60) for x in items[:5]))
    lines.append(_FOOTER)
    return "\n".join(lines)


__all__ = [
    "build_buzz_summary_md",
    "build_comment_summary_md",
    "build_needs_summary_md",
    "build_voice_summary_md",
]
