"""本人解決と RLS メタデータの単一真実源（cross-cutting・純粋ロジック）。

OC（OpenClaw）前面では OpenClaw を信用せず、Slack `user_id` からサーバ側で身元を解決する。
本モジュールは「解決済み身元 → RLS GUC メタデータ」への**唯一の変換点**であり、次を構造で保証する:

- ``user_role`` は常に ``"member"`` 固定（OC は role 供給不可＝MCP 越しの admin 昇格を不可能化）。
- ``user_groups`` は許可ドメイン/解決済グループのみ導出（OC 申告 groups は不採用）。
- email は ``normalize_email`` で strip+lower+形式検証（RLS 生比較の不一致や ``unknown`` を排除）。
- 不正/None/非メンバ/非許可ドメインは ``None``＝呼び出し側で fail-closed。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# SkillContext.metadata に載る RLS 予約キー（pgvector adapter が GUC へ流す）。
KEY_USER_EMAIL = "user_email"
KEY_USER_GROUPS = "user_groups"
KEY_USER_ROLE = "user_role"
KEY_IDENTITY_VERIFIED = "identity_verified"

# RLS は member/admin を識別。MCP 境界からは member のみ許す（admin 昇格は構造的に不可）。
ROLE_MEMBER = "member"


def normalize_email(raw: str | None) -> str | None:
    """email を RLS 比較用に正規化する（strip+lower+形式検証）。不正なら None。

    - 前後空白除去・小文字化（RLS/oauth_tokens は生比較・lower 保存のため両者を一致させる）。
    - ``"unknown"``（Slack user 欠落時の既定値）・空・空白含み・``@`` 無し・ドメインに ``.`` 無し・
      非 ASCII（homoglyph 対策）は None。
    """
    if not raw or not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or email == "unknown":
        return None
    if not email.isascii():  # 同型字（homoglyph）すり抜け対策
        return None
    if any(ch.isspace() for ch in email):
        return None
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain or "." not in domain:
        return None
    return email


@dataclass(frozen=True)
class ResolvedIdentity:
    """サーバ側で解決した身元（OC 申告ではない）。RLS メタの素になる不変値。"""

    slack_user_id: str
    email: str
    is_member: bool = True
    groups: tuple[str, ...] = ()
    display: str | None = None
    source: str = "slack_users_info"


# Slack user_id → 解決済み身元（解決不能/外部/ゲストは None=fail-closed）。
IdentityResolver = Callable[[str], Awaitable["ResolvedIdentity | None"]]


def no_access_metadata() -> dict[str, Any]:
    """身元未解決時の「何も見えない」RLS メタ（fail-safe）。

    user_email=None で documents/oauth_tokens の RLS は本人行に一切当たらない。
    role は member 固定、identity_verified=False（OAuth 経路は別途 fail-closed）。
    """
    return {
        KEY_USER_EMAIL: None,
        KEY_USER_GROUPS: [],
        KEY_USER_ROLE: ROLE_MEMBER,
        KEY_IDENTITY_VERIFIED: False,
    }


def shared_company_domains_from_env() -> frozenset[str] | None:
    """``TEAMAGENT_SHARED_COMPANY_DOMAINS``（カンマ区切り）の会社共有ドメイン集合。未設定は None。

    会社共有モデル(§G)の**単一真実源**。MCP gateway（会社メンバー identity の ``user_groups``）と
    ingest（``documents.acl_groups`` への会社ドメイン付与）が同じ値を使うことで、
    「会社メンバーが全社の共有ナレッジを読める」を一貫させる（片側だけ設定するズレを防ぐ）。
    """
    raw = os.environ.get("TEAMAGENT_SHARED_COMPANY_DOMAINS")
    if not raw:
        return None
    domains = frozenset(d.strip().lower() for d in raw.split(",") if d.strip())
    return domains or None


def company_member_metadata(allowed_domains: frozenset[str]) -> dict[str, Any]:
    """会社共有モデルの RLS メタ（全員が会社ナレッジを読む・本人識別は使わない）。

    社内の営業ナレッジは「会社の資産」で横連携のため全員可視＝per-user 行隔離は不要、という
    方針(§G)用。本人識別が信頼できない共有GW(OpenClaw)前提でも安全に会社ナレッジを出せる。

    - ``user_email=None``：owner_email/acl_emails 比較は不一致だが、``user_groups`` の
      acl_groups intersect で **会社ドメイン共有 doc** が見える（個人 owner-only doc は ingest 側で
      共有化する＝§G 実装デルタ）。
    - ``user_groups``=会社ドメイン群、``user_role="member"``（admin/書込は不可）、
      ``identity_verified=False``（万一 OAuth系tool が呼ばれても本人未確認で fail-closed）。
    """
    return {
        KEY_USER_EMAIL: None,
        KEY_USER_GROUPS: sorted(allowed_domains),
        KEY_USER_ROLE: ROLE_MEMBER,
        KEY_IDENTITY_VERIFIED: False,
    }


def build_rls_metadata(
    subject: ResolvedIdentity | str | None,
    *,
    allowed_domains: frozenset[str] | None = None,
    verified: bool = True,
) -> dict[str, Any] | None:
    """解決済み身元（または email 文字列）から RLS GUC メタを作る唯一の変換点。

    返り値 dict: ``{user_email, user_groups, user_role:"member", identity_verified}``。
    次のいずれかなら **None**（呼び出し側で require_rls 時 fail-closed）:
    email 不正/None、ResolvedIdentity が非メンバ、allowed_domains 指定下でドメイン非許可。

    - ``allowed_domains=None``（既定）はドメイン無制限＝現行 bot 挙動を保つ。
    - ``user_groups`` は許可ドメイン内の email ドメイン＋解決済みグループ（``,`` 含みは除外）。
    - ``user_role`` は常に ``"member"``（引数で admin を渡す口は無い）。
    """
    if subject is None:
        return None

    if isinstance(subject, ResolvedIdentity):
        if not subject.is_member:
            return None
        email = normalize_email(subject.email)
        extra_groups: tuple[str, ...] = subject.groups
    else:
        email = normalize_email(subject)
        extra_groups = ()

    if email is None:
        return None

    domain = email.split("@", 1)[1]
    if allowed_domains is not None and domain not in allowed_domains:
        return None

    groups: list[str] = [domain]
    for raw_group in extra_groups:
        group = raw_group.strip().lower()
        # ',' は string_to_array(...,',') を壊すため除外。空・重複も除外。
        if group and "," not in group and group not in groups:
            groups.append(group)

    return {
        KEY_USER_EMAIL: email,
        KEY_USER_GROUPS: groups,
        KEY_USER_ROLE: ROLE_MEMBER,
        KEY_IDENTITY_VERIFIED: bool(verified),
    }
