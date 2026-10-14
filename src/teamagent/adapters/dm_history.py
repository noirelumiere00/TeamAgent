"""Aico との DM（質問と回答の履歴）を、表示のたびに Slack から読む（上司 5・第 1 段）。

CLAUDE.md 6-bis Adapter 層。connect_web から slack_sdk を直接呼ばない。

守ること（設計: Artifacts/plan-20261007/history_view_design_v1_20261014.md）:
  - **本文を保存しない**（「本文は保存しない」原則・0007）。読んだ内容はその要求の中だけで使う。
    DB に書くのは管理者閲覧の監査（``history_audit``）だけ。
  - 読むのは bot token（xoxb）で、対象の 1 人の DM だけ。本人画面は「ログインした email →
    ``users.lookupByEmail`` → ``conversations.open``」で本人の DM に限る
    （他人の ID を受け取らない）。
  - 管理者は ``HISTORY_ADMIN_EMAILS``（空・未設定は誰もいない）。利用状況画面の
    ``CONNECT_ADMIN_EMAILS``・本人メモの ``PERSONAL_MEMORY_ADMIN_EMAILS`` とは共用しない。
  - DM には本人専用の一回限りのリンク（連携の開始 URL・署名付き S3 URL）が入る。
    表示前に :func:`redact_secret_links` で伏せる（管理者にも他人の連携リンクを見せない）。
  - **G8**: ログは件数・所要時間・Slack の error code だけ。本文・email・ID は出さない。
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog

logger = structlog.get_logger(__name__)

ADMIN_EMAILS_ENV: Final = "HISTORY_ADMIN_EMAILS"
HISTORY_DAYS_MAX: Final = 30
MESSAGE_CAP: Final = 1000
THREAD_EXPAND_CAP: Final = 50
AICO_REPLIES_PER_EXCHANGE: Final = 6
_PAGE_LIMIT: Final = 200
_SLACK_TIMEOUT_S: Final = 15
_USER_RE: Final = re.compile(r"U[A-Z0-9]{8,}")
_TEAM_RE: Final = re.compile(r"T[A-Z0-9]{8,}")
_CODE_RE: Final = re.compile(r"[a-z_]{1,40}")
_SKIP_SUBTYPES: Final = frozenset(
    {"channel_join", "channel_leave", "message_deleted", "message_changed", "tombstone"}
)

# 本人専用の一回限りのリンク。連携の開始 URL（Google / Slack）と、署名付き S3 URL。
_SECRET_URL_RES: Final = (
    re.compile(r"https?://[^\s|<>]*/(?:oauth2|slack/oauth)/start/[^\s|<>]*", re.IGNORECASE),
    # Slack は本文の & を &amp; で返すので、区切りは ? / & / &amp; のどれでもよい。
    re.compile(
        r"https?://[^\s|<>]*(?:[?&]|&amp;)"
        r"(?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=[^\s|<>]*",
        re.IGNORECASE,
    ),
)
SECRET_LINK_MASK: Final = "（本人専用のリンクのため表示しません）"
_SLACK_LINK_RE: Final = re.compile(r"<([^<>]*)>")


class DmHistoryError(RuntimeError):
    """読めなかった。``code`` は固定語（Slack の error code か、こちらの語）。値は載せない。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if _CODE_RE.fullmatch(code or "") else "slack_failed"


def admin_emails() -> frozenset[str]:
    """履歴を見られる管理者。空・未設定は誰もいない（利用状況画面のように既定へ倒さない）。"""
    raw = os.environ.get(ADMIN_EMAILS_ENV, "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_admin(email: str | None) -> bool:
    return bool(email) and str(email).strip().lower() in admin_emails()


def valid_user_id(slack_user_id: str) -> bool:
    return bool(_USER_RE.fullmatch(slack_user_id or ""))


def valid_team_id(team_id: str) -> bool:
    return bool(_TEAM_RE.fullmatch(team_id or ""))


@dataclass(frozen=True)
class DmMessage:
    ts: float
    role: Literal["user", "aico"]
    text: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Exchange:
    """利用者の 1 発言と、次の発言までの Aico の返事（最大 6 通・それ以上は件数だけ）。"""

    ts: float
    user_text: str
    files: tuple[str, ...]
    aico: tuple[str, ...]
    n_aico: int


@dataclass(frozen=True)
class DmUser:
    slack_user_id: str
    name: str


@dataclass(frozen=True)
class DmRead:
    messages: tuple[DmMessage, ...]
    truncated: bool


def _is_secret_url(url: str) -> bool:
    return any(r.search(url) or r.search(html.unescape(url)) for r in _SECRET_URL_RES)


def redact_secret_links(text: str) -> str:
    """一回限りのリンクを伏せる。

    ``<url|label>`` は label を残し、``<url>`` と裸の URL は伏せ字にする。
    """

    def _token(m: re.Match[str]) -> str:
        inner = m.group(1)
        url, _, label = inner.partition("|")
        if not _is_secret_url(url):
            return m.group(0)
        return f"{label}{SECRET_LINK_MASK}" if label else SECRET_LINK_MASK

    out = _SLACK_LINK_RE.sub(_token, text or "")
    for r in _SECRET_URL_RES:
        out = r.sub(SECRET_LINK_MASK, out)
    return out


def classify(raw: dict[str, Any], *, user_id: str) -> DmMessage | None:
    """Slack の 1 件を「利用者」か「Aico」に分ける。どちらでもないもの（参加通知など）は None。"""
    if raw.get("subtype") in _SKIP_SUBTYPES:
        return None
    try:
        ts = float(raw.get("ts") or "")
    except ValueError:
        return None
    if raw.get("bot_id") or raw.get("subtype") == "bot_message":
        role: Literal["user", "aico"] = "aico"
    elif raw.get("user") == user_id:
        role = "user"
    else:
        return None
    files = tuple(
        str(f.get("name") or f.get("title") or "ファイル")
        for f in (raw.get("files") or [])
        if isinstance(f, dict)
    )
    return DmMessage(ts=ts, role=role, text=str(raw.get("text") or ""), files=files)


def group_exchanges(
    messages: Sequence[DmMessage], *, aico_cap: int = AICO_REPLIES_PER_EXCHANGE
) -> list[Exchange]:
    """利用者の発言ごとに、次の発言までの Aico の返事を束ねる（古い順）。

    10/6・10/14 の全 DM 判定で使った区切り方と同じ。最初の発言より前の Aico だけの通知
    （朝のサマリー等）は質問ではないので束ねない。
    """
    ordered = sorted(messages, key=lambda m: m.ts)
    out: list[Exchange] = []
    for i, msg in enumerate(ordered):
        if msg.role != "user":
            continue
        replies: list[str] = []
        j = i + 1
        while j < len(ordered) and ordered[j].role != "user":
            replies.append(ordered[j].text)
            j += 1
        out.append(
            Exchange(
                ts=msg.ts,
                user_text=msg.text,
                files=msg.files,
                aico=tuple(replies[:aico_cap]),
                n_aico=len(replies),
            )
        )
    return out


def _data(resp: Any) -> dict[str, Any]:
    data = resp.data if hasattr(resp, "data") else resp
    return data if isinstance(data, dict) else {}


def _slack_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            code = str(_data(response).get("error") or "")
        except Exception:
            code = ""
        if code:
            return code
    return "slack_failed"


class DmHistoryReader:
    """bot token で 1 人の DM を読む。メソッドの失敗は :class:`DmHistoryError`（固定語）にする。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> DmHistoryReader:
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if not token:
            raise DmHistoryError("no_bot_token")
        from slack_sdk import WebClient

        return cls(WebClient(token=token, timeout=_SLACK_TIMEOUT_S))

    def _call(self, op: str, method: str, **kwargs: Any) -> dict[str, Any]:
        try:
            data = _data(getattr(self._client, method)(**kwargs))
        except Exception as exc:
            code = _slack_code(exc)
            logger.warning("dm_history_slack_failed", op=op, code=code)
            raise DmHistoryError(code) from None
        if not data.get("ok", True):
            code = str(data.get("error") or "slack_failed")
            logger.warning("dm_history_slack_failed", op=op, code=code)
            raise DmHistoryError(code)
        return data

    def user_id_for_email(self, email: str) -> str:
        """ログインした本人の email → Slack の U…（本人画面は他人の ID を受け取らない）。"""
        data = self._call("lookup", "users_lookupByEmail", email=email.strip().lower())
        uid = str((data.get("user") or {}).get("id") or "")
        if not valid_user_id(uid):
            raise DmHistoryError("user_not_found")
        return uid

    def _dm_channel(self, slack_user_id: str) -> str:
        data = self._call("open", "conversations_open", users=slack_user_id)
        channel = str((data.get("channel") or {}).get("id") or "")
        if not channel.startswith("D"):
            raise DmHistoryError("no_dm_channel")
        return channel

    def read(self, slack_user_id: str, *, days: int, now: float | None = None) -> DmRead:
        """直近 ``days`` 日の DM（スレッドの返信を含む）を古い順で返す。上限を超えたら打ち切る。"""
        if not valid_user_id(slack_user_id):
            raise DmHistoryError("bad_user")
        days = max(1, min(int(days), HISTORY_DAYS_MAX))
        started = time.monotonic()
        oldest = (time.time() if now is None else now) - days * 86400
        channel = self._dm_channel(slack_user_id)
        raws: list[dict[str, Any]] = []
        cursor = ""
        truncated = False
        while True:
            kwargs: dict[str, Any] = {
                "channel": channel,
                "oldest": f"{oldest:.6f}",
                "limit": _PAGE_LIMIT,
            }
            if cursor:
                kwargs["cursor"] = cursor
            data = self._call("history", "conversations_history", **kwargs)
            raws.extend(m for m in data.get("messages") or [] if isinstance(m, dict))
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if len(raws) >= MESSAGE_CAP:
                truncated = True
                break
            if not cursor or not data.get("has_more", bool(cursor)):
                break
        expanded = 0
        for parent in [r for r in raws if int(r.get("reply_count") or 0) > 0]:
            if len(raws) >= MESSAGE_CAP or expanded >= THREAD_EXPAND_CAP:
                truncated = True
                break
            expanded += 1
            data = self._call(
                "replies",
                "conversations_replies",
                channel=channel,
                ts=str(parent.get("ts") or ""),
                limit=_PAGE_LIMIT,
            )
            raws.extend(
                r
                for r in data.get("messages") or []
                if isinstance(r, dict) and r.get("ts") != parent.get("ts")
            )
        raws = raws[:MESSAGE_CAP]
        messages = tuple(
            sorted(
                (m for m in (classify(r, user_id=slack_user_id) for r in raws) if m is not None),
                key=lambda m: m.ts,
            )
        )
        logger.info(
            "dm_history_read",
            messages=len(messages),
            truncated=truncated,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return DmRead(messages=messages, truncated=truncated)

    def list_users(self) -> list[DmUser]:
        """Aico と DM のある人の一覧（管理者画面の入口）。名前は users.list から引く。"""
        ims: list[str] = []
        cursor = ""
        for _ in range(10):
            kwargs: dict[str, Any] = {"types": "im", "limit": _PAGE_LIMIT}
            if cursor:
                kwargs["cursor"] = cursor
            data = self._call("list_ims", "conversations_list", **kwargs)
            ims.extend(
                str(c.get("user") or "")
                for c in data.get("channels") or []
                if isinstance(c, dict) and not c.get("is_user_deleted")
            )
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        wanted = {u for u in ims if valid_user_id(u)}
        names: dict[str, str] = {}
        cursor = ""
        for _ in range(10):
            kwargs = {"limit": _PAGE_LIMIT}
            if cursor:
                kwargs["cursor"] = cursor
            data = self._call("list_users", "users_list", **kwargs)
            for member in data.get("members") or []:
                if not isinstance(member, dict) or member.get("id") not in wanted:
                    continue
                if member.get("is_bot") or member.get("deleted"):
                    wanted.discard(str(member.get("id")))
                    continue
                profile = member.get("profile") or {}
                names[str(member["id"])] = str(
                    profile.get("real_name")
                    or profile.get("display_name")
                    or member.get("name")
                    or ""
                )
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        users = [DmUser(uid, names.get(uid, "")) for uid in wanted]
        users.sort(key=lambda u: (u.name == "", u.name, u.slack_user_id))
        logger.info("dm_history_users_listed", users=len(users))
        return users


__all__ = [
    "ADMIN_EMAILS_ENV",
    "AICO_REPLIES_PER_EXCHANGE",
    "HISTORY_DAYS_MAX",
    "MESSAGE_CAP",
    "SECRET_LINK_MASK",
    "DmHistoryError",
    "DmHistoryReader",
    "DmMessage",
    "DmRead",
    "DmUser",
    "Exchange",
    "admin_emails",
    "classify",
    "group_exchanges",
    "is_admin",
    "redact_secret_links",
    "valid_team_id",
    "valid_user_id",
]
