"""自動調査の受付・ジョブ・DMファイル添付を外部接続なしで検証する。"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills._shared.long_jobs import ORIGIN_KEY, Origin
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import (
    ProposalBuilderInput,
    ProposalBuilderStatusInput,
    ProposalBuilderSubmitInput,
)
from teamagent.skills.proposal_builder.skill import (
    NOT_READY_MESSAGE,
    RESEARCH_NOT_READY_MESSAGE,
    ProposalBuilderSkill,
    ProposalBuilderStatusSkill,
    ProposalBuilderSubmitSkill,
)
from teamagent.skills.proposal_research.schema import ProposalResearchOutput, ResearchSummary
from tests.skills.proposal_builder.test_async_jobs import _FakeBuilder, _proposal_output
from tests.skills.proposal_builder.test_tiktok_enrichment import (
    _CapturingDeck,
    _configure_builder_test,
    _research,
)


def _input() -> ProposalBuilderSubmitInput:
    return ProposalBuilderSubmitInput.model_validate(
        {
            "research_brief": {
                "product_name": "ACME",
                "official_url": "https://example.com/product",
                "brief": "学生への認知獲得",
            },
            "posting_start_date": "2026-11-01",
        }
    )


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="research-integration",
        user_id="U123",
        metadata={
            "identity_verified": True,
            "user_email": "requester@example.com",
            "channel_id": "C123",
            "thread_ts": "1.234",
        },
    )


def _result() -> ProposalResearchOutput:
    payload = _research()
    payload["F_competitor"] = [copy.deepcopy(payload["F_competitor"][0]) for _ in range(3)]
    payload["C_tiktok"][0].update(
        representative_post_url="https://www.tiktok.com/@creator/video/123456789",
        search_demand_note="上位 2 本・最多再生 400 回",
    )
    return ProposalResearchOutput(
        research_json=payload,
        summary=ResearchSummary(
            source_count=7,
            discarded_count=2,
            discarded_by_section={"A_market_data": 2},
            elapsed_seconds=123.0,
            gemini_cost_usd=0.7,
            tiktok_search_count=6,
        ),
    )


def _immediate(target: Callable[[], None], _name: str) -> None:
    target()


@pytest.mark.parametrize(
    "extra",
    [{}, {"gemini_json": {}, "research_brief": {"product_name": "ACME"}}],
)
def test_exactly_one_input_is_required(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ProposalBuilderInput.model_validate({"posting_start_date": "2026-11-01", **extra})


def test_research_feature_off_returns_fixed_message_without_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "*")
    monkeypatch.delenv("PROPOSAL_RESEARCH_AUTO", raising=False)
    memory: dict[str, dict[str, Any]] = {}
    output = ProposalBuilderSubmitSkill(store=ProposalJobStore(table_name="", memory=memory)).run(
        _input(), _ctx()
    )
    assert output.status == "failed"
    assert output.message == RESEARCH_NOT_READY_MESSAGE
    assert not output.job_id and not memory


def test_allowlist_rejection_precedes_research_feature_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("PROPOSAL_RESEARCH_AUTO", raising=False)
    output = ProposalBuilderSubmitSkill(store=ProposalJobStore(table_name="", memory={})).run(
        _input(), _ctx()
    )
    assert output.message == NOT_READY_MESSAGE


@pytest.mark.parametrize("unreleased", [False, True])
def test_research_then_builder_receives_research_payload_and_brief(
    monkeypatch: pytest.MonkeyPatch, unreleased: bool
) -> None:
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("PROPOSAL_RESEARCH_AUTO", "1")
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    calls: list[str] = []

    class Research:
        def run(self, input: Any, ctx: SkillContext) -> ProposalResearchOutput:
            assert input.product_name == "ACME"
            assert ctx.request_id == "research-integration"
            row = next(iter(memory.values()))
            assert row["stage"] == "researching"
            state = ProposalBuilderStatusSkill(store=store).run(
                ProposalBuilderStatusInput(job_id=row["job_id"]), ctx
            )
            assert state.stage == "researching" and "調査中" in state.message
            calls.append("research")
            return _result()

    class Builder(_FakeBuilder):
        def run(self, input: ProposalBuilderInput, ctx: SkillContext) -> Any:
            assert input.gemini_json == _result().research_json
            assert input.research_brief is None
            assert input.proposal_brief == "学生への認知獲得"
            assert input.official_urls == ["https://example.com/product"]
            assert input.confidential_product_name is unreleased
            assert input.category_term == ("菓子" if unreleased else "")
            assert isinstance(ctx.metadata["_proposal_research_output"], ProposalResearchOutput)
            assert next(iter(memory.values()))["stage"] == "building"
            calls.append("builder")
            return _proposal_output()

    builder = Builder()
    initial = _input()
    if unreleased:
        assert initial.research_brief is not None
        initial = ProposalBuilderSubmitInput.model_validate(
            initial.model_dump(mode="python")
            | {
                "research_brief": initial.research_brief.model_dump()
                | {"unreleased": True, "category_term": "菓子"}
            }
        )
    output = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,  # type: ignore[return-value]
        research_factory=Research,
        store=store,
        thread_launcher=_immediate,
        heartbeat_seconds=0,
    ).run(initial, _ctx())
    assert calls == ["research", "builder"]
    assert memory[output.job_id]["status"] == "done"
    assert builder.cleaned.is_set()


def test_research_failure_is_reported_with_section_and_builder_is_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("PROPOSAL_RESEARCH_AUTO", "true")
    store = ProposalJobStore(table_name="", memory={})

    class Research:
        def run(self, input: Any, ctx: SkillContext) -> ProposalResearchOutput:
            raise ValueError("A_market_data の出典を確認できませんでした")

    builder = _FakeBuilder()
    accepted = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,  # type: ignore[return-value]
        research_factory=Research,
        store=store,
        thread_launcher=_immediate,
        heartbeat_seconds=0,
    ).run(_input(), _ctx())
    state = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=accepted.job_id), _ctx()
    )
    assert state.status == "failed" and "A_market_data" in state.message
    assert not builder.called.is_set()


class _FileSlack:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, str | None, str | None]] = []

    async def lookup_user_id_by_email(self, email: str, request_id: str) -> str:
        assert email == "requester@example.com"
        return "U123"

    async def open_dm(self, user_id: str, request_id: str) -> str:
        assert user_id == "U123"
        return "D123"

    async def upload_file(self, channel: str, path: str, request_id: str, **kwargs: Any) -> bool:
        self.uploads.append(
            (
                channel,
                kwargs["title"],
                Path(path).read_text(encoding="utf-8"),
                kwargs.get("initial_comment"),
                kwargs.get("thread_ts"),
            )
        )
        return True


@pytest.mark.parametrize("deferred", [False, True])
def test_auto_job_attaches_pptx_and_research_json_to_dm_with_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deferred: bool
) -> None:
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("PROPOSAL_RESEARCH_AUTO", "yes")
    _configure_builder_test(monkeypatch, media_configured=True)
    monkeypatch.setenv("PROPOSAL_BUILDER_DELIVER_INTERNAL_DRAFTS", "1")
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1" if deferred else "0")
    deck_path = tmp_path / "proposal.pptx"
    deck_path.write_text("fake-pptx", encoding="utf-8")

    class Deck(_CapturingDeck):
        def run(self, input: Any, ctx: SkillContext) -> Any:
            return super().run(input, ctx).model_copy(update={"pptx_path": str(deck_path)})

    class Research:
        def run(self, input: Any, ctx: SkillContext) -> ProposalResearchOutput:
            return _result()

    def never_search(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("段CのあとにTikTokへ重複検索してはならない")

    slack = _FileSlack()
    deck = Deck()
    builder = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        slack=slack,
        account_db_path="unused.xlsx",
        tiktok_searcher=never_search,
    )
    store = ProposalJobStore(table_name="", memory={})
    ctx = _ctx()
    target = Origin("C123", "1.234", "U123")
    if deferred:
        ctx.metadata[ORIGIN_KEY] = target
    accepted = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,
        research_factory=Research,
        store=store,
        thread_launcher=_immediate,
        heartbeat_seconds=0,
    ).run(_input(), ctx)
    state = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=accepted.job_id), ctx
    )
    assert state.status == "done", state.message
    if deferred:
        assert not slack.uploads and target.pending
        assert target.deliver() is True
        assert store.mark_delivered(accepted.job_id) is True
        state = ProposalBuilderStatusSkill(store=store).run(
            ProposalBuilderStatusInput(job_id=accepted.job_id), ctx
        )
    assert len(slack.uploads) == 2
    assert all(upload[0] == "D123" and upload[4] is None for upload in slack.uploads)
    assert slack.uploads[0][1].endswith(".pptx")
    assert slack.uploads[1][1].endswith(".json")
    assert json.loads(slack.uploads[1][2]) == _result().research_json
    expected = (
        "調査: 出典 7 件（リンク切れ・転送失敗の出典は除外済み、本文の内容確認は含みません）・出典が確かめられず外した主張 2 件\n"
        "調査の JSON を添付しました。直して渡せば、その JSON から作り直せます"
    )
    assert expected in (slack.uploads[0][3] or "")
    assert expected in state.result_message
    assert state.delivery_target == "dm" and state.slack_delivered is True
    assert state.total_cost_usd == 0.7
    assert "最多再生 400 回" in deck.inputs[0].research_material


def test_research_heartbeat_keeps_five_minute_job_alive() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)

    def clock() -> datetime:
        return now

    store = ProposalJobStore(table_name="", memory={}, clock=clock)
    store.create_job("pb_research_heartbeat", {})
    assert store.mark_running("pb_research_heartbeat")
    assert store.mark_stage("pb_research_heartbeat", "researching")
    status = ProposalBuilderStatusSkill(store=store, clock=clock, stale_after_seconds=180)
    for _ in range(10):
        now += timedelta(seconds=30)
        assert store.heartbeat("pb_research_heartbeat")
        result = status.run(ProposalBuilderStatusInput(job_id="pb_research_heartbeat"), _ctx())
        assert result.status == "running" and result.stage == "researching"


def test_submit_description_explains_json_free_research_input() -> None:
    description = ProposalBuilderSubmitSkill.description
    assert "商材名・公式URL・与件・投稿開始日" in description
    assert "PROPOSAL_RESEARCH_AUTO" in description and "JSONは不要" in description
