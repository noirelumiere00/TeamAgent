"""段階公開用のツール別 allowlist（前例: CONNECT_SEARCH_ALLOWED_EMAILS の型）。

SLACK_DM_ALLOWLIST は bot 全体の DM gate なのでツール別公開には使わない。
未設定/空 = **全員許可**（明示定義。個人ハードコードのフォールバックは作らない）。
"""

from __future__ import annotations

import os


def rollout_allowed(env_name: str, user_email: str | None) -> bool:
    """env のカンマ区切り email 一覧に user_email が含まれるか。空=全許可。

    user_email が解決できていない（None/空）場合、allowlist が設定されていれば拒否
    （段階公開中は身元不明を通さない）、未設定なら許可。
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return True
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return bool(user_email) and (user_email or "").strip().lower() in allowed


ROLLOUT_DENIED_MESSAGE = (
    "このツールは段階公開中です（現在は一部メンバーのみ利用できます）。"
    "利用希望は管理者(小俣)に連絡してください。"
)

__all__ = ["ROLLOUT_DENIED_MESSAGE", "rollout_allowed"]
