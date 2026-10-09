"""DynamoDB-backed state for in-process proposal-builder jobs.

When ``PROPOSAL_JOBS_TABLE`` is unset, all store instances in this process share
one locked in-memory mapping.  A configured DynamoDB backend never silently
falls back to memory: losing the durable state boundary must fail loudly.

重複受付の錠（dedup lock）も同じ table の別キー（``dedup_`` で始まる job_id）に置く。
錠の取得・奪取・返却はすべて条件付き PutItem 1 回（既存の IAM: GetItem/PutItem/UpdateItem
の範囲・新しい table や GSI は要らない）。同時に 2 本来ても DynamoDB が同じキーへの
条件付き書込を直列に評価するので、勝つのは 1 本だけになる。
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
# 錠は job 行と別の mapping に置く（memory 版で「台帳の行数＝ジョブ数」を保つため）。
_MEMORY_DEDUP_LOCKS: dict[str, dict[str, Any]] = {}
DEDUP_LOCK_PREFIX = "dedup_"


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
        dedup_memory: dict[str, dict[str, Any]] | None = None,
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
        # 専用 memory を渡されたら錠も専用にする（別の台帳のジョブを指す錠を共有しない）。
        if dedup_memory is not None:
            self._dedup_memory = dedup_memory
        else:
            self._dedup_memory = _MEMORY_DEDUP_LOCKS if memory is None else {}

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

    # ------------------------------------------------------------------
    # dedup lock（同じ依頼の二重受付をはじく錠）
    # ------------------------------------------------------------------

    def get_dedup_lock(self, lock_id: str) -> dict[str, Any] | None:
        """錠を強い整合性で読む。無ければ None。返り値は target_job_id / claimed_at。"""

        _require_dedup_lock_id(lock_id)
        if not self.uses_dynamodb:
            with self._memory_lock:
                cached = self._dedup_memory.get(lock_id)
                return copy.deepcopy(cached) if cached is not None else None

        response = self._client().get_item(
            TableName=self._table_name,
            Key={"job_id": {"S": lock_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not isinstance(item, dict) or not item:
            return None

        def string_value(name: str) -> str:
            value = item.get(name)
            raw = value.get("S") if isinstance(value, dict) else None
            return raw if isinstance(raw, str) else ""

        return {
            "lock_id": lock_id,
            "target_job_id": string_value("target_job_id"),
            "claimed_at": string_value("claimed_at"),
        }

    def put_dedup_lock(
        self,
        lock_id: str,
        target_job_id: str,
        *,
        expected_target: str | None,
    ) -> bool:
        """錠を条件付きで書く（取得・奪取・返却の共通口）。

        ``expected_target=None`` は「錠がまだ無いときだけ」、文字列なら「錠がその job を
        指しているときだけ」書く（楽観ロック）。条件に負けたら False（例外にしない）。
        ``target_job_id=""`` は返却（その錠を次の依頼が奪ってよい印）。
        """

        _require_dedup_lock_id(lock_id)
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now = now.astimezone(UTC)
        now_text = _isoformat(now)
        expires_at = int(now.timestamp()) + _JOB_TTL_SECONDS
        if not self.uses_dynamodb:
            with self._memory_lock:
                current = self._dedup_memory.get(lock_id)
                if expected_target is None:
                    if current is not None:
                        return False
                elif current is None or current.get("target_job_id") != expected_target:
                    return False
                self._dedup_memory[lock_id] = {
                    "lock_id": lock_id,
                    "target_job_id": target_job_id,
                    "claimed_at": now_text,
                }
                return True

        arguments: dict[str, Any] = {
            "TableName": self._table_name,
            "Item": {
                "job_id": {"S": lock_id},
                "kind": {"S": "dedup_lock"},
                "target_job_id": {"S": target_job_id},
                "claimed_at": {"S": now_text},
                "updated_at": {"S": now_text},
                "expires_at": {"N": str(expires_at)},
            },
        }
        if expected_target is None:
            arguments["ConditionExpression"] = "attribute_not_exists(job_id)"
        else:
            arguments["ConditionExpression"] = "#target_job_id = :expected_target"
            arguments["ExpressionAttributeNames"] = {"#target_job_id": "target_job_id"}
            arguments["ExpressionAttributeValues"] = {":expected_target": {"S": expected_target}}
        try:
            self._client().put_item(**arguments)
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

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
        error_summary = string_value("error_summary")
        stage = string_value("stage")
        if result_json is not None:
            row["result_json"] = result_json
        if error_code is not None:
            row["error_code"] = error_code
        if error_summary is not None:
            row["error_summary"] = error_summary
        if stage is not None:
            row["stage"] = stage
        for name in ("research_delivery_status", "research_delivery_error"):
            value = string_value(name)
            if value is not None:
                row[name] = value
        return row

    def record_research_delivery(self, job_id: str, status: str, *, error: str = "") -> bool:
        """JSON添付の成否を資料の生成状態とは別に記録する。本文は保存しない。"""
        if status not in {"pending", "delivered", "failed"}:
            raise ValueError("invalid research delivery status")
        if not self.uses_dynamodb:
            with self._memory_lock:
                row = self._memory.get(job_id)
                if row is None:
                    return False
                row["research_delivery_status"] = status
                row["research_delivery_error"] = error
                return True
        try:
            self._client().update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                UpdateExpression="SET #delivery = :delivery, #error = :error",
                ConditionExpression="attribute_exists(job_id)",
                ExpressionAttributeNames={
                    "#delivery": "research_delivery_status",
                    "#error": "research_delivery_error",
                },
                ExpressionAttributeValues={
                    ":delivery": {"S": status},
                    ":error": {"S": error},
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

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

    def mark_stage(self, job_id: str, stage: str) -> bool:
        """Persist a running proposal's phase and refresh its heartbeat."""
        if stage not in {"researching", "building"}:
            raise ValueError("invalid proposal job stage")
        now_text = _isoformat(self._clock())
        if not self.uses_dynamodb:
            with self._memory_lock:
                row = self._memory.get(job_id)
                if row is None or row.get("status") != "running":
                    return False
                row["stage"] = stage
                row["updated_at"] = now_text
                return True
        try:
            self._client().update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                UpdateExpression="SET #stage = :stage, #updated_at = :updated_at",
                ConditionExpression="#status = :running",
                ExpressionAttributeNames={
                    "#stage": "stage",
                    "#status": "status",
                    "#updated_at": "updated_at",
                },
                ExpressionAttributeValues={
                    ":stage": {"S": stage},
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

    def mark_delivered(self, job_id: str) -> bool:
        """保存済み結果の配信記録だけを更新する（生成や terminal 状態は変更しない）。"""
        row = self.get_job(job_id)
        if row is None or row.get("status") != "done":
            return False
        raw = row.get("result_json")
        result = json.loads(raw) if isinstance(raw, str) else None
        if not isinstance(result, dict) or "slack_delivered" not in result:
            return False
        result["slack_delivered"] = True
        summary = json.loads(row.get("request_summary") or "{}")
        research_auto = isinstance(summary, dict) and summary.get("research_auto") is True
        target = "dm" if research_auto and result.get("delivery_target") == "dm" else "thread"
        result["delivery_target"] = target
        suffix = " DMへ添付しました。" if target == "dm" else " この会話へ添付しました。"
        result["message"] = str(result.get("message") or "") + suffix
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > _MAX_RESULT_BYTES:
            return False
        return self._transition(
            job_id,
            expected_statuses=("done",),
            next_status="done",
            result_json=serialized,
        )

    def record_primary_delivery_failure(self, job_id: str, *, uncertain: bool = False) -> bool:
        """生成済み資料の配信失敗を、JSONの添付状態とは別に保存する。"""
        row = self.get_job(job_id)
        if row is None or row.get("status") != "done":
            return False
        raw = row.get("result_json")
        result = json.loads(raw) if isinstance(raw, str) else None
        if not isinstance(result, dict) or "slack_delivered" not in result:
            return False
        # 通知の部品はお土産資料と共用。お土産の結果は strict に検証されるため、余分なキーを
        # 書くと次の状態照会で RESULT_INVALID になる（10-09 の検証で発覚）。
        # 調査からの自動作成だけに限る。
        summary = json.loads(row.get("request_summary") or "{}")
        if not (isinstance(summary, dict) and summary.get("research_auto") is True):
            return False
        warning = "提案書の添付結果を確認できません" if uncertain else "Slackファイル添付に失敗"
        result["warnings"] = list(dict.fromkeys([*result.get("warnings", []), warning]))
        result["message"] = str(result.get("message") or "") + (
            " 提案書の添付結果を確認できませんでした。DMをご確認ください。"
            if uncertain
            else " 提案書の添付に失敗しました。"
        )
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > _MAX_RESULT_BYTES:
            return False
        return self._transition(
            job_id, expected_statuses=("done",), next_status="done", result_json=serialized
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
        error_summary: str | None = None,
    ) -> bool:
        """failed へ遷移する。error_summary は利用者に見せてよい短い理由（本文断片を含めない）。"""

        return self._transition(
            job_id,
            expected_statuses=expected_statuses,
            expected_updated_at=expected_updated_at,
            expected_updated_at_missing=expected_updated_at_missing,
            expected_updated_at_invalid=expected_updated_at_invalid,
            next_status="failed",
            error_code=error_code,
            error_summary=error_summary,
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
        error_summary: str | None = None,
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
                row.pop("error_summary", None)
                if result_json is not None:
                    row["result_json"] = result_json
                if error_code is not None:
                    row["error_code"] = error_code
                if error_summary:
                    row["error_summary"] = error_summary
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
        if error_summary:
            sets.append("#error_summary = :error_summary")
            values[":error_summary"] = {"S": error_summary}
        else:
            removes.append("#error_summary")

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
                    "#error_summary": "error_summary",
                },
                ExpressionAttributeValues=values,
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise


def _require_dedup_lock_id(lock_id: str) -> None:
    # job 行（pb_/omy_/clp_）を錠として上書きしない。
    if not lock_id.startswith(DEDUP_LOCK_PREFIX):
        raise ValueError("dedup lock id must start with the dedup prefix")


def new_proposal_job_id() -> str:
    return f"pb_{uuid.uuid4().hex}"


__all__ = ["DEDUP_LOCK_PREFIX", "ProposalJobStore", "new_proposal_job_id"]
