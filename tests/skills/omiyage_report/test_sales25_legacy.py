"""The golden was generated from the unmodified HEAD skill, with fixed ID/date."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.omiyage_report import skill as module
from teamagent.skills.omiyage_report.schema import OmiyageReportStatusInput, Sales25Intake

_fixtures = import_module("tests.skills.omiyage_report.test_async_flow")
_build = _fixtures._build
_ctx = _fixtures._ctx
_GateThreadLauncher = _fixtures._GateThreadLauncher
_input = _fixtures._input


@pytest.mark.parametrize(
    "condition", ["unset", "empty", "zero", "false", "outside", "allowlist_empty", "unverified"]
)
def test_legacy_request_output_and_dm_are_identical_to_baseline(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    monkeypatch.setenv("USE_OMIYAGE_SALES25", "1")
    monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "sales@example.com")
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    monkeypatch.setenv("FIRST_VISIT_CAPTURE", "0")
    monkeypatch.setenv("OMIYAGE_LLM_INPUT_CHECK", "0")
    context = _ctx()
    context.metadata.update(
        identity_verified=True,
        verified_slack_user_id="U123",
        verified_slack_team_id="T_TEST",
        channel_id="D999",
    )
    if condition == "unset":
        monkeypatch.delenv("USE_OMIYAGE_SALES25")
    elif condition in {"empty", "zero", "false"}:
        monkeypatch.setenv(
            "USE_OMIYAGE_SALES25", {"empty": "", "zero": "0", "false": "false"}[condition]
        )
    elif condition == "outside":
        monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "other@example.test")
    elif condition == "allowlist_empty":
        monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "")
    else:
        context.metadata["identity_verified"] = False
    monkeypatch.setattr(module, "new_omiyage_job_id", lambda: "omy_" + "a" * 32)
    monkeypatch.setattr(module, "_utc_now", lambda: datetime(2026, 10, 23, tzinfo=UTC))
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher(released=True)
    skill, search, _builder, slack = _build(store=store, launcher=launcher)
    request = _input()
    # Even supplied new fields cannot alter old request JSON, output, or DM while gated off.
    request.sales25 = Sales25Intake(target_product="unused", claim_constraints="unused")
    accepted = skill.run(request, context)
    assert launcher.finished.wait(10)
    output = module.OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=accepted.job_id), context
    )
    row = store.get_job(accepted.job_id)
    assert row is not None
    actual = {
        "request": request.model_dump(),
        "accepted": accepted.model_dump(),
        "summary": json.loads(row["request_summary"]),
        "search_calls": search.calls,
        "status": output.model_dump(),
        "dm_uploads": [{k: v for k, v in u.items() if k != "file_path"} for u in slack.uploads],
        "dm_posts": slack.posts,
    }
    expected = json.loads(Path(__file__).with_name("sales25_legacy_golden.json").read_text())
    assert actual == expected
    assert "edition" not in json.dumps(actual)
    assert all(u["channel"] == "D999" for u in actual["dm_uploads"])
