"""検索結果由来テキスト／URL の無害化（web_research 専用）。

前提: SERP のタイトル・スニペット・LLM 要約は **すべて攻撃者が書ける外部Webテキスト**。
Slack へ逐語転送されるとフィッシングの配信面になる。_shared/text_safety.safe_href は
SNS ホスト allowlist 専用で任意ドメインには使えないため、web 版をここに置く。

無害化でやること（display text）:
  ① 制御文字の除去（改行/タブは空白へ畳む）
  ② URL の伏字化（本文中の生 URL を出典と誤認させない・出典はサーバが機械付与する）
  ③ Slack mrkdwn / markdown のリンク書式を無力化（`<url|label>` と `[label](url)`）
  ④ 長さ上限

URL 側（safe_web_href）でやること:
  https のみ・userinfo/port 拒否・ホスト必須・空白/制御文字拒否・長さ上限。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# 改行/タブ以外の C0 制御文字と DEL（Slack 描画の破壊・不可視の細工に使われる）。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_RE = re.compile(r"(?:https?|ftp)://\S+|(?:javascript|data|vbscript|file):\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_URL_PLACEHOLDER = "［URL省略］"

# markdown/mrkdwn のリンク書式を作れる文字。全角へ落として「見た目は残すが機能しない」形にする。
_BRACKET_MAP = {"[": "［", "]": "］", "(": "（", ")": "）"}

_MAX_URL_LEN = 500


def sanitize_display_text(text: str, *, max_len: int) -> str:
    """外部Web由来テキストを Slack へ出せる形に落とす（不可逆・表示専用）。

    ここを通していない外部由来文字列を message / sources に載せてはいけない。
    """
    if not text:
        return ""
    cleaned = _CONTROL_RE.sub("", text)
    cleaned = _URL_RE.sub(_URL_PLACEHOLDER, cleaned)
    # mrkdwn の <...> リンク書式ごと殺す（& を先に置換しないと二重エスケープになる）。
    cleaned = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for src, dst in _BRACKET_MAP.items():
        cleaned = cleaned.replace(src, dst)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "…"
    return cleaned


def sanitize_summary(text: str, *, max_len: int) -> str:
    """LLM 要約の無害化。段落の改行は残しつつ、リンク書式と URL は殺す。"""
    if not text:
        return ""
    cleaned = _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    cleaned = _URL_RE.sub(_URL_PLACEHOLDER, cleaned)
    cleaned = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for src, dst in _BRACKET_MAP.items():
        cleaned = cleaned.replace(src, dst)
    lines = [_WS_RE.sub(" ", line).strip() for line in cleaned.split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "…"
    return cleaned


def sanitize_query(query: str, *, max_len: int = 200) -> str:
    """利用者クエリの無害化。プロンプトの区切り記号を持ち込ませない（枠の突破防止）。"""
    cleaned = _CONTROL_RE.sub(" ", query or "")
    cleaned = cleaned.replace("<<<", " ").replace(">>>", " ")
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned[:max_len].strip()


def safe_web_href(url: str) -> str | None:
    """https の実 URL だけを返す。それ以外（http/javascript/data/認証情報付き等）は None。

    任意ドメインが対象なのでホスト allowlist は持たない。スキームと形の検証だけを行う。
    """
    if not url:
        return None
    candidate = url.strip()
    if not candidate or len(candidate) > _MAX_URL_LEN:
        return None
    if _CONTROL_RE.search(candidate) or any(c.isspace() for c in candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    netloc = parts.netloc
    if not netloc or "@" in netloc:  # userinfo は許可しない
        return None
    try:
        host = parts.hostname
        port = parts.port  # 不正ポートは ValueError
    except ValueError:
        return None
    if port is not None:  # 明示ポートは許可しない（443 も含め弾く＝形を一意に保つ）
        return None
    if not host or "." not in host:
        return None
    return candidate


def host_of(url: str) -> str:
    """検証済み URL からホスト名を取り出す（chunk の domain を信用せず自前で導く）。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower()


__all__ = [
    "host_of",
    "safe_web_href",
    "sanitize_display_text",
    "sanitize_query",
    "sanitize_summary",
]
