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
from teamagent.mcp_gateway.server import (
    USER_CONTEXT_KEY,
    company_shared_groups_from_env,
    dispatch_tool,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext


class _In(BaseModel):
    q: str


class _Out(BaseModel):
    echo: str
    email: str | None
    groups: list[str]
    role: str | None
    verified: bool


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


_TARO = ResolvedIdentity(slack_user_id="U12345", email="taro@vectorinc.co.jp")
_OK = _resolver({"U12345": _TARO})


async def test_strict_resolves_and_drops_all_oc_fields() -> None:
    # OC が攻撃的に email/groups/role を申告しても、観測値はサーバ解決値のみ（全破棄＝anti-spoof 本旨）。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {
                "q": "hi",
                USER_CONTEXT_KEY: {
                    "slack_user_id": "U12345",
                    "user_email": "attacker@evil.com",
                    "user_groups": ["secret-group"],
                    "user_role": "admin",
                },
            },
            identity_resolver=_OK,
        )
    )
    assert out["email"] == "taro@vectorinc.co.jp"
    assert out["groups"] == ["vectorinc.co.jp"]
    assert out["role"] == "member"  # 🔴 admin 破棄
    assert out["verified"] is True


async def test_strict_downgrade_closed_without_slack_user_id() -> None:
    # 🔴 slack_user_id を省略して OC email を通そうとする＝fail-closed（フォールバックしない）。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"user_email": "attacker@evil.com"}},
            identity_resolver=_OK,
            require_rls=True,
        )
    )
    assert "fail-closed" in out["error"]
    assert "slack_user_id" in out["error"]


async def test_strict_resolver_none_fail_closed() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"slack_user_id": "U99999"}},  # 未知ユーザ
            identity_resolver=_OK,
            require_rls=True,
        )
    )
    assert "fail-closed" in out["error"]


async def test_strict_resolver_exception_fail_closed() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"slack_user_id": "U12345"}},
            identity_resolver=_resolver({}, raises=True),
            require_rls=True,
        )
    )
    assert "fail-closed" in out["error"]


async def test_strict_disallowed_domain_fail_closed() -> None:
    ext = ResolvedIdentity(slack_user_id="U12345", email="x@evil.com")
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"slack_user_id": "U12345"}},
            identity_resolver=_resolver({"U12345": ext}),
            allowed_domains=frozenset({"vectorinc.co.jp"}),
            require_rls=True,
        )
    )
    assert "fail-closed" in out["error"]


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
                require_rls=True,
            )
        )
        # 解決できないので必ず fail-closed。role が admin で観測されることは無い。
        assert "error" in out
        assert out.get("role") != "admin"


# ── §G 会社共有モード（全員が会社ナレッジを読む・本人識別は監査のみ） ──────────


async def test_company_shared_ignores_oc_and_uses_company_groups() -> None:
    # OC が email/groups/role/slack_user_id を申告しても、観測は会社ドメイン共有メタのみ。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {
                "q": "hi",
                USER_CONTEXT_KEY: {
                    "slack_user_id": "U12345",  # 監査のみ・認可には不使用
                    "user_email": "attacker@evil.com",
                    "user_groups": ["secret-group"],
                    "user_role": "admin",
                },
            },
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
        )
    )
    assert out["email"] is None  # 個人 email は使わない
    assert out["groups"] == ["vectorinc.co.jp"]  # 会社ドメイン共有
    assert out["role"] == "member"  # admin 不可
    assert out["verified"] is False  # OAuth系tool は別途 fail-closed


async def test_company_shared_serves_without_slack_user_id() -> None:
    # 会社共有は本人識別不要＝slack_user_id 無しでも fail-closed しない（全員が会社ナレッジ）。
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {}},
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
            require_rls=True,
        )
    )
    assert out["echo"] == "hi"
    assert out["groups"] == ["vectorinc.co.jp"]
    assert out["role"] == "member"


def test_company_shared_groups_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
    assert company_shared_groups_from_env() is None
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "VectorInc.co.jp, foo.com ,")
    assert company_shared_groups_from_env() == frozenset({"vectorinc.co.jp", "foo.com"})
