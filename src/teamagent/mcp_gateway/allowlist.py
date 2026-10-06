"""機能ごとの許可リスト（``*_ALLOWED_EMAILS``）の照合。

- 空の許可リストは全員拒否（段階公開の既定。``skills/_shared/rollout.py`` の「空＝全員」とは逆）。
- ``*`` を含めると、本人確認済みの全員に開く（2026-10-06 小俣さん指示「私だけに限定している機能は
  全員に使えるように」）。照合する email は resolver が解決した値だけで、未解決なら ``*`` でも拒否。
"""

from __future__ import annotations

from collections.abc import Collection

WILDCARD = "*"


def email_allowed(email: object, allowed: Collection[str]) -> bool:
    """``email`` が許可リストで許されるか。``allowed`` は小文字化済みの集合を想定する。"""
    if not isinstance(email, str) or not email.strip() or "@" not in email:
        return False
    if WILDCARD in allowed:
        return True
    return email.strip().lower() in allowed


__all__ = ["WILDCARD", "email_allowed"]
