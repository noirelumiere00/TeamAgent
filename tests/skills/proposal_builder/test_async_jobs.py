"""proposal_builder のMCP内非同期job境界。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import (
    ProposalBuilderOutput,
    ProposalBuilderStatusInput,
    ProposalBuilderSubmitInput,
)
from teamagent.skills.proposal_builder.skill import (
    ProposalBuilderStatusSkill,
    ProposalBuilderSubmitSkill,
)


class _GateThreadLauncher:
    """Start a real daemon thread but hold production work behind an Event."""

    def __init__(self, *, released: bool = False) -> None:
        self.gate = threading.Event()
        self.finished = threading.Event()
        if released:
            self.gate.set()

    def __call__(self, target: Callable[[], None], name: str) -> None:
        def gated_target() -> None:
            if not self.gate.wait(timeout=10):
                self.finished.set()
                return
            try:
                target()
            finally:
                self.finished.set()

        threading.Thread(target=gated_target, name=name, daemon=True).start()


class _FakeBuilder:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.called = threading.Event()
        self.cleaned = threading.Event()

    def run(
        self,
        input: ProposalBuilderSubmitInput,
        ctx: SkillContext,
    ) -> ProposalBuilderOutput:
        del input, ctx
        self.called.set()
        if self.error is not None:
            raise self.error
        return _proposal_output()

    def cleanup_output(self, output: ProposalBuilderOutput) -> None:
        del output
        self.cleaned.set()


@dataclass
class _MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _proposal_input() -> ProposalBuilderSubmitInput:
    return ProposalBuilderSubmitInput(
        gemini_json={"schema_boundary": "validated-before-submit"},
        posting_start_date=date(2026, 8, 10),
    )


def _proposal_output() -> ProposalBuilderOutput:
    return ProposalBuilderOutput(
        status="ready",
        message="生成済み",
        pptx_url="https://example.com/proposal.pptx",
        version_id="pb-version-1",
        filled_count=95,
        skipped_count=0,
        coverage_ratio=1.0,
        slack_delivered=True,
        delivery_target="thread",
        total_cost_usd=1.25,
    )


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="proposal-async-test",
        user_id="U123",
        metadata={"channel_id": "C123", "thread_ts": "123.456"},
    )


def test_submit_returns_before_work_and_status_moves_from_queued_to_done() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher()
    builder = _FakeBuilder()
    submit = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,  # type: ignore[return-value]
        store=store,
        thread_launcher=launcher,
        input_validator=lambda _: None,
        heartbeat_seconds=0,
    )
    status = ProposalBuilderStatusSkill(store=store)

    accepted = submit.run(_proposal_input(), _ctx())

    assert accepted.status == "queued"
    assert accepted.retry_after_seconds > 0
    assert not builder.called.is_set()
    queued = status.run(ProposalBuilderStatusInput(job_id=accepted.job_id), _ctx())
    assert queued.status == "queued"

    launcher.gate.set()
    assert launcher.finished.wait(timeout=10)
    done = status.run(ProposalBuilderStatusInput(job_id=accepted.job_id), _ctx())
    assert done.status == "done"
    assert done.proposal_status == "ready"
    assert done.slack_delivered is True
    assert done.pptx_url == "https://example.com/proposal.pptx"
    assert builder.cleaned.is_set()


def test_background_exception_becomes_failed_with_error_code() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher(released=True)
    builder = _FakeBuilder(error=RuntimeError("synthetic production failure"))
    submit = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,  # type: ignore[return-value]
        store=store,
        thread_launcher=launcher,
        input_validator=lambda _: None,
        heartbeat_seconds=0,
    )

    accepted = submit.run(_proposal_input(), _ctx())

    assert launcher.finished.wait(timeout=10)
    failed = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=accepted.job_id),
        _ctx(),
    )
    assert failed.status == "failed"
    assert failed.error_code == "PROPOSAL_BUILD_FAILED"


def test_thread_start_failure_is_persisted() -> None:
    store = ProposalJobStore(table_name="", memory={})

    def reject_start(_target: Callable[[], None], _name: str) -> None:
        raise RuntimeError("thread quota exhausted")

    submit = ProposalBuilderSubmitSkill(
        builder_factory=lambda: _FakeBuilder(),  # type: ignore[return-value]
        store=store,
        thread_launcher=reject_start,
        input_validator=lambda _: None,
        heartbeat_seconds=0,
    )

    rejected = submit.run(_proposal_input(), _ctx())

    assert rejected.status == "failed"
    failed = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=rejected.job_id),
        _ctx(),
    )
    assert failed.status == "failed"
    assert failed.error_code == "JOB_START_FAILED"


def test_submit_validates_before_creating_job_row() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    submit = ProposalBuilderSubmitSkill(
        builder_factory=lambda: _FakeBuilder(),  # type: ignore[return-value]
        store=store,
        heartbeat_seconds=0,
    )

    with pytest.raises(ValueError):
        submit.run(_proposal_input(), _ctx())
    assert memory == {}


def test_stale_running_job_fails_closed_as_mcp_restarted() -> None:
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    store = ProposalJobStore(table_name="", memory={}, clock=clock)
    job_id = "pb_stale_running"
    store.create_job(job_id, {"request_id": "stale-test"})
    assert store.mark_running(job_id)
    clock.value += timedelta(seconds=181)

    failed = ProposalBuilderStatusSkill(
        store=store,
        stale_after_seconds=180,
        clock=clock,
    ).run(ProposalBuilderStatusInput(job_id=job_id), _ctx())

    assert failed.status == "failed"
    assert failed.error_code == "MCP_RESTARTED"
    persisted = store.get_job(job_id)
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["error_code"] == "MCP_RESTARTED"


def test_stale_queued_job_fails_closed_as_mcp_restarted() -> None:
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    store = ProposalJobStore(table_name="", memory={}, clock=clock)
    job_id = "pb_stale_queued"
    store.create_job(job_id, {"request_id": "stale-before-thread-start"})
    clock.value += timedelta(seconds=181)

    failed = ProposalBuilderStatusSkill(
        store=store,
        stale_after_seconds=180,
        clock=clock,
    ).run(ProposalBuilderStatusInput(job_id=job_id), _ctx())

    assert failed.status == "failed"
    assert failed.error_code == "MCP_RESTARTED"


def test_missing_active_timestamp_fails_closed_as_invalid_state() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    job_id = "pb_missing_timestamp"
    store.create_job(job_id, {"request_id": "corrupt-row"})
    memory[job_id].pop("updated_at")

    failed = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=job_id),
        _ctx(),
    )

    assert failed.status == "failed"
    assert failed.error_code == "JOB_STATE_INVALID"
    assert memory[job_id]["status"] == "failed"


def test_non_string_active_timestamp_is_terminalized() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    job_id = "pb_invalid_timestamp_type"
    store.create_job(job_id, {"request_id": "corrupt-row"})
    memory[job_id]["updated_at"] = 0

    failed = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=job_id),
        _ctx(),
    )

    assert failed.status == "failed"
    assert failed.error_code == "JOB_STATE_INVALID"
    assert memory[job_id]["status"] == "failed"


def test_missing_job_uses_failed_state_contract() -> None:
    missing = ProposalBuilderStatusSkill(store=ProposalJobStore(table_name="", memory={})).run(
        ProposalBuilderStatusInput(job_id="pb_missing"), _ctx()
    )

    assert missing.status == "failed"
    assert missing.error_code == "JOB_NOT_FOUND"


def test_default_stale_window_stays_above_heartbeat_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPOSAL_JOB_HEARTBEAT_SECONDS", "300")
    monkeypatch.setenv("PROPOSAL_JOB_STALE_SECONDS", "60")
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    store = ProposalJobStore(table_name="", memory={}, clock=clock)
    job_id = "pb_healthy_slow_heartbeat"
    store.create_job(job_id, {"request_id": "heartbeat-window"})
    assert store.mark_running(job_id)
    clock.value += timedelta(seconds=301)

    running = ProposalBuilderStatusSkill(store=store, clock=clock).run(
        ProposalBuilderStatusInput(job_id=job_id),
        _ctx(),
    )

    assert running.status == "running"


def test_unset_ddb_env_uses_process_shared_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROPOSAL_JOBS_TABLE", raising=False)
    submit_store = ProposalJobStore()
    status_store = ProposalJobStore()
    assert submit_store.uses_dynamodb is False
    assert status_store.uses_dynamodb is False
    launcher = _GateThreadLauncher(released=True)
    builder = _FakeBuilder()
    submit = ProposalBuilderSubmitSkill(
        builder_factory=lambda: builder,  # type: ignore[return-value]
        store=submit_store,
        thread_launcher=launcher,
        input_validator=lambda _: None,
        heartbeat_seconds=0,
    )

    accepted = submit.run(_proposal_input(), _ctx())

    assert launcher.finished.wait(timeout=10)
    done = ProposalBuilderStatusSkill(store=status_store).run(
        ProposalBuilderStatusInput(job_id=accepted.job_id),
        _ctx(),
    )
    assert done.status == "done"
    assert done.version_id == "pb-version-1"
