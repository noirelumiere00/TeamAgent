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

# 2026-08-18 本番実測: files.slack.com の url_private は、一定サイズ以上（実測 2.4MB の PDF）で
# **302 → https://slack-files.com/files-pri-safe/...（署名付き URL）** を返す。小さい .txt は
# 200 直返しのため、従来のリダイレクト非追従でも成功していた＝サイズ依存で download_failed に
# なる本番バグの正体。転送先は Slack の配信ドメインだが *.slack.com ではないため、
# _DEFAULT_SLACK_FILE_HOSTS の allowlist では弾かれる。
#
# 追従は **この allowlist のホストへ 1 回だけ**。かつ **転送先へ Authorization を送らない**
# （転送先 URL は署名で自己完結しており token は不要＝別ドメインへ bot token を漏らさない）。
_DEFAULT_SLACK_REDIRECT_HOSTS: frozenset[str] = frozenset({"slack-files.com"})

# 追従する 3xx。307/308 は「メソッドもヘッダも保って再送」の意味論で、
# Authorization を落とす本実装とは前提が食い違うため **追わない**。
_FOLLOWABLE_REDIRECT_STATUS: frozenset[int] = frozenset({302, 303})

# 追従は 1 ホップだけ（多段リダイレクトでの allowlist 洗浄・SSRF チェーンを構造的に禁止）。
SLACK_FILE_MAX_REDIRECTS = 1

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


def _canonical_https(url: str) -> tuple[str, str]:
    """空 / 長さ / scheme / authority を検査し ``(正規化 URL, host)`` を返す。

    ホスト allowlist は**含めない**（呼び出し側が用途別 allowlist で判定する）。
    エラー文言は従来の ``validate_slack_file_url`` と 1 文字も変えない（既存契約）。
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
    return cleaned, host


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
    cleaned, host = _canonical_https(url)
    if not _host_matches(host, allowed if allowed is not None else slack_file_allowed_hosts()):
        logger.warning("slack_file_host_blocked", request_id=request_id, host=host)
        raise SlackFileGuardError(
            "SLACK_FILE_HOST_BLOCKED: Slack 以外のホストのファイルは取得しません"
        )
    return cleaned


def slack_file_redirect_allowed_hosts() -> frozenset[str]:
    """302 の転送先として許すホスト。``SLACK_FILE_REDIRECT_ALLOWED_HOSTS``（カンマ区切り）。

    未設定/空は保守的既定（``slack-files.com`` のみ）。``slack_file_allowed_hosts`` とは
    **別の env・別の集合**にする。url_private の allowlist を広げても転送先が自動で広がらない
    ようにするため（allowlist 洗浄の連鎖を作らない）。
    """
    raw = os.environ.get("SLACK_FILE_REDIRECT_ALLOWED_HOSTS")
    if not raw:
        return _DEFAULT_SLACK_REDIRECT_HOSTS
    hosts = frozenset(h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip())
    return hosts or _DEFAULT_SLACK_REDIRECT_HOSTS


def is_followable_redirect(status_code: int) -> bool:
    """この 3xx を 1 回だけ追ってよいか（302 / 303 のみ True）。"""
    return int(status_code) in _FOLLOWABLE_REDIRECT_STATUS


def validate_slack_file_redirect(
    location: str,
    *,
    allowed: frozenset[str] | None = None,
    request_id: str | None = None,
) -> str:
    """302 の ``Location`` を検証し、通れば追従先 URL を返す。

    ``url_private`` 本体より **厳しい** allowlist（既定 ``slack-files.com`` のみ）を使う。
    相対 Location は scheme 検査で落ちる＝**絶対 https URL のみ**（相対解決で元ホストへ
    戻る経路も作らない）。

    Raises:
        SlackFileGuardError: Location 欠落 / 非 HTTPS / 非 canonical authority /
            転送先 allowlist 外。
    """
    if not location or not str(location).strip():
        raise SlackFileGuardError("SLACK_FILE_REDIRECT_NO_LOCATION: 転送先が示されていません")
    cleaned, host = _canonical_https(location)
    if not _host_matches(
        host, allowed if allowed is not None else slack_file_redirect_allowed_hosts()
    ):
        logger.warning("slack_file_redirect_blocked", request_id=request_id, host=host)
        raise SlackFileGuardError("SLACK_FILE_REDIRECT_HOST_BLOCKED: 許可されていない転送先です")
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
    "SLACK_FILE_MAX_REDIRECTS",
    "SlackFileGuardError",
    "is_external_file",
    "is_followable_redirect",
    "slack_file_allowed_hosts",
    "slack_file_redirect_allowed_hosts",
    "validate_slack_file_redirect",
    "validate_slack_file_url",
]
