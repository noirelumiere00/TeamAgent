"""完了見張り（async_job_notify）の poll closure は ctx に「見張り経路」の印を立てて status を呼ぶ。

status skill 側（tiktok_acquire_status）はこの印を見て、課金を伴う Apify 補完を発火させない。
LLM の照会と見張りが同時に来ても同じ URL を並列 run しない、の入口側の固定。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.mcp_gateway import server
from teamagent.skills.base import ASYNC_JOB_POLL_METADATA_KEY, SkillContext
from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusOutput


def test_tiktok_poll_ctx_carries_async_poll_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    import teamagent.skills.tiktok_acquire.skill as skill_module

    captured: dict[str, Any] = {}

    class _FakeStatusSkill:
        def run(self, input: Any, ctx: SkillContext) -> TikTokAcquireStatusOutput:
            captured["ctx"] = ctx
            return TikTokAcquireStatusOutput(job_id=input.job_id, status="done", message="ok")

    monkeypatch.setattr(skill_module, "TikTokAcquireStatusSkill", _FakeStatusSkill)
    ctx = SkillContext(request_id="req-1", user_id="U1", metadata={"user_email": "a@example.com"})
    poll = server._build_async_job_poll("tiktok_acquire", "tk_0123456789ab", ctx)

    status, text = poll()
    assert status == "done" and "tk_0123456789ab" in text
    poll_ctx = captured["ctx"]
    assert poll_ctx.metadata[ASYNC_JOB_POLL_METADATA_KEY] is True
    assert poll_ctx.metadata["user_email"] == "a@example.com"  # 元の metadata は保つ
    assert poll_ctx.request_id == "req-1"
    assert ASYNC_JOB_POLL_METADATA_KEY not in ctx.metadata  # 呼び出し元 ctx は汚さない
