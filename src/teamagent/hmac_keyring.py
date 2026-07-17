"""用途別 HMAC 鍵を環境変数から安全に組み立てる。

設定契約（secret 値そのものはログ・例外へ出さない）:

* メール action:
  ``MAIL_ACTION_HMAC_SECRET``（主鍵）
  ``MAIL_ACTION_HMAC_PREVIOUS_SECRET``（移行中の検証専用鍵）
  ``MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL``（Unix epoch 秒）
* レポート短縮リンク:
  ``REPORT_LINK_HMAC_SECRET``（主鍵）
  ``REPORT_LINK_HMAC_PREVIOUS_SECRET``（移行中の検証専用鍵）
  ``REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL``（Unix epoch 秒）

主鍵だけが新規署名に使われる。previous は主鍵と対で明示設定し、有効期限内の検証にだけ
加える。期限は旧 token の最大寿命（mail=24h / report=7日）より先には設定できず、期限後は
環境変数が残っていても検証鍵から自動除外する。これにより、移行前に DB URL で署名された
token を一時救済しつつ、DB 資格情報への恒久 fallback を作らない。

主鍵として DB DSN / Slack token、``DATABASE_URL`` / ``SLACK_BOT_TOKEN`` と同じ値、または
別用途の HMAC 鍵と同じ値は拒否する。旧 DB 由来値は、期限付き previous に限って許可する。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field

MAIL_ACTION_HMAC_SECRET = "MAIL_ACTION_HMAC_SECRET"
MAIL_ACTION_HMAC_PREVIOUS_SECRET = "MAIL_ACTION_HMAC_PREVIOUS_SECRET"
MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL = "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL"
REPORT_LINK_HMAC_SECRET = "REPORT_LINK_HMAC_SECRET"
REPORT_LINK_HMAC_PREVIOUS_SECRET = "REPORT_LINK_HMAC_PREVIOUS_SECRET"
REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL = "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL"

_MAIL_ACTION_MAX_PREVIOUS_S = 60 * 60 * 24
_REPORT_LINK_MAX_PREVIOUS_S = 60 * 60 * 24 * 7
_VALID_UNTIL_CLOCK_SKEW_S = 5 * 60
_MIN_SECRET_BYTES = hashlib.sha256().digest_size
_NON_HMAC_CREDENTIAL_ENVS = ("DATABASE_URL", "SLACK_BOT_TOKEN")
_DATABASE_SCHEMES = {
    "mariadb",
    "mssql",
    "mysql",
    "oracle",
    "postgres",
    "postgresql",
}
_SLACK_TOKEN_PREFIXES = ("xapp-", "xoxb-", "xoxp-", "xoxs-")


class HmacKeyConfigurationError(RuntimeError):
    """新規署名に使える用途別主鍵が無い（メッセージは常に秘密非含有）。"""


@dataclass(frozen=True, repr=False)
class HmacKeyring:
    """主鍵と検証鍵を保持する。repr では鍵を必ず伏せる。"""

    _primary: bytes = field(repr=False)
    _verification_keys: tuple[bytes, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "HmacKeyring(<redacted>)"

    def sign(self, payload: bytes, *, digest_bytes: int) -> bytes:
        """主鍵だけで HMAC-SHA256 署名する。"""
        return hmac.new(self._primary, payload, hashlib.sha256).digest()[:digest_bytes]

    def verify(self, payload: bytes, signature: bytes, *, digest_bytes: int) -> bool:
        """主鍵と有効な previous を全て constant-time 比較する。

        主鍵で一致しても loop を打ち切らず、previous の比較も必ず実行する。どの鍵で一致したかは
        呼出元へ返さないため、検証経路から鍵世代を観測・ログ出力できない。
        """
        if len(signature) != digest_bytes:
            return False
        matched = 0
        for key in self._verification_keys:
            expected = hmac.new(key, payload, hashlib.sha256).digest()[:digest_bytes]
            matched |= int(hmac.compare_digest(expected, signature))
        return bool(matched)


def _valid_secret(raw: str | None) -> bool:
    if raw is None or not raw or raw != raw.strip():
        return False
    return len(raw.encode("utf-8")) >= _MIN_SECRET_BYTES


def _same_secret(left: bytes, right_raw: str | None) -> bool:
    if right_raw is None or not right_raw:
        return False
    return hmac.compare_digest(left, right_raw.encode("utf-8"))


def _looks_like_non_hmac_credential(raw: str) -> bool:
    lowered = raw.casefold()
    if lowered.startswith(_SLACK_TOKEN_PREFIXES):
        return True
    scheme, separator, _rest = lowered.partition(":")
    base_scheme = scheme.split("+", 1)[0]
    return bool(separator) and base_scheme in _DATABASE_SCHEMES


def _load_keyring(
    *,
    primary_env: str,
    previous_env: str,
    previous_valid_until_env: str,
    other_hmac_envs: tuple[str, ...],
    max_previous_s: int,
    now: int | None,
) -> HmacKeyring | None:
    primary_raw = os.environ.get(primary_env)
    if not _valid_secret(primary_raw):
        return None
    assert primary_raw is not None  # _valid_secret で絞り込み済み
    primary = primary_raw.encode("utf-8")

    if _looks_like_non_hmac_credential(primary_raw):
        return None
    for env_name in (*_NON_HMAC_CREDENTIAL_ENVS, *other_hmac_envs):
        if _same_secret(primary, os.environ.get(env_name)):
            return None

    previous_raw = os.environ.get(previous_env)
    valid_until_raw = os.environ.get(previous_valid_until_env)
    if previous_raw is None and valid_until_raw is None:
        return HmacKeyring(primary, (primary,))
    if previous_raw is None or valid_until_raw is None:
        return None
    if not _valid_secret(previous_raw):
        return None
    previous = previous_raw.encode("utf-8")
    if hmac.compare_digest(primary, previous):
        return None
    if not valid_until_raw.isdigit():
        return None
    valid_until = int(valid_until_raw)
    if valid_until <= 0:
        return None

    current = int(time.time()) if now is None else int(now)
    if valid_until > current + max_previous_s + _VALID_UNTIL_CLOCK_SKEW_S:
        return None
    if current <= valid_until:
        return HmacKeyring(primary, (primary, previous))
    return HmacKeyring(primary, (primary,))


def load_mail_action_hmac_keyring(*, now: int | None = None) -> HmacKeyring | None:
    """有効なメール action keyring。設定不正時は None（fail-closed）。"""
    return _load_keyring(
        primary_env=MAIL_ACTION_HMAC_SECRET,
        previous_env=MAIL_ACTION_HMAC_PREVIOUS_SECRET,
        previous_valid_until_env=MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL,
        other_hmac_envs=(REPORT_LINK_HMAC_SECRET, REPORT_LINK_HMAC_PREVIOUS_SECRET),
        max_previous_s=_MAIL_ACTION_MAX_PREVIOUS_S,
        now=now,
    )


def require_mail_action_hmac_keyring(*, now: int | None = None) -> HmacKeyring:
    keyring = load_mail_action_hmac_keyring(now=now)
    if keyring is None:
        raise HmacKeyConfigurationError("HMAC signing key configuration is invalid")
    return keyring


def load_report_link_hmac_keyring(*, now: int | None = None) -> HmacKeyring | None:
    """有効なレポート短縮リンク keyring。設定不正時は None（fail-closed）。"""
    return _load_keyring(
        primary_env=REPORT_LINK_HMAC_SECRET,
        previous_env=REPORT_LINK_HMAC_PREVIOUS_SECRET,
        previous_valid_until_env=REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL,
        other_hmac_envs=(MAIL_ACTION_HMAC_SECRET, MAIL_ACTION_HMAC_PREVIOUS_SECRET),
        max_previous_s=_REPORT_LINK_MAX_PREVIOUS_S,
        now=now,
    )


def require_report_link_hmac_keyring(*, now: int | None = None) -> HmacKeyring:
    keyring = load_report_link_hmac_keyring(now=now)
    if keyring is None:
        raise HmacKeyConfigurationError("HMAC signing key configuration is invalid")
    return keyring


__all__ = [
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "MAIL_ACTION_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "REPORT_LINK_HMAC_SECRET",
    "HmacKeyConfigurationError",
    "HmacKeyring",
    "load_mail_action_hmac_keyring",
    "load_report_link_hmac_keyring",
    "require_mail_action_hmac_keyring",
    "require_report_link_hmac_keyring",
]
