"""検索・runtime から共用する出典 URL の整形ヘルパー。"""

from __future__ import annotations

import os
import re

_SLACK_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_SLACK_SOURCE_URI_RE = re.compile(
    r"^slack://(?P<channel_id>[A-Za-z0-9]+)/(?P<thread_ts>\d+(?:\.\d+)?)$"
)


def slack_thread_permalink(source_uri: str) -> str | None:
    """内部 ``slack://`` 識別子をブラウザで開ける permalink に変換する。

    ``SLACK_WORKSPACE`` が未設定、または識別子・workspace 名が不正な場合は
    URL を推測せず ``None`` を返す。
    """

    workspace = os.environ.get("SLACK_WORKSPACE", "").strip()
    if not workspace or not _SLACK_WORKSPACE_RE.fullmatch(workspace):
        return None

    match = _SLACK_SOURCE_URI_RE.fullmatch(source_uri.strip())
    if match is None:
        return None

    channel_id = match.group("channel_id")
    thread_ts = match.group("thread_ts").replace(".", "")
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{thread_ts}"


__all__ = ["slack_thread_permalink"]
