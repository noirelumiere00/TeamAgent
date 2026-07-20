from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from teamagent.media.contracts import (
    AcquireOperation,
    FrameOperation,
    PdfOperation,
    ProposalEvidence,
    ProposalPptxOperation,
    ProxyOperation,
    S3ObjectRef,
    SlidesOperation,
    ThumbnailOperation,
    TikTokAcquireOperation,
    make_job_request,
)

_HANDLER = (
    Path(__file__).parents[2] / "infra" / "terraform" / "lambda" / "tiktok_dispatch" / "handler.py"
)


class _ConditionalFailureError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("conditional failure")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _Dynamo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.item: dict[str, Any] = {}
        self.reject_claim = False
        self.reject_stopped = False

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        expression = kwargs["UpdateExpression"]
        if expression.startswith("SET dispatch_owner") and self.reject_claim:
            raise _ConditionalFailureError
        if "dispatched_task_arn = :task" in kwargs.get("ConditionExpression", "") and (
            self.reject_stopped
        ):
            raise _ConditionalFailureError
        return {}

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}


class _Ecs:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            "tasks": [{"taskArn": "arn:aws:ecs:region:account:task/media/1"}],
            "failures": [],
        }
        self.calls: list[dict[str, Any]] = []

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _load_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ddb: _Dynamo,
    ecs: _Ecs,
) -> Any:
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = (  # type: ignore[attr-defined]
        lambda name, **_kwargs: ecs if name == "ecs" else ddb
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    name = f"_teamagent_media_dispatch_{id(ddb)}_{id(ecs)}"
    spec = importlib.util.spec_from_file_location(name, _HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body(now: int = 1_000, *, audit_principal_hash: str | None = None) -> str:
    request = make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="dispatch-test",
        now_epoch_s=now,
        timeout_s=300,
        audit_principal_hash=audit_principal_hash,
    )
    return request.to_json_bytes().decode()


def _staged_ref(name: str) -> S3ObjectRef:
    return S3ObjectRef(
        bucket="teamagent-media",
        key=f"media-jobs/mj_0123456789abcdef01234567/input/{name}",
        version_id="version-1",
        sha256="0" * 64,
        size=1,
        content_type="application/octet-stream",
    )


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "CLUSTER_ARN": "cluster",
        "TASKDEF_ARN": "taskdef",
        "JOBS_TABLE": "jobs",
        "JOB_BUCKET": "teamagent-media",
        "MEDIA_ARTIFACT_TTL_SECONDS": "2592000",
        "SUBNETS": "subnet-a,subnet-b",
        "SG_ID": "sg-media",
        "CONTAINER": "media-worker",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _stopped_event(
    body: str,
    *,
    audit_principal_hash: str | None = None,
    include_tags: bool = False,
) -> dict[str, Any]:
    request = json.loads(body)
    tags = [
        {"key": "teamagent-job-id", "value": request["job_id"]},
        {
            "key": "teamagent-payload-sha256",
            "value": request["payload_sha256"],
        },
    ]
    if audit_principal_hash is not None:
        tags.append(
            {
                "key": "teamagent-audit-principal-hash",
                "value": audit_principal_hash,
            }
        )
    environment = [
        {"name": "MEDIA_JOB_ID", "value": request["job_id"]},
        {
            "name": "MEDIA_JOB_PAYLOAD_SHA256",
            "value": request["payload_sha256"],
        },
    ]
    if audit_principal_hash is not None:
        environment.append(
            {
                "name": "MEDIA_JOB_AUDIT_PRINCIPAL_HASH",
                "value": audit_principal_hash,
            }
        )
    detail = {
        "clusterArn": "arn:aws:ecs:ap-northeast-1:718959508629:cluster/teamagent-dev-tiktok",
        "taskDefinitionArn": (
            "arn:aws:ecs:ap-northeast-1:718959508629:"
            "task-definition/teamagent-dev-tiktok-acquire:42"
        ),
        "taskArn": (
            "arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev-tiktok/0123456789abcdef"
        ),
        "startedBy": request["job_id"],
        "lastStatus": "STOPPED",
        "stopCode": "EssentialContainerExited",
        "stoppedReason": "Essential container in task exited",
        "containers": [
            {
                "name": "media-worker",
                "exitCode": 137,
                "reason": "OutOfMemoryError",
            }
        ],
        "overrides": {
            "containerOverrides": [
                {
                    "name": "media-worker",
                    "environment": environment,
                }
            ]
        },
    }
    if include_tags:
        detail["tags"] = tags
    return {
        "source": "aws.ecs",
        "detail-type": "ECS Task State Change",
        "detail": detail,
    }


def _configure_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv(
        "CLUSTER_ARN",
        "arn:aws:ecs:ap-northeast-1:718959508629:cluster/teamagent-dev-tiktok",
    )
    monkeypatch.setenv(
        "TASKDEF_ARN",
        ("arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-tiktok-acquire:43"),
    )


def test_dispatcher_passes_only_bounded_persisted_envelope_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    body = _body()

    result = module.handler(
        {"Records": [{"messageId": "message-1", "body": body}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {
        "started": ["arn:aws:ecs:region:account:task/media/1"],
        "batchItemFailures": [],
    }
    assert len(ecs.calls) == 1
    call = ecs.calls[0]
    assert call["count"] == 1
    assert call["taskDefinition"] == "taskdef"
    assert call["clientToken"] == json.loads(body)["idempotency_key"]
    assert call["startedBy"] == json.loads(body)["job_id"]
    assert call["tags"] == [
        {"key": "teamagent-job-id", "value": json.loads(body)["job_id"]},
        {
            "key": "teamagent-payload-sha256",
            "value": json.loads(body)["payload_sha256"],
        },
    ]
    override = call["overrides"]["containerOverrides"]
    assert override == [
        {
            "name": "media-worker",
            "environment": [
                {
                    "name": "MEDIA_JOB_ID",
                    "value": json.loads(body)["job_id"],
                },
                {
                    "name": "MEDIA_JOB_PAYLOAD_SHA256",
                    "value": json.loads(body)["payload_sha256"],
                },
                {
                    "name": "MEDIA_JOB_DEADLINE_EPOCH_S",
                    "value": str(json.loads(body)["deadline_epoch_s"]),
                },
            ],
        }
    ]
    assert len(module._canonical(call["overrides"]).decode()) <= 8192
    assert any(
        value["UpdateExpression"].startswith("SET dispatched_task_arn") for value in ddb.calls
    )


def _large_body() -> str:
    job_id = "mj_0123456789abcdef01234567"

    def large_ref(index: int) -> S3ObjectRef:
        return S3ObjectRef(
            bucket="teamagent-media",
            key=f"media-jobs/{job_id}/input/{index:02d}-{'a' * 900}",
            version_id=f"version-{index}",
            sha256=f"{index:064x}",
            size=1,
            content_type="application/octet-stream",
        )

    request = make_job_request(
        operation=ProposalPptxOperation(
            kind="proposal_pptx",
            template=large_ref(30),
            composer_json=large_ref(31),
            evidence=tuple(
                ProposalEvidence(
                    placeholder_id=index + 1,
                    rank=index + 1,
                    source=large_ref(index),
                )
                for index in range(20)
            ),
        ),
        output_bucket="teamagent-media",
        request_fingerprint="large-dispatch-envelope",
        job_id=job_id,
        now_epoch_s=1_000,
        timeout_s=300,
    )
    return request.to_json_bytes().decode()


def test_large_valid_envelope_still_uses_override_below_ecs_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    body = _large_body()
    assert 8192 < len(body) < 128 * 1024

    result = module.handler(
        {"Records": [{"messageId": "large-envelope", "body": body}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {
        "started": ["arn:aws:ecs:region:account:task/media/1"],
        "batchItemFailures": [],
    }
    overrides = ecs.calls[0]["overrides"]
    assert len(module._canonical(overrides).decode()) <= 8192
    assert "MEDIA_JOB_JSON" not in module._canonical(overrides).decode()


def test_dispatcher_carries_audit_owner_in_task_tag_and_stopped_event_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    audit_hash = "a" * 64

    module.handler(
        {
            "Records": [
                {
                    "messageId": "message-audit",
                    "body": _body(audit_principal_hash=audit_hash),
                }
            ]
        },
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    call = ecs.calls[0]
    assert {
        "key": "teamagent-audit-principal-hash",
        "value": audit_hash,
    } in call["tags"]
    environment = call["overrides"]["containerOverrides"][0]["environment"]
    assert {
        "name": "MEDIA_JOB_AUDIT_PRINCIPAL_HASH",
        "value": audit_hash,
    } in environment


def test_task_override_enforces_ecs_8192_character_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    spec = json.loads(_body())

    with pytest.raises(ValueError, match="8192-character"):
        module._task_overrides("x" * 8192, spec)


def test_operation_schema_error_fails_row_without_starting_or_orphaning_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    malformed = json.loads(_body())
    malformed["operation"].pop("url")
    without_hash = dict(malformed)
    without_hash.pop("payload_sha256")
    malformed["payload_sha256"] = module.hashlib.sha256(module._canonical(without_hash)).hexdigest()
    body = module._canonical(malformed).decode()

    with pytest.raises(ValueError, match="acquire operation keys"):
        module.handler(
            {"Records": [{"messageId": "invalid-operation", "body": body}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert ecs.calls == []
    assert len(ddb.calls) == 1
    assert ddb.calls[0]["ExpressionAttributeValues"][":failed"] == {"S": "failed"}
    assert "request_json = :request_json" in ddb.calls[0]["ConditionExpression"]
    assert "payload_sha256 = :payload" in ddb.calls[0]["ConditionExpression"]
    assert "idempotency_key = :idempotency" in ddb.calls[0]["ConditionExpression"]
    assert ddb.calls[0]["ExpressionAttributeValues"][":request_json"] == {"S": body}


def test_unproven_invalid_envelope_cannot_mutate_same_job_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    malformed = json.loads(_body())
    malformed["operation"].pop("url")
    body = module._canonical(malformed).decode()

    with pytest.raises(ValueError, match="acquire operation keys"):
        module.handler(
            {"Records": [{"messageId": "unproven-invalid", "body": body}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert ddb.calls == []
    assert ecs.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        TikTokAcquireOperation(kind="tiktok_acquire", keywords=("coffee",)),
        ProxyOperation(kind="proxy", source=_staged_ref("video.mp4")),
        FrameOperation(
            kind="frame",
            source=_staged_ref("video.mp4"),
            timecodes=(0.0, 1.0),
        ),
        ThumbnailOperation(kind="thumbnail", source=_staged_ref("video.mp4")),
        SlidesOperation(kind="slides", html=_staged_ref("slides.html")),
        ProposalPptxOperation(
            kind="proposal_pptx",
            template=_staged_ref("template.pptx"),
            composer_json=_staged_ref("composer.json"),
            evidence=(
                ProposalEvidence(
                    placeholder_id=1,
                    rank=1,
                    source=_staged_ref("evidence.jpg"),
                ),
            ),
        ),
        PdfOperation(kind="pdf", html=_staged_ref("document.html")),
    ],
)
def test_dispatcher_schema_accepts_every_worker_operation(
    monkeypatch: pytest.MonkeyPatch,
    operation: Any,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=operation,
        output_bucket="teamagent-media",
        request_fingerprint=f"dispatcher-parity:{operation.kind}",
        job_id="mj_0123456789abcdef01234567",
        now_epoch_s=1_000,
        timeout_s=300,
    )

    validated = module._validate_envelope(
        request.to_json_bytes().decode(),
        expected_bucket="teamagent-media",
        now=1_001,
    )

    assert validated["operation"]["kind"] == operation.kind


def test_dispatcher_rejects_future_created_envelope_before_task_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="dispatcher-future-created",
        now_epoch_s=1_100,
        timeout_s=300,
    )

    with pytest.raises(ValueError, match="timing"):
        module._validate_envelope(
            request.to_json_bytes().decode(),
            expected_bucket="teamagent-media",
            now=1_000,
        )


def test_dispatcher_rejects_staged_object_outside_exact_job_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=ProxyOperation(kind="proxy", source=_staged_ref("video.mp4")),
        output_bucket="teamagent-media",
        request_fingerprint="dispatcher-scope",
        job_id="mj_0123456789abcdef01234567",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    malformed = json.loads(request.to_json_bytes())
    malformed["operation"]["source"]["key"] = (
        "media-jobs/mj_ffffffffffffffffffffffff/input/video.mp4"
    )
    without_hash = dict(malformed)
    without_hash.pop("payload_sha256")
    malformed["payload_sha256"] = module.hashlib.sha256(module._canonical(without_hash)).hexdigest()

    with pytest.raises(ValueError, match="outside dispatcher scope"):
        module._validate_envelope(
            module._canonical(malformed).decode(),
            expected_bucket="teamagent-media",
            now=1_001,
        )


def test_dispatcher_enforces_deployed_artifact_ttl_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="dispatcher-ttl",
        now_epoch_s=1_000,
        timeout_s=300,
        artifact_ttl_s=601,
    )

    with pytest.raises(ValueError, match="timing"):
        module._validate_envelope(
            request.to_json_bytes().decode(),
            expected_bucket="teamagent-media",
            now=1_001,
            max_artifact_ttl_s=600,
        )


def test_terminal_duplicate_does_not_launch_or_mutate_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ddb.reject_claim = True
    body = _body()
    payload = json.loads(body)["payload_sha256"]
    ddb.item = {
        "status": {"S": "done"},
        "payload_sha256": {"S": payload},
    }
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)

    result = module.handler(
        {"Records": [{"messageId": "duplicate", "body": body}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {"started": [], "batchItemFailures": []}
    assert ecs.calls == []
    assert len(ddb.calls) == 1


def test_deadline_exhaustion_after_dispatch_claim_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    readings = [1_001, 1_001, 1_001, 1_290]

    def advancing_clock() -> int:
        return readings.pop(0) if readings else 1_290

    monkeypatch.setattr(module.time, "time", advancing_clock)

    with pytest.raises(TimeoutError, match="before task launch"):
        module.handler(
            {"Records": [{"messageId": "deadline", "body": _body()}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert ecs.calls == []
    assert any(
        call.get("ExpressionAttributeValues", {}).get(":failed") == {"S": "failed"}
        for call in ddb.calls
    )
    assert any(call["UpdateExpression"].startswith("REMOVE dispatch_owner") for call in ddb.calls)


def test_legacy_payload_has_no_trusted_deadline_and_causes_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    legacy = json.dumps(
        {
            "job_id": "mj_0123456789abcdef01234567",
            "keywords": ["coffee"],
        }
    )

    with pytest.raises(ValueError, match="keys"):
        module.handler(
            {"Records": [{"messageId": "legacy", "body": legacy}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert ecs.calls == []
    assert ddb.calls == []


def test_expired_envelope_causes_no_post_deadline_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_300)

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        module.handler(
            {"Records": [{"messageId": "expired", "body": _body()}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert ddb.calls == []
    assert ecs.calls == []


def test_dispatcher_rejects_non_exact_task_count_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs(
        {
            "tasks": [
                {"taskArn": "arn:task/1"},
                {"taskArn": "arn:task/2"},
            ],
            "failures": [],
        }
    )
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)

    with pytest.raises(RuntimeError, match="exactly one task"):
        module.handler(
            {"Records": [{"messageId": "message-1", "body": _body()}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert any(value["UpdateExpression"].startswith("REMOVE dispatch_owner") for value in ddb.calls)


def test_ecs_stopped_reconciler_terminalizes_owned_queued_or_running_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=_Ecs())
    _configure_stopped(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_500)
    audit_hash = "a" * 64
    request = make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="stopped-reconcile",
        now_epoch_s=1_000,
        timeout_s=900,
        audit_principal_hash=audit_hash,
    )

    result = module.handler(
        _stopped_event(
            request.to_json_bytes().decode(),
            audit_principal_hash=audit_hash,
        ),
        types.SimpleNamespace(),
    )

    assert result == {
        "reconciled": True,
        "job_id": request.job_id,
        "status": "failed",
    }
    update = ddb.calls[0]
    assert "dispatched_task_arn = :task" in update["ConditionExpression"]
    assert "audit_principal_hash = :audit" in update["ConditionExpression"]
    assert "cleanup_at = if_not_exists(hard_cleanup_at, :cleanup)" in update["UpdateExpression"]
    assert update["ExpressionAttributeValues"][":cleanup"] == {"N": str(1_500 + 30 * 24 * 60 * 60)}
    detail = json.loads(update["ExpressionAttributeValues"][":detail"]["S"])
    assert detail["error_code"] == "MEDIA_ECS_TASK_STOPPED"
    assert detail["metadata"]["ecs"]["containers"][0]["exit_code"] == 137


def test_ecs_stopped_reconciler_rejects_optional_task_tag_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    event = _stopped_event(body, include_tags=True)
    event["detail"]["tags"][0]["value"] = "mj_ffffffffffffffffffffffff"
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    _configure_stopped(monkeypatch)

    with pytest.raises(ValueError, match="disagrees with task tags"):
        module.handler(event, types.SimpleNamespace())


def test_ecs_stopped_reconciler_never_overwrites_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    request = json.loads(body)
    ddb = _Dynamo()
    ddb.reject_stopped = True
    ddb.item = {
        "status": {"S": "done"},
        "dispatched_task_arn": {
            "S": (
                "arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev-tiktok/0123456789abcdef"
            )
        },
        "payload_sha256": {"S": request["payload_sha256"]},
    }
    module = _load_handler(monkeypatch, ddb=ddb, ecs=_Ecs())
    _configure_stopped(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_100)

    result = module.handler(_stopped_event(body), types.SimpleNamespace())

    assert result == {
        "reconciled": False,
        "job_id": request["job_id"],
        "status": "done",
    }
