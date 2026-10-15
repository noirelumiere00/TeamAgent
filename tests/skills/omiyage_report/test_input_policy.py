"""ブランドだけの受付から実際の資料契約・配信要約まで追う。"""

from __future__ import annotations

from importlib import import_module

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills._shared import input_policy as policy
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput

# 既存のローカル取得・解析・レンダラ契約フェイクを再利用する。
_fixtures = import_module("tests.skills.omiyage_report.test_async_flow")
_build = _fixtures._build
_ctx = _fixtures._ctx
_GateThreadLauncher = _fixtures._GateThreadLauncher


def test_brand_only_reaches_deck_and_compose_without_empty_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "1")
    monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", "*")
    monkeypatch.setattr(policy, "CACHE", policy.PolicyCache())
    ctx = _ctx()
    ctx.metadata["identity_verified"] = True
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher(released=True)
    skill, searcher, builder, slack = _build(store=store, launcher=launcher)
    accepted = skill.run(OmiyageReportSubmitInput(brand="エムキュア"), ctx)
    assert accepted.status == "queued"
    assert "今回は競合比較を省きました" in accepted.message
    assert launcher.finished.wait(timeout=10)
    assert [c["query"] for c in searcher.calls] == ["エムキュア"]
    assert len(builder.plans) == 1
    plan = builder.plans[0]
    assert plan.deck_meta.brand_b is None
    assert {s.type for s in plan.slide_plan} == {"A", "B", "E", "H"}
    assert not ({"Q1", "Q2", "Q3", "Q4"} & {s.q_number for s in plan.slide_plan})
    text = plan.model_dump_json()
    assert "競合なし" not in text
    assert "競合の露出" not in text
    assert "登場率の分母" not in text
    assert slack.uploads
    comment = str(slack.uploads[0]["initial_comment"])
    assert "今回は一般キーワード検索と登場率、競合比較を省きました。" in comment
    assert "登場率（" not in comment
