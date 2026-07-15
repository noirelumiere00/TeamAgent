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
    who = (
        f"@{p.author_handle}"
        if getattr(p, "author_handle", "")
        else (getattr(p, "author_name", "") or "投稿者不明")
    )
    verified = bool(getattr(p, "verified", False))
    verify_note = str(getattr(p, "verify_note", "") or "")
    note = f" ⚠️{_clean(verify_note, max_len=40)}" if (not verified and verify_note) else ""
    likes = int(getattr(p, "like_count", 0) or 0)
    return f"- 「{_clean(p.text)}」 — {who}（❤️{likes:,}）{note}"


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


# 注: needs/buzz の要約ビルダーは v1 descope（任意テーマの cls_project 肥大回避）に伴い削除。
# theme/keyword→取引先の名寄せ後に voice と同流儀で再導入する。


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
    "build_comment_summary_md",
    "build_voice_summary_md",
]
