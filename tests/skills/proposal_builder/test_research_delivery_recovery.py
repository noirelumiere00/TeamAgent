"""調査JSONとPPTXを独立して届ける回帰検証。通信はすべてフェイク。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.long_jobs import ORIGIN_KEY, Origin
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder import skill as module
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.selectors import CaseCandidate, SelectedAccount
from teamagent.skills.proposal_builder.skill import (
    ProposalBuilderSkill,
    ProposalBuilderStatusSkill,
    ProposalBuilderSubmitSkill,
    _research_summary_lines,
)
from teamagent.skills.proposal_research.schema import ProposalResearchOutput
from tests.skills.proposal_builder.test_auto_research import _ctx, _immediate, _input, _result
from tests.skills.proposal_builder.test_tiktok_enrichment import (
    _CapturingDeck,
    _configure_builder_test,
)


class _Slack:
    def __init__(self, *, first_dm_failure: bool = False, fail_extension: str = "") -> None:
        self.first_dm_failure = first_dm_failure
        self.fail_extension = fail_extension
        self.dm_calls = 0
        self.uploads: list[dict[str, Any]] = []

    async def lookup_user_id_by_email(self, *_args: Any) -> str:
        return "U123"

    async def open_dm(self, *_args: Any) -> str | None:
        self.dm_calls += 1
        return None if self.first_dm_failure and self.dm_calls == 1 else "D123"

    async def upload_file(self, channel: str, path: str, _request: str, **kwargs: Any) -> bool:
        self.uploads.append({"channel": channel, "text": Path(path).read_text(), **kwargs})
        return not (self.fail_extension and kwargs["title"].endswith(self.fail_extension))


def _build_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    deferred: bool,
    ready: bool = False,
    build_failure: bool = False,
    slack: _Slack | None = None,
) -> tuple[str, SkillContext, Origin, _Slack, ProposalJobStore, list[str]]:
    monkeypatch.setenv("PROPOSAL_RESEARCH_AUTO", "1")
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1" if deferred else "0")
    _configure_builder_test(monkeypatch, media_configured=False)
    if ready:
        monkeypatch.setattr(
            module,
            "load_and_select_accounts",
            lambda *_a, **_kw: [
                SelectedAccount(
                    rank=1,
                    score=1,
                    name="アカウント",
                    category=["食品"],
                    desc="食品",
                    tt="",
                    ig="",
                    yt="",
                    matched_categories=["食品"],
                    matched_keywords=[],
                )
            ],
        )
        monkeypatch.setattr(
            module,
            "search_case_candidates",
            lambda *_a, **_kw: [
                CaseCandidate(
                    source="report_rag",
                    title="事例",
                    url="https://example.test/case",
                    excerpt="■施策: 投稿\n■結果: 認知",
                    score=1.0,
                )
            ],
        )
    deck_path = tmp_path / "generated.pptx"
    deck_path.write_text("fake-pptx")

    class Deck(_CapturingDeck):
        def run(self, input: Any, ctx: SkillContext) -> Any:
            if build_failure:
                raise ValueError("組み立て失敗")
            return super().run(input, ctx).model_copy(update={"pptx_path": str(deck_path)})

    class Research:
        def run(self, *_args: Any) -> ProposalResearchOutput:
            return _result()

    slack = slack or _Slack()
    builder = ProposalBuilderSkill(
        search=object(), deck=Deck(), slack=slack, account_db_path="unused.xlsx"
    )  # type: ignore[arg-type]
    store = ProposalJobStore(table_name="", memory={})
    ctx = _ctx()
    target = Origin("C123", "1.234", "U123")
    if deferred:
        ctx.metadata[ORIGIN_KEY] = target
    posts: list[str] = []
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    monkeypatch.setattr(
        detached_jobs, "post_to_origin", lambda message, *_a, **_kw: posts.append(message) or True
    )
    accepted = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,
        research_factory=Research,
        store=store,
        thread_launcher=_immediate,
        heartbeat_seconds=0,
    ).run(_input(), ctx)
    assert "DMにお届け" in accepted.message
    return accepted.job_id, ctx, target, slack, store, posts


@pytest.mark.parametrize("deferred", [False, True])
def test_default_draft_delivers_json_without_pptx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deferred: bool
) -> None:
    job, ctx, target, slack, store, posts = _build_job(monkeypatch, tmp_path, deferred=deferred)
    assert store.get_job(job)["status"] == "done"  # type: ignore[index]
    if deferred:
        assert slack.dm_calls == 0 and target.pending
        assert notify.publish_notice("ドラフトです", origin=target, request_id="req", job_id=job)
        assert posts == ["調査JSONをDMへお届けしました。提案書の添付は行っていません。"]
    assert len(slack.uploads) == 1
    assert slack.uploads[0]["title"].endswith(".json")
    assert slack.uploads[0]["channel"] == "D123"
    assert json.loads(slack.uploads[0]["text"]) == _result().research_json
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.proposal_status == "draft" and not state.slack_delivered
    assert state.research_delivery_status == "delivered"
    assert "調査の JSON を添付しました" in state.result_message


@pytest.mark.parametrize("deferred", [False, True])
def test_build_failure_preserves_completed_json_for_failure_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deferred: bool
) -> None:
    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=deferred, build_failure=True
    )
    assert store.get_job(job)["status"] == "failed"  # type: ignore[index]
    if deferred:
        assert not slack.uploads and target.pending
        assert notify.publish_notice(
            "組み立てに失敗しました。", origin=target, request_id="req", job_id=job, completed=False
        )
        assert "調査JSONはDMへお届け" in posts[0]
    assert len(slack.uploads) == 1 and slack.uploads[0]["title"].endswith(".json")
    assert json.loads(slack.uploads[0]["text"]) == _result().research_json
    assert store.get_job(job)["research_delivery_status"] == "delivered"  # type: ignore[index]
    assert (
        ProposalBuilderStatusSkill(store=store)
        .run(ProposalBuilderStatusInput(job_id=job), ctx)
        .status
        == "failed"
    )


@pytest.mark.parametrize("deferred", [False, True])
def test_ready_pptx_survives_json_upload_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deferred: bool
) -> None:
    slack = _Slack(fail_extension=".json")
    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=deferred, ready=True, slack=slack
    )
    if deferred:
        assert target.pending and slack.dm_calls == 0
        assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
        assert "調査JSONの添付に失敗" in posts[0]
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and state.proposal_status == "ready"
    assert state.slack_delivered and state.delivery_target == "dm"
    assert state.research_delivery_status == "failed"
    assert "調査JSONの添付に失敗" in state.result_message
    assert "調査の JSON を添付しました" not in state.result_message
    assert slack.dm_calls == 1
    assert [item["title"].rsplit(".", 1)[-1] for item in slack.uploads] == ["pptx", "json"]


@pytest.mark.parametrize("deferred", [False, True])
def test_one_transient_dm_open_retries_before_uploads_and_shares_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deferred: bool
) -> None:
    slack = _Slack(first_dm_failure=True)
    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=deferred, ready=True, slack=slack
    )
    if deferred:
        assert slack.dm_calls == 0
        assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
        assert posts == ["資料をDMへお届けしました。"]
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and state.proposal_status == "ready" and state.slack_delivered
    assert slack.dm_calls == 2 and len(slack.uploads) == 2
    assert state.research_delivery_status == "delivered"


def test_draft_json_delivery_failure_is_recorded_and_requests_new_research(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, ctx, target, _slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, slack=_Slack(fail_extension=".json")
    )
    assert notify.publish_notice("ドラフト", origin=target, request_id="req", job_id=job)
    row = store.get_job(job)
    assert row and row["status"] == "done" and row["research_delivery_status"] == "failed"
    assert row["research_delivery_error"] == "SLACK_JSON_DELIVERY_FAILED"
    assert "再度調査をご依頼" in posts[0]
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert "添付予定" not in state.result_message


def test_pptx_failure_still_delivers_json_and_reports_deck_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True, slack=_Slack(fail_extension=".pptx")
    )
    assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and not state.slack_delivered
    assert state.research_delivery_status == "delivered" and len(slack.uploads) == 2
    assert "提案書の添付に失敗" in posts[0] and "調査JSONはDMへ" in posts[0]


def test_json_file_write_failure_is_failed_instead_of_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_builder_test(monkeypatch, media_configured=False)
    builder = ProposalBuilderSkill(
        search=object(), deck=_CapturingDeck(), slack=_Slack(), account_db_path="unused.xlsx"
    )  # type: ignore[arg-type]
    ctx = _ctx()
    monkeypatch.setattr(
        module.tempfile, "TemporaryDirectory", lambda **_kw: (_ for _ in ()).throw(OSError())
    )
    assert not asyncio.run(
        builder._deliver_research_only(
            ctx=ctx,
            research=_result(),
            title="research.json",
            comment="調査",
        )
    )
    assert ctx.metadata["_proposal_research_delivery"] == "failed"


def test_bot_blocked_summary_does_not_claim_all_sources_confirmed() -> None:
    result = _result()
    result.summary.unconfirmed_count = 2
    message = _research_summary_lines(result)
    assert "出典 7 件（2 件はサイトが自動の確認を拒否）" in message
    assert "すべて実在を確認" not in message


def test_multiple_primary_files_require_every_upload_to_succeed(tmp_path: Path) -> None:
    target = Origin("C123", "1.234", "U123")
    path = tmp_path / "file.txt"
    path.write_text("file")
    slack = _Slack(fail_extension=".pdf")
    target.defer(slack, str(path), "first.pptx", "完成", "req")
    target.defer(slack, str(path), "second.pdf", "完成", "req")
    assert not target.deliver()
    assert not target.primary_delivered and len(slack.uploads) == 2


def test_delivery_record_failure_does_not_stop_later_uploads(tmp_path: Path) -> None:
    target = Origin("C123", "1.234", "U123")
    path = tmp_path / "file.txt"
    path.write_text("file")
    slack = _Slack()

    def record(_ok: bool) -> None:
        raise RuntimeError("台帳一時失敗")

    target.defer(slack, str(path), "first.json", "調査", "req", on_result=record)
    target.defer(slack, str(path), "second.pptx", "完成", "req")
    assert target.deliver() and target.primary_delivered
    assert len(slack.uploads) == 2


def test_dm_retry_can_be_disabled_without_losing_ready_job_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_RETRY", "0")
    slack = _Slack(first_dm_failure=True)
    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True, slack=slack
    )
    assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and state.proposal_status == "ready"
    assert state.research_delivery_status == "failed"
    assert slack.dm_calls == 1 and not slack.uploads
    assert "調査JSONの添付に失敗" in posts[0]


def test_primary_timeout_delivers_json_and_reports_uncertainty_to_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class TimeoutSlack(_Slack):
        async def upload_file(self, channel: str, path: str, request: str, **kwargs: Any) -> bool:
            if kwargs["title"].endswith(".pptx"):
                raise TimeoutError("添付結果不確実")
            return await super().upload_file(channel, path, request, **kwargs)

    job, ctx, target, slack, store, posts = _build_job(
        monkeypatch, tmp_path, deferred=True, ready=True, slack=TimeoutSlack()
    )
    assert notify.publish_notice("完成", origin=target, request_id="req", job_id=job)
    assert "提案書の添付結果を確認できません" in posts[0]
    assert "調査JSONはDMへお届け" in posts[0]
    assert not target.pending
    assert len(slack.uploads) == 1 and slack.uploads[0]["title"].endswith(".json")
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and not state.slack_delivered
    assert state.research_delivery_status == "delivered"
