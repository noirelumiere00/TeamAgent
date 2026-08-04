"""proposal_builder job ledger のDynamoDB契約。"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill


class _ConditionalFailureError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("conditional failure")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamo:
    """Evaluate only the expressions emitted by ProposalJobStore."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, dict[str, str]]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.before_update: Callable[[_FakeDynamo, dict[str, Any]], None] | None = None
        self._lock = threading.RLock()

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.put_calls.append(copy.deepcopy(kwargs))
            job_id = kwargs["Item"]["job_id"]["S"]
            if job_id in self.items:
                raise _ConditionalFailureError
            assert kwargs["ConditionExpression"] == "attribute_not_exists(job_id)"
            self.items[job_id] = copy.deepcopy(kwargs["Item"])
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.get_calls.append(copy.deepcopy(kwargs))
            assert kwargs["ConsistentRead"] is True
            job_id = kwargs["Key"]["job_id"]["S"]
            item = self.items.get(job_id)
            return {"Item": copy.deepcopy(item)} if item is not None else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.update_calls.append(copy.deepcopy(kwargs))
            callback = self.before_update
            self.before_update = None
            if callback is not None:
                callback(self, kwargs)

            job_id = kwargs["Key"]["job_id"]["S"]
            item = self.items.get(job_id)
            if item is None or not self._condition_matches(item, kwargs):
                raise _ConditionalFailureError
            self._apply_update(item, kwargs)
        return {}

    @staticmethod
    def _condition_matches(
        item: dict[str, dict[str, str]],
        kwargs: dict[str, Any],
    ) -> bool:
        condition = kwargs["ConditionExpression"]
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        status_name = names["#status"]
        status = item.get(status_name)
        expected_statuses = [
            value
            for placeholder, value in values.items()
            if placeholder.startswith(":expected_status_") and placeholder in condition
        ]
        if expected_statuses and status not in expected_statuses:
            return False
        if "#status = :running" in condition and status != values[":running"]:
            return False

        updated_at_name = names.get("#updated_at", "updated_at")
        if "#updated_at = :expected_updated_at" in condition:
            if item.get(updated_at_name) != values[":expected_updated_at"]:
                return False
        if "attribute_not_exists(#updated_at)" in condition and updated_at_name in item:
            return False
        if "attribute_exists(#updated_at)" in condition and updated_at_name not in item:
            return False
        if (
            "NOT attribute_type(#updated_at, :updated_at_string_type)" in condition
            and "S" in item[updated_at_name]
        ):
            return False
        return True

    @staticmethod
    def _apply_update(
        item: dict[str, dict[str, str]],
        kwargs: dict[str, Any],
    ) -> None:
        expression = kwargs["UpdateExpression"]
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        set_expression, separator, remove_expression = expression.partition(" REMOVE ")
        assert set_expression.startswith("SET ")
        for assignment in set_expression.removeprefix("SET ").split(", "):
            attribute, placeholder = assignment.split(" = ", maxsplit=1)
            item[names[attribute]] = copy.deepcopy(values[placeholder])
        if separator:
            for attribute in remove_expression.split(", "):
                item.pop(names[attribute], None)


class _BrokenDynamo:
    def put_item(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ddb unavailable")


@dataclass
class _MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _ctx() -> SkillContext:
    return SkillContext(request_id="proposal-ddb-test", user_id="U123", metadata={})


def test_ddb_create_and_get_round_trip_uses_minimal_typed_row() -> None:
    now = datetime(2026, 8, 4, 1, 2, 3, tzinfo=UTC)
    dynamo = _FakeDynamo()
    store = ProposalJobStore(
        table_name="proposal-jobs",
        dynamodb_client=dynamo,
        clock=lambda: now,
    )

    store.create_job("pb_round_trip", {"z": 2, "a": "first"})

    assert dynamo.put_calls == [
        {
            "TableName": "proposal-jobs",
            "Item": {
                "job_id": {"S": "pb_round_trip"},
                "status": {"S": "queued"},
                "created_at": {"S": "2026-08-04T01:02:03Z"},
                "updated_at": {"S": "2026-08-04T01:02:03Z"},
                "request_summary": {"S": '{"a":"first","z":2}'},
                "expires_at": {"N": str(int(now.timestamp()) + 7 * 24 * 60 * 60)},
            },
            "ConditionExpression": "attribute_not_exists(job_id)",
        }
    ]
    assert store.get_job("pb_round_trip") == {
        "job_id": "pb_round_trip",
        "status": "queued",
        "created_at": "2026-08-04T01:02:03Z",
        "updated_at": "2026-08-04T01:02:03Z",
        "request_summary": '{"a":"first","z":2}',
    }
    assert dynamo.get_calls[-1] == {
        "TableName": "proposal-jobs",
        "Key": {"job_id": {"S": "pb_round_trip"}},
        "ConsistentRead": True,
    }


def test_ddb_conditional_state_transitions_preserve_terminal_state() -> None:
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    dynamo = _FakeDynamo()
    store = ProposalJobStore(
        table_name="proposal-jobs",
        dynamodb_client=dynamo,
        clock=clock,
    )
    store.create_job("pb_done", {"request_id": "done"})

    assert store.mark_running("pb_done") is True
    clock.value += timedelta(seconds=30)
    assert store.heartbeat("pb_done") is True
    result_json = json.dumps({"status": "ready"})
    assert store.mark_done("pb_done", result_json) is True
    assert store.mark_failed("pb_done", "LATE_FAILURE") is False
    done = store.get_job("pb_done")
    assert done is not None
    assert done["status"] == "done"
    assert done["result_json"] == result_json
    assert "error_code" not in done

    store.create_job("pb_failed", {"request_id": "failed"})
    assert store.mark_running("pb_failed") is True
    assert store.mark_failed("pb_failed", "PROPOSAL_BUILD_FAILED") is True
    failed = store.get_job("pb_failed")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error_code"] == "PROPOSAL_BUILD_FAILED"
    assert "result_json" not in failed


def test_ddb_stale_failure_compares_the_observed_updated_at() -> None:
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    dynamo = _FakeDynamo()
    store = ProposalJobStore(
        table_name="proposal-jobs",
        dynamodb_client=dynamo,
        clock=clock,
    )
    store.create_job("pb_stale", {"request_id": "stale"})
    assert store.mark_running("pb_stale") is True
    observed_updated_at = store.get_job("pb_stale")["updated_at"]  # type: ignore[index]
    clock.value += timedelta(seconds=181)

    assert store.mark_failed(
        "pb_stale",
        "MCP_RESTARTED",
        expected_statuses=("running",),
        expected_updated_at=observed_updated_at,
    )

    stale_call = dynamo.update_calls[-1]
    assert stale_call["ConditionExpression"] == (
        "(#status = :expected_status_0) AND #updated_at = :expected_updated_at"
    )
    assert stale_call["ExpressionAttributeValues"][":expected_updated_at"] == {
        "S": observed_updated_at
    }


def test_status_stale_cas_loses_to_concurrent_heartbeat() -> None:
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    dynamo = _FakeDynamo()
    store = ProposalJobStore(
        table_name="proposal-jobs",
        dynamodb_client=dynamo,
        clock=clock,
    )
    job_id = "pb_heartbeat_race"
    store.create_job(job_id, {"request_id": "race"})
    assert store.mark_running(job_id) is True
    clock.value += timedelta(seconds=181)

    def heartbeat_wins(fake: _FakeDynamo, _call: dict[str, Any]) -> None:
        fake.items[job_id]["updated_at"] = {"S": "2026-08-04T01:03:01Z"}

    dynamo.before_update = heartbeat_wins
    status = ProposalBuilderStatusSkill(
        store=store,
        stale_after_seconds=180,
        clock=clock,
    ).run(ProposalBuilderStatusInput(job_id=job_id), _ctx())

    assert status.status == "running"
    assert store.get_job(job_id)["updated_at"] == "2026-08-04T01:03:01Z"  # type: ignore[index]


def test_status_terminalizes_non_string_ddb_timestamp() -> None:
    clock = _MutableClock(datetime(2026, 8, 4, 1, 0, tzinfo=UTC))
    dynamo = _FakeDynamo()
    store = ProposalJobStore(
        table_name="proposal-jobs",
        dynamodb_client=dynamo,
        clock=clock,
    )
    job_id = "pb_invalid_timestamp_type"
    store.create_job(job_id, {"request_id": "corrupt-ddb-row"})
    assert store.mark_running(job_id) is True
    dynamo.items[job_id]["updated_at"] = {"N": "0"}

    status = ProposalBuilderStatusSkill(store=store, clock=clock).run(
        ProposalBuilderStatusInput(job_id=job_id),
        _ctx(),
    )

    assert status.status == "failed"
    assert status.error_code == "JOB_STATE_INVALID"
    persisted = store.get_job(job_id)
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["error_code"] == "JOB_STATE_INVALID"
    assert dynamo.update_calls[-1]["ConditionExpression"] == (
        "(#status = :expected_status_0) AND attribute_exists(#updated_at) "
        "AND NOT attribute_type(#updated_at, :updated_at_string_type)"
    )


def test_configured_ddb_failure_never_falls_back_to_memory() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(
        table_name="proposal-jobs",
        dynamodb_client=_BrokenDynamo(),
        memory=memory,
    )

    with pytest.raises(RuntimeError, match="ddb unavailable"):
        store.create_job("pb_ddb_down", {"request_id": "outage"})
    assert memory == {}
