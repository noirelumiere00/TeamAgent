"""警告が添付・台帳・完了通知に共通して届くことをフェイクで確認する。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.mcp_gateway.server import _format_proposal_completion
from teamagent.skills._shared.deck_review import warning_lines
from teamagent.skills.proposal_builder import skill as module
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill
from tests.skills.proposal_builder.test_research_delivery_recovery import _build_job
from tests.skills.proposal_builder.test_tiktok_enrichment import _CapturingDeck


@pytest.mark.parametrize("review", [[7, 63], [], None])
@pytest.mark.parametrize("ready,deferred", [(True, False), (True, True), (False, False)])
def test_warning_all_delivery_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    review: list[int] | None,
    ready: bool,
    deferred: bool,
) -> None:
    original = _CapturingDeck.run

    def run(self, input, ctx):
        return original(self, input, ctx).model_copy(update={"review_slides": review})

    monkeypatch.setattr(_CapturingDeck, "run", run)
    job, ctx, target, slack, store, _ = _build_job(
        monkeypatch, tmp_path, deferred=deferred, ready=ready
    )
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done"
    assert state.proposal_status == ("ready" if ready else "draft")
    completion = _format_proposal_completion(state)
    for line in warning_lines(review):
        assert line in state.result_message
        assert line in completion
        assert line in state.warnings
    if ready:
        if deferred:
            from teamagent.mcp_gateway.async_job_notify import publish_notice

            assert publish_notice(completion, origin=target, request_id="req", job_id=job)
        comment = next(
            item["initial_comment"] for item in slack.uploads if item["title"].endswith(".pptx")
        )
        for line in warning_lines(review):
            assert line in comment
        if review:
            assert "出典の無い数値は『要確認』に置き換えています" in state.result_message
            assert "検証済み" not in comment and "検証を通過" not in state.result_message
            assert "出典の無い数値は『要確認』に置き換えています" in comment
        elif review == []:
            assert "数値出典・95枠・統合FMTを検証済みです" in comment
            assert "PowerPoint の左の一覧" not in comment
    target.discard()


@pytest.mark.parametrize("failure", ["helper", "join"])
def test_format_failure_still_delivers_pptx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    def fail(_review):
        if failure == "join":
            return [object()]
        raise ValueError("PRIVATE-TEXT")

    monkeypatch.setattr(module, "warning_lines", fail)
    with capture_logs() as logs:
        job, ctx, target, slack, store, _ = _build_job(
            monkeypatch, tmp_path, deferred=False, ready=True
        )
    event = next(item for item in logs if item["event"] == "proposal_builder_review_format_failed")
    assert event["error_type"] == ("ValueError" if failure == "helper" else "TypeError")
    assert "PRIVATE-TEXT" not in str(logs)
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and state.slack_delivered
    assert warning_lines(None)[0] in slack.uploads[0]["initial_comment"]
    target.discard()


def test_old_ledger_result_keeps_strict_validation() -> None:
    from tests.skills.proposal_builder.test_async_jobs import _proposal_output

    old = _proposal_output().model_dump(mode="json")
    assert "review_slides" not in old
    skill = ProposalBuilderStatusSkill(store=MagicMock())
    assert skill._done_output("old-job", {"result_json": old}).status == "done"
