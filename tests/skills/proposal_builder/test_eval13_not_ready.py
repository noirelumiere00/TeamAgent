"""未提供の提案書生成は一文で伝え、入力要求や架空の通知を付けない。"""

from unittest.mock import Mock

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.proposal_builder.skill import ProposalBuilderSubmitSkill
from tests.skills.proposal_builder.test_auto_research import _ctx, _input


@pytest.mark.parametrize(("allowed", "verified"), [("", True), ("*", True), ("*", False)])
def test_not_ready_is_one_honest_sentence_without_job(monkeypatch, allowed, verified):
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", allowed)
    monkeypatch.setenv("PROPOSAL_RESEARCH_AUTO", "0")
    ctx = _ctx()
    ctx.metadata["identity_verified"] = verified
    memory = {}
    launch = Mock(side_effect=AssertionError("must not launch a job"))
    skill = ProposalBuilderSubmitSkill(
        store=ProposalJobStore(table_name="", memory=memory), thread_launcher=launch
    )
    out = skill.run(_input(), ctx)
    assert out.status == "failed" and out.job_id == ""
    assert "準備中" in out.message
    assert out.message.endswith("。") and out.message.count("。") == 1
    for forbidden in ("Gemini", "JSON", "お知らせ", "骨子", "job"):
        assert forbidden not in out.message
    assert memory == {}
    launch.assert_not_called()
