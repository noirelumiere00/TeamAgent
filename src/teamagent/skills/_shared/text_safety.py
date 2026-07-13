"""納品HTML / Slack 返信の安全化ヘルパ（PRリサーチ系スキル共通）。

背景（self-review 指摘）:
- 納品HTMLの <a href> はサードパーティ Apify actor の生出力やユーザー入力から組まれるため、
  javascript:/data: スキームを載せると presigned URL で開いたレポート上で任意JSが走る。
  → safe_href() で https かつ既知SNSホストのみリンク化し、それ以外はプレーン表示に落とす。
- X投稿本文/TikTokコメントは攻撃者が自由に書けるため、LLM が生成する自由記述フィールド
  （noise_note / hypothesis_summary / spike_analysis / insight 等）に注入文言が混じり、
  slack_summary や status message へ逐語転送されるとフィッシングの配信面になる。
  → sanitize_llm_text() で長さを制限し URL を伏字化する（投稿本文の逐語引用には使わない）。
"""

from __future__ import annotations

import re

# リンク化を許すホスト（サブドメイン含む末尾一致）。SNSの投稿/リール/動画URLのみ。
_ALLOWED_LINK_HOSTS = (
    "x.com",
    "twitter.com",
    "tiktok.com",
    "instagram.com",
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HOST_RE = re.compile(r"^https://([^/]+)/", re.IGNORECASE)


def safe_href(url: str) -> str | None:
    """https かつ既知SNSホストの URL だけを返す。危険/不明スキームは None（=非リンク化）。

    末尾一致でサブドメインを許容（www.instagram.com 等）。ポート・認証情報付きは弾く。
    """
    if not url:
        return None
    m = _HOST_RE.match(url.strip())
    if not m:
        return None
    host = m.group(1).lower()
    if "@" in host or ":" in host:  # userinfo / port は許可しない
        return None
    if any(host == h or host.endswith("." + h) for h in _ALLOWED_LINK_HOSTS):
        return url.strip()
    return None


def sanitize_llm_text(text: str, *, max_len: int = 600) -> str:
    """LLM生成の自由記述を安全化する（URL伏字＋長さ制限）。

    投稿本文の逐語引用（納品物）には使わない。あくまで noise_note / insight /
    spike_analysis 等の「ツールの注記」に対してのみ適用する。
    """
    if not text:
        return ""
    cleaned = _URL_RE.sub("［URL省略］", text).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "…"
    return cleaned


__all__ = ["safe_href", "sanitize_llm_text"]
