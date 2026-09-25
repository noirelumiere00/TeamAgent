"""本人メモ（DM 本人メモ v1・M5）の入口の門。標準ライブラリだけを使う。

ここで決めるのは「この呼び出しを本人メモとして受け付けてよいか」だけ:

- DM か: 署名済み caller claim の ``channel_id`` を ``D[A-Z0-9]{8,}`` で fullmatch する
  （strip しない・申告値は見ない。``C08D3KQ7ABC`` のように D を含む公開チャンネルを通さない）
- plugin が直接呼んだか: ``tool_call_id`` が ``aico-pm-(obs|ctx|cmd)-<32hex>`` に fullmatch し、
  kind がツール名と一致し、``run_id == tool_call_id``（plugin の直接呼び出しの印）
- 対象者か: ``PERSONAL_MEMORY_ALLOWED_EMAILS``（カンマ区切り）。**空・未設定なら全員拒否**。
  ``skills/_shared/rollout.py`` の ``rollout_allowed`` は空＝全員許可なので流用しない
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

ALLOWED_EMAILS_ENV: Final = "PERSONAL_MEMORY_ALLOWED_EMAILS"

DM_CHANNEL_RE: Final = re.compile(r"D[A-Z0-9]{8,}")
RESERVED_INVOCATION_RE: Final = re.compile(r"aico-pm-(obs|ctx|cmd)-[0-9a-f]{32}")

TOOL_KIND: Final[dict[str, str]] = {
    "personal_memory_observe": "obs",
    "personal_memory_context": "ctx",
    "personal_memory_command": "cmd",
}


def is_dm_channel(channel_id: object) -> bool:
    """署名済み claim の channel_id が 1 対 1 DM（D…）なら True。"""
    return isinstance(channel_id, str) and DM_CHANNEL_RE.fullmatch(channel_id) is not None


def reserved_invocation_ok(tool: str, tool_call_id: object, run_id: object) -> bool:
    """plugin が本人メモのために作った呼び出し ID か（モデル経由の呼び出しを通さない）。"""
    kind = TOOL_KIND.get(tool)
    if kind is None or not isinstance(tool_call_id, str) or not isinstance(run_id, str):
        return False
    match = RESERVED_INVOCATION_RE.fullmatch(tool_call_id)
    if match is None or match.group(1) != kind:
        return False
    return run_id == tool_call_id


def allowed_emails_from_env() -> frozenset[str]:
    """対象者の email 集合（strip＋小文字）。空・未設定なら空集合＝全員拒否。"""
    raw = os.environ.get(ALLOWED_EMAILS_ENV, "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_allowed(email: object) -> bool:
    """resolver が解決した email が対象者なら True（申告値を渡さないこと）。"""
    if not isinstance(email, str) or not email.strip():
        return False
    return email.strip().lower() in allowed_emails_from_env()


_SDK_LOGGER: Final = "mcp.server.lowlevel.server"
_SDK_NOT_LISTED_MSG: Final = "Tool '%s' not listed, no validation will be performed"


class _NotListedFilter(logging.Filter):
    """SDK が一覧に無いツールの呼び出しごとに出す WARNING を、本人メモの 3 ツールだけ落とす。

    中身はツール名だけだが DM 1 通につき 2 行出るため。ほかのツール名の WARNING は残す。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != _SDK_NOT_LISTED_MSG or not isinstance(record.args, tuple):
            return True
        return not (record.args and record.args[0] in TOOL_KIND)


def install_sdk_warning_filter() -> None:
    sdk_logger = logging.getLogger(_SDK_LOGGER)
    if not any(isinstance(f, _NotListedFilter) for f in sdk_logger.filters):
        sdk_logger.addFilter(_NotListedFilter())


__all__ = [
    "ALLOWED_EMAILS_ENV",
    "DM_CHANNEL_RE",
    "RESERVED_INVOCATION_RE",
    "TOOL_KIND",
    "allowed_emails_from_env",
    "install_sdk_warning_filter",
    "is_allowed",
    "is_dm_channel",
    "reserved_invocation_ok",
]
