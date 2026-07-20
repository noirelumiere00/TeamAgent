from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import sys
import types
import zlib
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
_ATTEMPT_ID = "01234567-89ab-4cde-8fab-0123456789ab"
_CAPABILITY_SECRET = "a" * 64
_CAPABILITY_SHA256 = hashlib.sha256(_CAPABILITY_SECRET.encode()).hexdigest()
_CONTROL_SHA256 = "d" * 64


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
        condition = kwargs.get("ConditionExpression", "")
        if "#status = :queued" in condition and "#version = :previous" in condition and (
            self.reject_claim
        ):
            raise _ConditionalFailureError
        if "capability_sha256 = :capability" in condition and self.reject_stopped:
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


class _S3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.control_version = "control-version-1"

    def add_ref(self, ref: dict[str, Any]) -> None:
        self.objects[(ref["key"], ref["version_id"])] = {
            "VersionId": ref["version_id"],
            "ServerSideEncryption": "AES256",
            "ContentLength": ref["size"],
            "ContentType": ref["content_type"],
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(ref["sha256"])).decode(),
            "Metadata": {"sha256": ref["sha256"]},
            "Body": io.BytesIO(b"x" * ref["size"]),
        }

    def add_output(
        self,
        *,
        key: str,
        version_id: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        digest = hashlib.sha256(body).hexdigest()
        self.objects[(key, version_id)] = {
            "VersionId": version_id,
            "ServerSideEncryption": "AES256",
            "ContentLength": len(body),
            "ContentType": content_type,
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode(),
            "Metadata": metadata,
            "Body": io.BytesIO(body),
        }
        return {
            "bucket": "teamagent-media",
            "key": key,
            "version_id": version_id,
            "sha256": digest,
            "size": len(body),
            "content_type": content_type,
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head_object", kwargs))
        return dict(self.objects[(kwargs["Key"], kwargs["VersionId"])])

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        body = bytes(kwargs["Body"])
        digest = hashlib.sha256(body).hexdigest()
        self.objects[(kwargs["Key"], self.control_version)] = {
            "VersionId": self.control_version,
            "ServerSideEncryption": "AES256",
            "ContentLength": len(body),
            "ContentType": kwargs["ContentType"],
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode(),
            "Metadata": kwargs["Metadata"],
            "Body": io.BytesIO(body),
        }
        return {"VersionId": self.control_version}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs))
        return {}

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.calls.append(("generate_presigned_url", {"operation": operation, **kwargs}))
        return f"https://signed.example/{operation}"

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("generate_presigned_post", kwargs))
        return {
            "url": "https://teamagent-media.s3.ap-northeast-1.amazonaws.com/",
            "fields": {
                "key": kwargs["Key"],
                **kwargs["Fields"],
                "policy": "bounded-policy",
                "x-amz-signature": "signature",
            },
        }

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_object_versions", kwargs))
        key = kwargs["Prefix"]
        versions = [
            {"Key": stored_key, "VersionId": version_id}
            for stored_key, version_id in self.objects
            if stored_key == key
        ]
        return {"Versions": versions, "DeleteMarkers": [], "IsTruncated": False}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        value = dict(self.objects[(kwargs["Key"], kwargs["VersionId"])])
        raw = value["Body"].getvalue()
        value["Body"] = io.BytesIO(raw)
        return value


def _load_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ddb: _Dynamo,
    ecs: _Ecs,
    s3: _S3 | None = None,
) -> Any:
    s3 = s3 or _S3()
    fake_boto3 = types.ModuleType("boto3")
    clients = {"ecs": ecs, "dynamodb": ddb, "s3": s3}
    fake_boto3.client = lambda name, **_kwargs: clients[name]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    name = f"_teamagent_media_dispatch_{id(ddb)}_{id(ecs)}"
    spec = importlib.util.spec_from_file_location(name, _HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_queued(ddb: _Dynamo, body: str, s3: _S3 | None = None) -> None:
    spec = json.loads(body)
    ddb.item = {
        "job_id": {"S": spec["job_id"]},
        "idempotency_key": {"S": spec["idempotency_key"]},
        "payload_sha256": {"S": spec["payload_sha256"]},
        "request_json": {"S": body},
        "status": {"S": "queued"},
        "version": {"N": "0"},
        **(
            {"audit_principal_hash": {"S": spec["audit_principal_hash"]}}
            if spec["audit_principal_hash"] is not None
            else {}
        ),
    }
    if s3 is not None:
        operation = spec["operation"]
        for name in ("source", "html", "template", "composer_json"):
            if isinstance(operation.get(name), dict):
                s3.add_ref(operation[name])
        for evidence in operation.get("evidence", []):
            s3.add_ref(evidence["source"])


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
    exit_code: int = 137,
) -> dict[str, Any]:
    request = json.loads(body)
    control_key = f"{request['output_prefix']}control/{_ATTEMPT_ID}.env"
    tags = [
        {"key": "teamagent-job-id", "value": request["job_id"]},
        {
            "key": "teamagent-payload-sha256",
            "value": request["payload_sha256"],
        },
        {"key": "teamagent-attempt-id", "value": _ATTEMPT_ID},
        {"key": "teamagent-attempt-version", "value": "1"},
        {"key": "teamagent-capability-sha256", "value": _CAPABILITY_SHA256},
        {"key": "teamagent-control-sha256", "value": _CONTROL_SHA256},
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
        {
            "name": "MEDIA_JOB_DEADLINE_EPOCH_S",
            "value": str(request["deadline_epoch_s"]),
        },
        {"name": "MEDIA_ATTEMPT_ID", "value": _ATTEMPT_ID},
        {"name": "MEDIA_ATTEMPT_VERSION", "value": "1"},
        {"name": "MEDIA_CAPABILITY_SHA256", "value": _CAPABILITY_SHA256},
        {"name": "MEDIA_CONTROL_SHA256", "value": _CONTROL_SHA256},
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
                "exitCode": exit_code,
                "reason": "OutOfMemoryError",
            }
        ],
        "overrides": {
            "containerOverrides": [
                {
                    "name": "media-worker",
                    "environment": environment,
                    "environmentFiles": [
                        {
                            "type": "s3",
                            "value": f"arn:aws:s3:::teamagent-media/{control_key}",
                        }
                    ],
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


def _seed_running(ddb: _Dynamo, body: str, *, task_arn: str = "") -> None:
    _seed_queued(ddb, body)
    spec = json.loads(body)
    ddb.item.update(
        {
            "status": {"S": "running"},
            "version": {"N": "1"},
            "attempt_id": {"S": _ATTEMPT_ID},
            "capability_sha256": {"S": _CAPABILITY_SHA256},
            "control_key": {
                "S": f"{spec['output_prefix']}control/{_ATTEMPT_ID}.env",
            },
            "control_version_id": {"S": "control-version-1"},
            "control_sha256": {"S": _CONTROL_SHA256},
            "completion_key": {
                "S": (
                    f"{spec['output_prefix']}attempts/1/{_ATTEMPT_ID}/"
                    "_COMPLETION.json"
                )
            },
            "dispatch_client_token": {"S": "e" * 64},
            **(
                {"dispatched_task_arn": {"S": task_arn}}
                if task_arn
                else {}
            ),
        }
    )


def _add_done_completion(s3: _S3, body: str) -> tuple[str, str]:
    spec = json.loads(body)
    metadata = {
        "job-id": spec["job_id"],
        "attempt-id": _ATTEMPT_ID,
        "attempt-version": "1",
        "capability-sha256": _CAPABILITY_SHA256,
    }
    artifact_key = (
        f"{spec['output_prefix']}attempts/1/{_ATTEMPT_ID}/output/media"
    )
    ref = s3.add_output(
        key=artifact_key,
        version_id="artifact-version-1",
        body=b"bounded-video",
        content_type="video/mp4",
        metadata=metadata,
    )
    completion = {
        "schema_version": "1",
        "job_id": spec["job_id"],
        "payload_sha256": spec["payload_sha256"],
        "attempt_id": _ATTEMPT_ID,
        "attempt_version": 1,
        "capability_secret": _CAPABILITY_SECRET,
        "result": {
            "schema_version": "1",
            "job_id": spec["job_id"],
            "status": "done",
            "artifacts": [{"name": "media", "object": ref}],
            "metadata": {"source": "roleless-tool"},
            "error_code": None,
        },
    }
    completion_body = json.dumps(
        completion,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    completion_key = (
        f"{spec['output_prefix']}attempts/1/{_ATTEMPT_ID}/_COMPLETION.json"
    )
    s3.add_output(
        key=completion_key,
        version_id="completion-version-1",
        body=completion_body,
        content_type="application/json",
        metadata=metadata,
    )
    return artifact_key, completion_key


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
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    body = _body()
    _seed_queued(ddb, body, s3)

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
    assert call["platformVersion"] == "1.4.0"
    assert call["enableExecuteCommand"] is False
    assert len(call["clientToken"]) == 64
    assert call["clientToken"] != json.loads(body)["idempotency_key"]
    assert call["startedBy"] == json.loads(body)["job_id"]
    tags = {item["key"]: item["value"] for item in call["tags"]}
    assert tags["teamagent-job-id"] == json.loads(body)["job_id"]
    assert tags["teamagent-payload-sha256"] == json.loads(body)["payload_sha256"]
    assert module._ATTEMPT_ID.fullmatch(tags["teamagent-attempt-id"])
    assert tags["teamagent-attempt-version"] == "1"
    assert module._SHA256.fullmatch(tags["teamagent-capability-sha256"])
    override = call["overrides"]["containerOverrides"]
    assert override[0]["name"] == "media-worker"
    environment = {item["name"]: item["value"] for item in override[0]["environment"]}
    assert environment["MEDIA_JOB_ID"] == json.loads(body)["job_id"]
    assert environment["MEDIA_JOB_PAYLOAD_SHA256"] == json.loads(body)["payload_sha256"]
    assert environment["MEDIA_ATTEMPT_ID"] == tags["teamagent-attempt-id"]
    assert environment["MEDIA_CAPABILITY_SHA256"] == tags["teamagent-capability-sha256"]
    assert set(environment).isdisjoint(
        {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        }
    )
    assert override[0]["environmentFiles"] == [
        {
            "type": "s3",
            "value": (
                "arn:aws:s3:::teamagent-media/"
                f"media-jobs/{json.loads(body)['job_id']}/control/"
                f"{tags['teamagent-attempt-id']}.env"
            ),
        }
    ]
    assert len(module._canonical(call["overrides"]).decode()) <= 8192
    assert any(
        value["UpdateExpression"].startswith("SET dispatched_task_arn") for value in ddb.calls
    )
    control_put = next(kwargs for name, kwargs in s3.calls if name == "put_object")
    assert control_put["IfNoneMatch"] == "*"
    assert control_put["ServerSideEncryption"] == "AES256"
    packed = bytes(control_put["Body"]).split(b"=", 1)[1].strip()
    control_json = zlib.decompress(base64.b64decode(packed))
    control = json.loads(control_json)
    assert environment["MEDIA_CONTROL_SHA256"] == hashlib.sha256(control_json).hexdigest()
    assert control["capability_secret"] not in module._canonical(call["overrides"]).decode()
    assert control["outputs"][0]["post"]["fields"]["x-amz-checksum-algorithm"] == "SHA256"
    post_call = next(kwargs for name, kwargs in s3.calls if name == "generate_presigned_post")
    assert ["starts-with", "$x-amz-checksum-sha256", ""] in post_call["Conditions"]


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
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    body = _large_body()
    _seed_queued(ddb, body, s3)
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
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    audit_hash = "a" * 64
    body = _body(audit_principal_hash=audit_hash)
    _seed_queued(ddb, body, s3)

    module.handler(
        {
            "Records": [
                    {
                        "messageId": "message-audit",
                        "body": body,
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
    attempt = {
        "attempt_id": _ATTEMPT_ID,
        "attempt_version": 1,
        "capability_sha256": _CAPABILITY_SHA256,
        "control_sha256": _CONTROL_SHA256,
        "control_key": f"{spec['output_prefix']}control/{_ATTEMPT_ID}.env",
    }

    with pytest.raises(ValueError, match="8192-character"):
        module._task_overrides(
            "x" * 8192,
            spec,
            attempt,
            bucket="teamagent-media",
        )


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
    body = _body()
    _seed_queued(ddb, body)
    ddb.item["status"] = {"S": "done"}
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
    assert ddb.calls == []


def test_deadline_exhaustion_after_dispatch_claim_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    body = _body()
    _seed_queued(ddb, body, s3)
    readings = [1_001, 1_001, 1_001, 1_001, 1_290]

    def advancing_clock() -> int:
        return readings.pop(0) if readings else 1_290

    monkeypatch.setattr(module.time, "time", advancing_clock)

    with pytest.raises(TimeoutError, match="before task launch"):
        module.handler(
            {"Records": [{"messageId": "deadline", "body": body}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert ecs.calls == []
    assert any(
        call.get("ExpressionAttributeValues", {}).get(":failed") == {"S": "failed"}
        for call in ddb.calls
    )
    assert any("#status = :failed" in call["UpdateExpression"] for call in ddb.calls)


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
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    body = _body()
    _seed_queued(ddb, body, s3)

    with pytest.raises(RuntimeError, match="exactly one task"):
        module.handler(
            {"Records": [{"messageId": "message-1", "body": body}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert any(
        value.get("ExpressionAttributeValues", {}).get(":status") == {"S": "failed"}
        for value in ddb.calls
    )


def test_ecs_stopped_reconciler_terminalizes_owned_queued_or_running_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=_Ecs(), s3=s3)
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
    task_arn = (
        "arn:aws:ecs:ap-northeast-1:718959508629:"
        "task/teamagent-dev-tiktok/0123456789abcdef"
    )
    _seed_running(ddb, request.to_json_bytes().decode(), task_arn=task_arn)

    event = _stopped_event(
        request.to_json_bytes().decode(),
        audit_principal_hash=audit_hash,
    )
    # EventBridge's documented task-state event does not promise this optional
    # RunTask field; DDB + explicit identity hashes must remain sufficient.
    del event["detail"]["overrides"]["containerOverrides"][0]["environmentFiles"]
    result = module.handler(event, types.SimpleNamespace())

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


def test_ecs_stopped_finalizer_commits_only_exact_s3_checked_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    ddb = _Dynamo()
    s3 = _S3()
    task_arn = (
        "arn:aws:ecs:ap-northeast-1:718959508629:"
        "task/teamagent-dev-tiktok/0123456789abcdef"
    )
    _seed_running(ddb, body, task_arn=task_arn)
    artifact_key, completion_key = _add_done_completion(s3, body)
    module = _load_handler(monkeypatch, ddb=ddb, ecs=_Ecs(), s3=s3)
    _configure_stopped(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_100)

    result = module.handler(
        _stopped_event(body, exit_code=0),
        types.SimpleNamespace(),
    )

    assert result == {
        "reconciled": True,
        "job_id": json.loads(body)["job_id"],
        "status": "done",
    }
    update = ddb.calls[0]
    detail = json.loads(update["ExpressionAttributeValues"][":detail"]["S"])
    assert detail["status"] == "done"
    assert update["ExpressionAttributeValues"][":manifest"]["S"] == (
        module._artifact_manifest_sha256(detail["artifacts"])
    )
    list_keys = [
        kwargs["Prefix"]
        for name, kwargs in s3.calls
        if name == "list_object_versions"
    ]
    assert completion_key in list_keys
    assert artifact_key in list_keys
    head = next(
        kwargs
        for name, kwargs in s3.calls
        if name == "head_object" and kwargs["Key"] == artifact_key
    )
    assert head["VersionId"] == "artifact-version-1"
    assert head["ChecksumMode"] == "ENABLED"


def test_ecs_stopped_finalizer_fails_closed_on_artifact_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    ddb = _Dynamo()
    s3 = _S3()
    task_arn = (
        "arn:aws:ecs:ap-northeast-1:718959508629:"
        "task/teamagent-dev-tiktok/0123456789abcdef"
    )
    _seed_running(ddb, body, task_arn=task_arn)
    artifact_key, _completion_key = _add_done_completion(s3, body)
    s3.objects[(artifact_key, "artifact-version-1")]["ChecksumSHA256"] = (
        base64.b64encode(b"\x00" * 32).decode()
    )
    module = _load_handler(monkeypatch, ddb=ddb, ecs=_Ecs(), s3=s3)
    _configure_stopped(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_100)

    result = module.handler(
        _stopped_event(body, exit_code=0),
        types.SimpleNamespace(),
    )

    assert result["status"] == "failed"
    detail = json.loads(ddb.calls[0]["ExpressionAttributeValues"][":detail"]["S"])
    assert detail["error_code"] == "MEDIA_COMPLETION_INVALID"
    assert ":manifest" not in ddb.calls[0]["ExpressionAttributeValues"]


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
    ddb.item = {"status": {"S": "done"}}
    module = _load_handler(monkeypatch, ddb=ddb, ecs=_Ecs())
    _configure_stopped(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_100)

    result = module.handler(_stopped_event(body), types.SimpleNamespace())

    assert result == {
        "reconciled": False,
        "job_id": request["job_id"],
        "status": "done",
    }
