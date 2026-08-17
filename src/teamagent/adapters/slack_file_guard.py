"""Slack 添付ファイル URL の allowlist ガード（Adapter 層・最下層）。

``url_private`` へは **bot token を Authorization ヘッダに載せて** GET する。つまり
url_private のホストを検証しないまま GET すると、Slack の ``files`` 配列に混入し得る
外部ファイル（``is_external`` / ``external_type`` 付き＝Google Drive / Box 等の
外部ホスト URL）へ **bot token が送信される**。ここはその 1 か所の検証点。

3 層分離: adapters は最下層（skills / ingest を import しない）。ホスト検証の実装は
本モジュールの 1 実装だけにし、``SlackClient.download_file_guarded`` と skill 側の
事前選別の **両方が同じ関数を呼ぶ**（＝検証を外すと両経路が同時に開く）。

設計は ``url_guard.py`` の ``_host_matches``（末尾一致・部分文字列禁止）と同型。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

# 既定 allowlist。Slack の url_private は ``https://files.slack.com/files-pri/...``。
# Enterprise Grid 等で別ホストになる構成は SLACK_FILE_ALLOWED_HOSTS で明示追加する
# （既定を広げない＝知らないホストへ bot token を出さない）。
_DEFAULT_SLACK_FILE_HOSTS: frozenset[str] = frozenset({"files.slack.com"})

_MAX_URL_LEN = 2048


class SlackFileGuardError(ValueError):
    """Slack 添付ファイルの取得を拒否した（ホスト不一致・外部ファイル・容量超過等）。

    呼び出し側が利用者向けの案内文へ変換する（生 URL はログにも戻り値にも出さない）。
    """


def slack_file_allowed_hosts() -> frozenset[str]:
    """``SLACK_FILE_ALLOWED_HOSTS``（カンマ区切り）。未設定/空は保守的既定。

    env 規約は ``url_guard.allowed_domains_from_env`` と同形（strip().lower()・先頭ドット除去）。
    """
    raw = os.environ.get("SLACK_FILE_ALLOWED_HOSTS")
    if not raw:
        return _DEFAULT_SLACK_FILE_HOSTS
    hosts = frozenset(h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip())
    return hosts or _DEFAULT_SLACK_FILE_HOSTS


def _host_matches(host: str, allowed: frozenset[str]) -> bool:
    """末尾一致（部分文字列禁止）。``host == dom`` もしくは ``host`` が ``*.dom`` のみ許可。

    ``evilfiles.slack.com.attacker.jp``（接尾辞偽装）も
    ``attacker.jp/?x=files.slack.com``（部分文字列）も弾く。
    """
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in allowed)


def validate_slack_file_url(
    url: str,
    *,
    allowed: frozenset[str] | None = None,
    request_id: str | None = None,
) -> str:
    """Slack 添付の ``url_private`` を検証し、通れば正規化 URL を返す。

    Raises:
        SlackFileGuardError: 空 / 長すぎ / 非 HTTPS / 非 canonical authority /
            ホスト無し / allowlist 外ホスト。
    """
    if not url or not str(url).strip():
        raise SlackFileGuardError("SLACK_FILE_URL_EMPTY: ファイル URL が空です")
    cleaned = str(url).strip()
    if len(cleaned) > _MAX_URL_LEN:
        raise SlackFileGuardError("SLACK_FILE_URL_TOO_LONG: ファイル URL が長すぎます")
    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise SlackFileGuardError("SLACK_FILE_SCHEME_BLOCKED: HTTPS URL のみ許可されます")
    host = parsed.hostname  # urllib が user:pass@ / :port / [IPv6] を厳密分離
    if not host:
        raise SlackFileGuardError("SLACK_FILE_NO_HOST: ホスト名がありません")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SlackFileGuardError("SLACK_FILE_AUTHORITY_BLOCKED: URL authority が不正です") from exc
    if parsed.username or parsed.password or port not in (None, 443):
        raise SlackFileGuardError(
            "SLACK_FILE_AUTHORITY_BLOCKED: canonical HTTPS authority のみ許可されます"
        )
    if not _host_matches(host, allowed if allowed is not None else slack_file_allowed_hosts()):
        logger.warning("slack_file_host_blocked", request_id=request_id, host=host)
        raise SlackFileGuardError(
            "SLACK_FILE_HOST_BLOCKED: Slack 以外のホストのファイルは取得しません"
        )
    return cleaned


def is_external_file(file: dict[str, Any]) -> bool:
    """Slack file dict が外部共有（Drive/Box 等のリンク）なら True。

    ``is_external`` が真、``external_type`` が非空、``mode`` が external 系
    （``external`` / ``hosted``以外）のいずれかで外部と判定する。判定不能な形は
    「外部かもしれない」側（True）へ倒す＝fail-closed。
    """
    if bool(file.get("is_external")):
        return True
    if str(file.get("external_type") or "").strip():
        return True
    mode = str(file.get("mode") or "").strip().lower()
    return mode in {"external", "hosted_external"}


__all__ = [
    "SlackFileGuardError",
    "is_external_file",
    "slack_file_allowed_hosts",
    "validate_slack_file_url",
]
