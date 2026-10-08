"""調査の進行段階は本番と同じDynamoDB契約で保持する。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from tests.adapters.test_proposal_job_store import _FakeDynamo


def test_research_stage_round_trips_and_refreshes_heartbeat_in_dynamodb() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    dynamo = _FakeDynamo()
    store = ProposalJobStore(table_name="proposal-jobs", dynamodb_client=dynamo, clock=lambda: now)
    store.create_job("pb_stage", {})
    assert store.mark_stage("pb_stage", "researching") is False
    assert store.mark_running("pb_stage") is True
    now += timedelta(seconds=30)
    assert store.mark_stage("pb_stage", "researching") is True
    row = store.get_job("pb_stage")
    assert row is not None
    assert row["stage"] == "researching"
    assert row["updated_at"] == "2026-10-08T00:00:30Z"
    assert store.mark_stage("pb_stage", "building") is True
    assert store.mark_done("pb_stage", "{}") is True
    assert store.mark_stage("pb_stage", "researching") is False
    assert store.get_job("pb_stage")["stage"] == "building"  # type: ignore[index]


def test_stage_rejects_unknown_phase() -> None:
    with pytest.raises(ValueError, match="invalid proposal job stage"):
        ProposalJobStore(table_name="", memory={}).mark_stage("pb_missing", "unknown")


def test_deferred_dm_delivery_keeps_dm_destination_in_job_result() -> None:
    store = ProposalJobStore(table_name="", memory={})
    store.create_job("pb_dm", {"research_auto": True})
    assert store.mark_running("pb_dm")
    assert store.mark_done(
        "pb_dm",
        json.dumps({"slack_delivered": False, "delivery_target": "dm", "message": "生成済み"}),
    )
    assert store.mark_delivered("pb_dm")
    row = store.get_job("pb_dm")
    assert row is not None
    result = json.loads(row["result_json"])
    assert result["slack_delivered"] is True and result["delivery_target"] == "dm"
    assert result["message"] == "生成済み DMへ添付しました。"
