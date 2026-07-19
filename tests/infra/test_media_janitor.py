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
    def __init__(
        self,
        *,
        fail_delete: bool = False,
        keys: list[str] | None = None,
    ) -> None:
        self.fail_delete = fail_delete
        self.keys = (
            keys
            if keys is not None
            else ["media-jobs/mj_0123456789abcdef01234567/attempts/1/output/media"]
        )
        self.lists: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.metadata_overrides: dict[str, dict[str, str]] = {}
        self.tag_overrides: dict[str, dict[str, str]] = {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.lists.append(kwargs)
        return {
            "Contents": [{"Key": key} for key in self.keys],
            "IsTruncated": False,
        }

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.deletes.append(kwargs)
        return {"Errors": [{"Code": "InternalError"}]} if self.fail_delete else {}

    @staticmethod
    def _attempt_parts(key: str) -> tuple[str, str, str, bool]:
        parts = key.split("/")
        return parts[1], parts[3], parts[4], parts[5] == "_FINALIZED.json"

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        job_id, version, attempt_id, finalized = self._attempt_parts(key)
        return {
            "Metadata": self.metadata_overrides.get(
                key,
                {
                    "job-id": job_id,
                    "attempt-id": attempt_id,
                    "lease-version": version,
                    "finalized": str(finalized).lower(),
                },
            )
        }

    def get_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        _job_id, _version, attempt_id, finalized = self._attempt_parts(key)
        tags = self.tag_overrides.get(
            key,
            {
                "teamagent-attempt-id": attempt_id,
                "teamagent-finalized": str(finalized).lower(),
            },
        )
        return {"TagSet": [{"Key": name, "Value": value} for name, value in tags.items()]}


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

    assert result == {
        "cleaned_jobs": 1,
        "deleted_objects": 1,
        "reclaimed_attempt_objects": 0,
    }
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

    assert result == {
        "cleaned_jobs": 0,
        "deleted_objects": 0,
        "reclaimed_attempt_objects": 0,
    }
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

    assert result == {
        "cleaned_jobs": 0,
        "deleted_objects": 0,
        "reclaimed_attempt_objects": 0,
    }
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


def test_expired_lease_reclaims_only_unfinalized_attempt_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "11111111-1111-4111-8111-111111111111"
    orphan = "22222222-2222-4222-8222-222222222222"
    item = _item(
        status="running",
        cleanup_at=2_000,
        hard_cleanup_at=4_000,
        consumer_guard_until=2_000,
    )
    item["lease_expires_at"] = {"N": "900"}
    item["finalized_attempt_id"] = {"S": current}
    prefix = "media-jobs/mj_0123456789abcdef01234567/attempts/3"
    s3 = _S3(
        keys=[
            f"{prefix}/{current}/_FINALIZED.json",
            f"{prefix}/{current}/output/media",
            f"{prefix}/{orphan}/output/media",
        ]
    )
    ddb = _Dynamo([item])
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    result = module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert result == {
        "cleaned_jobs": 0,
        "deleted_objects": 0,
        "reclaimed_attempt_objects": 1,
    }
    assert len(s3.deletes) == 1
    assert s3.deletes[0]["Delete"]["Objects"] == [{"Key": f"{prefix}/{orphan}/output/media"}]
    assert "MEDIA_JOB_STALE_TERMINALIZED" in ddb.claims[0]["ExpressionAttributeValues"][
        ":detail"
    ]["S"]
    assert ddb.claims[1]["UpdateExpression"].startswith("SET orphan_cleanup_owner")
    assert ddb.claims[2]["UpdateExpression"].startswith("REMOVE orphan_cleanup_owner")


def test_stale_queued_job_is_durably_terminalized_without_waiting_for_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item(
        status="queued",
        cleanup_at=2_000,
        hard_cleanup_at=4_000,
        consumer_guard_until=2_000,
    )
    ddb = _Dynamo([item])
    s3 = _S3(keys=[])
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    result = module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert result == {
        "cleaned_jobs": 0,
        "deleted_objects": 0,
        "reclaimed_attempt_objects": 0,
    }
    terminal = ddb.claims[0]
    assert terminal["ConditionExpression"] == (
        "#version = :version AND #status = :status AND deadline = :deadline"
    )
    assert terminal["ExpressionAttributeValues"][":status"] == {"S": "queued"}
    assert "MEDIA_JOB_STALE_TERMINALIZED" in terminal["ExpressionAttributeValues"][":detail"]["S"]


@pytest.mark.parametrize("mutation", ["metadata", "tag"])
def test_orphan_sweep_refuses_subject_mismatch_without_deleting(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    orphan = "22222222-2222-4222-8222-222222222222"
    item = _item(
        status="running",
        cleanup_at=2_000,
        hard_cleanup_at=4_000,
        consumer_guard_until=2_000,
    )
    item["lease_expires_at"] = {"N": "900"}
    key = f"media-jobs/mj_0123456789abcdef01234567/attempts/3/{orphan}/output/media"
    s3 = _S3(keys=[key])
    if mutation == "metadata":
        s3.metadata_overrides[key] = {
            "job-id": "mj_0123456789abcdef01234567",
            "attempt-id": "33333333-3333-4333-8333-333333333333",
            "lease-version": "3",
            "finalized": "false",
        }
    else:
        s3.tag_overrides[key] = {
            "teamagent-attempt-id": orphan,
            "teamagent-finalized": "true",
        }
    ddb = _Dynamo([item])
    module = _load_handler(monkeypatch, ddb=ddb, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_000)

    with pytest.raises(RuntimeError, match="exact attempt metadata and tags"):
        module.handler({}, types.SimpleNamespace(aws_request_id="janitor-1"))

    assert s3.deletes == []
