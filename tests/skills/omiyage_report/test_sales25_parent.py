"""PR6: no network; durable parent and the existing notification lock with fake stages/DM."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.long_jobs import _OWNER_KEY, ORIGIN_KEY, Origin
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report import sales25
from teamagent.skills.omiyage_report.schema import (
    OmiyageReportStatusInput,
    OmiyageReportSubmitInput,
    Sales25Intake,
)
from teamagent.skills.omiyage_report.skill import (
    JobAdmission,
    OmiyageReportStatusSkill,
    OmiyageReportSubmitSkill,
)


def ctx(user: str = "U_SELF", channel: str = "D_SELF") -> SkillContext:
    return SkillContext(
        request_id="pr6-fake",
        user_id="self@example.test",
        metadata={
            "identity_verified": True,
            "user_email": "self@example.test",
            "verified_slack_user_id": user,
            "verified_slack_team_id": "T_TEST",
            "channel_id": channel,
            "thread_ts": "123.456",
            "_sales25_session": "session-1",
            _OWNER_KEY: "T_TEST\x1f" + user,
            ORIGIN_KEY: Origin(channel, "123.456", user),
        },
    )


def request(**updates: Any) -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(
        **{
            "brand": "ブランドA",
            "competitors": ["ブランドB", "ブランドC"],
            "keywords": ["朝のケア", "乾燥対策", "時短ケア"],
            "category": "ケア用品",
            "sales25": Sales25Intake(target_product="商品A", claim_constraints="効能を断定しない"),
            **updates,
        }
    )


@pytest.fixture(autouse=True)
def flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_OMIYAGE_SALES25", "1")
    monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "self@example.test")
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1")
    monkeypatch.delenv("PROPOSAL_JOBS_TABLE", raising=False)
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **k: "posted")
    monkeypatch.setattr(detached_jobs, "post_to_origin", lambda *a, **k: True)


@dataclass
class Pipeline:
    actual: int = 100
    calls: list[str] = field(default_factory=list)
    data: bytes = b"PK-fake-editable-sales25"
    bad_hash: bool = False
    pages: int | None = None
    check_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    actual_by_query: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    crash: str = ""

    def step(
        self, case: dict[str, Any], previous: dict[str, sales25.StepResult], key: str
    ) -> sales25.StepResult:
        stage = key.split(":")[-1]
        self.calls.append(key)
        if stage == self.crash:
            raise SimulatedRestart
        if self.outcomes.get(stage) == "timeout":
            raise TimeoutError("private URL/exception must never reach the user")
        if self.outcomes.get(stage) == "refused":
            raise PermissionError("private diagnostics")
        if stage.startswith("search_"):
            assert case["query"] == case["queries"][int(stage.split("_")[1]) - 1]
            assert case["target"] == 100
            return sales25.StepResult(
                status="incomplete" if self.outcomes.get(stage) == "shortfall" else "complete",
                reason="shortfall" if self.outcomes.get(stage) == "shortfall" else None,
                actual=self.actual_by_query.get(case["query"], self.actual),
                fetched_at="2026-10-23T00:00:00Z",
                child_job_ids=["child-" + stage],
                refs=[{"raw": key}],
            )
        if stage == "fill":
            ref = sales25.Artifact(
                bucket="private-fixture",
                key=key + ".pptx",
                version_id="v1",
                sha256=hashlib.sha256(self.data).hexdigest(),
                size=len(self.data),
                content_type=sales25.PPTX_TYPE,
                owner_id=case["owner_id"],
            )
            return sales25.StepResult(status="complete", artifact=ref)
        if stage == "machine_check":
            artifact = previous["fill"].artifact
            assert artifact is not None
            passed = {
                "status": "passed",
                "checked_sha256": "0" * 64 if self.bad_hash else artifact.sha256,
            }
            checks: dict[str, dict[str, Any]] = {
                name: dict(passed) for name in sales25.MachineChecks.model_fields
            }
            checks["deck_qa_l1"].update(error_count=0, review_pages=[])
            for name, update in self.check_changes.items():
                checks[name].update(update)
            return sales25.StepResult(
                status="complete", checks=sales25.MachineChecks.model_validate(checks)
            )
        if stage == "page_qa":
            if self.pages is None:
                return sales25.rendering_not_available(case, previous, key)
            artifact = previous["fill"].artifact
            assert artifact is not None
            return sales25.StepResult(
                status="complete",
                checked_sha256="bad" if self.bad_hash else artifact.sha256,
                reviewed_pages=list(range(1, self.pages + 1)),
            )
        if self.outcomes.get(stage) == "audio_unverified":
            return sales25.StepResult(status="incomplete", reason="audio_unverified")
        if self.outcomes.get(stage) == "shortfall":
            return sales25.StepResult(status="incomplete", reason="shortfall")
        return sales25.StepResult(status="complete", refs=[{"evidence": key}])

    def stages(self) -> sales25.Stages:
        return sales25.Stages(**{s: self.step for s in ("search", *sales25.STAGES)})


class SimulatedRestart(BaseException):
    pass


@dataclass
class FakeSlack:
    store: ProposalJobStore
    job_id: str = ""
    uncertain: bool = False
    uploads: list[dict[str, Any]] = field(default_factory=list)
    dm_users: list[str] = field(default_factory=list)

    async def open_dm(self, user: str, request_id: str) -> str:
        self.dm_users.append(user)
        return "D_SELF"

    async def upload_file(self, channel: str, path: str, request_id: str, **kwargs: Any) -> bool:
        from pathlib import Path

        assert self.store.get_job(self.job_id)["status"] == "done"  # type: ignore[index]
        self.uploads.append({"channel": channel, "data": Path(path).read_bytes(), **kwargs})
        if self.uncertain:
            raise TimeoutError
        return True


def build(
    store: ProposalJobStore, pipeline: Pipeline | None = None, **kwargs: Any
) -> OmiyageReportSubmitSkill:
    return OmiyageReportSubmitSkill(
        store=store,
        sales25_stages=pipeline.stages() if pipeline else None,
        thread_launcher=kwargs.pop("thread_launcher", lambda target, name: target()),
        heartbeat_seconds=0,
        admission=JobAdmission(2),
        **kwargs,
    )


def result(store: ProposalJobStore, job_id: str) -> sales25.ParentResult:
    row = store.get_job(job_id)
    assert row is not None and row["status"] == "done"
    return sales25.ParentResult.model_validate_json(row["result_json"])


@pytest.mark.parametrize("flag", [None, "", "0", "false", "no"])
def test_flag_default_off(monkeypatch: pytest.MonkeyPatch, flag: str | None) -> None:
    if flag is None:
        monkeypatch.delenv("USE_OMIYAGE_SALES25")
    else:
        monkeypatch.setenv("USE_OMIYAGE_SALES25", flag)
    assert not sales25.enabled(ctx().metadata)


@pytest.mark.parametrize(
    "allowed,verified,email",
    [
        ("", True, "self@example.test"),
        (" , ", True, "self@example.test"),
        ("*", True, "self@example.test"),
        ("self@example.test", False, "self@example.test"),
        ("self@example.test", True, "other@example.test"),
    ],
)
def test_empty_allowlist_and_unverified_are_off(
    monkeypatch: pytest.MonkeyPatch, allowed: str, verified: bool, email: str
) -> None:
    monkeypatch.setenv("OMIYAGE_SALES25_ALLOWED_EMAILS", allowed)
    metadata = {**ctx().metadata, "identity_verified": verified, "user_email": email}
    assert not sales25.enabled(metadata)


def test_preflight_remembers_confirmed_answers_across_skill_instances() -> None:
    store = ProposalJobStore(table_name="", memory={})
    first = build(store).run(request(competitors=[], keywords=[], sales25=None), ctx())
    assert first.status == "needs_input"
    assert first.missing == ["competitors", "keywords", "target_product", "claim_constraints"]
    assert "カテゴリー" not in first.message and "JSON" not in first.message
    second = build(store).run(
        OmiyageReportSubmitInput(
            competitors=["ブランドB", "ブランドC"],
            keywords=["朝のケア", "乾燥対策", "時短ケア"],
            sales25=Sales25Intake(target_product="商品A"),
        ),
        ctx(),
    )
    assert second.missing == ["claim_constraints"]
    assert "競合" not in second.message
    third = build(store).run(
        OmiyageReportSubmitInput(sales25=Sales25Intake(claim_constraints="条件なし")), ctx()
    )
    assert third.status == "queued"
    assert result(store, third.job_id).status == "incomplete"
    other = build(store).run(OmiyageReportSubmitInput(), ctx("U_OTHER"))
    assert "brand" in other.missing


def test_input_policy_cannot_skip_sales25_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "1")
    monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", "*")
    store = ProposalJobStore(table_name="", memory={})
    for _ in range(2):
        response = build(store).run(OmiyageReportSubmitInput(brand="ブランドA"), ctx())
        assert response.status == "needs_input"
        assert {
            "competitors",
            "category",
            "keywords",
            "target_product",
            "claim_constraints",
        } <= set(response.missing)
    assert not store._memory


def test_fake_pipeline_partial_delivers_once_after_terminal_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(actual=37, check_changes={"deck_qa_l1": {"review_pages": [3, 7, 21]}})
    accepted = build(store, pipeline).run(request(), ctx())
    assert "約35〜45分（暫定" in accepted.message
    saved = result(store, accepted.job_id)
    assert saved.status == "partial" and len(saved.checkpoints) == 12
    assert saved.final_pptx_ref is not None
    for query in ["ブランドA", "ブランドB", "ブランドC", "朝のケア", "乾燥対策", "時短ケア"]:
        assert f"「{query}」100本目標→37本取得" in "\n".join(saved.summary_lines)
    assert saved.checkpoints["page_qa"].status == "not_available"
    assert "完成" not in saved.message
    slack = FakeSlack(store, accepted.job_id)
    parent = sales25.Parent(store, reader=lambda ref: pipeline.data, slack=slack)
    status = OmiyageReportStatusSkill(store=store, sales25_parent=parent)
    context = ctx()
    output = status.run(OmiyageReportStatusInput(job_id=accepted.job_id), context)
    assert output.report_status == "partial" and not slack.uploads
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    target = context.metadata[ORIGIN_KEY]
    assert notify.publish_notice(
        output.message, origin=target, request_id="fake", job_id=accepted.job_id
    )
    assert len(slack.uploads) == 1 and slack.dm_users == ["U_SELF"]
    assert slack.uploads[0]["data"] == pipeline.data
    assert slack.uploads[0]["channel"] == "D_SELF"
    comment = slack.uploads[0]["initial_comment"]
    assert "partial" in comment
    assert "見た目（描画）は未確認。PowerPoint の左の一覧で 3・7・21枚目がL1の要確認です" in comment
    assert result(store, accepted.job_id).delivery_status == "delivered"
    # A restarted watcher shares the durable notice lock, not the old Origin object.
    again = ctx()
    status.run(OmiyageReportStatusInput(job_id=accepted.job_id), again)
    assert not notify.publish_notice(
        "again", origin=again.metadata[ORIGIN_KEY], request_id="fake", job_id=accepted.job_id
    )
    assert len(slack.uploads) == 1


@pytest.mark.parametrize(
    "stage,cause", [("search_2", "refused"), ("search_3", "timeout"), ("analysis", "timeout")]
)
def test_refusal_and_timeout_never_imply_complete(stage: str, cause: str) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(outcomes={stage: cause})
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "incomplete" and saved.delivery_status == "not_ready"
    assert saved.checkpoints[stage].status == cause
    assert "再開地点" in saved.next_step and "private" not in saved.model_dump_json()
    assert not any(c.endswith(":fill") for c in pipeline.calls)


@pytest.mark.parametrize("bad_hash,pages", [(True, 25), (False, 24)])
def test_failed_connected_qa_cannot_deliver(bad_hash: bool, pages: int) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(bad_hash=bad_hash, pages=pages)
    accepted = build(store, pipeline).run(request(), ctx())
    assert result(store, accepted.job_id).status == "incomplete"
    parent = sales25.Parent(store, reader=lambda ref: pytest.fail("no attachment for failed QA"))
    output = parent.status(accepted.job_id, store.get_job(accepted.job_id), ctx())  # type: ignore[arg-type]
    assert output.delivery_status == "not_ready"


def test_dedup_separates_edition_case_and_actual_queries() -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()
    skill = build(store, pipeline)
    first = skill.run(request(), ctx())
    second = skill.run(request(), ctx())
    assert second.deduplicated and first.job_id == second.job_id
    changed = request(
        sales25=Sales25Intake(
            target_product="商品A",
            claim_constraints="効能を断定しない",
            queries=["A ケア", "B ケア", "C ケア", "朝", "乾燥", "時短"],
        )
    )
    third = skill.run(changed, ctx())
    assert third.job_id != first.job_id
    case, _ = sales25.preflight(store, changed, ctx())
    original = sales25.fingerprint(case, "owner")
    assert sales25.fingerprint({**case, "edition": "legacy"}, "owner") != original
    assert sales25.fingerprint({**case, "target_product": "商品D"}, "owner") != original


def test_other_person_cannot_read_or_resume_even_when_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProposalJobStore(table_name="", memory={})
    accepted = build(store, Pipeline()).run(request(), ctx())
    monkeypatch.setenv("USE_OMIYAGE_SALES25", "0")
    other = ctx("U_OTHER")
    output = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=accepted.job_id), other
    )
    assert output.error_code == "JOB_NOT_FOUND" and not output.summary_lines
    assert result(store, accepted.job_id).status == "ready"


def test_resume_after_restart_uses_saved_steps_and_same_child_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 23, tzinfo=UTC)
    store = ProposalJobStore(table_name="", memory={}, clock=lambda: now)
    pipeline = Pipeline(crash="search_3")
    pending: list[Callable[[], None]] = []
    accepted = build(
        store, pipeline, thread_launcher=lambda target, name: pending.append(target)
    ).run(request(), ctx())
    with pytest.raises(SimulatedRestart):
        pending[0]()
    job_id = accepted.job_id
    old = store.get_job(job_id)
    assert old is not None
    assert set(json.loads(old["checkpoint_json"])["steps"]) == {"search_1", "search_2"}
    now += timedelta(minutes=10)
    pipeline.crash = ""
    parent = sales25.Parent(
        store,
        stages=pipeline.stages(),
        heartbeat_seconds=0,
        reader=lambda ref: pipeline.data,
        slack=FakeSlack(store, job_id),
    )
    monkeypatch.setenv("USE_OMIYAGE_SALES25", "0")
    recovered = OmiyageReportStatusSkill(
        store=store,
        sales25_parent=parent,
        clock=lambda: now,
        thread_launcher=lambda target, name: target(),
    )
    recovered.run(OmiyageReportStatusInput(job_id=job_id), ctx())
    assert result(store, job_id).status == "ready"
    assert sum(c.endswith(":search_1") for c in pipeline.calls) == 1
    assert sum(c.endswith(":search_3") for c in pipeline.calls) == 2
    assert len({c for c in pipeline.calls if c.endswith(":search_3")}) == 1
    assert not store.mark_done(job_id, "{}", expected_checkpoint=old["checkpoint_json"])


def test_old_worker_is_fenced_at_checkpoint_heartbeat_and_terminal_write() -> None:
    store = ProposalJobStore(table_name="", memory={})
    store.create_job("omy_fencing", {"edition": "sales25"})
    assert store.save_checkpoint("omy_fencing", "old", expected_checkpoint=None)
    assert store.save_checkpoint("omy_fencing", "new", expected_checkpoint="old")
    assert not store.heartbeat("omy_fencing", expected_checkpoint="old")
    assert not store.save_checkpoint("omy_fencing", "stale", expected_checkpoint="old")
    assert not store.mark_done("omy_fencing", "{}", expected_checkpoint="old")
    assert store.mark_done("omy_fencing", "{}", expected_checkpoint="new")


def test_uncertain_upload_is_not_retried_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()
    accepted = build(store, pipeline).run(request(), ctx())
    slack = FakeSlack(store, accepted.job_id, uncertain=True)
    parent = sales25.Parent(store, reader=lambda ref: pipeline.data, slack=slack)
    context = ctx()
    parent.status(accepted.job_id, store.get_job(accepted.job_id), context)  # type: ignore[arg-type]
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    assert not notify.publish_notice(
        "sales25", origin=context.metadata[ORIGIN_KEY], request_id="fake", job_id=accepted.job_id
    )
    assert result(store, accepted.job_id).delivery_status == "uncertain"
    again = ctx()
    parent.status(accepted.job_id, store.get_job(accepted.job_id), again)  # type: ignore[arg-type]
    assert not again.metadata[ORIGIN_KEY].pending
    assert not notify.publish_notice(
        "sales25", origin=again.metadata[ORIGIN_KEY], request_id="fake", job_id=accepted.job_id
    )
    assert len(slack.uploads) == 1


def test_artifact_hash_and_real_version_are_required() -> None:
    store = ProposalJobStore(table_name="", memory={})
    accepted = build(store, Pipeline()).run(request(), ctx())
    ref = result(store, accepted.job_id).final_pptx_ref
    assert ref is not None
    for version in (None, "null", "", " "):
        with pytest.raises(ValidationError):
            sales25.Artifact.model_validate({**ref.model_dump(), "version_id": version})
    parent = sales25.Parent(
        store, reader=lambda ref: b"x" * ref.size, slack=FakeSlack(store, accepted.job_id)
    )
    context = ctx()
    output = parent.status(accepted.job_id, store.get_job(accepted.job_id), context)  # type: ignore[arg-type]
    assert output.delivery_status == "failed" and not context.metadata[ORIGIN_KEY].pending


def test_revision_reuses_research_and_rechecks_final_bytes() -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()
    skill = build(store, pipeline)
    source = skill.run(request(), ctx())
    calls = len(pipeline.calls)
    revision = skill.run(
        request(
            sales25=Sales25Intake(mode="revision", source_job_id=source.job_id),
            research_notes="文章の修正",
        ),
        ctx(),
    )
    assert revision.job_id != source.job_id
    assert [c.split(":")[-1] for c in pipeline.calls[calls:]] == [
        "fill",
        "machine_check",
        "page_qa",
    ]
    assert result(store, revision.job_id).status == "ready"
    revised = json.loads(store.get_job(revision.job_id)["request_summary"])  # type: ignore[index]
    assert revised["case"]["source_job_id"] == source.job_id


def test_normal_zero_is_distinct_from_refusal_and_not_ready() -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()

    def zero(
        case: dict[str, Any], previous: dict[str, sales25.StepResult], key: str
    ) -> sales25.StepResult:
        return sales25.StepResult(
            status="zero",
            actual=0,
            fetched_at="2026-10-23T00:00:00Z",
            refs=[{"diagnostic": "truly_empty"}],
        )

    stages = pipeline.stages()
    from dataclasses import replace

    skill = OmiyageReportSubmitSkill(
        store=store,
        sales25_stages=replace(stages, search=zero),
        heartbeat_seconds=0,
        admission=JobAdmission(1),
        thread_launcher=lambda target, name: target(),
    )
    accepted = skill.run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "partial"  # the fake subsequent stages and QA are complete
    assert all(saved.checkpoints[f"search_{i}"].status == "zero" for i in range(1, 7))
    assert "正常0件" in saved.next_step and "取得拒否" not in saved.next_step


def test_refused_response_is_preserved_without_exception() -> None:
    from dataclasses import replace

    store = ProposalJobStore(table_name="", memory={})
    stages = replace(Pipeline().stages(), search=lambda *args: sales25.StepResult(status="refused"))
    skill = OmiyageReportSubmitSkill(
        store=store,
        sales25_stages=stages,
        heartbeat_seconds=0,
        admission=JobAdmission(1),
        thread_launcher=lambda target, name: target(),
    )
    accepted = skill.run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "incomplete" and saved.checkpoints["search_1"].status == "refused"
    assert "取得拒否" in saved.next_step


def test_stale_duplicate_keeps_the_original_parent() -> None:
    now = datetime(2026, 10, 23, tzinfo=UTC)
    store = ProposalJobStore(table_name="", memory={}, clock=lambda: now)
    pending: list[Callable[[], None]] = []
    skill = build(
        store, clock=lambda: now, thread_launcher=lambda target, name: pending.append(target)
    )
    first = skill.run(request(), ctx())
    now += timedelta(hours=2)
    second = skill.run(request(), ctx())
    assert second.deduplicated and second.job_id == first.job_id
    assert len(pending) == 1


def test_one_origin_only_queues_one_attachment_before_publish() -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()
    accepted = build(store, pipeline).run(request(), ctx())
    parent = sales25.Parent(
        store, reader=lambda ref: pipeline.data, slack=FakeSlack(store, accepted.job_id)
    )
    context = ctx()
    row = store.get_job(accepted.job_id)
    assert row is not None
    parent.status(accepted.job_id, row, context)
    parent.status(accepted.job_id, row, context)
    target = context.metadata[ORIGIN_KEY]
    assert len(target._uploads) == 1
    target.discard()


@pytest.mark.parametrize("reason", ["audio_unverified", "shortfall"])
def test_incomplete_analysis_delivers_partial_with_shortage(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(
        outcomes={"analysis": reason},
        check_changes={"check_top5_analysis": {"status": "incomplete", "reason": reason}},
    )
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "partial" and saved.delivery_status == "pending"
    assert saved.checkpoints["analysis"].status == "incomplete"
    assert "fill" in saved.checkpoints
    slack = FakeSlack(store, accepted.job_id)
    parent = sales25.Parent(store, reader=lambda ref: pipeline.data, slack=slack)
    context = ctx()
    parent.status(accepted.job_id, store.get_job(accepted.job_id), context)  # type: ignore[arg-type]
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    assert notify.publish_notice(
        saved.message,
        origin=context.metadata[ORIGIN_KEY],
        request_id="fake",
        job_id=accepted.job_id,
    )
    comment = slack.uploads[0]["initial_comment"]
    assert "partial" in comment and "見た目（描画）は未確認" in comment
    assert ("原音が未確認" if reason == "audio_unverified" else "必要件数が不足") in comment


def test_shortfall_names_only_the_actual_queries_that_are_short() -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(actual_by_query={"実際の検索語": 37}, outcomes={"search_6": "shortfall"})
    accepted = build(store, pipeline).run(
        request(
            sales25=Sales25Intake(
                target_product="商品A",
                claim_constraints="効能を断定しない",
                queries=["A", "B", "C", "朝", "乾燥", "実際の検索語"],
            )
        ),
        ctx(),
    )
    saved = result(store, accepted.job_id)
    assert saved.status == "partial" and saved.delivery_status == "pending"
    assert saved.summary_lines == ["「実際の検索語」100本目標→37本取得"]


@pytest.mark.parametrize("check", list(sales25.MachineChecks.model_fields))
@pytest.mark.parametrize("change", [{"status": "failed"}, {"checked_sha256": "0" * 64}])
def test_each_required_machine_check_failure_or_stale_sha_blocks_delivery(
    check: str, change: dict[str, Any]
) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(check_changes={check: change})
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "incomplete" and saved.delivery_status == "not_ready"
    assert saved.checkpoints["machine_check"].reason == "fmt_invalid"
    parent = sales25.Parent(store, reader=lambda ref: pytest.fail("must not restore invalid PPTX"))
    context = ctx()
    parent.status(accepted.job_id, store.get_job(accepted.job_id), context)  # type: ignore[arg-type]
    assert not context.metadata[ORIGIN_KEY].pending


def test_l1_errors_block_delivery_even_when_stage_claims_passed() -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(check_changes={"deck_qa_l1": {"error_count": 1}})
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "incomplete" and saved.delivery_target == "none"


@pytest.mark.parametrize("pages", [None, 25])
def test_visual_disclosure_and_all_l1_warning_pages_share_one_line(pages: int | None) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(
        pages=pages, check_changes={"deck_qa_l1": {"review_pages": list(range(1, 26))}}
    )
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "ready" and saved.delivery_status == "pending"
    warning = saved.message.splitlines()[1]
    assert ("見た目（描画）は未確認" if pages is None else "全25頁確認済み") in warning
    assert "・".join(str(n) for n in range(1, 26)) + "枚目" in warning
    assert "確かめてから使ってください" in warning and "ほか" not in warning


@pytest.mark.parametrize("status", ["passed", "incomplete"])
def test_top5_shortage_alone_is_partial_not_ready(status: str) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline(
        check_changes={"check_top5_analysis": {"status": status, "reason": "audio_unverified"}}
    )
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.checkpoints["analysis"].status == "complete"
    assert saved.status == "partial" and saved.delivery_status == "pending"
    assert "原音が未確認" in saved.next_step


@pytest.mark.parametrize("reason", ["audio_unverified", "shortfall"])
def test_completed_stage_with_shortage_remains_partial(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()
    original = pipeline.step

    def step(
        case: dict[str, Any], previous: dict[str, sales25.StepResult], key: str
    ) -> sales25.StepResult:
        if key.endswith(":analysis"):
            return sales25.StepResult.model_validate({"status": "complete", "reason": reason})
        return original(case, previous, key)

    monkeypatch.setattr(pipeline, "step", step)
    accepted = build(store, pipeline).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.status == "partial" and saved.delivery_status == "pending"
