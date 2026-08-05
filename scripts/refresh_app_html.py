#!/usr/bin/env python3
"""Refresh the immutable connect-web /app artifact and its ECS task contract."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts import build_app_html, export_vault  # noqa: E402

_BUCKET = "teamagent-dev-raw-files"
_KEY = "codebuild/connect-web-app.html"
_EXPECTED_BUCKET_OWNER = "718959508629"
_REGION = "ap-northeast-1"
_CLUSTER = "teamagent-dev"
_SERVICE = "teamagent-dev-connect-web"
_CONTAINER = "connect-web"

_TARGET_ENV_NAMES = (
    "CONNECT_APP_HTML_S3_VERSION_ID",
    "CONNECT_APP_HTML_SHA256",
    "CONNECT_APP_HTML_MANIFEST_SHA256",
    "CONNECT_APP_HTML_BUILD_INPUTS_SHA256",
)
_BAKED_ENV_NAME = "CONNECT_APP_HTML_BAKED_SHA256"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_ID_RE = re.compile(r"[-A-Za-z0-9._~+/=]{1,1024}")

# Fields accepted by ECS RegisterTaskDefinition. Response-only fields are
# deliberately omitted instead of passing a loosely filtered API response.
_REGISTER_TASK_DEFINITION_FIELDS = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "tags",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
    "ephemeralStorage",
    "runtimePlatform",
    "enableFaultInjection",
)


class RefreshError(RuntimeError):
    """A fail-closed refresh error."""


@dataclass(frozen=True)
class Config:
    dry_run: bool = False
    region: str = _REGION
    cluster: str = _CLUSTER
    service: str = _SERVICE
    container: str = _CONTAINER


@dataclass(frozen=True)
class Artifact:
    body: bytes
    sha256: str
    manifest_sha256: str
    build_inputs_sha256: str


@contextmanager
def _argv(argv: list[str]) -> Iterator[None]:
    previous = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = previous


def _run_export(vault: Path) -> int:
    # export_vault.main has no argv parameter, so isolate its argparse input.
    with _argv(["export_vault.py", "--out", str(vault), "--commit"]):
        return export_vault.main()


def _run_build(vault: Path, out: Path) -> int:
    # Do not pass --allow-shrink: build_app_html owns and enforces that gate.
    return build_app_html.main(["--vault", str(vault), "--out", str(out)])


def _call_generator(name: str, function: Any, *args: Path) -> None:
    try:
        result = function(*args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code == 0:
            return
        raise RefreshError(f"{name} exited with status {code}") from exc
    if result != 0:
        raise RefreshError(f"{name} returned status {result}")


def _required_stats_sha256(stats: dict[str, Any], name: str) -> str:
    value = stats.get(name)
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RefreshError(f"generated stats contain an invalid {name}")
    return value


def _generate_artifact(progress: list[str]) -> Artifact:
    with tempfile.TemporaryDirectory(prefix="teamagent-refresh-app-") as directory:
        root = Path(directory)
        vault = root / "vault"
        out = root / "app.html"

        _call_generator("export_vault", _run_export, vault)
        progress.append("vault_exported")
        _call_generator("build_app_html", _run_build, vault, out)
        progress.append("app_html_generated")

        stats_path = Path(str(out) + ".stats.json")
        try:
            body = out.read_bytes()
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RefreshError(f"cannot read generated artifact/stats: {exc}") from exc
        if not body:
            raise RefreshError("generated app.html is empty")
        if not isinstance(stats, dict):
            raise RefreshError("generated stats are not a JSON object")

        artifact = Artifact(
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            manifest_sha256=_required_stats_sha256(stats, "manifest_sha256"),
            build_inputs_sha256=_required_stats_sha256(stats, "build_inputs_sha256"),
        )
        progress.append("artifact_validated")
        return artifact


def _put_or_reuse_artifact(
    s3: Any,
    artifact: Artifact,
    progress: list[str],
) -> tuple[str, bool]:
    expected_metadata = {
        "app-sha256": artifact.sha256,
        "manifest-sha256": artifact.manifest_sha256,
        "build-inputs-sha256": artifact.build_inputs_sha256,
    }
    expected_checksum = base64.b64encode(bytes.fromhex(artifact.sha256)).decode("ascii")
    try:
        current = s3.head_object(
            Bucket=_BUCKET,
            Key=_KEY,
            ExpectedBucketOwner=_EXPECTED_BUCKET_OWNER,
            ChecksumMode="ENABLED",
        )
    except Exception as exc:
        error_response = getattr(exc, "response", {})
        error = error_response.get("Error", {}) if isinstance(error_response, dict) else {}
        if error.get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        current = {}

    metadata = current.get("Metadata", {}) if isinstance(current, dict) else {}
    if (
        metadata == expected_metadata
        and current.get("ContentLength") == len(artifact.body)
        and current.get("ChecksumSHA256") == expected_checksum
    ):
        version_id = current.get("VersionId")
        if not isinstance(version_id, str) or _VERSION_ID_RE.fullmatch(version_id) is None:
            raise RefreshError("matching S3 object has no valid immutable VersionId")
        return version_id, False

    progress.append("s3_put_started")
    response = s3.put_object(
        Bucket=_BUCKET,
        Key=_KEY,
        Body=artifact.body,
        ContentType="text/html; charset=utf-8",
        ExpectedBucketOwner=_EXPECTED_BUCKET_OWNER,
        Metadata=expected_metadata,
        ChecksumSHA256=expected_checksum,
    )
    version_id = response.get("VersionId") if isinstance(response, dict) else None
    if not isinstance(version_id, str) or _VERSION_ID_RE.fullmatch(version_id) is None:
        raise RefreshError("S3 put_object did not return a valid immutable VersionId")
    return version_id, True


def _describe_task_definition(ecs: Any, config: Config) -> dict[str, Any]:
    services = ecs.describe_services(cluster=config.cluster, services=[config.service])
    failures = services.get("failures", [])
    found = services.get("services", [])
    if failures or not isinstance(found, list) or len(found) != 1:
        raise RefreshError("connect-web service could not be resolved uniquely")
    task_definition_arn = found[0].get("taskDefinition")
    if not isinstance(task_definition_arn, str) or not task_definition_arn:
        raise RefreshError("connect-web service has no task definition ARN")

    response = ecs.describe_task_definition(taskDefinition=task_definition_arn, include=["TAGS"])
    task_definition = response.get("taskDefinition")
    if not isinstance(task_definition, dict):
        raise RefreshError("ECS returned no task definition")
    described = copy.deepcopy(task_definition)
    tags = response.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            raise RefreshError("ECS task definition tags are invalid")
        described["tags"] = copy.deepcopy(tags)
    return described


def _registration_request(
    described: dict[str, Any],
    *,
    container_name: str,
    version_id: str,
    artifact: Artifact,
) -> tuple[dict[str, Any], bool]:
    task_definition = copy.deepcopy(described)
    containers = task_definition.get("containerDefinitions")
    if not isinstance(containers, list):
        raise RefreshError("task definition containerDefinitions are invalid")
    if not all(isinstance(item, dict) for item in containers):
        raise RefreshError("task definition container entry is invalid")
    matches = [item for item in containers if item.get("name") == container_name]
    if len(matches) != 1:
        raise RefreshError(f"expected exactly one {container_name!r} container")

    environment = matches[0].get("environment")
    if not isinstance(environment, list):
        raise RefreshError("connect-web environment is invalid")
    replacements = {
        "CONNECT_APP_HTML_S3_VERSION_ID": version_id,
        "CONNECT_APP_HTML_SHA256": artifact.sha256,
        "CONNECT_APP_HTML_MANIFEST_SHA256": artifact.manifest_sha256,
        "CONNECT_APP_HTML_BUILD_INPUTS_SHA256": artifact.build_inputs_sha256,
    }
    counts = {name: 0 for name in (*_TARGET_ENV_NAMES, _BAKED_ENV_NAME)}
    baked_value: str | None = None
    changed = False
    for item in environment:
        if not isinstance(item, dict):
            raise RefreshError("connect-web environment entry is invalid")
        name = item.get("name")
        if name in counts:
            counts[name] += 1
        if name == _BAKED_ENV_NAME:
            baked_value = item.get("value")
        if name in replacements:
            changed = changed or item.get("value") != replacements[name]
            item["value"] = replacements[name]

    invalid_counts = sorted(name for name, count in counts.items() if count != 1)
    if invalid_counts:
        raise RefreshError(f"required environment keys are missing or duplicated: {invalid_counts}")
    if not isinstance(baked_value, str) or _SHA256_RE.fullmatch(baked_value) is None:
        raise RefreshError(f"{_BAKED_ENV_NAME} is invalid")
    if baked_value == artifact.sha256:
        raise RefreshError("live app SHA-256 must remain distinct from baked fallback SHA-256")

    request = {
        name: task_definition[name]
        for name in _REGISTER_TASK_DEFINITION_FIELDS
        if name in task_definition
    }
    if "family" not in request or "containerDefinitions" not in request:
        raise RefreshError("task definition lacks required registration fields")
    return request, changed


def _register_and_deploy(
    ecs: Any,
    config: Config,
    request: dict[str, Any],
    progress: list[str],
) -> str:
    progress.append("task_definition_register_started")
    response = ecs.register_task_definition(**request)
    registered = response.get("taskDefinition") if isinstance(response, dict) else None
    arn = registered.get("taskDefinitionArn") if isinstance(registered, dict) else None
    if not isinstance(arn, str) or not arn:
        raise RefreshError("register_task_definition returned no task definition ARN")
    progress.append("task_definition_registered")
    progress.append("service_update_started")
    ecs.update_service(cluster=config.cluster, service=config.service, taskDefinition=arn)
    progress.append("service_updated")
    progress.append("service_stability_wait_started")
    ecs.get_waiter("services_stable").wait(
        cluster=config.cluster,
        services=[config.service],
    )
    progress.append("service_stable")
    return arn


def run_refresh(
    config: Config,
    *,
    s3_client: Any | None = None,
    ecs_client: Any | None = None,
    progress: list[str] | None = None,
) -> dict[str, Any]:
    completed = progress if progress is not None else []
    artifact = _generate_artifact(completed)
    result: dict[str, Any] = {
        "dry_run": config.dry_run,
        "bucket": _BUCKET,
        "key": _KEY,
        "sha256": artifact.sha256,
        "manifest_sha256": artifact.manifest_sha256,
        "build_inputs_sha256": artifact.build_inputs_sha256,
    }
    if config.dry_run:
        completed.append("dry_run_complete")
        return result

    if s3_client is None or ecs_client is None:
        import boto3

        s3_client = s3_client or boto3.client("s3", region_name=config.region)
        ecs_client = ecs_client or boto3.client("ecs", region_name=config.region)

    version_id, uploaded = _put_or_reuse_artifact(s3_client, artifact, completed)
    completed.append("s3_object_uploaded" if uploaded else "s3_object_reused")
    result["version_id"] = version_id
    result["s3_uploaded"] = uploaded

    described = _describe_task_definition(ecs_client, config)
    completed.append("task_definition_described")
    request, changed = _registration_request(
        described,
        container_name=config.container,
        version_id=version_id,
        artifact=artifact,
    )
    if not changed:
        current_arn = described.get("taskDefinitionArn")
        if not isinstance(current_arn, str) or not current_arn:
            raise RefreshError("unchanged task definition has no ARN")
        completed.append("task_definition_unchanged")
        result["task_definition_arn"] = current_arn
        return result
    arn = _register_and_deploy(ecs_client, config, request, completed)
    result["task_definition_arn"] = arn
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="generate and validate only")
    parser.add_argument("--region", default=_REGION)
    parser.add_argument("--cluster", default=_CLUSTER)
    parser.add_argument("--service", default=_SERVICE)
    parser.add_argument("--container", default=_CONTAINER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = Config(
        dry_run=args.dry_run,
        region=args.region,
        cluster=args.cluster,
        service=args.service,
        container=args.container,
    )
    progress: list[str] = []
    try:
        result = run_refresh(config, progress=progress)
    except Exception as exc:
        completed = ",".join(progress) if progress else "none"
        print(
            f"[ERROR] refresh_app_html failed: {exc}; completed={completed}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
