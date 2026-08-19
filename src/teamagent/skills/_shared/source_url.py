"""検索・runtime から共用する出典 URL の整形ヘルパー。

出典 URL 方針（ユーザー最重要指示 2026-08-18）:
「出典やエビデンス部分は引用元の URL を出す。全ての機能で」。
ここは Slack 由来の出典を **決定論で** permalink 化する 1 か所。workspace が
分からなければ **URL を推測せず None**（壊れたリンクを出すくらいなら出さない）。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

_SLACK_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_SLACK_SOURCE_URI_RE = re.compile(
    r"^slack://(?P<channel_id>[A-Za-z0-9]+)/(?P<thread_ts>\d+(?:\.\d+)?)$"
)
_SLACK_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
_SLACK_TS_RE = re.compile(r"^\d+(?:\.\d+)?$")


def slack_workspace_domain() -> str:
    """permalink のサブドメイン（例 ``vectorinc``）。取れなければ空文字。

    ``SLACK_WORKSPACE_DOMAIN`` を先に見て、無ければ既存の ``SLACK_WORKSPACE``
    （terraform の ``var.slack_workspace`` が入る）へフォールバックする。
    ``vectorinc.slack.com`` のようにドメイン全体で渡されても受け付ける
    （運用でどちらの書き方をされても壊れないようにするため）。
    """
    for name in ("SLACK_WORKSPACE_DOMAIN", "SLACK_WORKSPACE"):
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            continue
        host = raw.removeprefix("https://").removeprefix("http://").rstrip("/")
        sub = host.removesuffix(".slack.com")
        if _SLACK_WORKSPACE_RE.fullmatch(sub):
            return sub
    return ""


def slack_permalink(channel_id: str, thread_ts: str) -> str | None:
    """``(channel_id, thread_ts)`` から Slack permalink を機械的に組み立てる。

    形式は ``https://<ws>.slack.com/archives/<channel_id>/p<ts の数字だけ>``。
    workspace が未設定、または id/ts の形が想定外なら **None**（fail-open: 呼び出し側は
    リンク無しで通常どおり応答する。壊れたリンクを出さないことを優先する）。
    """
    workspace = slack_workspace_domain()
    if not workspace:
        return None
    channel = (channel_id or "").strip()
    ts = (thread_ts or "").strip()
    if not _SLACK_CHANNEL_ID_RE.fullmatch(channel) or not _SLACK_TS_RE.fullmatch(ts):
        return None
    return f"https://{workspace}.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def slack_thread_permalink(source_uri: str) -> str | None:
    """内部 ``slack://`` 識別子をブラウザで開ける permalink に変換する。

    ``SLACK_WORKSPACE_DOMAIN`` / ``SLACK_WORKSPACE`` が未設定、または識別子・
    workspace 名が不正な場合は URL を推測せず ``None`` を返す。
    """
    match = _SLACK_SOURCE_URI_RE.fullmatch(source_uri.strip())
    if match is None:
        return None
    return slack_permalink(match.group("channel_id"), match.group("thread_ts"))


# ingest が source_uri に焼く「そのまま開ける URL」のホスト（シート行直リンク・
# Drive の web_view_link）。ここに無いホストは内部識別子扱いで URL を出さない。
_TRUSTED_HTTPS_SOURCE_HOSTS = frozenset({"docs.google.com", "drive.google.com"})


def source_link(source_uri: str) -> str | None:
    """内部 ``source_uri`` をブラウザで開ける出典 URL へ決定論で変換する。

    - ``slack://<channel>/<ts>`` → workspace permalink（workspace 未設定なら None）
    - ``https://`` かつ信頼ホスト（Google Sheets 行直リンク / Drive web_view_link）
      → そのまま返す
    - それ以外（``gdrive://`` 等の内部識別子・未知ホスト）→ None（URL を推測しない）
    """
    uri = (source_uri or "").strip()
    if uri.startswith("slack://"):
        return slack_thread_permalink(uri)
    if uri.startswith("https://"):
        host = (urlsplit(uri).hostname or "").lower()
        if host in _TRUSTED_HTTPS_SOURCE_HOSTS:
            return uri
    return None


__all__ = [
    "slack_permalink",
    "slack_thread_permalink",
    "slack_workspace_domain",
    "source_link",
]
