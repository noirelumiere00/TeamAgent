from __future__ import annotations

import base64
import copy
import hashlib
from typing import Any

import pytest

from scripts import refresh_app_html as refresh

_OLD_VALUES = {
    "CONNECT_APP_HTML_S3_VERSION_ID": "old-version",
    "CONNECT_APP_HTML_SHA256": "1" * 64,
    "CONNECT_APP_HTML_MANIFEST_SHA256": "2" * 64,
    "CONNECT_APP_HTML_BUILD_INPUTS_SHA256": "3" * 64,
    "CONNECT_APP_HTML_BAKED_SHA256": "4" * 64,
}


def _artifact() -> refresh.Artifact:
    body = b"<!doctype html><title>fresh</title>"
    return refresh.Artifact(
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        manifest_sha256="5" * 64,
        build_inputs_sha256="6" * 64,
    )


def _checksum(artifact: refresh.Artifact) -> str:
    return base64.b64encode(bytes.fromhex(artifact.sha256)).decode("ascii")


def _task_definition() -> dict[str, Any]:
    environment = [
        {"name": "FIRST", "value": "unchanged"},
        *({"name": name, "value": value} for name, value in _OLD_VALUES.items()),
        {"name": "LAST", "value": "also-unchanged"},
    ]
    return {
        "taskDefinitionArn": "arn:old",
        "revision": 9,
        "status": "ACTIVE",
        "family": "teamagent-dev-connect-web",
        "networkMode": "awsvpc",
        "containerDefinitions": [
            {
                "name": "connect-web",
                "image": "example.invalid/image@sha256:deadbeef",
                "environment": environment,
                "secrets": [{"name": "SECRET", "valueFrom": "arn:secret"}],
            }
        ],
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "registeredAt": "yesterday",
        "tags": [{"key": "owner", "value": "teamagent"}],
    }


class FakeS3:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.put_calls.append(kwargs)
        return {"VersionId": "new-version"}


class FakeWaiter:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def wait(self, **kwargs: Any) -> None:
        self.calls.append(("wait", kwargs))


class FakeECS:
    def __init__(self, task_definition: dict[str, Any] | None = None) -> None:
        self.task_definition = task_definition or _task_definition()
        self.register_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_services(self, **kwargs: Any) -> dict[str, Any]:
        return {"services": [{"taskDefinition": "arn:old"}], "failures": []}

    def describe_task_definition(self, **kwargs: Any) -> dict[str, Any]:
        definition = copy.deepcopy(self.task_definition)
        tags = definition.pop("tags", [])
        return {"taskDefinition": definition, "tags": tags}

    def register_task_definition(self, **kwargs: Any) -> dict[str, Any]:
        self.register_calls.append(kwargs)
        return {"taskDefinition": {"taskDefinitionArn": "arn:new"}}

    def update_service(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        return {}

    def get_waiter(self, name: str) -> FakeWaiter:
        assert name == "services_stable"
        return FakeWaiter(self.calls)


def test_generation_failure_touches_neither_s3_nor_task_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = FakeS3()
    ecs = FakeECS()
    monkeypatch.setattr(refresh, "_run_export", lambda vault: 0)
    monkeypatch.setattr(refresh, "_run_build", lambda vault, out: 1)

    with pytest.raises(refresh.RefreshError, match="build_app_html"):
        refresh.run_refresh(refresh.Config(), s3_client=s3, ecs_client=ecs)

    assert s3.put_calls == []
    assert ecs.register_calls == []
    assert ecs.update_calls == []


def test_registration_preserves_baked_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _artifact()
    monkeypatch.setattr(refresh, "_generate_artifact", lambda progress: artifact)
    ecs = FakeECS()

    refresh.run_refresh(refresh.Config(), s3_client=FakeS3(), ecs_client=ecs)

    environment = ecs.register_calls[0]["containerDefinitions"][0]["environment"]
    values = {item["name"]: item["value"] for item in environment}
    assert values["CONNECT_APP_HTML_BAKED_SHA256"] == _OLD_VALUES["CONNECT_APP_HTML_BAKED_SHA256"]
    assert values["CONNECT_APP_HTML_SHA256"] == artifact.sha256


def test_registration_changes_only_four_target_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact()
    original = _task_definition()
    monkeypatch.setattr(refresh, "_generate_artifact", lambda progress: artifact)
    ecs = FakeECS(original)

    refresh.run_refresh(refresh.Config(), s3_client=FakeS3(), ecs_client=ecs)

    before = original["containerDefinitions"][0]["environment"]
    after = ecs.register_calls[0]["containerDefinitions"][0]["environment"]
    assert [item["name"] for item in after] == [item["name"] for item in before]
    expected = copy.deepcopy(before)
    replacements = {
        "CONNECT_APP_HTML_S3_VERSION_ID": "new-version",
        "CONNECT_APP_HTML_SHA256": artifact.sha256,
        "CONNECT_APP_HTML_MANIFEST_SHA256": artifact.manifest_sha256,
        "CONNECT_APP_HTML_BUILD_INPUTS_SHA256": artifact.build_inputs_sha256,
    }
    for item in expected:
        if item["name"] in replacements:
            item["value"] = replacements[item["name"]]
    assert after == expected
    assert (
        ecs.register_calls[0]["containerDefinitions"][0]["secrets"]
        == original["containerDefinitions"][0]["secrets"]
    )


def test_dry_run_does_not_put_register_or_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "_generate_artifact", lambda progress: _artifact())
    s3 = FakeS3()
    ecs = FakeECS()

    result = refresh.run_refresh(
        refresh.Config(dry_run=True),
        s3_client=s3,
        ecs_client=ecs,
    )

    assert result["dry_run"] is True
    assert s3.put_calls == []
    assert ecs.register_calls == []
    assert ecs.update_calls == []


def test_retry_reuses_identical_s3_version(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _artifact()
    monkeypatch.setattr(refresh, "_generate_artifact", lambda progress: artifact)

    class ExistingS3(FakeS3):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "VersionId": "existing-version",
                "ContentLength": len(artifact.body),
                "ChecksumSHA256": _checksum(artifact),
                "Metadata": {
                    "app-sha256": artifact.sha256,
                    "manifest-sha256": artifact.manifest_sha256,
                    "build-inputs-sha256": artifact.build_inputs_sha256,
                },
            }

    s3 = ExistingS3()
    result = refresh.run_refresh(refresh.Config(), s3_client=s3, ecs_client=FakeECS())

    assert result["version_id"] == "existing-version"
    assert result["s3_uploaded"] is False
    assert s3.put_calls == []


def test_fully_applied_retry_does_not_create_another_task_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact()
    monkeypatch.setattr(refresh, "_generate_artifact", lambda progress: artifact)

    class ExistingS3(FakeS3):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "VersionId": "existing-version",
                "ContentLength": len(artifact.body),
                "ChecksumSHA256": _checksum(artifact),
                "Metadata": {
                    "app-sha256": artifact.sha256,
                    "manifest-sha256": artifact.manifest_sha256,
                    "build-inputs-sha256": artifact.build_inputs_sha256,
                },
            }

    task_definition = _task_definition()
    replacements = {
        "CONNECT_APP_HTML_S3_VERSION_ID": "existing-version",
        "CONNECT_APP_HTML_SHA256": artifact.sha256,
        "CONNECT_APP_HTML_MANIFEST_SHA256": artifact.manifest_sha256,
        "CONNECT_APP_HTML_BUILD_INPUTS_SHA256": artifact.build_inputs_sha256,
    }
    for item in task_definition["containerDefinitions"][0]["environment"]:
        if item["name"] in replacements:
            item["value"] = replacements[item["name"]]
    ecs = FakeECS(task_definition)

    refresh.run_refresh(refresh.Config(), s3_client=ExistingS3(), ecs_client=ecs)

    assert ecs.register_calls == []
    assert ecs.update_calls == []
