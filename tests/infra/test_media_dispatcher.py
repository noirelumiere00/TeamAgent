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

from teamagent.media import tool_contracts
from teamagent.media.contracts import (
    AcquireOperation,
    FrameOperation,
    MediaOperation,
    PdfOperation,
    ProposalEvidence,
    ProposalPptxOperation,
    ProxyOperation,
    S3ObjectRef,
    Sales25MediaOperation,
    Sales25PptxOperation,
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


def test_sales25_media_exact_transport_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=Sales25MediaOperation(
            kind="sales25_media",
            post_id="9000000010",
            url="https://www.tiktok.com/@synthetic/video/9000000010",
            media_type="video",
            expected_duration_sec=12.0,
        ),
        output_bucket="teamagent-media",
        request_fingerprint="sales25-transport",
        now_epoch_s=1_000,
        timeout_s=900,
    )
    spec = json.loads(request.to_json_bytes())
    module._validate_operation(spec["operation"])
    attempt = {"attempt_version": 1, "attempt_id": _ATTEMPT_ID}
    slots = module._operation_output_slots(spec, attempt)
    assert [(s["name"], s["key"], s["max_bytes"]) for s in slots] == list(
        tool_contracts._output_slots(request, _ATTEMPT_ID, 1)
    )
    assert [(s["name"], s["max_bytes"]) for s in slots] == [
        ("media-manifest.json", 1024 * 1024),
        ("media-bundle.zip", 128 * 1024 * 1024),
    ]
    assert module._required_output_names(spec) == {"media-manifest.json", "media-bundle.zip"}
    assert module._allowed_content_types(spec, "media-manifest.json") == {"application/json"}
    assert module._allowed_content_types(spec, "media-bundle.zip") == {"application/zip"}
    assert tool_contracts._input_refs(request) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"post_id": "9000000011"},
        {"url": "https://example.invalid/video/9000000010"},
        {"url": "https://www.tiktok.com/@synthetic/video/9000000010?x=1"},
        {"url": "https://www.tiktok.com:443/@synthetic/video/9000000010"},
        {"media_type": "photo"},
        {"media_type": []},
        {"expected_photo_count": 1},
        {"expected_duration_sec": 181.0},
        {"expected_duration_sec": True},
        {"expected_duration_sec": float("nan")},
        {"expected_duration_sec": float("inf")},
        {"max_bytes": 64 * 1024 * 1024 + 1},
        {"expected_sha256": "bad"},
        {"extra": "forbidden"},
    ],
)
def test_sales25_media_core_and_lambda_reject_same_invalid_request(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    operation = Sales25MediaOperation(
        kind="sales25_media",
        post_id="9000000010",
        url="https://www.tiktok.com/@synthetic/video/9000000010",
        media_type="video",
    ).model_dump(mode="json")
    operation.update(change)
    with pytest.raises(ValueError):
        Sales25MediaOperation.model_validate(operation)
    with pytest.raises(ValueError):
        module._validate_operation(operation)


def test_sales25_photo_count_and_strict_missing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    operation = Sales25MediaOperation(
        kind="sales25_media",
        post_id="9000000010",
        url="https://www.tiktok.com/@synthetic/photo/9000000010",
        media_type="photo",
        expected_photo_count=35,
    ).model_dump(mode="json")
    module._validate_operation(operation)
    operation["expected_photo_count"] = 36
    with pytest.raises(ValueError):
        module._validate_operation(operation)
    with pytest.raises(ValueError):
        Sales25MediaOperation.model_validate(operation)
    operation.pop("expected_sha256")
    with pytest.raises(ValueError, match="keys"):
        module._validate_operation(operation)


@pytest.mark.parametrize("tamper", [False, True])
def test_sales25_completion_has_exact_artifacts_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tamper: bool
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=Sales25MediaOperation(
            kind="sales25_media",
            post_id="9000000010",
            url="https://www.tiktok.com/@synthetic/video/9000000010",
            media_type="video",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="sales25-completion",
        now_epoch_s=1_000,
        timeout_s=900,
    )
    spec = json.loads(request.to_json_bytes())
    attempt = {
        "attempt_version": 1,
        "attempt_id": _ATTEMPT_ID,
        "capability_sha256": _CAPABILITY_SHA256,
    }
    s3 = _S3()
    artifacts = [
        {
            "name": slot["name"],
            "object": s3.add_output(
                key=slot["key"],
                version_id=f"version-{index}",
                body=b"synthetic",
                content_type=content_type,
                metadata=module._fixed_object_metadata(spec, attempt),
            ),
        }
        for index, (slot, content_type) in enumerate(
            zip(
                module._operation_output_slots(spec, attempt),
                ["application/json", "application/zip"],
                strict=True,
            )
        )
    ]
    metadata = {"post_id": "9000000010", "media_type": "video", "status": "ready"}
    if tamper:
        metadata["unexpected"] = "forbidden"
    completion = {
        "schema_version": "1",
        "job_id": request.job_id,
        "payload_sha256": request.payload_sha256,
        "attempt_id": _ATTEMPT_ID,
        "attempt_version": 1,
        "capability_secret": _CAPABILITY_SECRET,
        "_completion_version_id": "completion-version",
        "result": {
            "schema_version": "1",
            "job_id": request.job_id,
            "status": "done",
            "artifacts": artifacts,
            "metadata": metadata,
            "error_code": None,
        },
    }
    if tamper:
        with pytest.raises(ValueError, match="metadata differs"):
            module._validate_completion_result(
                completion,
                s3=s3,
                bucket=request.output_bucket,
                spec=spec,
                attempt=attempt,
                exit_code=0,
            )
    else:
        result, manifest = module._validate_completion_result(
            completion,
            s3=s3,
            bucket=request.output_bucket,
            spec=spec,
            attempt=attempt,
            exit_code=0,
        )
        assert result["status"] == "done"
        assert manifest == module._artifact_manifest_sha256(artifacts)


class _ConditionalFailureError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("conditional failure")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _EcsServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Dynamo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.item: dict[str, Any] = {}
        self.reject_claim = False
        self.reject_stopped = False

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        if self.item:
            raise _ConditionalFailureError
        self.item = kwargs["Item"]
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        condition = kwargs.get("ConditionExpression", "")
        if (
            "#status = :queued" in condition
            and "#version = :previous" in condition
            and (self.reject_claim)
        ):
            raise _ConditionalFailureError
        if "capability_sha256 = :capability" in condition and self.reject_stopped:
            raise _ConditionalFailureError
        return {}

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}


class _Ecs:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response or {
            "tasks": [{"taskArn": "arn:aws:ecs:region:account:task/media/1"}],
            "failures": [],
        }
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
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
                "S": (f"{spec['output_prefix']}attempts/1/{_ATTEMPT_ID}/_COMPLETION.json")
            },
            "dispatch_client_token": {"S": "e" * 64},
            **({"dispatched_task_arn": {"S": task_arn}} if task_arn else {}),
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
    artifact_key = f"{spec['output_prefix']}attempts/1/{_ATTEMPT_ID}/output/media"
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
    completion_key = f"{spec['output_prefix']}attempts/1/{_ATTEMPT_ID}/_COMPLETION.json"
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
    assert len(ddb.put_calls) == 1
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
    assert control["capability_secret"] not in json.dumps(ddb.calls)
    assert control["outputs"][0]["post"]["fields"]["x-amz-checksum-algorithm"] == "SHA256"
    post_call = next(kwargs for name, kwargs in s3.calls if name == "generate_presigned_post")
    assert ["starts-with", "$x-amz-checksum-sha256", ""] in post_call["Conditions"]


def test_dispatcher_creates_authoritative_row_before_claiming_first_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    body = _body()

    result = module.handler(
        {"Records": [{"messageId": "first-delivery", "body": body}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result["started"] == ["arn:aws:ecs:region:account:task/media/1"]
    assert len(ddb.put_calls) == 1
    created = ddb.put_calls[0]["Item"]
    spec = json.loads(body)
    assert created["request_json"] == {"S": body}
    assert created["idempotency_key"] == {"S": spec["idempotency_key"]}
    assert created["status"] == {"S": "queued"}
    assert int(created["hard_cleanup_at"]["N"]) > spec["deadline_epoch_s"]
    assert int(created["consumer_guard_until"]["N"]) > spec["deadline_epoch_s"]


def test_dispatcher_retry_uses_original_persisted_timestamp_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AcquireOperation(
        kind="acquire",
        url="https://www.youtube.com/watch?v=BaW_jenozKc",
    )
    original = make_job_request(
        operation=operation,
        output_bucket="teamagent-media",
        request_fingerprint="dispatch-semantic-retry",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    retry = make_job_request(
        operation=operation,
        output_bucket="teamagent-media",
        request_fingerprint="dispatch-semantic-retry",
        now_epoch_s=1_060,
        timeout_s=300,
    )
    assert original.idempotency_key == retry.idempotency_key
    assert original.payload_sha256 != retry.payload_sha256

    ddb = _Dynamo()
    ecs = _Ecs()
    _seed_queued(ddb, original.to_json_bytes().decode())
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_061)

    result = module.handler(
        {
            "Records": [
                {
                    "messageId": "semantic-retry",
                    "body": retry.to_json_bytes().decode(),
                }
            ]
        },
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result["started"] == ["arn:aws:ecs:region:account:task/media/1"]
    tags = {item["key"]: item["value"] for item in ecs.calls[0]["tags"]}
    assert tags["teamagent-payload-sha256"] == original.payload_sha256


def test_dispatcher_presigns_immutable_input_with_checksum_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    ecs = _Ecs()
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    request = make_job_request(
        operation=ProxyOperation(kind="proxy", source=_staged_ref("video.mp4")),
        output_bucket="teamagent-media",
        request_fingerprint="dispatch-checksummed-input",
        job_id="mj_0123456789abcdef01234567",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    body = request.to_json_bytes().decode()
    _seed_queued(ddb, body, s3)

    result = module.handler(
        {"Records": [{"messageId": "message-1", "body": body}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result["batchItemFailures"] == []
    get_call = next(kwargs for name, kwargs in s3.calls if name == "generate_presigned_url")
    assert get_call["operation"] == "get_object"
    assert isinstance(request.operation, ProxyOperation)
    assert get_call["Params"] == {
        "Bucket": "teamagent-media",
        "Key": request.operation.source.key,
        "VersionId": request.operation.source.version_id,
        "ChecksumMode": "ENABLED",
    }


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


def test_running_unconfirmed_retry_reuses_exact_attempt_and_ecs_client_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    ddb = _Dynamo()
    _seed_running(ddb, body)
    ecs = _Ecs()
    s3 = _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)

    result = module.handler(
        {"Records": [{"messageId": "retry", "body": body}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result["started"] == ["arn:aws:ecs:region:account:task/media/1"]
    assert ecs.calls[0]["clientToken"] == "e" * 64
    environment = {
        item["name"]: item["value"]
        for item in ecs.calls[0]["overrides"]["containerOverrides"][0]["environment"]
    }
    assert environment["MEDIA_ATTEMPT_ID"] == _ATTEMPT_ID
    assert environment["MEDIA_ATTEMPT_VERSION"] == "1"
    assert not any(name == "put_object" for name, _kwargs in s3.calls)


def test_running_confirmed_duplicate_never_launches_second_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    ddb = _Dynamo()
    _seed_running(ddb, body, task_arn="arn:aws:ecs:region:account:task/media/1")
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


def test_ambiguous_run_task_error_preserves_attempt_for_same_token_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    s3 = _S3()
    body = _body()
    _seed_queued(ddb, body, s3)
    ecs = _Ecs(error=RuntimeError("read timeout after request write"))
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)

    with pytest.raises(RuntimeError, match="read timeout"):
        module.handler(
            {"Records": [{"messageId": "ambiguous", "body": body}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    assert len(ecs.calls) == 1
    assert not any(
        call.get("ExpressionAttributeValues", {}).get(":status") == {"S": "failed"}
        for call in ddb.calls
    )
    assert not any(name == "delete_object" for name, _kwargs in s3.calls)


def test_definitive_run_task_rejection_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = _Dynamo()
    s3 = _S3()
    body = _body()
    _seed_queued(ddb, body, s3)
    ecs = _Ecs(error=_EcsServiceError("ClientException"))
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)

    with pytest.raises(_EcsServiceError):
        module.handler(
            {"Records": [{"messageId": "rejected", "body": body}]},
            types.SimpleNamespace(aws_request_id="request-1"),
        )

    terminal = [
        call
        for call in ddb.calls
        if call.get("ExpressionAttributeValues", {}).get(":status") == {"S": "failed"}
    ]
    assert len(terminal) == 1


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


def test_dispatcher_rejects_non_exact_task_count_without_falsely_terminalizing(
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

    assert not any(
        value.get("ExpressionAttributeValues", {}).get(":status") == {"S": "failed"}
        for value in ddb.calls
    )
    assert not any(name == "delete_object" for name, _kwargs in s3.calls)


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
    task_arn = "arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev-tiktok/0123456789abcdef"
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
    task_arn = "arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev-tiktok/0123456789abcdef"
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
    list_keys = [kwargs["Prefix"] for name, kwargs in s3.calls if name == "list_object_versions"]
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
    task_arn = "arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev-tiktok/0123456789abcdef"
    _seed_running(ddb, body, task_arn=task_arn)
    artifact_key, _completion_key = _add_done_completion(s3, body)
    s3.objects[(artifact_key, "artifact-version-1")]["ChecksumSHA256"] = base64.b64encode(
        b"\x00" * 32
    ).decode()
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


def test_every_output_slot_has_an_approved_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全 operation の全出力スロットが content-type 契約を持つ（通過不能スロット禁止）。

    実測回帰: tiktok_acquire(full) の `video-<pid>` スロットに分岐が無く、
    worker が 9.5MB の mp4 を S3 へ上げても完了封筒が必ず
    MEDIA_COMPLETION_INVALID になっていた（job tk_d24be671ec1a）。
    """

    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    operations: list[MediaOperation] = [
        AcquireOperation(kind="acquire", url="https://www.youtube.com/watch?v=BaW_jenozKc"),
        TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("coffee",),
            artifact_mode="full",
        ),
        ProxyOperation(kind="proxy", source=_staged_ref("video.mp4")),
        FrameOperation(
            kind="frame",
            source=_staged_ref("video.mp4"),
            timecodes=(0.0, 1.0),
        ),
        ThumbnailOperation(kind="thumbnail", source=_staged_ref("video.mp4")),
        SlidesOperation(kind="slides", html=_staged_ref("slides.html")),
        PdfOperation(kind="pdf", html=_staged_ref("document.html")),
    ]
    attempt = {
        "attempt_version": 1,
        "attempt_id": "00000000-0000-4000-8000-000000000000",
    }
    for operation in operations:
        request = make_job_request(
            operation=operation,
            output_bucket="teamagent-media",
            request_fingerprint=f"content-type-contract:{operation.kind}",
            job_id="mj_0123456789abcdef01234567",
            now_epoch_s=1_000,
            timeout_s=300,
        )
        spec = json.loads(request.to_json_bytes())
        for slot in module._operation_output_slots(spec, attempt):
            allowed = module._allowed_content_types(spec, slot["name"])
            assert allowed, f"{operation.kind}/{slot['name']} has an empty content-type contract"


def test_tiktok_acquire_video_slot_accepts_mp4(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("coffee",),
            artifact_mode="full",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="content-type-video",
        job_id="mj_0123456789abcdef01234567",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    spec = json.loads(request.to_json_bytes())
    assert "video/mp4" in module._allowed_content_types(spec, "video-p01001")
    # 画像/JSON の既存契約は弱体化させない
    assert module._allowed_content_types(spec, "thumb-p01001") == {"image/jpeg"}
    assert module._allowed_content_types(spec, "posts.json") == {"application/json"}


@pytest.mark.parametrize("enabled", [None, False, True])
def test_first_visit_capture_operation_accepts_only_optional_bool_and_grants_exact_slot(
    monkeypatch: pytest.MonkeyPatch, enabled: bool | None
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("synthetic",),
            videos_per_kw=0,
            artifact_mode="metadata_only",
            first_visit_capture=enabled is True,
        ),
        output_bucket="teamagent-media",
        request_fingerprint="first-visit-output-slot",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    spec = json.loads(request.to_json_bytes())
    if enabled is not None:
        spec["operation"]["first_visit_capture"] = enabled
    module._validate_operation(spec["operation"])
    attempt = {"attempt_version": 1, "attempt_id": _ATTEMPT_ID}
    slots = module._operation_output_slots(spec, attempt)
    expected_names = ["posts.json"] + (["first-visit-capture.json"] if enabled else [])
    assert [slot["name"] for slot in slots] == expected_names
    assert [(slot["name"], slot["key"], slot["max_bytes"]) for slot in slots] == list(
        tool_contracts._output_slots(request, _ATTEMPT_ID, 1)
    )
    assert module._required_output_names(spec) == {"posts.json"}
    if enabled:
        assert slots[-1]["key"].endswith("/output/first-visit-capture.json")
        assert slots[-1]["max_bytes"] == 128 * 1024 * 1024
        assert module._allowed_content_types(spec, "first-visit-capture.json") == {
            "application/json"
        }


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", "1", [], {}])
def test_first_visit_capture_operation_rejects_non_boolean_opt_in(
    monkeypatch: pytest.MonkeyPatch, invalid: Any
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("synthetic",),
        videos_per_kw=0,
        artifact_mode="metadata_only",
    ).model_dump(mode="json")
    operation["first_visit_capture"] = invalid
    with pytest.raises(ValueError):
        module._validate_operation(operation)


@pytest.mark.parametrize("with_capture", [False, True])
def test_first_visit_capture_completion_is_optional_and_validated_in_exact_s3_slot(
    monkeypatch: pytest.MonkeyPatch, with_capture: bool
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("synthetic",),
            videos_per_kw=0,
            artifact_mode="metadata_only",
            first_visit_capture=True,
        ),
        output_bucket="teamagent-media",
        request_fingerprint="first-visit-completion",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    spec = json.loads(request.to_json_bytes())
    attempt = {
        "attempt_version": 1,
        "attempt_id": _ATTEMPT_ID,
        "capability_sha256": _CAPABILITY_SHA256,
    }
    s3 = _S3()
    metadata = module._fixed_object_metadata(spec, attempt)
    names = ["posts.json"] + (["first-visit-capture.json"] if with_capture else [])
    keys = ["posts.normalized.json"] + (["first-visit-capture.json"] if with_capture else [])
    artifacts = [
        {
            "name": name,
            "object": s3.add_output(
                key=f"{request.output_prefix}attempts/1/{_ATTEMPT_ID}/output/{key}",
                version_id=f"version-{index}",
                body=b"{}",
                content_type="application/json",
                metadata=metadata,
            ),
        }
        for index, (name, key) in enumerate(zip(names, keys, strict=True))
    ]
    completion = {
        "schema_version": "1",
        "job_id": request.job_id,
        "payload_sha256": request.payload_sha256,
        "attempt_id": _ATTEMPT_ID,
        "attempt_version": 1,
        "capability_secret": _CAPABILITY_SECRET,
        "_completion_version_id": "completion-version",
        "result": {
            "schema_version": "1",
            "job_id": request.job_id,
            "status": "done",
            "artifacts": artifacts,
            "metadata": {},
            "error_code": None,
        },
    }
    result, manifest = module._validate_completion_result(
        completion,
        s3=s3,
        bucket=request.output_bucket,
        spec=spec,
        attempt=attempt,
        exit_code=0,
    )
    assert result["status"] == "done"
    assert manifest == module._artifact_manifest_sha256(artifacts)


def test_first_visit_capture_on_reaches_dispatcher_launch_with_bounded_output_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb, ecs, s3 = _Dynamo(), _Ecs(), _S3()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs, s3=s3)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)
    request = make_job_request(
        operation=TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=("synthetic",),
            videos_per_kw=0,
            artifact_mode="metadata_only",
            first_visit_capture=True,
        ),
        output_bucket="teamagent-media",
        request_fingerprint="first-visit-dispatch",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    body = request.to_json_bytes().decode()
    _seed_queued(ddb, body, s3)
    result = module.handler(
        {"Records": [{"messageId": "synthetic-capture-message", "body": body}]},
        types.SimpleNamespace(aws_request_id="synthetic-dispatch-request"),
    )
    assert result["started"] == ["arn:aws:ecs:region:account:task/media/1"]
    control_put = next(kwargs for name, kwargs in s3.calls if name == "put_object")
    packed = bytes(control_put["Body"]).split(b"=", 1)[1].strip()
    control = json.loads(zlib.decompress(base64.b64decode(packed)))
    assert control["request"]["operation"]["first_visit_capture"] is True
    sidecar = next(
        item for item in control["outputs"] if item["name"] == "first-visit-capture.json"
    )
    assert sidecar["key"].startswith(request.output_prefix)
    assert sidecar["key"].endswith("/output/first-visit-capture.json")
    assert sidecar["max_bytes"] == 128 * 1024 * 1024
    assert control["inputs"] == []


def _sales25_operation() -> TikTokAcquireOperation:
    return TikTokAcquireOperation(
        kind="tiktok_acquire",
        edition="sales25",
        keywords=("synthetic",),
        n_per_kw=100,
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )


def test_sales25_hundred_has_three_required_bounded_json_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    request = make_job_request(
        operation=_sales25_operation(),
        output_bucket="teamagent-media",
        request_fingerprint="sales25-output-slots",
        now_epoch_s=1_000,
        timeout_s=300,
    )
    spec = json.loads(request.to_json_bytes())
    module._validate_operation(spec["operation"])
    attempt = {"attempt_version": 1, "attempt_id": _ATTEMPT_ID}
    slots = module._operation_output_slots(spec, attempt)
    expected = {"posts.json", "sales25-raw.json", "acquisition-status.json"}
    assert {slot["name"] for slot in slots} == expected
    assert module._required_output_names(spec) == expected
    assert [(slot["name"], slot["key"], slot["max_bytes"]) for slot in slots] == list(
        tool_contracts._output_slots(request, _ATTEMPT_ID, 1)
    )
    for name in expected:
        assert module._allowed_content_types(spec, name) == {"application/json"}


@pytest.mark.parametrize(
    "changes",
    [
        {"n_per_kw": 101},
        {"n_per_kw": 0},
        {"n_per_kw": True},
        {"keywords": ["one", "two"]},
        {"videos_per_kw": 1},
        {"videos_per_kw": False},
        {"artifact_mode": "full"},
        {"sort": "recent"},
        {"first_visit_capture": True},
        {"edition": None},
        {"edition": "unknown"},
        {"edition": 1},
        {"sessions": 2},
        {"owner_id": "untrusted"},
    ],
)
def test_sales25_rejects_invalid_envelope_in_core_and_dispatcher(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    payload = {**_sales25_operation().model_dump(mode="json"), **changes}
    with pytest.raises(ValueError):
        module._validate_operation(payload)
    with pytest.raises(ValueError):
        TikTokAcquireOperation.model_validate_json(json.dumps(payload))


def test_sales25_dispatcher_accounts_for_deep_search_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    monkeypatch.setattr(module, "_TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS", 239)
    with pytest.raises(ValueError, match="immutable job deadline"):
        module._validate_operation(_sales25_operation().model_dump(mode="json"))
    monkeypatch.setattr(module, "_TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS", 240)
    module._validate_operation(_sales25_operation().model_dump(mode="json"))


@pytest.mark.parametrize("count", [30, 31])
def test_ordinary_dispatcher_limit_stays_thirty(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    payload = _sales25_operation().model_dump(mode="json")
    del payload["edition"]
    payload["n_per_kw"] = count
    if count == 30:
        module._validate_operation(payload)
    else:
        with pytest.raises(ValueError, match="TikTok n_per_kw"):
            module._validate_operation(payload)


@pytest.mark.parametrize("flag", [None, "", "0", "false"])
def test_capture_off_keeps_identical_ordinary_payload_and_slots(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    from teamagent.skills.first_visit.recording import capture_enabled

    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    baseline = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("synthetic",),
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )
    if flag is None:
        monkeypatch.delenv("FIRST_VISIT_CAPTURE", raising=False)
    else:
        monkeypatch.setenv("FIRST_VISIT_CAPTURE", flag)
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("synthetic",),
        videos_per_kw=0,
        artifact_mode="metadata_only",
        first_visit_capture=capture_enabled(),
    )
    requests = [
        make_job_request(
            operation=value,
            output_bucket="teamagent-media",
            request_fingerprint="off-unchanged",
            now_epoch_s=1_000,
            timeout_s=300,
        )
        for value in (baseline, operation)
    ]
    assert requests[0].to_json_bytes() == requests[1].to_json_bytes()
    spec = json.loads(requests[1].to_json_bytes())
    module._validate_operation(spec["operation"])
    assert "edition" not in spec["operation"]
    assert "first_visit_capture" not in spec["operation"]
    assert [
        slot["name"]
        for slot in module._operation_output_slots(
            spec, {"attempt_version": 1, "attempt_id": _ATTEMPT_ID}
        )
    ] == ["posts.json"]


def test_sales25_pptx_exact_transport_and_owner_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    source = _staged_ref("input.zip").model_copy(update={"content_type": "application/zip"})
    operation = Sales25PptxOperation(
        kind="sales25_pptx", source=source, owner_id="T_TEST\x1fU_SELF"
    )
    request = make_job_request(
        operation=operation,
        output_bucket="teamagent-media",
        request_fingerprint="sales25-pptx",
        job_id="mj_0123456789abcdef01234567",
        now_epoch_s=1_000,
        timeout_s=480,
    )
    spec = json.loads(request.to_json_bytes())
    module._validate_operation(spec["operation"])
    assert module._operation_s3_refs(spec["operation"]) == [source.model_dump(mode="json")]
    assert tool_contracts._input_refs(request) == (source,)
    slots = module._operation_output_slots(spec, {"attempt_version": 1, "attempt_id": _ATTEMPT_ID})
    assert [(s["name"], s["key"], s["max_bytes"]) for s in slots] == list(
        tool_contracts._output_slots(request, _ATTEMPT_ID, 1)
    )
    assert module._required_output_names(spec) == {"deck.pptx", "sales25-qa.json", "sales25-qa.zip"}
    assert module._allowed_content_types(spec, "sales25-qa.zip") == {"application/zip"}
    for owner in ("", " ", "\t\n", "a" * 201):
        invalid = {**spec["operation"], "owner_id": owner}
        with pytest.raises(ValueError):
            Sales25PptxOperation.model_validate(invalid)
        with pytest.raises(ValueError):
            module._validate_operation(invalid)
    for changes in ({"content_type": "text/html"}, {"size": 0}, {"size": 128 * 1024 * 1024 + 1}):
        invalid = {**spec["operation"], "source": {**spec["operation"]["source"], **changes}}
        with pytest.raises(ValueError):
            Sales25PptxOperation.model_validate(invalid)
        with pytest.raises(ValueError):
            module._validate_operation(invalid)


def test_concrete_pptx_exact_transport_and_identity_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.media.contracts import ConcretePptxOperation

    module = _load_handler(monkeypatch, ddb=_Dynamo(), ecs=_Ecs())
    source = _staged_ref("input.zip").model_copy(update={"content_type": "application/zip"})
    operation = ConcretePptxOperation(
        kind="concrete_pptx", source=source, owner_id="T_TEST\x1fU_SELF", request_id="synthetic"
    )
    request = make_job_request(
        operation=operation,
        output_bucket="teamagent-media",
        request_fingerprint="concrete-pptx",
        job_id="mj_0123456789abcdef01234567",
        now_epoch_s=1_000,
        timeout_s=480,
    )
    spec = json.loads(request.to_json_bytes())
    module._validate_operation(spec["operation"])
    assert module._operation_s3_refs(spec["operation"]) == [source.model_dump(mode="json")]
    assert tool_contracts._input_refs(request) == (source,)
    slots = module._operation_output_slots(spec, {"attempt_version": 1, "attempt_id": _ATTEMPT_ID})
    assert [(s["name"], s["key"], s["max_bytes"]) for s in slots] == list(
        tool_contracts._output_slots(request, _ATTEMPT_ID, 1)
    )
    assert module._required_output_names(spec) == {"concrete-result.json"}
    assert module._allowed_content_types(spec, "concrete.pptx") == {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    }
    for owner in ("", " ", "\t\n", "a" * 201):
        invalid = {**spec["operation"], "owner_id": owner}
        with pytest.raises(ValueError):
            ConcretePptxOperation.model_validate(invalid)
        with pytest.raises(ValueError):
            module._validate_operation(invalid)
    for changes in ({"content_type": "text/html"}, {"size": 0}, {"size": 128 * 1024 * 1024 + 1}):
        invalid = {**spec["operation"], "source": {**spec["operation"]["source"], **changes}}
        with pytest.raises(ValueError):
            ConcretePptxOperation.model_validate(invalid)
        with pytest.raises(ValueError):
            module._validate_operation(invalid)

    for request_id in ("", " ", "a" * 201):
        invalid = {**spec["operation"], "request_id": request_id}
        with pytest.raises(ValueError):
            ConcretePptxOperation.model_validate(invalid)
        with pytest.raises(ValueError):
            module._validate_operation(invalid)
