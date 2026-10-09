"""配信の失敗・中断・再起動で、届いたものだけを案内する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.skills._shared.long_jobs import Origin
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import (
    ProposalBuilderStatusSkill,
    _research_summary_lines,
)
from tests.skills.proposal_builder.test_auto_research import _ctx, _result
from tests.skills.proposal_builder.test_research_delivery_recovery import _build_job, _Slack


@pytest.mark.parametrize("failure", ["open_dm", "upload"])
def test_both_attachments_failed_notice_never_claims_completion(
    failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FailingSlack(_Slack):
        async def open_dm(self, *args: Any) -> str | None:
            if failure == "open_dm":
                return None
            return await super().open_dm(*args)

        async def upload_file(self, *args: Any, **kwargs: Any) -> bool:
            return False

    job, ctx, target, _, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True, slack=FailingSlack()
    )
    assert notify.publish_notice(
        "✅ 完了\n調査の JSON を添付予定です", origin=target, request_id="req", job_id=job
    )
    assert "提案書の添付に失敗" in posts[0] and "調査JSONの添付に失敗" in posts[0]
    assert "完了" not in posts[0] and "添付予定" not in posts[0]
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert not state.slack_delivered and state.research_delivery_status == "failed"
    target.discard()


def test_pptx_defer_error_is_failed_even_if_json_is_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = Origin.defer

    def fail_deck(
        self: Origin, slack: Any, path: str, title: str, *args: Any, **kwargs: Any
    ) -> None:
        if title.endswith(".pptx"):
            raise OSError(28, "no space")
        original(self, slack, path, title, *args, **kwargs)

    monkeypatch.setattr(Origin, "defer", fail_deck)
    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True
    )
    assert store.get_job(job)["status"] == "failed"
    assert target.pending and not target.primary_pending
    assert notify.publish_notice(
        "提案書生成に失敗しました", origin=target, request_id="req", job_id=job, completed=False
    )
    assert len(slack.uploads) == 1 and slack.uploads[0]["title"].endswith(".json")
    assert "失敗" in posts[0] and "調査JSONはDMへお届け" in posts[0]
    assert (
        ProposalBuilderStatusSkill(store=store)
        .run(ProposalBuilderStatusInput(job_id=job), ctx)
        .research_delivery_status
        == "delivered"
    )


def test_discard_updates_json_delivery_status_and_requests_new_research(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, ctx, target, _, store, _ = _build_job(monkeypatch, tmp_path, deferred=True)
    target.discard()
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.research_delivery_status == "failed"
    assert "添付予定" not in state.result_message and "再度調査" in state.result_message


def test_cancelled_defer_calls_failure_callback(tmp_path: Path) -> None:
    target = Origin("C123", "1.234", "U123", cancelled=True)
    path = tmp_path / "research.json"
    path.write_text("{}")
    results: list[bool] = []
    target.defer(
        _Slack(),
        str(path),
        "research.json",
        "調査",
        "req",
        deliver_on_failure=True,
        on_result=results.append,
    )
    assert results == [False] and not target.pending


@pytest.mark.parametrize("status", ["done", "running"])
def test_restart_never_leaves_lost_json_pending(
    status: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, _, target, _, store, _ = _build_job(monkeypatch, tmp_path, deferred=True)
    # Abrupt process loss: new ctx and Origin, no callback or memory attachment survives.
    row = store._memory[job]
    row["status"] = status
    row["stage"] = "building"
    if status == "running":
        row.pop("research_delivery_status", None)
    later = datetime.now(UTC) + timedelta(minutes=10)
    state = ProposalBuilderStatusSkill(store=store, clock=lambda: later).run(
        ProposalBuilderStatusInput(job_id=job), _ctx()
    )
    assert state.research_delivery_status == "failed"
    message = state.result_message if status == "done" else state.message
    assert "添付予定" not in message and "再度調査" in message
    target.discard()


def test_dm_thread_receives_delivery_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, _, target, _, _, posts = _build_job(monkeypatch, tmp_path, deferred=True, ready=True)
    target.channel_id = "D123"
    assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
    assert posts == ["資料をDMへお届けしました。"]


def test_summary_describes_reachability_without_claiming_content_verified() -> None:
    message = _research_summary_lines(_result())
    assert "すべて実在を確認" not in message
    assert "本文の内容確認は含みません" in message


def test_delivery_record_failure_never_suppresses_truthful_failure_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, _, target, _, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True, slack=_Slack(fail_extension=".pptx")
    )

    def fail(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(store, "record_primary_delivery_failure", fail)
    assert notify.publish_notice("✅ 完了", origin=target, request_id="req", job_id=job)
    assert "提案書の添付に失敗" in posts[0] and "調査JSONはDMへお届け" in posts[0]
    assert "✅" not in posts[0]


def test_missing_primary_copy_is_reported_as_failure_not_intentional_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, _, target, slack, _, posts = _build_job(monkeypatch, tmp_path, deferred=True)
    with pytest.raises(FileNotFoundError):
        target.defer(slack, str(tmp_path / "missing.pptx"), "proposal.pptx", "完成", "req")
    assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
    assert "提案書の添付に失敗" in posts[0] and "調査JSONはDMへお届け" in posts[0]
    assert "添付は行っていません" not in posts[0]


def test_failed_job_keeps_failure_notice_when_research_json_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """失敗したジョブで調査JSONの添付も失敗したとき、失敗の知らせを上書きしない（10-09 検証）。"""

    class FailingSlack(_Slack):
        async def upload_file(self, *args: Any, **kwargs: Any) -> bool:
            return False

    job, _ctx, target, _, _store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True, slack=FailingSlack()
    )
    failure = "提案書生成に失敗しました。資料の組み立てで止まりました。"
    notify.publish_notice(failure, origin=target, request_id="req", job_id=job, completed=False)
    assert posts and posts[0].startswith(failure)
    assert "完了" not in posts[0]
    target.discard()
