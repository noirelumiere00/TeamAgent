"""identity.py（本人解決と RLS メタの単一真実源）の単体テスト（外部I/O無し）。

検証主眼: ①email 正規化と形式すり抜け排除 ②build_rls_metadata が role を常に member 固定し
OC が admin/任意 groups を注入できない ③非メンバ/非許可ドメイン/不正 email は None（fail-closed）。
"""

from __future__ import annotations

import pytest

from teamagent.identity import (
    ResolvedIdentity,
    build_rls_metadata,
    no_access_metadata,
    normalize_email,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Taro@VectorInc.CO.JP ", "taro@vectorinc.co.jp"),  # strip + lower
        ("alice@vectorinc.co.jp", "alice@vectorinc.co.jp"),
        ("unknown", None),  # Slack 既定値のすり抜け
        ("", None),
        ("   ", None),
        (None, None),
        ("no-at-sign", None),
        ("a@b", None),  # ドメインに '.' 無し
        ("a b@x.co", None),  # 空白含み
        ("аdmin@vectorinc.co.jp", None),  # 先頭が Cyrillic а（homoglyph・非ASCII）
        ("a@b.co", "a@b.co"),
    ],
)
def test_normalize_email(raw: str | None, expected: str | None) -> None:
    assert normalize_email(raw) == expected


def test_build_rls_metadata_basic_domain_group_and_member_role() -> None:
    meta = build_rls_metadata("taro@vectorinc.co.jp")
    assert meta == {
        "user_email": "taro@vectorinc.co.jp",
        "user_groups": ["vectorinc.co.jp"],
        "user_role": "member",  # 常に member
        "identity_verified": True,
    }


def test_build_rls_metadata_lowercases_for_rls_match() -> None:
    meta = build_rls_metadata("Taro@VectorInc.CO.JP")
    assert meta is not None
    assert meta["user_email"] == "taro@vectorinc.co.jp"
    assert meta["user_groups"] == ["vectorinc.co.jp"]


def test_build_rls_metadata_none_and_invalid_return_none() -> None:
    assert build_rls_metadata(None) is None
    assert build_rls_metadata("unknown") is None
    assert build_rls_metadata("not-an-email") is None


def test_build_rls_metadata_rejects_non_member_identity() -> None:
    ident = ResolvedIdentity(slack_user_id="U1", email="guest@vectorinc.co.jp", is_member=False)
    assert build_rls_metadata(ident) is None


def test_build_rls_metadata_merges_resolved_groups_excluding_comma() -> None:
    ident = ResolvedIdentity(
        slack_user_id="U1",
        email="taro@vectorinc.co.jp",
        groups=("Sales@vectorinc.co.jp", "bad,group", "vectorinc.co.jp"),
    )
    meta = build_rls_metadata(ident)
    assert meta is not None
    # domain + 解決済み group（lower・重複除外・',' 含みは除外）
    assert meta["user_groups"] == ["vectorinc.co.jp", "sales@vectorinc.co.jp"]
    assert meta["user_role"] == "member"


def test_build_rls_metadata_allowed_domains_enforced() -> None:
    allow = frozenset({"vectorinc.co.jp"})
    assert build_rls_metadata("attacker@evil.com", allowed_domains=allow) is None
    ok = build_rls_metadata("taro@vectorinc.co.jp", allowed_domains=allow)
    assert ok is not None and ok["user_groups"] == ["vectorinc.co.jp"]


def test_build_rls_metadata_verified_flag() -> None:
    assert build_rls_metadata("a@b.co", verified=False)["identity_verified"] is False  # type: ignore[index]
    assert build_rls_metadata("a@b.co", verified=True)["identity_verified"] is True  # type: ignore[index]


def test_no_access_metadata_is_fail_safe() -> None:
    meta = no_access_metadata()
    assert meta["user_email"] is None
    assert meta["user_groups"] == []
    assert meta["user_role"] == "member"
    assert meta["identity_verified"] is False
