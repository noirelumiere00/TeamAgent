"""Slack 投稿リンク（permalink）の解析と「添付し直してよいか」の判定（純関数・I/O 無し）。

投稿リンクの先の添付を読む経路（``ATTACHMENT_PERMALINK_ENABLED``・既定 OFF）の部品。

⚠️ 死守ライン:
  P1 **自ワークスペースのリンクだけ**。ドメインが ``SLACK_WORKSPACE_DOMAIN``（無ければ
     ``SLACK_WORKSPACE``）と一致しないもの・設定が無いときは拒否（fail-closed）。
  P2 形の決まったものだけ通す（``/archives/<C|G|D…>/p<16 桁>``・``thread_ts`` は ts の形）。
     ``cid`` が付いていて本体の channel と違えば拒否（どちらを読むか曖昧にしない）。
  P3 情報漏れの規則（``may_repost``）: 本人 DM での依頼は本人が見えるものを返してよい。
     チャンネル（公開・非公開・グループ DM）での依頼は、同じチャンネルの投稿か、
     ファイルが公開（``is_public`` が bool の True）のときだけ。判定できなければ不可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills._shared.source_url import slack_workspace_domain

_PERMALINK_RE = re.compile(
    r"^https://(?P<ws>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.slack\.com"
    r"/archives/(?P<channel>[CGD][A-Z0-9]{2,32})/p(?P<digits>\d{16})/?$"
)
_TS_RE = re.compile(r"^\d{10}\.\d{6}$")
_CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{2,32}$")

# 解析失敗の理由（Output.error にそのまま載る）。
ERR_BAD_PERMALINK = "bad_permalink"
ERR_OTHER_WORKSPACE = "other_workspace"


@dataclass(frozen=True)
class PermalinkTarget:
    """投稿リンクが指す 1 件（channel と ts。スレッド返信なら親の thread_ts も）。"""

    channel_id: str
    ts: str
    thread_ts: str = ""


def _strip_slack_markup(raw: str) -> str:
    """Slack 本文の ``<https://…|表示名>`` / ``<https://…>`` をそのまま渡されても URL に戻す。"""
    s = raw.strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].split("|", 1)[0].strip()
    return s


def parse_permalink(raw: str) -> tuple[PermalinkTarget | None, str]:
    """投稿リンクを ``(target, error)`` に解析する（片方だけが有効）。

    受け付ける形:
      ``https://<ws>.slack.com/archives/<C|G|D…>/p<16 桁>``
      ``…?thread_ts=<10 桁>.<6 桁>&cid=<channel>``（スレッド返信のリンク）
    """
    url = _strip_slack_markup(raw or "")
    if not url or len(url) > 500:
        return None, ERR_BAD_PERMALINK
    parts = urlsplit(url)
    base = f"{parts.scheme}://{(parts.netloc or '').lower()}{parts.path}"
    m = _PERMALINK_RE.fullmatch(base)
    if m is None:  # userinfo・port・別ホストは netloc が正規表現に合わない
        return None, ERR_BAD_PERMALINK
    workspace = slack_workspace_domain()
    if not workspace or m.group("ws") != workspace:
        return None, ERR_OTHER_WORKSPACE
    channel = m.group("channel")
    digits = m.group("digits")
    ts = f"{digits[:10]}.{digits[10:]}"
    query = parse_qs(parts.query, keep_blank_values=False)
    thread_ts = (query.get("thread_ts") or [""])[0].strip()
    if thread_ts and not _TS_RE.fullmatch(thread_ts):
        return None, ERR_BAD_PERMALINK
    cid = (query.get("cid") or [""])[0].strip()
    if cid and (not _CHANNEL_RE.fullmatch(cid) or cid != channel):
        return None, ERR_BAD_PERMALINK
    return PermalinkTarget(channel_id=channel, ts=ts, thread_ts=thread_ts), ""


def may_repost(
    *,
    origin_channel: str,
    identity_verified: bool,
    source_channel: str,
    file_is_public: bool,
) -> bool:
    """リンク先のファイルの中身と原本を、依頼が来た場所へ出してよいか（P3）。

    - 本人 DM（D…・署名済み本人）: 宛先は本人だけ＝本人の可視範囲を出ない → 可
    - 同じチャンネルの投稿: その場にいる人は元から見られる → 可
    - それ以外のチャンネル: ファイルが公開（全メンバーが見られる）ときだけ可
      （非公開チャンネル・DM・グループ DM のファイルを、見られない人のいる場所へ出さない）
    """
    origin = (origin_channel or "").strip()
    if not origin:
        return False
    if is_private_surface(origin, identity_verified):
        return True
    if origin == source_channel:
        return True
    return file_is_public is True


__all__ = [
    "ERR_BAD_PERMALINK",
    "ERR_OTHER_WORKSPACE",
    "PermalinkTarget",
    "may_repost",
    "parse_permalink",
]
