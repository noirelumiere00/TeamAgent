"""mcp_gateway STRICT モード（WS-C anti-spoof）の単体テスト（外部I/O無し・FakeIdentityResolver）。

検証主眼（赤チーム摘出の重大脅威を回帰固定）:
- 🔴admin破棄：OC が user_role="admin" を申告しても skill が観測する role は "member"。
- 🔴ダウングレード封鎖：resolver 注入時に slack_user_id 欠落→fail-closed（OC email にフォールバックしない）。
- 🔴OC 申告 email/groups/role は STRICT で全破棄＝観測値はサーバ解決値のみ。
- 🟠resolver の例外/None は一律 fail-closed（fail-open 不在）。
- 🟠許可ドメイン外は fail-closed。LEGACY でも role は member 強制。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.caller_claim import VerifiedCallerClaim
from teamagent.mcp_gateway.server import (
    USER_CONTEXT_KEY,
    _resolve_metadata,
    build_server,
    company_shared_groups_from_env,
    dispatch_tool,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from tests.caller_claim_testkit import (
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    make_verifier,
    sign_arguments,
)


class _In(BaseModel):
    q: str


class _Out(BaseModel):
    echo: str
    email: str | None
    groups: list[str]
    role: str | None
    verified: bool
    verified_slack_user_id: str | None
    verified_slack_team_id: str | None
    has_verified_slack_user_id: bool
    has_verified_slack_team_id: bool


class _EchoSkill(BaseSkill[_In, _Out]):
    """ctx.metadata の RLS 値（email/groups/role/identity_verified）を観測するフェイク。"""

    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "テスト用エコー。"
    input_schema: ClassVar[type[BaseModel]] = _In
    output_schema: ClassVar[type[BaseModel]] = _Out

    def run(self, input: _In, ctx: SkillContext) -> _Out:
        return _Out(
            echo=input.q,
            email=ctx.metadata.get("user_email"),
            groups=list(ctx.metadata.get("user_groups") or []),
            role=ctx.metadata.get("user_role"),
            verified=bool(ctx.metadata.get("identity_verified")),
            verified_slack_user_id=ctx.metadata.get("verified_slack_user_id"),
            verified_slack_team_id=ctx.metadata.get("verified_slack_team_id"),
            has_verified_slack_user_id="verified_slack_user_id" in ctx.metadata,
            has_verified_slack_team_id="verified_slack_team_id" in ctx.metadata,
        )


_BY_NAME = {"echo": ToolSpec("echo", _EchoSkill.description, _EchoSkill)}


def _parse(contents: list[Any]) -> dict[str, Any]:
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _resolver(mapping: dict[str, ResolvedIdentity], *, raises: bool = False) -> IdentityResolver:
    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        if raises:
            raise RuntimeError("resolver boom")
        return mapping.get(slack_user_id)

    return resolve


_TARO = ResolvedIdentity(
    slack_user_id=TEST_SLACK_USER_ID,
    email="taro@vectorinc.co.jp",
)
_OK = _resolver({TEST_SLACK_USER_ID: _TARO})


def test_protected_server_configuration_requires_resolver_and_claim_verifier() -> None:
    with pytest.raises(RuntimeError, match="Slack identity resolver"):
        build_server(
            list(_BY_NAME.values()),
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
        )
    with pytest.raises(RuntimeError, match="signed caller claim verifier"):
        build_server(
            list(_BY_NAME.values()),
            identity_resolver=_OK,
        )


async def test_strict_resolves_and_drops_all_oc_fields() -> None:
    # OC が攻撃的に email/groups/role を申告しても、観測値はサーバ解決値のみ（全破棄＝anti-spoof 本旨）。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments(
                "echo",
                {"q": "hi"},
                declared_context={
                    "user_email": "attacker@evil.com",
                    "user_groups": ["secret-group"],
                    "user_role": "admin",
                },
            ),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
        )
    )
    assert out["email"] == "taro@vectorinc.co.jp"
    assert out["groups"] == ["vectorinc.co.jp"]
    assert out["role"] == "member"  # 🔴 admin 破棄
    assert out["verified"] is True
    assert out["verified_slack_user_id"] == TEST_SLACK_USER_ID
    assert out["verified_slack_team_id"] == TEST_SLACK_TEAM_ID


async def test_strict_downgrade_closed_without_slack_user_id() -> None:
    # 🔴 slack_user_id を省略して OC email を通そうとする＝fail-closed（フォールバックしない）。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"user_email": "attacker@evil.com"}},
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_strict_resolver_none_fail_closed() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments("echo", {"q": "hi"}, user_id="U9999999999"),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_strict_resolver_exception_fail_closed() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments("echo", {"q": "hi"}),
            identity_resolver=_resolver({}, raises=True),
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_strict_disallowed_domain_fail_closed() -> None:
    ext = ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email="x@evil.com")
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments("echo", {"q": "hi"}),
            identity_resolver=_resolver({TEST_SLACK_USER_ID: ext}),
            allowed_domains=frozenset({"vectorinc.co.jp"}),
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_legacy_forces_member_role() -> None:
    # LEGACY(resolver 無)でも OC 申告 role=admin は採らず member 強制。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {
                "q": "hi",
                USER_CONTEXT_KEY: {"user_email": "a@b.co", "user_role": "admin"},
            },
            require_rls=True,
        )
    )
    assert out["role"] == "member"
    assert out["verified"] is False  # legacy は未検証＝OAuth 経路は別途 fail-closed
    assert out["has_verified_slack_user_id"] is False
    assert out["has_verified_slack_team_id"] is False


async def test_no_access_metadata_never_carries_verified_slack_identity() -> None:
    """STRICT の no-access 成功経路に未検証／空の identity キーを追加しない。"""
    metadata, fail = await _resolve_metadata(
        {},
        verified_caller=None,
        require_rls=False,
        identity_resolver=_OK,
        allowed_domains=None,
        company_shared_groups=None,
        tool="echo",
    )

    assert fail is None
    assert "verified_slack_user_id" not in metadata
    assert "verified_slack_team_id" not in metadata


async def test_resolver_miss_no_access_never_carries_verified_slack_identity() -> None:
    """caller claim が有効でも resolver 失敗時の no-access に identity キーを残さない。"""

    async def resolve_none(_slack_user_id: str) -> None:
        return None

    caller = VerifiedCallerClaim(
        slack_user_id=TEST_SLACK_USER_ID,
        slack_team_id=TEST_SLACK_TEAM_ID,
        channel_id="C0123456789",
        thread_ts=None,
        message_id="1784424000.000001",
        session_sha256="0" * 64,
        run_id="11111111-1111-4111-8111-111111111111",
        tool_call_id="toolu_0123456789abcdef",
        nonce="test-nonce",
        issued_at=1,
        expires_at=2,
    )
    metadata, fail = await _resolve_metadata(
        {},
        verified_caller=caller,
        require_rls=False,
        identity_resolver=resolve_none,
        allowed_domains=None,
        company_shared_groups=None,
        tool="echo",
    )

    assert fail is None
    assert "verified_slack_user_id" not in metadata
    assert "verified_slack_team_id" not in metadata


async def test_strict_fuzz_never_admin_and_requires_resolution() -> None:
    # 任意の悪意 _user_context に対し「解決された時のみ実行・role!=admin」が不変。
    malicious = [
        {"user_role": "admin"},
        {"slack_user_id": "", "user_role": "admin"},
        {"slack_user_id": None, "user_email": "ceo@vectorinc.co.jp"},
        {"user_email": "ceo@vectorinc.co.jp", "user_groups": ["*"], "user_role": "admin"},
        {"slack_user_id": "U_BADFORMAT", "user_role": "admin"},  # 未知→解決None
    ]
    for uc in malicious:
        out = _parse(
            await dispatch_tool(
                _BY_NAME,
                "echo",
                {"q": "x", USER_CONTEXT_KEY: uc},
                identity_resolver=_OK,
                caller_claim_verifier=make_verifier(),
                require_rls=True,
            )
        )
        # 解決できないので必ず fail-closed。role が admin で観測されることは無い。
        assert "error" in out
        assert out.get("role") != "admin"


# ── §G 会社共有モード（署名済み・resolver済み会社memberだけが会社ナレッジを読む） ──


async def test_company_shared_ignores_oc_and_uses_company_groups() -> None:
    # 会社共有も署名済みSlack memberだけ。OC申告権限値は破棄しresolver値を使う。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments(
                "echo",
                {"q": "hi"},
                declared_context={
                    "user_email": "attacker@evil.com",
                    "user_groups": ["secret-group"],
                    "user_role": "admin",
                },
            ),
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
        )
    )
    assert out["email"] == "taro@vectorinc.co.jp"
    assert out["groups"] == ["vectorinc.co.jp"]
    assert out["role"] == "member"  # admin 不可
    assert out["verified"] is True
    assert out["verified_slack_user_id"] == TEST_SLACK_USER_ID
    assert out["verified_slack_team_id"] == TEST_SLACK_TEAM_ID


async def test_company_shared_rejects_without_signed_slack_user() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {}},
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


def test_company_shared_groups_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
    assert company_shared_groups_from_env() is None
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "VectorInc.co.jp, foo.com ,")
    assert company_shared_groups_from_env() == frozenset({"vectorinc.co.jp", "foo.com"})


# ── §U ハイブリッド: 会社共有グループ維持 + per-user 本人 email 解決 ────────────────


async def test_company_shared_with_resolver_loads_user_email() -> None:
    # 会社共有 + resolver: search 用の会社グループは維持しつつ、mail 用の本人 user_email も載る。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments("echo", {"q": "hi"}),
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
        )
    )
    assert out["email"] == "taro@vectorinc.co.jp"  # mail token lookup 用に解決される
    assert "vectorinc.co.jp" in out["groups"]  # 会社共有グループは維持＝search 全社可視
    assert out["role"] == "member"  # admin 昇格不可は不変
    assert out["verified"] is True  # server-side 解決済


async def test_company_shared_with_resolver_oc_fields_dropped() -> None:
    # OC申告email/groups/roleは破棄し、署名済みevent userをresolverした値だけを採る。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments(
                "echo",
                {"q": "hi"},
                declared_context={
                    "user_email": "attacker@evil.com",
                    "user_groups": ["secret"],
                    "user_role": "admin",
                },
            ),
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
        )
    )
    assert out["email"] == "taro@vectorinc.co.jp"  # OC 申告 email は不採用・解決値のみ
    assert "secret" not in out["groups"]
    assert out["role"] == "member"


async def test_company_shared_resolver_none_fails_closed() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments("echo", {"q": "hi"}, user_id="U9999999999"),
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            identity_resolver=_OK,
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_company_shared_resolver_exception_fails_closed() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            sign_arguments("echo", {"q": "hi"}),
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            identity_resolver=_resolver({}, raises=True),
            caller_claim_verifier=make_verifier(),
            require_rls=True,
        )
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"
