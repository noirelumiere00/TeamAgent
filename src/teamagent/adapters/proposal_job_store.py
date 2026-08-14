"""DynamoDB-backed state for in-process proposal-builder jobs.

When ``PROPOSAL_JOBS_TABLE`` is unset, all store instances in this process share
one locked in-memory mapping.  A configured DynamoDB backend never silently
falls back to memory: losing the durable state boundary must fail loudly.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

_JOB_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_RESULT_BYTES = 300 * 1024
_MEMORY_JOBS: dict[str, dict[str, Any]] = {}
_MEMORY_JOBS_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_conditional_failure(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


class ProposalJobStore:
    """Persist minimal proposal job rows with conditional state transitions."""

    def __init__(
        self,
        *,
        table_name: str | None = None,
        dynamodb_client: Any | None = None,
        clock: Callable[[], datetime] = _utc_now,
        memory: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._table_name = (
            os.environ.get("PROPOSAL_JOBS_TABLE", "").strip()
            if table_name is None
            else table_name.strip()
        )
        self._region = os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._dynamodb_client = dynamodb_client
        self._client_lock = threading.Lock()
        self._clock = clock
        self._memory = _MEMORY_JOBS if memory is None else memory
        self._memory_lock = _MEMORY_JOBS_LOCK if memory is None else threading.RLock()

    @property
    def uses_dynamodb(self) -> bool:
        return bool(self._table_name)

    def _client(self) -> Any:
        if self._dynamodb_client is not None:
            return self._dynamodb_client
        with self._client_lock:
            if self._dynamodb_client is None:
                import boto3

                self._dynamodb_client = boto3.session.Session().client(
                    "dynamodb",
                    region_name=self._region,
                )
        return self._dynamodb_client

    def create_job(self, job_id: str, request_summary: dict[str, Any]) -> None:
        """Create one queued row; an ID collision is an error."""

        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now = now.astimezone(UTC)
        now_text = _isoformat(now)
        summary_json = json.dumps(
            request_summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        row: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now_text,
            "updated_at": now_text,
            "request_summary": summary_json,
            "expires_at": int(now.timestamp()) + _JOB_TTL_SECONDS,
        }
        if not self.uses_dynamodb:
            with self._memory_lock:
                if job_id in self._memory:
                    raise ValueError("proposal job ID already exists")
                self._memory[job_id] = copy.deepcopy(row)
            return

        self._client().put_item(
            TableName=self._table_name,
            Item={
                "job_id": {"S": job_id},
                "status": {"S": "queued"},
                "created_at": {"S": now_text},
                "updated_at": {"S": now_text},
                "request_summary": {"S": summary_json},
                "expires_at": {"N": str(row["expires_at"])},
            },
            ConditionExpression="attribute_not_exists(job_id)",
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Read one job row using a strongly consistent DynamoDB read."""

        if not self.uses_dynamodb:
            with self._memory_lock:
                cached = self._memory.get(job_id)
                return copy.deepcopy(cached) if cached is not None else None

        response = self._client().get_item(
            TableName=self._table_name,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not isinstance(item, dict) or not item:
            return None

        def string_value(name: str) -> str | None:
            value = item.get(name)
            if not isinstance(value, dict):
                return None
            raw = value.get("S")
            return raw if isinstance(raw, str) else None

        row: dict[str, Any] = {
            "job_id": string_value("job_id") or job_id,
            "status": string_value("status"),
            "created_at": string_value("created_at") or "",
            "request_summary": string_value("request_summary") or "{}",
        }
        updated_at = string_value("updated_at")
        if updated_at is not None:
            row["updated_at"] = updated_at
        elif "updated_at" in item:
            row["_updated_at_invalid"] = True
        result_json = string_value("result_json")
        error_code = string_value("error_code")
        if result_json is not None:
            row["result_json"] = result_json
        if error_code is not None:
            row["error_code"] = error_code
        return row

    def mark_running(self, job_id: str) -> bool:
        return self._transition(
            job_id,
            expected_statuses=("queued",),
            next_status="running",
        )

    def heartbeat(self, job_id: str) -> bool:
        """Refresh a running row without changing its state."""

        now_text = _isoformat(self._clock())
        if not self.uses_dynamodb:
            with self._memory_lock:
                row = self._memory.get(job_id)
                if row is None or row.get("status") != "running":
                    return False
                row["updated_at"] = now_text
                return True

        try:
            self._client().update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                UpdateExpression="SET #updated_at = :updated_at",
                ConditionExpression="#status = :running",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#updated_at": "updated_at",
                },
                ExpressionAttributeValues={
                    ":running": {"S": "running"},
                    ":updated_at": {"S": now_text},
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def mark_done(self, job_id: str, result_json: str) -> bool:
        if len(result_json.encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("proposal job result exceeds the DynamoDB row boundary")
        return self._transition(
            job_id,
            expected_statuses=("running",),
            next_status="done",
            result_json=result_json,
        )

    def mark_failed(
        self,
        job_id: str,
        error_code: str,
        *,
        expected_statuses: tuple[str, ...] = ("queued", "running"),
        expected_updated_at: str | None = None,
        expected_updated_at_missing: bool = False,
        expected_updated_at_invalid: bool = False,
    ) -> bool:
        return self._transition(
            job_id,
            expected_statuses=expected_statuses,
            expected_updated_at=expected_updated_at,
            expected_updated_at_missing=expected_updated_at_missing,
            expected_updated_at_invalid=expected_updated_at_invalid,
            next_status="failed",
            error_code=error_code,
        )

    def _transition(
        self,
        job_id: str,
        *,
        expected_statuses: tuple[str, ...],
        next_status: str,
        expected_updated_at: str | None = None,
        expected_updated_at_missing: bool = False,
        expected_updated_at_invalid: bool = False,
        result_json: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        timestamp_conditions = sum(
            (
                expected_updated_at is not None,
                expected_updated_at_missing,
                expected_updated_at_invalid,
            )
        )
        if timestamp_conditions > 1:
            raise ValueError("updated_at CAS conditions are mutually exclusive")
        now_text = _isoformat(self._clock())
        if not self.uses_dynamodb:
            with self._memory_lock:
                row = self._memory.get(job_id)
                if row is None or row.get("status") not in expected_statuses:
                    return False
                if expected_updated_at is not None and row.get("updated_at") != expected_updated_at:
                    return False
                if expected_updated_at_missing and "updated_at" in row:
                    return False
                if expected_updated_at_invalid and (
                    "updated_at" not in row or isinstance(row["updated_at"], str)
                ):
                    return False
                row["status"] = next_status
                row["updated_at"] = now_text
                row.pop("result_json", None)
                row.pop("error_code", None)
                if result_json is not None:
                    row["result_json"] = result_json
                if error_code is not None:
                    row["error_code"] = error_code
                return True

        status_conditions: list[str] = []
        values: dict[str, dict[str, str]] = {
            ":next_status": {"S": next_status},
            ":updated_at": {"S": now_text},
        }
        for index, status in enumerate(expected_statuses):
            placeholder = f":expected_status_{index}"
            status_conditions.append(f"#status = {placeholder}")
            values[placeholder] = {"S": status}
        conditions = ["(" + " OR ".join(status_conditions) + ")"]
        if expected_updated_at is not None:
            conditions.append("#updated_at = :expected_updated_at")
            values[":expected_updated_at"] = {"S": expected_updated_at}
        elif expected_updated_at_missing:
            conditions.append("attribute_not_exists(#updated_at)")
        elif expected_updated_at_invalid:
            conditions.extend(
                (
                    "attribute_exists(#updated_at)",
                    "NOT attribute_type(#updated_at, :updated_at_string_type)",
                )
            )
            values[":updated_at_string_type"] = {"S": "S"}

        sets = ["#status = :next_status", "#updated_at = :updated_at"]
        removes: list[str] = []
        if result_json is not None:
            sets.append("#result_json = :result_json")
            values[":result_json"] = {"S": result_json}
        else:
            removes.append("#result_json")
        if error_code is not None:
            sets.append("#error_code = :error_code")
            values[":error_code"] = {"S": error_code}
        else:
            removes.append("#error_code")

        update_expression = "SET " + ", ".join(sets)
        if removes:
            update_expression += " REMOVE " + ", ".join(removes)
        try:
            self._client().update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                UpdateExpression=update_expression,
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#updated_at": "updated_at",
                    "#result_json": "result_json",
                    "#error_code": "error_code",
                },
                ExpressionAttributeValues=values,
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise


def new_proposal_job_id() -> str:
    return f"pb_{uuid.uuid4().hex}"


__all__ = ["ProposalJobStore", "new_proposal_job_id"]
