"""mcp_gateway の本人特定失敗（CALLER_IDENTITY_REJECTED）に診断行が付くことのテスト。

code は従来どおり CALLER_IDENTITY_REJECTED（attack_mcp / 外殻の判定は code）。message 末尾に
``診断: CONNECT-I01a|b|c <時刻 JST> <slack_user_id>`` が付き、reason ごとにサブコードが変わる。
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from pydantic import BaseModel

from teamagent.connect_diagnostics import ADMIN_FORWARD_HINT
from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.server import USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from tests.caller_claim_testkit import TEST_SLACK_USER_ID, make_verifier, sign_arguments

_DIAG_RE = re.compile(r"診断: (CONNECT-I01[abc]) \d{4}-\d\d-\d\d \d\d:\d\d JST (\S+)")


class _In(BaseModel):
    q: str


class _Out(BaseModel):
    echo: str


class _EchoSkill(BaseSkill[_In, _Out]):
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "テスト用エコー。"
    input_schema: ClassVar[type[BaseModel]] = _In
    output_schema: ClassVar[type[BaseModel]] = _Out

    def run(self, input: _In, ctx: SkillContext) -> _Out:
        return _Out(echo=input.q)


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


_OK = _resolver(
    {
        TEST_SLACK_USER_ID: ResolvedIdentity(
            slack_user_id=TEST_SLACK_USER_ID, email="t@vectorinc.co.jp"
        )
    }
)


def _diag(out: dict[str, Any]) -> tuple[str, str]:
    assert out["code"] == "CALLER_IDENTITY_REJECTED"
    m = _DIAG_RE.search(out["error"])
    assert m, out["error"]
    assert ADMIN_FORWARD_HINT in out["error"]
    return m.group(1), m.group(2)


async def test_strict_missing_verified_caller_is_i01a() -> None:
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
    code, subject = _diag(out)
    assert code == "CONNECT-I01a"
    assert subject == "-"  # 署名済み caller が無い＝ slack_user_id 不明（_verify_caller で拒否）


async def test_strict_resolver_error_is_i01b() -> None:
    """STRICT では resolver 例外→None→resolve_none の順に落ちるが、診断は I01b を保つ。"""
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
    code, subject = _diag(out)
    assert code == "CONNECT-I01b"
    assert subject == TEST_SLACK_USER_ID


async def test_strict_resolve_none_is_i01c_with_slack_user_id() -> None:
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
    code, subject = _diag(out)
    assert code == "CONNECT-I01c"
    assert subject == "U9999999999"


async def test_company_shared_missing_caller_is_i01a() -> None:
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
    assert _diag(out)[0] == "CONNECT-I01a"


async def test_company_shared_resolver_error_is_i01b() -> None:
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
    assert _diag(out) == ("CONNECT-I01b", TEST_SLACK_USER_ID)


async def test_company_shared_resolve_none_is_i01c() -> None:
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
    assert _diag(out) == ("CONNECT-I01c", "U9999999999")


async def test_rejection_message_keeps_legacy_prefix_and_code() -> None:
    """外殻/attack_mcp が見る code と英文プレフィックスは不変。"""
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
    assert out["error"].startswith("Caller authorization failed.")
