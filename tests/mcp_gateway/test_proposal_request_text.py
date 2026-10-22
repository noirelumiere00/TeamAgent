"""Only code-injected, signed DM request text can reach proposal inputs."""

import pytest

from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway.caller_claim import CallerClaimError
from teamagent.mcp_gateway.server import _resolve_metadata
from tests.caller_claim_testkit import make_verifier, sign_arguments


async def _resolver(slack_user_id: str) -> ResolvedIdentity:
    return ResolvedIdentity(slack_user_id=slack_user_id, email="member@example.com")


@pytest.mark.parametrize("tool", ["proposal_builder", "proposal_builder_submit"])
@pytest.mark.parametrize("shared", [False, True])
async def test_signed_request_text_is_forwarded_for_both_identity_modes(
    tool: str, shared: bool
) -> None:
    args = sign_arguments(
        tool, {}, channel_id="D0123456789", declared_context={"request_text": "単価２．５円/回"}
    )
    claim = await make_verifier().verify(tool=tool, arguments=args)
    metadata, error = await _resolve_metadata(
        args["_user_context"],
        verified_caller=claim,
        require_rls=True,
        identity_resolver=_resolver,
        allowed_domains=None,
        company_shared_groups=frozenset({"example.com"}) if shared else None,
        tool=tool,
    )
    assert error is None
    assert metadata["request_text"] == "単価２．５円/回"


@pytest.mark.parametrize(
    "tool,channel,text,signed",
    [
        ("search", "D0123456789", "10万円", True),
        ("proposal_builder", "C0123456789", "10万円", True),
        ("proposal_builder", "D0123456789", "10万円", False),
        ("proposal_builder", "D0123456789", "x" * 4001, True),
        ("proposal_builder", "D0123456789", ["10万円"], True),
        ("proposal_builder", "D0123456789", None, True),
    ],
)
async def test_untrusted_or_unavailable_text_is_not_forwarded(tool, channel, text, signed) -> None:
    args = sign_arguments(tool, {}, channel_id=channel, declared_context={"request_text": text})
    claim = await make_verifier().verify(tool=tool, arguments=args) if signed else None
    metadata, error = await _resolve_metadata(
        args["_user_context"],
        verified_caller=claim,
        require_rls=False,
        identity_resolver=_resolver if signed else None,
        allowed_domains=None,
        company_shared_groups=None,
        tool=tool,
    )
    assert error is None
    assert "request_text" not in metadata


async def test_request_text_is_bound_to_the_signature() -> None:
    args = sign_arguments(
        "proposal_builder",
        {},
        channel_id="D0123456789",
        declared_context={"request_text": "単価2円"},
    )
    args["_user_context"]["request_text"] = "単価999円"
    with pytest.raises(CallerClaimError):
        await make_verifier().verify(tool="proposal_builder", arguments=args)
