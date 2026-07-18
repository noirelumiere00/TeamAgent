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

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        expression = kwargs["UpdateExpression"]
        if expression.startswith("SET dispatch_owner") and self.reject_claim:
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
    fake_boto3.client = lambda name: ecs if name == "ecs" else ddb  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    name = f"_teamagent_media_dispatch_{id(ddb)}_{id(ecs)}"
    spec = importlib.util.spec_from_file_location(name, _HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body(now: int = 1_000) -> str:
    request = make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        output_bucket="teamagent-media",
        request_fingerprint="dispatch-test",
        now_epoch_s=now,
        timeout_s=300,
    )
    return request.to_json_bytes().decode()


def _staged_ref(name: str) -> S3ObjectRef:
    return S3ObjectRef(
        bucket="teamagent-media",
        key=f"media-jobs/mj_0123456789abcdef01234567/input/{name}",
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
        "MEDIA_ARTIFACT_TTL_SECONDS": "3600",
        "SUBNETS": "subnet-a,subnet-b",
        "SG_ID": "sg-media",
        "CONTAINER": "media-worker",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_dispatcher_passes_canonical_generic_envelope_unchanged(
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

    assert result == {"started": ["arn:aws:ecs:region:account:task/media/1"]}
    assert len(ecs.calls) == 1
    call = ecs.calls[0]
    assert call["count"] == 1
    assert call["taskDefinition"] == "taskdef"
    assert call["clientToken"] == json.loads(body)["idempotency_key"]
    override = call["overrides"]["containerOverrides"]
    assert override == [
        {
            "name": "media-worker",
            "environment": [{"name": "MEDIA_JOB_JSON", "value": body}],
        }
    ]
    parsed = json.loads(override[0]["environment"][0]["value"])
    assert parsed["operation"]["kind"] == "acquire"
    assert "keywords" not in parsed
    assert any(
        value["UpdateExpression"].startswith("SET dispatched_task_arn") for value in ddb.calls
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
    ddb.item = {"status": {"S": "done"}}
    ecs = _Ecs()
    module = _load_handler(monkeypatch, ddb=ddb, ecs=ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 1_001)

    result = module.handler(
        {"Records": [{"messageId": "duplicate", "body": _body()}]},
        types.SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {"started": []}
    assert ecs.calls == []
    assert len(ddb.calls) == 1


def test_legacy_top_level_payload_is_rejected_and_queued_row_is_failed(
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
    assert len(ddb.calls) == 1
    assert ":failed" in ddb.calls[0]["ExpressionAttributeValues"]


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
