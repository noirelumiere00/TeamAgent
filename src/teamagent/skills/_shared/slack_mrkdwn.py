"""標準 Markdown の太字を Slack mrkdwn の太字へ戻す（Slack API へ直接投稿する経路専用）。

skill の ``slack_summary`` は OpenClaw 経由（@openclaw/slack の markdownToSlackMrkdwn が
Markdown→mrkdwn へ変換する）を正として、見出しを標準 Markdown の ``**語**`` で書く。
一方、次の経路は ``chat.postMessage`` へ直接 mrkdwn で出すため変換が掛からず、
``**語**`` がそのまま（記号つきで）表示されてしまう:

- 旧 Socket Mode Bot（``runtime/slack_bot.py``）の video_algorithm 返信
- mcp の切り離し（detach）完了投稿（``mcp_gateway/detached_jobs.py``）

そこで投稿の直前にだけ ``**語**`` → ``*語*`` へ直す。他の記法（``_斜体_`` 等）は素通し。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from teamagent.skills._shared.slack_blocks import clip

_RULES_PATH = Path(__file__).with_name("outbound_rules.json")
if not _RULES_PATH.exists():  # ソース実行時。wheel とイメージには同じ正本を同梱する。
    _RULES_PATH = (
        Path(__file__).resolve().parents[4]
        / "infra/openclaw/caller-identity-plugin/outbound_rules.json"
    )
_OUTBOUND_RULES = json.loads(_RULES_PATH.read_text())
_ERROR = re.compile(_OUTBOUND_RULES["error_pattern"], re.MULTILINE | re.IGNORECASE)
_SIGNED_URL = re.compile(_OUTBOUND_RULES["signed_url_pattern"], re.IGNORECASE)
_BROKEN_URL = re.compile(_OUTBOUND_RULES["broken_url_pattern"], re.IGNORECASE)
_TERMS = {k.lower(): v for k, v in _OUTBOUND_RULES["internal_terms"].items()}
_TERM = re.compile(
    r"(?<![a-z0-9_])(?:" + "|".join(map(re.escape, _TERMS)) + r")(?![a-z0-9_])",
    re.IGNORECASE,
)
_TOOLS = _OUTBOUND_RULES["tool_names"]
_TOOL = re.compile(
    r"(?:`\s*(?:"
    + "|".join(_TOOLS)
    + r")\s*`|(?<![a-z0-9_])(?:teamagent__[a-z0-9_]*|"
    + "|".join(name for name in _TOOLS if "_" in name)
    + r")(?![a-z0-9_]))[ \t　]*(?:を[ \t　]*呼(?:び出して|んで|び出し|び)[ \t　]*[、,]?"
    r"|では?[ \t　]*|[をにのがはへと][ \t　]*)?[、,]?[ \t　]*",
    re.IGNORECASE,
)
_LINK_START = re.compile(r"<https?://|<[@!#][^<>\n]*>|\[[^\]\n]*\]\(https?://|https?://", re.I)
_BARE_STOP = re.compile(r'[\s<>"`|）」』、。！？]')
_PERMANENT = re.compile(_OUTBOUND_RULES["permanent_url_pattern"])
_DRIVE = re.compile(_OUTBOUND_RULES["drive_url_pattern"])


def outbound_permanent_urls(data: dict[str, object]) -> dict[str, str]:
    """同じ結果レコードに明記された原本だけを使う。別資料との推測照合はしない。"""
    original = next(
        (data[key] for key in _OUTBOUND_RULES["permanent_url_fields"] if data.get(key)), None
    )
    if not isinstance(original, str) or not _PERMANENT.fullmatch(original):
        return {}
    if _SIGNED_URL.search(original) or _BROKEN_URL.search(original):
        return {}
    return {
        value: original
        for key in _OUTBOUND_RULES["temporary_url_fields"]
        if isinstance(value := data.get(key), str) and _SIGNED_URL.search(value)
    }


def scrub_outbound_reply(text: str, permanent_urls: dict[str, str] | None = None) -> str:
    """既存の送信前整形。判定辞書は caller-identity plugin と共通。"""
    error = _OUTBOUND_RULES["error_text"]
    text, errors = _ERROR.subn(error + "\n", text)
    if errors:
        text = re.sub("(?:" + re.escape(error) + r"\n)+", error + "\n", text).rstrip()

    def plain(value: str) -> str:
        return _TOOL.sub("", _TERM.sub(lambda m: _TERMS[m[0].lower()], value))

    parts: list[str] = []
    end = 0
    while match := _LINK_START.search(text, end):
        start = match.start()
        token = match[0]
        if re.match(r"<[@!#]", token):
            parts.extend((plain(text[end:start]), token))
            end = match.end()
            continue
        url_start = match.end() - len(
            "https://" if token.lower().endswith("https://") else "http://"
        )
        close = -1
        if token.startswith("<"):
            close = text.find(">", url_start)
        elif token.startswith("["):
            depth = 1
            for i in range(url_start, len(text)):
                if text[i] == "\n":
                    break
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        close = i
                        break
        if close >= 0:
            token = text[start : close + 1]
            url = re.split(r"[\s|]", text[url_start:close], maxsplit=1)[0]
            next_end = close + 1
        else:
            start = url_start
            next_end = url_start
            while next_end < len(text) and not _BARE_STOP.match(text[next_end]):
                next_end += 1
            url = token = text[url_start:next_end]
        parts.append(plain(text[end:start]))
        if _SIGNED_URL.search(url) or _BROKEN_URL.search(url):
            original = (permanent_urls or {}).get(url.replace("&amp;", "&"))
            drive = _DRIVE.match(url) if not _BROKEN_URL.search(url) else None
            if not original and drive:
                original = f"https://drive.google.com/file/d/{drive[1]}/view"
            if (
                original
                and _PERMANENT.fullmatch(original)
                and not (_SIGNED_URL.search(original) or _BROKEN_URL.search(original))
            ):
                parts.append(original)
            else:
                parts.append(_OUTBOUND_RULES["omitted_link_text"])
        else:
            # URL は保護し、表示名・Markdownタイトルは本文と同じ規則で洗う。
            if close >= 0 and token.startswith("<") and "|" in token:
                label = scrub_outbound_reply(token.split("|", 1)[1][:-1], permanent_urls)
                parts.append(f"<{url}|{label.strip() or 'リンク'}>")
            elif close >= 0 and token.startswith("["):
                label_end = token.index("](")
                label = scrub_outbound_reply(token[1:label_end], permanent_urls)
                title = scrub_outbound_reply(token[label_end + 2 + len(url) : -1], permanent_urls)
                parts.append(f"[{label.strip() or 'リンク'}]({url}{title})")
            else:
                parts.append(token)
        end = next_end
    parts.append(plain(text[end:]))
    cleaned = clip("".join(parts), _OUTBOUND_RULES["max_reply_chars"])
    # clip が裸URL/Markdownの途中を切った場合はリンク全体を落とす。
    if cleaned.endswith("…"):
        cleaned = re.sub(r"\[[^\]\n]*\]\([^)]*…$|https?://[^\s]*…$", "…", cleaned)
    return cleaned


# 標準 Markdown の太字 `**語**`（語の前後が空白でないものだけ）。
_MARKDOWN_BOLD_PATTERN = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def markdown_bold_to_mrkdwn(text: str) -> str:
    """`**語**` を Slack mrkdwn の太字 `*語*` へ直す（直接投稿の経路専用・他の記法は素通し）。"""
    return _MARKDOWN_BOLD_PATTERN.sub(r"*\1*", text)


__all__ = ["markdown_bold_to_mrkdwn", "outbound_permanent_urls", "scrub_outbound_reply"]
