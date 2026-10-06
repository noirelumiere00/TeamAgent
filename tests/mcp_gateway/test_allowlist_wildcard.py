"""機能の許可リストの ``*``＝本人確認済みの全員（2026-10-06 小俣さん指示）。

本番の状態: 直接投稿・動画分析の切り離し・検索上位の追い分析は、許可リストに 1 人（小俣さん）だけで、
空にすると全員拒否の作り（「全員」を書く方法が無かった）。
変異: email_allowed の ``*`` 分岐を外す・未解決の email を通すと赤。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.mcp_gateway import detached_jobs, direct_summary, surface_video_followup
from teamagent.mcp_gateway.allowlist import email_allowed
from tests.mcp_gateway.test_video_algorithm_detach import _claim as _detach_claim
from tests.mcp_gateway.test_video_algorithm_detach import _policy as _detach_policy

OTHER = "someone@vectorinc.co.jp"
ALL = frozenset({"*"})


@pytest.mark.parametrize(
    ("email", "allowed", "expected"),
    [
        (OTHER, ALL, True),
        (OTHER.upper(), ALL, True),
        (None, ALL, False),
        ("", ALL, False),
        ("not-an-email", ALL, False),
        (OTHER, frozenset(), False),
        (OTHER, frozenset({"s-komata@vectorinc.co.jp"}), False),
        (" Someone@VectorInc.co.jp ", frozenset({OTHER}), True),
    ],
)
def test_email_allowed(email: Any, allowed: frozenset[str], expected: bool) -> None:
    assert email_allowed(email, allowed) is expected


def test_direct_summary_wildcard_opens_to_every_verified_user() -> None:
    from tests.mcp_gateway.test_direct_summary import TOOL, _claim

    policy = direct_summary.DirectPolicy(True, ALL)
    verified = {"identity_verified": True, "user_email": OTHER}
    destination, reason = direct_summary.decide(
        policy, tool=TOOL, verified_caller=_claim(), metadata=verified
    )
    assert reason == "ok" and destination is not None
    _, reason = direct_summary.decide(
        policy,
        tool=TOOL,
        verified_caller=_claim(),
        metadata={"identity_verified": False, "user_email": OTHER},
    )
    assert reason == "unverified"


def test_surface_followup_wildcard_opens_to_every_verified_user() -> None:
    from tests.mcp_gateway.test_surface_video_followup import _claim

    policy = surface_video_followup.FollowupPolicy(enabled=True, allowed_emails=ALL)
    destination, reason = surface_video_followup.decide(
        policy,
        verified_caller=_claim(),
        metadata={"identity_verified": True, "user_email": OTHER},
    )
    assert reason == "ok" and destination is not None


def test_detach_wildcard_opens_to_every_verified_user() -> None:
    policy = _detach_policy(allowed_emails=ALL)
    destination, reason = detached_jobs.decide(
        policy,
        tool="video_algorithm",
        verified_caller=_detach_claim(),
        metadata={"identity_verified": True, "user_email": OTHER},
    )
    assert reason == "ok" and destination is not None
