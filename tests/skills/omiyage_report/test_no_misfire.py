"""既存 proposal_builder 経路の不発火と、ジョブ種別の相互不可侵。"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.orchestrator import factory
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.schema import (
    OmiyageReportResult,
    OmiyageReportStatusInput,
)
from teamagent.skills.omiyage_report.skill import (
    OmiyageReportStatusSkill,
    new_omiyage_job_id,
)
from teamagent.skills.proposal_builder.schema import (
    ProposalBuilderStatusInput,
    ProposalBuilderSubmitInput,
)
from teamagent.skills.proposal_builder.skill import (
    ProposalBuilderStatusSkill,
    _validate_submit_input,
)


def _ctx() -> SkillContext:
    return SkillContext(request_id="omiyage-misfire-test", user_id="U123")


def test_proposal_builder_submit_rejects_omiyage_style_conversation_payload() -> None:
    """「お土産つくって」相当の入力は Gemini A-H 契約を満たせず提案書経路に入れない。"""
    payload = ProposalBuilderSubmitInput(
        gemini_json={
            "brand": "エムキュア",
            "competitors": ["ラサーナ", "THE ANSWER"],
            "request": "エムキュアのお土産資料つくって",
        },
        posting_start_date="2026-09-01",
    )
    with pytest.raises(ValidationError):
        _validate_submit_input(payload)


def test_factory_gates_are_independent_env_flags() -> None:
    """omiyage は独自フラグ配下で、proposal_builder のフラグでは登録されない（逆も同様）。"""
    source = inspect.getsource(factory.build_production_tools)

    omiyage_gate = re.search(
        r'if _envflag\("USE_OMIYAGE_REPORT_TOOLS"\):(?P<body>(?:\n {8}.*)+)',
        source,
    )
    assert omiyage_gate is not None, "omiyage_report の env ゲートが factory に無い"
    assert "OmiyageReportSubmitSkill" in omiyage_gate.group("body")
    assert "OmiyageReportStatusSkill" in omiyage_gate.group("body")

    builder_gate = re.search(
        r'if _envflag\("USE_PROPOSAL_BUILDER_TOOLS"\):(?P<body>(?:\n {8}.*)+)',
        source,
    )
    assert builder_gate is not None
    assert "Omiyage" not in builder_gate.group("body")
    assert "ProposalBuilder" not in omiyage_gate.group("body")


def _omiyage_done_row(store: ProposalJobStore) -> str:
    job_id = new_omiyage_job_id()
    store.create_job(job_id, {"kind": "omiyage_report"})
    assert store.mark_running(job_id)
    result = OmiyageReportResult(
        status="ready",
        message="done",
        summary_lines=["1", "2", "3"],
        next_step="次の一手",
        slack_delivered=True,
        delivery_target="thread",
    )
    assert store.mark_done(job_id, result.model_dump_json())
    return job_id


def test_proposal_builder_status_refuses_omiyage_jobs_without_destroying_them() -> None:
    """pb status が omiyage の done 行を RESULT_INVALID へ破壊的 terminalize しないこと。"""
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    job_id = _omiyage_done_row(store)

    out = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=job_id), _ctx()
    )
    assert out.status == "failed"
    assert out.error_code == "JOB_KIND_MISMATCH"

    row = store.get_job(job_id)
    assert row is not None
    assert row["status"] == "done"  # 破壊されていない
    assert "result_json" in row


def test_omiyage_status_refuses_foreign_kind_rows_without_writes() -> None:
    """スキーマ pattern を突破する omy_ 行でも kind 不一致なら読み取りだけで拒否する。"""
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    # 事故で omy_ prefix を持つ異種 job が同居した想定（kind が別物）
    job_id = "omy_" + "a" * 32
    store.create_job(job_id, {"kind": "proposal_builder", "request_id": "x"})

    out = OmiyageReportStatusSkill(store=store).run(OmiyageReportStatusInput(job_id=job_id), _ctx())
    assert out.status == "failed"
    assert out.error_code == "JOB_KIND_MISMATCH"
    row = store.get_job(job_id)
    assert row is not None
    assert row["status"] == "queued"  # 書き込みしていない
    assert json.loads(str(row["request_summary"]))["kind"] == "proposal_builder"


def test_omiyage_and_proposal_jobs_share_store_without_id_collision_space() -> None:
    assert new_omiyage_job_id().startswith("omy_")
    from teamagent.adapters.proposal_job_store import new_proposal_job_id

    assert new_proposal_job_id().startswith("pb_")
