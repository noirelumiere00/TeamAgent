"""Use real signed identity resolution; a declared email cannot opt a caller in."""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway.server import _resolve_metadata
from teamagent.skills.omiyage_report import sales25
from tests.caller_claim_testkit import make_verifier, sign_arguments


@pytest.mark.parametrize("shared", [False, True])
async def test_rollout_uses_resolver_email_instead_of_declared_email(
    monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    monkeypatch.setenv("USE_OMIYAGE_SALES25", "1")
    monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "self@example.test")
    args = sign_arguments(
        "omiyage_report_submit",
        {},
        channel_id="D0123456789",
        declared_context={"user_email": "self@example.test"},
    )
    claim = await make_verifier().verify(tool="omiyage_report_submit", arguments=args)

    async def resolver(user: str) -> ResolvedIdentity:
        return ResolvedIdentity(slack_user_id=user, email="other@example.test")

    metadata, error = await _resolve_metadata(
        args["_user_context"],
        verified_caller=claim,
        require_rls=True,
        identity_resolver=resolver,
        allowed_domains=None,
        company_shared_groups=frozenset({"example.test"}) if shared else None,
        tool="omiyage_report_submit",
    )
    assert error is None and metadata["identity_verified"] is True
    assert not sales25.enabled(metadata)
    metadata["user_email"] = "self@example.test"
    assert sales25.enabled(metadata)


def test_progress_only_on_phase_changes_and_one_hour_does_not_imply_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters.proposal_job_store import ProposalJobStore
    from teamagent.mcp_gateway import detached_jobs
    from teamagent.skills._shared.long_jobs import Origin

    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1")
    store = ProposalJobStore(table_name="", memory={})
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    posted: list[str] = []

    def post(message: str, *args: Any, **kwargs: Any) -> str:
        posted.append(message)
        return "posted"

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", post)
    clock = [0.0]
    monkeypatch.setattr(notify, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(notify, "_MAX_WATCH_SECONDS", 10_000)
    monkeypatch.setattr(
        notify,
        "_wait_until_next_poll",
        lambda deadline, interval: clock.__setitem__(0, clock[0] + 30),
    )

    def poll() -> tuple[str, str]:
        if clock[0] >= 7200:
            return "done", "sales25は未完成（incomplete）です。"
        stage = "検索 1/6" if clock[0] < 100 else "検索 6/6" if clock[0] < 200 else "分析"
        return "running", f"sales25: {stage}。目安は1〜3時間（仮）です。"

    assert notify._run_completion_notice(
        job_id="omy_progress", origin=Origin("D_SELF", None, "U_SELF"), request_id="fake", poll=poll
    )
    assert len(posted) == 4  # search, analysis, one stalled notice, terminal
    assert sum("検索" in m for m in posted) == 1
    assert sum("まだ完了していません" in m for m in posted) == 1
    assert all("停止" not in m and "通常より" not in m for m in posted)
    assert "incomplete" in posted[-1]


def test_recovered_watcher_restores_attachment_in_its_original_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from importlib import import_module

    from teamagent.adapters.proposal_job_store import ProposalJobStore
    from teamagent.skills._shared.long_jobs import ORIGIN_KEY
    from teamagent.skills.omiyage_report import skill as module

    f = import_module("tests.skills.omiyage_report.test_sales25_parent")
    monkeypatch.setenv("USE_OMIYAGE_SALES25", "1")
    monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "self@example.test")
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1")
    store = ProposalJobStore(table_name="", memory={})
    pipeline = f.Pipeline()
    accepted = f.build(store, pipeline).run(f.request(), f.ctx())
    slack = f.FakeSlack(store, accepted.job_id)
    parent = sales25.Parent(store, reader=lambda ref: pipeline.data, slack=slack)
    status_class = module.OmiyageReportStatusSkill
    monkeypatch.setattr(
        module, "OmiyageReportStatusSkill", lambda: status_class(store=store, sales25_parent=parent)
    )
    entry = {
        "tool": "omiyage_report_submit",
        "job_id": accepted.job_id,
        "channel_id": "D_SELF",
        "thread_ts": "123.456",
        "user_id": "U_SELF",
        "owner": "T_TEST\x1fU_SELF",
        "request_id": "fake",
        "principal": "test-principal",
        "updated_at": time.time() - 400,
        "created_at": time.time() - 400,
    }
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    monkeypatch.setattr(notify, "_active", {})
    notify._change_outbox(lambda entries: {"recovery": entry})
    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify, "schedule_completion_notice", lambda **kwargs: scheduled.append(kwargs)
    )
    assert notify.recover_pending_notices() == 1
    recovered = scheduled[0]
    assert recovered["ctx"].metadata[ORIGIN_KEY] is recovered["origin"]
    assert recovered["poll"]()[0] == "done"
    assert recovered["origin"].pending and not slack.uploads
    recovered["origin"].discard()
