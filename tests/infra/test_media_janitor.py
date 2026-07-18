from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_HANDLER = (
    Path(__file__).parents[2] / "infra" / "terraform" / "lambda" / "media_janitor" / "handler.py"
)


class _ConditionalFailureError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("conditional failure")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def _item(
    *,
    status: str = "done",
    version: int = 2,
    active_consumers: int = 0,
    cleanup_at: int = 900,
    hard_cleanup_at: int = 4_000,
    consumer_guard_until: int = 900,
) -> dict[str, Any]:
    return {
        "job_id": {"S": "mj_0123456789abcdef01234567"},
        "status": {"S": status},
        "version": {"N": str(version)},
        "active_consumers": {"N": str(active_consumers)},
        "cleanup_at": {"N": str(cleanup_at)},
        "hard_cleanup_at": {"N": str(hard_cleanup_at)},
        "consumer_guard_until": {"N": str(consumer_guard_until)},
        "deadline": {"N": "950"},
        "output_prefix": {"S": "media-jobs/mj_0123456789abcdef01234567/"},
    }


class _Dynamo:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.claims: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.reject_claim = False

    def scan(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Items": self.items}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(kwargs)
        if self.reject_claim:
            raise _ConditionalFailureError
        version = int(kwargs["ExpressionAttributeValues"][":version"]["N"]) + 1
        return {"Attributes": {"version": {"N": str(version)}}}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self.deletes.append(kwargs)
        return {}


class _S3:
    def __init__(self, *, fail_delete: bool = False) -> None:
        self.fail_delete = fail_delete
        self.lists: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.lists.append(kwargs)
        return {
            "Contents": [
                {"Key": ("media-jobs/mj_0123456789abcdef01234567/attempts/1/output/media")}
            ],
            "IsTruncated": False,
        }

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.deletes.append(kwargs)
        return {"Errors": [{"Code": "InternalError"}]} if self.fail_delete else {}


def _load_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ddb: _Dynamo,
    s3: _S3,
) -> Any:
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name: ddb if name == "dynamodb" else s3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    name = f"_teamagent_media_janitor_{id(ddb)}_{id(s3)}"
    spec = importlib.util.spec_from_file_location(name, _HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    monkeypatch.setenv("JOB_BUCKET", "teamagent-media")


def test_janitor_owner_version_fences_atomic_eligibility_and_deletes_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo([_item()])
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    result = module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert result == {"cleaned_jobs": 1, "deleted_objects": 1}
    assert len(ddb.claims) == 1
    condition = ddb.claims[0]["ConditionExpression"]
    assert "#version = :version" in condition
    assert "active_consumers = :zero" in condition
    assert "consumer_guard_until <= :now" in condition
    assert "hard_cleanup_at <= :now" in condition
    assert s3.lists[0]["Prefix"] == "media-jobs/mj_0123456789abcdef01234567/"
    assert len(ddb.deletes) == 1
    assert ddb.deletes[0]["ExpressionAttributeValues"][":version"] == {"N": "3"}


def test_active_consumer_prevents_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo([_item(active_consumers=1)])
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    result = module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert result == {"cleaned_jobs": 0, "deleted_objects": 0}
    assert ddb.claims == []
    assert s3.lists == []


def test_race_after_scan_fails_claim_without_deleting_shared_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo([_item()])
    ddb.reject_claim = True
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    result = module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert result == {"cleaned_jobs": 0, "deleted_objects": 0}
    assert s3.lists == []
    assert ddb.deletes == []


def test_s3_cleanup_error_fails_invocation_and_preserves_row_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo([_item()])
    s3 = _S3(fail_delete=True)
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    with pytest.raises(RuntimeError, match="delete errors"):
        module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert len(s3.deletes) == 1
    assert ddb.deletes == []
