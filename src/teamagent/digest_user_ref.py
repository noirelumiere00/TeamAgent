"""``user_ref`` — 予約ペイロードに載せる **不可逆** の利用者参照。

EventBridge Scheduler の Input / SQS の body に **メールアドレスを載せない** ための鍵。
発火側は「ref → email」を復号するのではなく、連携済み利用者を列挙して同じ hash を
計算し照合する（総当たりではなく 29 件の等価比較）。したがって復号可能である必要が無い。

強度: ``sha256(pepper + ":" + email)`` の先頭 32 hex。``DIGEST_USER_REF_PEPPER`` が
設定されていれば辞書攻撃（ドメイン＋姓名の総当たり）にも耐える。未設定でも機能は動くが、
ペイロードを読める者がドメインを知っていれば候補を試せる（ペイロードは Scheduler / SQS
の内側にしか無く、SQS は SSE 暗号化済み）。本番では pepper を設定すること。
"""

from __future__ import annotations

import hashlib
import os

_REF_LEN = 32  # schedule 名の 64 文字制限に収める（"digest-" + 32 + "-" + 8 = 47）


def user_ref(email: str, *, pepper: str | None = None) -> str:
    """email → 不可逆な 32 hex の参照。空/不正な email は空文字。"""
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return ""
    salt = pepper if pepper is not None else os.environ.get("DIGEST_USER_REF_PEPPER", "")
    material = f"{salt}:digestref:{normalized}".encode()
    return hashlib.sha256(material).hexdigest()[:_REF_LEN]


def resolve_user_ref(ref: str, candidates: list[str], *, pepper: str | None = None) -> str | None:
    """ref に一致する email を候補から探す（見つからなければ None＝fail-closed）。"""
    target = (ref or "").strip().lower()
    if len(target) != _REF_LEN or not target.isalnum():
        return None
    for email in candidates:
        if user_ref(email, pepper=pepper) == target:
            return email
    return None


def digest_schedule_name(ref: str, day_compact: str) -> str:
    """``digest-<user_ref>-<YYYYMMDD>``。1 日 1 人 1 本＝planner 再実行でも重複しない。"""
    return f"digest-{ref}-{day_compact}"


__all__ = ["digest_schedule_name", "resolve_user_ref", "user_ref"]
